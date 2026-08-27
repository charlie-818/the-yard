#!/usr/bin/env python3
"""CC Dispatch — phone control for a fleet of live Claude Code panes.

Reads and drives EXISTING iTerm2 sessions via the iTerm2 Python API. Nothing is
restarted. The one exception to "no session is created" is /api/spawn, which opens
a new Claude pane in a throwaway scratch dir on explicit request from the UI.

Safety model
------------
* Writes only ever happen in response to an authenticated request from the UI.
* SEND_ALLOW gates every write: a pane must be running Claude to receive keys,
  so scratch shells and unrelated panes are unreachable by construction.
* Killing this process leaves the fleet exactly as it was.

Run:  .venv/bin/python server.py
"""
import asyncio, json, os, re, secrets, sys, time, glob, pathlib
from aiohttp import web, WSMsgType
import iterm2
import auth

HERE = pathlib.Path(__file__).parent
PORT = int(os.environ.get("DISPATCH_PORT", 8788))
# Loopback by default, on purpose. Reachability is Tailscale's job: `tailscale
# serve` terminates TLS and proxies to this port, so there is no listener on any
# network interface for a stranger to find. Binding anywhere else is opt-in and
# shouted about at startup.
BIND = os.environ.get("DISPATCH_BIND", "127.0.0.1")
FLEET_DIR = os.environ.get("CC_FLEET_DIR", "/tmp/cc-status")
POLL = 0.45                      # seconds between screen samples
STALE = 90                       # fleet json older than this is dropped

# A pane may receive keystrokes if its foreground job is Claude itself, OR if
# statusline.sh has recently written a fleet file for it. The second clause
# matters: while Claude runs a Bash tool the foreground job is that child
# process (bash/git/npm...), and gating on jobName alone would refuse Esc and
# Ctrl-C at precisely the moment you need them.
SEND_ALLOW = {"claude", "node", "caffeinate"}

# Panes confirmed to be Claude at any point. Identity does not expire: a pane
# sitting at a permission prompt stops re-rendering its statusline, so its
# fleet file ages out of the STALE window — and that is precisely when you need
# to answer it. Gating on freshness locked out exactly the wrong case.
KNOWN_CLAUDE = set()

# Markers unique to Claude's TUI chrome, used as a last-resort identity check
# when jobName is a child process and no fleet file is present.
CLAUDE_MARKERS = ("shift+tab to cycle", "for shortcuts", "bypass permissions on",
                  "auto mode on", "plan mode on", "accept edits on",
                  "manual mode on", "esc to interrupt")


def looks_like_claude(text):
    low = (text or "").lower()
    return any(m in low for m in CLAUDE_MARKERS)


def is_claude_pane(uuid, job, text=None):
    u = (uuid or "").upper()
    if job in SEND_ALLOW or u in KNOWN_CLAUDE:
        return True
    if u in read_fleet_files() or looks_like_claude(text):
        KNOWN_CLAUDE.add(u)
        return True
    return False

TOKEN_FILE = HERE / ".token"
if TOKEN_FILE.exists():
    TOKEN = TOKEN_FILE.read_text().strip()
else:
    TOKEN = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(TOKEN)
    TOKEN_FILE.chmod(0o600)

# ── key map ────────────────────────────────────────────────────────────────
# Every value is written in ONE send_text call so terminals parse it as a
# single key, never as loose bytes. See probe_keys.py for the validation.
KEYS = {
    "esc":   "\x1b",
    "^C":    "\x03",
    "^D":    "\x04",
    "s-tab": "\x1b[Z",
    "tab":   "\t",
    "up":    "\x1b[A",
    "down":  "\x1b[B",
    "left":  "\x1b[D",
    "right": "\x1b[C",
    "enter": "\r",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    "6": "6", "7": "7", "8": "8", "9": "9",
}

CONN = None          # iterm2.Connection
APP = None           # iterm2.App

# ── Grid layout ─────────────────────────────────────────────────────────────
# Spawns become splits in the fleet's own tab, arranged into a grid. Growth is
# ROW-MAJOR so the dividers stay aligned: fill the top row across up to MAX_COLS
# full-height columns, THEN drop a second row into each column, and so on. Row-
# major is the only single-split growth path that keeps a clean NxM grid — a
# column-first order leaves later columns split inside one pane's region and the
# dividers no longer line up.
GRID_MAX_COLS = 3    # top row grows to this many columns before rows start filling


# ── iTerm helpers ──────────────────────────────────────────────────────────
async def all_sessions():
    """Every live pane, keyed by uppercase session UUID."""
    await APP.async_refresh()
    out = {}
    for w in APP.terminal_windows:
        for t in w.tabs:
            for s in t.sessions:
                out[s.session_id.upper()] = s
    return out


def _column_sessions(node):
    """Leaf sessions under a column node, top-to-bottom."""
    if isinstance(node, iterm2.Session):
        return [node]
    out = []
    for c in node.children:
        out.extend(_column_sessions(c))
    return out


def grid_columns(tab):
    """The tab's panes grouped into visual columns, left-to-right.

    Returns a list of columns; each column is a list of Sessions top-to-bottom.
    A vertical splitter at the root means its children ARE the columns; a
    horizontal root (or a bare session) is a single column.
    """
    root = tab.root
    if isinstance(root, iterm2.Session):
        return [[root]]
    if root.vertical:                      # dividers vertical -> children side by side
        return [_column_sessions(child) for child in root.children]
    return [_column_sessions(root)]        # dividers horizontal -> one stacked column


def pick_grid_split(tab):
    """Where the next pane should go to grow the grid row-major.

    Returns (session_to_split, vertical) — vertical=True makes a new column to
    the right, vertical=False drops a new row below the chosen pane.
    """
    cols = grid_columns(tab)
    building_top_row = all(len(c) == 1 for c in cols)
    if building_top_row and len(cols) < GRID_MAX_COLS:
        # New full-height column on the right (every column is one full-height pane).
        return cols[-1][0], True
    # Fill a row: the leftmost column with the fewest rows, split its bottom pane.
    target = min(range(len(cols)), key=lambda i: (len(cols[i]), i))
    return cols[target][-1], False


def fleet_tab(app):
    """The tab holding the most known-Claude panes, across ALL windows.

    Scanning every window matters: a spawn triggered from the phone runs while
    iTerm is unfocused, so `current_terminal_window` is None and the fleet may
    not live in `terminal_windows[0]` — splitting there would open the pane in
    the wrong window. Falls back to the current/first window's current tab.
    """
    best, best_n = None, 0
    for w in app.terminal_windows:
        for t in w.tabs:
            n = sum(1 for s in t.sessions if s.session_id.upper() in KNOWN_CLAUDE)
            if n > best_n:
                best, best_n = t, n
    if best is not None:
        return best
    win = app.current_terminal_window or (
        app.terminal_windows[0] if app.terminal_windows else None)
    return win.current_tab if win else None


def trust_dir(path):
    """Pre-mark a directory trusted in ~/.claude.json so the first-run trust
    dialog never appears for it.

    Claude reads `projects[<abspath>].hasTrustDialogAccepted` at startup. We only
    ADD our fresh scratch path's entry — never touch other projects — so a
    concurrent Claude rewriting the file can at worst drop this one new key (the
    dialog reappears once), never corrupt anything else.
    """
    import json, os
    p = os.path.expanduser("~/.claude.json")
    try:
        with open(p) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    key = os.path.abspath(path)
    projects = cfg.setdefault("projects", {})
    entry = projects.setdefault(key, {})
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("hasCompletedProjectOnboarding", True)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, p)                 # atomic swap; no half-written config
    except Exception as e:
        print(f"  [trust] could not pre-trust {key}: {type(e).__name__}: {e}",
              flush=True)


async def pane_history(session, max_lines=600):
    """Scrollback above the visible screen, so the phone can read the whole chat.

    Absolute line numbers run from `overflow` (oldest line iTerm still holds)
    upward; anything below that has already been discarded.
    """
    try:
        info = await session.async_get_line_info()
        start = info.overflow
        end = info.overflow + info.scrollback_buffer_height
        if end <= start:
            return []
        first = max(start, end - max_lines)
        lines = await session.async_get_contents(first, end - first)
        return [l.string.replace("\x00", " ").rstrip() for l in lines]
    except Exception as e:
        print(f"  [history] failed: {type(e).__name__}: {e}", flush=True)
        return []


async def pane_cols(session):
    try:
        return int(session.grid_size.width)
    except Exception:
        return 80


async def pane_text(session):
    c = await session.async_get_screen_contents()
    # iTerm returns NUL for every unwritten cell, not space. NULs are stripped
    # by innerHTML, which collapses the layout — translate them back to spaces.
    return "\n".join(c.line(i).string.replace("\x00", " ").rstrip()
                     for i in range(c.number_of_lines)).rstrip()


OPTION_RE = re.compile(r"^\s*[❯>]?\s*(\d)\.\s+(\S.*?)\s*$")


def detect_prompt(text):
    """Find a numbered choice Claude is waiting on (permission, plan approval).

    Returns {"question": str, "options": [{"key","label","selected"}]} or None.
    Only the LAST run of consecutive numbered lines counts — earlier ones are
    scrollback from prompts already answered.
    """
    lines = text.splitlines()
    runs, cur = [], []
    label_col = 0                      # column where the current run's labels start

    def opt(i, l, m):
        return [i, m.group(1), m.group(2), "❯" in l]

    def close():
        if cur:
            runs.append(list(cur))
            cur.clear()

    for i, l in enumerate(lines):
        m = OPTION_RE.match(l)
        indent = len(l) - len(l.lstrip())
        if m:
            if cur and int(m.group(1)) != len(cur) + 1:
                close()                # a number out of sequence starts a new run
            if not cur:
                label_col = l.index(m.group(2))
            cur.append(opt(i, l, m))
            continue
        if not cur:
            continue
        s = l.strip()
        # A narrow pane (a split can be 16 columns wide) hard-wraps every option
        # onto continuation lines indented to the label column. Those belong to
        # the option above, not to the end of the run — without this, no prompt
        # on a narrow pane is ever detected.
        if s and indent >= label_col and not set(s) <= set("─━│ ⎿"):
            cur[-1][2] = (cur[-1][2] + " " + s)[:200]
            continue
        if not s:
            continue                   # blank gutter between options is fine
        close()
    close()
    runs = [r for r in runs if len(r) >= 2]
    if not runs:
        return None
    run = runs[-1]

    # The question sits above the first option, but the terminal may have
    # hard-wrapped it ("Would you like to / proceed?"), so walk upward and
    # rejoin the run of non-blank lines rather than taking only the last one.
    parts = []
    for j in range(run[0][0] - 1, max(-1, run[0][0] - 9), -1):
        cand = lines[j].strip()
        if not cand:
            if parts:                  # blank above the text block ends it
                break
            continue                   # blank between question and options
        if OPTION_RE.match(lines[j]) or set(cand) <= set("─━│ ⎿"):
            break
        parts.append(cand)
    q = " ".join(reversed(parts))
    return {
        "question": q[:160],
        "options": [{"key": k, "label": lbl[:70], "selected": sel}
                    for _, k, lbl, sel in run][:9],
    }


_EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# Harness-injected user lines that aren't something a human typed — excluded from
# the prompt count, mirroring cc-dashboard's _NOISE filter.
_PROMPT_NOISE = ("Caveat:", "<command-name>", "<command-message>", "<local-command",
                 "[Request interrupted", "system-reminder", "<user-prompt-submit")
_OPS = {}   # transcript path -> {"off","files","seen","prompts","fn","pn","action"}


def _short_action(tool, inp):
    """A compact arg for the fleet bubble from a tool_use input dict: the command
    for Bash, the basename for file tools, the query/target otherwise."""
    if tool == "Bash":
        return (inp.get("command") or "").strip().splitlines()[0][:40] if inp.get("command") else ""
    for k in ("file_path", "notebook_path", "path"):
        if inp.get(k):
            return os.path.basename(str(inp[k]).rstrip("/"))[:28]
    for k in ("pattern", "url", "query", "description", "prompt", "subagent_type"):
        if inp.get(k):
            return str(inp[k]).strip().splitlines()[0][:32] if str(inp[k]).strip() else ""
    return ""


def session_ops(path):
    """(files_edited, prompts) for a session, read straight from its transcript.

    Incremental like cc-dashboard's ops scan: each transcript's byte offset is
    remembered and only newly appended bytes are parsed, so the fleet loop never
    re-reads a multi-MB file. First sight is bounded to the last ~4MB so a huge
    backlog can't stall a frame (older ops may be missed — a glance, not an audit).
    A prompt is a non-meta user message carrying real text (tool-result user lines
    and harness noise don't count), deduped by message uuid."""
    st = _OPS.get(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return ((st or {}).get("fn", 0), (st or {}).get("pn", 0))
    if st is None or size < st["off"]:            # new, or shrank (compaction/clear)
        st = {"off": max(0, size - 4_000_000), "files": set(),
              "seen": set(), "prompts": 0, "fn": 0, "pn": 0, "action": None}
    if size > st["off"]:
        try:
            with open(path, "rb") as fh:
                fh.seek(st["off"]); data = fh.read()
        except OSError:
            data = b""
        cut = data.rfind(b"\n") + 1               # only whole lines
        st["off"] += cut
        for line in data[:cut].decode("utf-8", "ignore").splitlines():
            if '"tool_use"' not in line and '"type":"user"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            # prompts: a real user turn (text, not meta, not a tool result)
            if o.get("type") == "user" and not o.get("isMeta"):
                ct = (o.get("message") or {}).get("content")
                txt = (ct if isinstance(ct, str) else
                       " ".join(b.get("text", "") for b in ct
                                if isinstance(b, dict) and b.get("type") == "text")
                       if isinstance(ct, list) else "")
                if txt.strip() and not any(s in txt for s in _PROMPT_NOISE):
                    uid = o.get("uuid")
                    if uid is None or uid not in st["seen"]:
                        if uid is not None:
                            st["seen"].add(uid)
                        st["prompts"] += 1
            m = o.get("message")
            if not isinstance(m, dict):
                continue
            for b in (m.get("content") or []):
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name = b.get("name")
                inp = b.get("input") or {}
                if name in _EDIT_TOOLS:
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if fp:
                        st["files"].add(fp)
                # Newest tool call = what the pane is doing right now. Sourced
                # from the transcript (structured, reliable) rather than scraped
                # off the scrolling screen, which loses the ⏺ line under output.
                if name:
                    st["action"] = {"tool": name,
                                    "arg": _short_action(name, inp)}
    st["fn"], st["pn"] = len(st["files"]), st["prompts"]
    _OPS[path] = st
    return st["fn"], st["pn"]


def read_fleet_files():
    """Status dumps written by ~/.claude/statusline.sh + cc-active.sh.

    We only read these; cc-dashboard.py owns them and prunes its own staleness.
    """
    now, out = time.time(), {}
    for p in glob.glob(os.path.join(FLEET_DIR, "*.json")):
        try:
            if now - os.path.getmtime(p) > STALE:
                continue
            d = json.load(open(p))
        except Exception:
            continue
        pane = d.get("iterm_pane") or ""
        uuid = pane.split(":")[-1].upper() if ":" in pane else ""
        if not uuid:
            continue
        key = pathlib.Path(p).stem
        state = "idle"
        state_mt = None                       # mtime of the winning .state file
        sf = os.path.join(FLEET_DIR, f"claude-{key}.state")
        for cand in (sf, os.path.join(FLEET_DIR, f"{key}.state")):
            if os.path.exists(cand):
                try:
                    state = open(cand).read().strip() or "idle"
                    state_mt = os.path.getmtime(cand)
                    break
                except Exception:
                    pass
        cw = d.get("context_window") or {}
        cst = d.get("cost") or {}
        tp = d.get("transcript_path") or ""
        out[uuid] = {
            "sid": key,
            "transcript": tp,
            "state": state,
            # When this pane entered its current state, straight from the .state
            # file the working/idle hooks touch — the SAME clock cc-dashboard and
            # the iTerm tab colour use, so the phone timer matches them and, being
            # on disk, survives a server restart.
            "state_since": state_mt,
            "cwd": (d.get("workspace") or {}).get("current_dir", ""),
            "model": (d.get("model") or {}).get("display_name", ""),
            "cost": round(cst.get("total_cost_usd", 0) or 0, 2),
            "ctx": cw.get("used_percentage"),
            "limits": d.get("rate_limits") or {},
            "effort": (d.get("effort") or {}).get("level"),
            "mtime": os.path.getmtime(p),
            # richer per-agent metrics, ported from cc-dashboard's fleet row
            "tokens": (cw.get("total_input_tokens") or 0)
                    + (cw.get("total_output_tokens") or 0),
            "lines_add": cst.get("total_lines_added") or 0,
            "lines_del": cst.get("total_lines_removed") or 0,
            "dur_ms": cst.get("total_duration_ms") or 0,
            "age": int(now - os.path.getmtime(p)),
        }
        fcount, pcount = session_ops(tp) if tp else (0, 0)
        out[uuid]["files"] = fcount
        out[uuid]["prompts"] = pcount
        out[uuid]["action"] = (_OPS.get(tp) or {}).get("action") if tp else None
    return out


def fleet_limits(files):
    """Account-wide 5h/7d usage. Each pane only refreshes its own copy on an
    API call, so take the reading from the newest rate-limit window."""
    best, best_key = {}, (-1, -1)
    for f in files.values():
        fh = (f.get("limits") or {}).get("five_hour")
        if isinstance(fh, dict):
            k = (fh.get("resets_at", 0), fh.get("used_percentage", -1))
            if k > best_key:
                best_key, best = k, f.get("limits")
    return best


# ── live working directory ─────────────────────────────────────────────────
# The critter name tracks the dir the session is CURRENTLY in, not the launch dir.
# Ported from cc-dashboard's latest_cwd so the phone and the TUI name panes the same.
def tail_lines(path, n, size=524288):
    # last n lines without reading the whole (tens-of-MB) transcript
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); sz = f.tell()
            f.seek(max(0, sz - size))
            data = f.read()
        return data.decode("utf-8", "ignore").splitlines()[-n:]
    except OSError:
        return []

def _is_scratch(p):
    # a temp/scratch launch dir is never the repo the session is really working in
    return bool(p) and ("cc-scratch" in p or "/var/folders/" in p
                        or p.startswith("/tmp") or p.startswith("/private/tmp"))

def _cd_target(cmd):
    # destination of the last absolute `cd <dir>` in a (possibly compound) command
    best = None
    for m in re.finditer(r'(?:^|[;&|]|&&)\s*cd\s+("([^"]+)"|\'([^\']+)\'|([^\s;&|]+))', cmd):
        t = m.group(2) or m.group(3) or m.group(4)
        if t and t.startswith("/"): best = t.rstrip("/")
    return best

_cwd_cache = {}   # transcript path -> ((mtime,size), cwd)
def latest_cwd(path):
    # The dir the session is CURRENTLY working in: normally the transcript's per-entry
    # `cwd` (follows the session across dirs, unlike statusline's launch-pinned
    # workspace.current_dir). A session launched in a scratch dir keeps that scratch cwd
    # even while editing a real repo, so when cwd is scratch we recover the working dir
    # from recent `cd /repo` moves and, failing that, the dir of the files it's touching.
    if not path:
        return None
    try: st = os.stat(path)
    except OSError: return None
    key = (st.st_mtime, st.st_size)
    hit = _cwd_cache.get(path)
    if hit and hit[0] == key: return hit[1]
    base = None; cd_hint = None; file_hint = None
    for line in reversed(tail_lines(path, 80)):
        try: o = json.loads(line)
        except Exception: continue
        if base is None and o.get("cwd"): base = o["cwd"]
        if base and not _is_scratch(base): break
        for b in ((o.get("message") or {}).get("content") or []):
            if not (isinstance(b, dict) and b.get("type") == "tool_use"): continue
            inp = b.get("input") or {}
            if cd_hint is None and isinstance(inp.get("command"), str):
                cd_hint = _cd_target(inp["command"])
            fp = inp.get("file_path") or inp.get("path")
            if file_hint is None and isinstance(fp, str) and fp.startswith("/") and not _is_scratch(fp):
                file_hint = os.path.dirname(fp.rstrip("/"))
        if cd_hint: break
    cwd = base
    if base and _is_scratch(base):
        cwd = cd_hint or file_hint or base
    _cwd_cache[path] = (key, cwd)
    return cwd


async def build_fleet():
    sessions = await all_sessions()
    files = read_fleet_files()
    rows = []
    for uuid, s in sessions.items():
        f = files.get(uuid)
        try:
            job = await s.async_get_variable("jobName") or ""
            cwd = await s.async_get_variable("path") or ""
        except Exception:
            job, cwd = "", ""
        txt = await pane_text(s)
        if not is_claude_pane(uuid, job, txt):
            continue                      # hide scratch shells entirely
        lines = [l for l in txt.splitlines() if l.strip()]
        mode = detect_mode(txt)
        prompt = detect_prompt(txt)
        # live working dir: the transcript's current cwd (follows `cd`s), then the
        # statusline's launch-pinned dir, then the iTerm pane path — same order as ccdash
        live_cwd = latest_cwd((f or {}).get("transcript")) or (f or {}).get("cwd") or cwd
        rows.append({
            "uuid": uuid,
            "job": job,
            "cwd": live_cwd,
            "name": os.path.basename(live_cwd.rstrip("/")) or "?",
            "state": (f or {}).get("state", "idle"),
            "model": (f or {}).get("model", ""),
            "ctx": (f or {}).get("ctx"),
            "cost": (f or {}).get("cost"),
            "effort": (f or {}).get("effort"),
            "mode": mode,
            "prompt": prompt,
            "sendable": is_claude_pane(uuid, job, txt),
            "tokens": (f or {}).get("tokens"),
            "lines_add": (f or {}).get("lines_add"),
            "lines_del": (f or {}).get("lines_del"),
            "files": (f or {}).get("files"),
            "prompts": (f or {}).get("prompts"),
            "age": (f or {}).get("age"),
            "dur_ms": (f or {}).get("dur_ms"),
            "work_since": (f or {}).get("state_since"),
            "action": (f or {}).get("action"),   # newest tool call, from transcript
            # Enough lines that the current `⏺ Tool(args)` action line is in the
            # window — it sits several lines above the bottom, behind its `⎿`
            # result, the spinner and the prompt box. The bubble distiller scans
            # this backwards for the newest tool call to show what Claude's doing.
            "tail": lines[-14:],
        })
    # anything blocked on a human answer outranks everything else
    rows.sort(key=lambda r: (0 if r.get("prompt") else 1,
                             {"working": 0, "idle": 1, "ended": 2}.get(r["state"], 3),
                             r["name"]))
    return rows, fleet_limits(files)


# ── session summary (headless `claude -p`) ─────────────────────────────────
# Two header lines for the chat view: a rolling summary of the whole session,
# and — while the task is still running — the condition that will make it stop.
# Generated by shelling out to `claude -p` (the local subscription, no API key).
#
# The summary of a run barely changes and its stop condition even less, so we do
# NOT re-summarise on a timer. Instead we compute ONCE per user prompt — keyed on
# the transcript's prompt count — and reuse it for the whole run. Sending a new
# prompt bumps the count and is what triggers the next (single) summarisation,
# fired proactively the moment the prompt goes out. Results persist to disk so a
# restart reuses them instead of paying for a fresh call.
_SUMMARY_FILE = HERE / ".summaries.json"
_summary_locks = {}          # uuid -> asyncio.Lock (one claude call per pane)


def _load_summaries():
    try:
        return json.loads(_SUMMARY_FILE.read_text())
    except Exception:
        return {}


_summaries = _load_summaries()   # uuid -> {"prompts","summary","success","at"}


def _save_summaries():
    try:
        _SUMMARY_FILE.write_text(json.dumps(_summaries))
        _SUMMARY_FILE.chmod(0o600)
    except Exception as e:
        print(f"  [summary] save failed: {type(e).__name__}: {e}", flush=True)


def _transcript_digest(path, max_chars=20000):
    """Compact text of a session for summarisation: the opening user prompt (the
    task) plus the tail of the conversation, so both 'what it set out to do' and
    'what it's doing now' survive the truncation."""
    try:
        raw = pathlib.Path(path).read_text(errors="replace").splitlines()
    except Exception:
        return ""
    msgs = []
    for ln in raw:
        try:
            o = json.loads(ln)
        except Exception:
            continue
        role = (o.get("message") or {}).get("role") or o.get("type")
        content = (o.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text")
        if not isinstance(content, str) or not content.strip():
            continue
        if any(n in content for n in _PROMPT_NOISE):
            continue
        msgs.append(f"{role}: {content.strip()}")
    if not msgs:
        return ""
    first = msgs[0]
    tail = "\n".join(msgs[1:])
    if len(tail) > max_chars:
        tail = "…" + tail[-max_chars:]
    return (first + "\n" + tail)[:max_chars + 2000]


async def _claude_summary(digest, running):
    """Ask `claude -p` for the two lines. Returns (summary, success|None)."""
    ask = (
        "You are labeling a Claude Code coding session for a phone status bar. "
        "Below is a transcript digest (first prompt, then recent messages). "
        "Reply with EXACTLY two lines and nothing else:\n"
        "SUMMARY: <one sentence, <=110 chars, what this session has been doing overall>\n"
        "SUCCESS: <" + (
            "one sentence, <=110 chars, the concrete condition that will make the "
            "current task stop/finish>" if running else "the word NONE") + "\n\n"
        "Transcript digest:\n" + digest)
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", ask,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
    except Exception as e:
        print(f"  [summary] claude -p failed: {type(e).__name__}: {e}", flush=True)
        return None, None
    text = out.decode(errors="replace")
    summary, success = None, None
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("SUMMARY:"):
            summary = s.split(":", 1)[1].strip()[:140]
        elif s.upper().startswith("SUCCESS:"):
            v = s.split(":", 1)[1].strip()
            success = None if v.upper() in ("NONE", "N/A", "") else v[:140]
    return summary, success


async def _ensure_summary(uuid, path, pcount, running):
    """Return this run's stored summary, computing it once if the prompt count
    moved (a new prompt = a new run to describe). One claude call per pane."""
    entry = _summaries.get(uuid)
    if entry and entry.get("prompts") == pcount:
        return entry
    lock = _summary_locks.setdefault(uuid, asyncio.Lock())
    async with lock:
        entry = _summaries.get(uuid)          # recheck after waiting on the lock
        if entry and entry.get("prompts") == pcount:
            return entry
        digest = await asyncio.to_thread(_transcript_digest, path)
        if not digest:
            return entry
        summary, success = await _claude_summary(digest, running)
        entry = {"prompts": pcount, "summary": summary, "success": success,
                 "at": time.time()}
        _summaries[uuid] = entry
        await asyncio.to_thread(_save_summaries)
        print(f"  [summary] {uuid[:8]} summarised at prompt #{pcount}", flush=True)
        return entry


async def _summarise_after_send(uuid):
    """Fire-and-forget: right after a prompt is sent, wait for Claude to write it
    into the transcript, then compute+store this run's summary so it's ready
    before the phone ever asks."""
    await asyncio.sleep(2.5)
    try:
        f = read_fleet_files().get(uuid) or {}
        path = f.get("transcript")
        if not path or not os.path.exists(path):
            return
        _, pcount = session_ops(path)
        await _ensure_summary(uuid, path, pcount, f.get("state") == "working")
    except Exception as e:
        print(f"  [summary] after-send failed: {type(e).__name__}: {e}", flush=True)


async def api_summary(request):
    if not authed(request):
        return web.json_response({"error": "locked"}, status=401)
    uuid = (request.query.get("uuid") or "").upper()
    f = read_fleet_files().get(uuid) or {}
    path = f.get("transcript")
    running = f.get("state") == "working"
    if not path or not os.path.exists(path):
        # Pane gone — still surface the last summary we saved for it, if any.
        e = _summaries.get(uuid) or {}
        return web.json_response({"summary": e.get("summary"),
                                  "success": e.get("success"), "running": False})
    _, pcount = session_ops(path)
    entry = await _ensure_summary(uuid, path, pcount, running) or {}
    return web.json_response({"summary": entry.get("summary"),
                              "success": entry.get("success"),
                              "running": running, "prompts": pcount})


# ── auth ───────────────────────────────────────────────────────────────────
# The token is a BOOTSTRAP credential only: it is accepted once, at "/", and
# immediately exchanged for an HttpOnly session cookie. No API or socket ever
# looks at it, so it cannot be replayed from a URL, a screenshot or a log.
def authed(request):
    return auth.unlocked(request) is not None


def guard(handler):
    async def wrapped(request):
        if not auth.same_origin(request):
            auth.audit(request, "csrf.block", {"origin": request.headers.get("Origin")})
            return web.json_response({"error": "bad origin"}, status=403)
        if auth.unlocked(request) is None:
            s = auth.get_session(request, touch=False)
            return web.json_response(
                {"error": "locked" if s else "no session",
                 "relock": bool(s)}, status=401)
        return await handler(request)
    return wrapped


def writes(action):
    """Wrap a state-changing endpoint: audit it, and never let it run locked."""
    def deco(handler):
        async def wrapped(request):
            if not auth.same_origin(request):
                auth.audit(request, "csrf.block",
                           {"origin": request.headers.get("Origin"), "for": action})
                return web.json_response({"error": "bad origin"}, status=403)
            if auth.unlocked(request) is None:
                s = auth.get_session(request, touch=False)
                return web.json_response({"error": "locked", "relock": bool(s)},
                                         status=401)
            body = {}
            try:
                body = await request.json()
            except Exception:
                pass
            request["_body"] = body
            auth.audit(request, action, {k: str(v)[:120] for k, v in body.items()})
            return await handler(request)
        return wrapped
    return deco


# ── routes ─────────────────────────────────────────────────────────────────
async def client_log(request):
    """A phone has no console you can open. Errors come here instead."""
    try:
        d = await request.json()
    except Exception:
        d = {}
    print(f"  [client] {auth.client_ip(request)}: {str(d.get('msg'))[:400]}",
          flush=True)
    return web.json_response({"ok": True})



# The icon and manifest are the only unauthenticated responses in the app. They
# have to be: a launcher fetches them with no cookie when the icon is installed,
# and they reveal nothing but a logo.
async def manifest(request):
    return web.json_response({
        "name": "The Yard", "short_name": "Yard",
        "start_url": "/", "scope": "/",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#14171b", "theme_color": "#22262d",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icons/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }, headers={"Cache-Control": "public, max-age=3600"})


async def icon(request):
    name = request.match_info["name"]
    if not re.fullmatch(r"icon-(maskable-)?\d{3}\.png", name):
        return web.Response(status=404)
    path = HERE / "static" / "icons" / name
    if not path.exists():
        return web.Response(status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})



async def index(request):
    """Bootstrap, then serve the shell.

    A `?t=` token in the URL (the QR) is spent here exactly once: it mints a
    session cookie and we redirect to a clean "/" so the secret never sits in
    the address bar, history or a screenshot. After that the cookie — plus a
    passkey — is the only way in.
    """
    ip = auth.client_ip(request)
    left = auth.locked_out(ip)
    if left:
        return web.Response(status=429, text=f"locked out, retry in {left}s",
                            headers={"Retry-After": str(left)})

    tok = request.query.get("t")
    if tok is not None and auth.get_session(request) is None:
        if not secrets.compare_digest(tok, TOKEN):
            auth.note_fail(ip)
            auth.audit(request, "bootstrap.fail", {"len": len(tok)})
            print(f"  [index] {ip} REJECTED — bad bootstrap token", flush=True)
            return web.Response(status=401, text="401 — bad token")
        sid = auth.new_session(ip)
        auth.audit(request, "bootstrap.ok")
        resp = web.HTTPFound("/")                       # drop ?t= from the bar
        auth.set_session_cookie(resp, sid, request)
        print(f"  [index] {ip} bootstrapped a session", flush=True)
        return resp

    if tok is not None:                                 # already had a session
        resp = web.HTTPFound("/")
        return resp

    # No session cookie. If a passkey is already registered for this origin,
    # serve the shell anyway — its gate runs a passkey unlock that mints a fresh
    # session (see auth.login_begin), so a lapsed session no longer forces a
    # token paste. Only fall back to the token gate when there's no passkey to
    # unlock with, i.e. a brand-new browser that must bootstrap to enrol one.
    have_passkey = (auth.passkey_capable(request)
                    and auth.has_passkey(auth.rp_id(request)))
    if auth.get_session(request) is None and not have_passkey:
        auth.audit(request, "index.nosession")
        return web.Response(
            status=401, content_type="text/html",
            headers={"Cache-Control": "no-store"},
            text="""<meta name=viewport content="width=device-width,initial-scale=1">
<body style="background:#14171b;color:#f2f5f9;font:15px/1.6 -apple-system,
 BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:34px 22px;margin:0">
<h2 style="font:700 14px/1 sans-serif;letter-spacing:.16em;text-transform:uppercase;
 color:#d77757;margin:0 0 14px">The Yard — locked</h2>
<p style="color:#a8b2bf;margin:0 0 18px">No session in this browser. Paste the
 token printed by the server to start one.</p>
<input id=t placeholder="token" autocomplete="off" autocapitalize="none"
 spellcheck="false" style="width:100%;box-sizing:border-box;background:#22262d;
 border:1px solid #434b58;border-radius:6px;color:#f2f5f9;padding:13px;
 font:14px ui-monospace,monospace">
<button onclick="go()" style="margin-top:12px;width:100%;background:#d77757;
 border:none;border-radius:6px;color:#2a1206;font:700 15px sans-serif;
 padding:14px;cursor:pointer">Start session</button>
<div id=e style="color:#ff6b61;font:12px ui-monospace,monospace;margin-top:12px"></div>
<p style="color:#78828f;font-size:12px;margin-top:22px">Scanning the QR in an
 app's built-in browser starts the session there, not in Chrome. Open the link
 in your real browser, or paste the token here.</p>
<script>
async function go(){
  const r = await fetch('/auth/bootstrap', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({token: document.getElementById('t').value.trim()})});
  const d = await r.json().catch(()=>({}));
  if (r.ok) location.replace('/');
  else document.getElementById('e').textContent = d.error || r.status;
}
document.getElementById('t').addEventListener('keydown', e => {
  if (e.key === 'Enter') go();
});
</script></body>""")

    body = (HERE / "static" / "index.html").read_text()
    resp = web.Response(text=body, content_type="text/html")
    # served straight off disk and edited often — never let the phone cache it
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    if authed(request):
        resp.set_cookie("t", TOKEN, max_age=60 * 60 * 24 * 365,
                        samesite="Lax", httponly=False)
    return resp


@guard
async def api_fleet(request):
    rows, limits = await build_fleet()
    return web.json_response({"sessions": rows, "limits": limits})


@writes("key")
async def api_key(request):
    body = await request.json()
    uuid, k = body.get("uuid", "").upper(), body.get("key")
    if k not in KEYS:
        return web.json_response({"error": f"unknown key {k!r}"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    # never type into a plain shell by accident — but identify the pane by its
    # Claude UI, not by jobName, which is often a child (caffeinate, bash, git)
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    await s.async_send_text(KEYS[k])
    return web.json_response({"ok": True, "sent": k})


@writes("send")
async def api_send(request):
    body = await request.json()
    uuid = body.get("uuid", "").upper()
    text = body.get("text", "")
    submit = bool(body.get("submit", True))
    if not text.strip():
        return web.json_response({"error": "empty"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    # literal, byte-exact: $ ` " ' and newlines all survive (probe_keys.py test 1)
    await s.async_send_text(text)
    if submit:
        await asyncio.sleep(0.15)
        await s.async_send_text("\r")
        asyncio.create_task(_summarise_after_send(uuid))   # refresh the brief
    return web.json_response({"ok": True, "chars": len(text)})


# Claude's input line: a `❯` prompt. A ghost auto-suggestion is rendered with a
# NON-breaking space after the caret (❯\xa0…); text the user actually typed uses
# a regular space (❯ …). We key off that to tell "accept Claude's suggestion"
# apart from "submit what's already typed".
_PROMPT_CARET = "❯"


def _input_suggestion(text):
    """(kind, suggestion) for the pane's input line.

    kind: "ghost" with the suggested text, "typed" if there's real typed text,
    or "empty". Ghosts can't be committed by a keystroke over the API (Tab/→ do
    nothing), so the caller retypes the suggestion as a real prompt instead.
    """
    for line in reversed(text.splitlines()):
        st = line.strip()
        if not st.startswith(_PROMPT_CARET):
            continue
        rest = st[len(_PROMPT_CARET):]
        if rest.startswith("\xa0"):
            return "ghost", rest[1:].strip()
        if rest.strip():
            return "typed", rest.strip()
        return "empty", ""
    return "empty", ""


@writes("send")
async def api_submit(request):
    """"Just send it" — the phone's ▶ with an empty box.

    If Claude is showing a ghost-suggested prompt, retype it and submit (a bare
    Enter won't: the suggestion isn't in the buffer and no accept-key commits it
    through the API). Otherwise just press Enter to submit whatever is typed.
    """
    body = await request.json()
    uuid = body.get("uuid", "").upper()
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    txt = await pane_text(s)
    if not is_claude_pane(uuid, job, txt):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    kind, sug = _input_suggestion(txt)
    if kind == "ghost" and sug:
        await s.async_send_text(sug)          # retype the suggestion as real input
        await asyncio.sleep(0.15)
    await s.async_send_text("\r")             # submit (typed text, or the retype)
    asyncio.create_task(_summarise_after_send(uuid))       # refresh the brief
    return web.json_response({"ok": True, "kind": kind, "sent": sug})


async def _auto_trust(sess):
    """Auto-answer Claude's first-run "Do you trust the files in this folder?".

    trust_dir() pre-writes hasTrustDialogAccepted so the prompt normally never
    fires, but a concurrent ~/.claude.json rewrite by another Claude can drop
    that fresh key before this pane reads it, and then the pane sits blocked on
    the trust dialog. This is the belt-and-suspenders: watch the new pane for a
    few seconds and, if the trust prompt appears, pick its "Yes, proceed" option.
    Scoped hard to the trust dialog — any other prompt is left untouched.
    """
    deadline = time.time() + 15
    while time.time() < deadline:
        await asyncio.sleep(0.6)
        try:
            text = await pane_text(sess)
        except Exception:
            continue
        if "trust the files in this folder" not in text.lower():
            continue                      # only ever act on the trust dialog
        p = detect_prompt(text)
        if not p:
            continue
        yes = next((o for o in p["options"]
                    if any(w in o["label"].lower()
                           for w in ("yes", "proceed", "trust"))), None)
        if not yes:
            return
        await sess.async_send_text(KEYS[yes["key"]])
        print(f"  [spawn] auto-accepted trust prompt (option {yes['key']})",
              flush=True)
        return


@writes("spawn")
async def api_spawn(request):
    """Open a brand-new Claude pane in the iTerm window and hand back its UUID.

    It starts in a throwaway scratch dir so nothing real is touched until you tell
    it where to work. The scratch dir is pre-trusted in ~/.claude.json so Claude's
    first-run "trust the files in this folder?" prompt never fires. We add the UUID
    to KNOWN_CLAUDE up front so it lands in the fleet the instant it opens, before
    its jobName has even settled to `node`.
    """
    import tempfile, shlex
    await APP.async_refresh()
    scratch = tempfile.mkdtemp(prefix="cc-scratch-")
    trust_dir(scratch)                     # skip Claude's first-run trust prompt
    # Grow the fleet's own tab into a grid instead of opening a new tab. Panes are
    # placed row-major (see GRID_MAX_COLS) so the split lands in an aligned column
    # or row rather than as a random narrow sliver.
    try:
        tab = fleet_tab(APP)
        if tab is None:
            return web.json_response(
                {"error": "no iTerm window open to spawn into"}, status=409)
        src, vertical = pick_grid_split(tab)
        sess = await src.async_split_pane(vertical=vertical, before=False)
    except Exception as e:
        return web.json_response(
            {"error": f"could not open pane: {type(e).__name__}: {e}"}, status=500)
    uuid = sess.session_id.upper()
    KNOWN_CLAUDE.add(uuid)
    await sess.async_send_text(f"cd {shlex.quote(scratch)} && clear && claude\n")
    # Fallback in case the pre-trust key got clobbered — auto-accept the trust
    # dialog if it still shows. Fire-and-forget so the spawn returns immediately.
    asyncio.create_task(_auto_trust(sess))
    print(f"  [spawn] new pane {uuid} in {scratch}", flush=True)
    return web.json_response({"uuid": uuid, "dir": scratch})


@writes("kill")
async def api_kill(request):
    """End a session and close its iTerm pane — the drag-to-trash gesture.

    Ctrl-C first so Claude tears down its own child processes, then /exit so it
    saves the transcript the way a normal quit would, then close the pane. The
    close is what removes the split/tab from the Mac; without it the shell just
    returns to a prompt and the pane lingers.
    """
    d = await request.json()
    uuid = (d.get("uuid") or "").upper()
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    try:
        await s.async_send_text("\x03")          # interrupt whatever is running
        await asyncio.sleep(0.2)
        await s.async_send_text("/exit\r")       # let Claude close its session
        await asyncio.sleep(0.6)
    except Exception as e:
        print(f"  [kill] {uuid} graceful stop failed: {type(e).__name__}: {e}",
              flush=True)
    try:
        await s.async_close(force=True)
    except Exception as e:
        return web.json_response(
            {"error": f"could not close pane: {type(e).__name__}: {e}"}, status=500)
    KNOWN_CLAUDE.discard(uuid)
    print(f"  [kill] closed pane {uuid}", flush=True)
    return web.json_response({"ok": True, "uuid": uuid})


EFFORTS = ("low", "medium", "high")
# Measured cycle (probe_mode.py): auto → manual → accept edits → plan → auto.
# "bypass" is separate — sessions launched with --dangerously-skip-permissions
# sit in it and it is not part of the Shift-Tab rotation.
MODES = ("manual", "auto", "accept", "plan", "bypass")
MODE_LABEL = {"manual": "manual", "auto": "auto",
              "accept": "accept edits", "plan": "plan", "bypass": "bypass"}


def detect_mode(text):
    """Read the current permission mode off Claude's status line."""
    for l in text.splitlines():
        low = l.lower()
        if "bypass permissions" in low: return "bypass"
        if "plan mode on" in low:       return "plan"
        if "accept edits on" in low:    return "accept"
        if "auto mode on" in low:       return "auto"
        if "manual mode on" in low:     return "manual"
    return None


@writes("mode")
async def api_mode(request):
    """Shift-Tab until the requested mode is showing.

    The cycle order isn't assumed — we re-read the status line after each press
    and stop when it matches, so this stays correct if Claude reorders modes.
    """
    body = await request.json()
    uuid, want = body.get("uuid", "").upper(), body.get("mode")
    if want not in MODES:
        return web.json_response({"error": f"bad mode {want!r}"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)

    if want == "bypass":
        return web.json_response(
            {"error": "bypass is not reachable via Shift-Tab; "
                      "it is set by launching with --dangerously-skip-permissions"},
            status=400)
    cur0 = detect_mode(await pane_text(s))
    if cur0 == "bypass":
        return web.json_response(
            {"error": "pane is in bypass-permissions mode; Shift-Tab does not "
                      "cycle out of it — refusing to guess"}, status=409)

    seen = []
    for _ in range(len(MODES) + 1):
        cur = detect_mode(await pane_text(s))
        seen.append(cur)
        if cur == want:
            return web.json_response({"ok": True, "mode": cur, "path": seen})
        await s.async_send_text("\x1b[Z")
        await asyncio.sleep(0.7)
    final = detect_mode(await pane_text(s))
    if final == want:
        return web.json_response({"ok": True, "mode": final, "path": seen})
    # Never leave it spinning — report honestly rather than silently mis-set.
    return web.json_response(
        {"error": f"could not reach {want!r}; ended on {final!r}",
         "mode": final, "path": seen}, status=409)


@writes("effort")
async def api_effort(request):
    """Fire /effort <level>.

    Typing "/" opens Claude's autocomplete, which swallows the first Enter —
    so the sequence is text, Enter (accept completion), Enter (submit).
    Verified in probe_effort.py against all three levels.
    """
    body = await request.json()
    uuid, level = body.get("uuid", "").upper(), body.get("level")
    if level not in EFFORTS:
        return web.json_response({"error": f"bad level {level!r}"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    await send_slash(s, f"/effort {level}")
    return web.json_response({"ok": True, "level": level})


MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}

# Allowlisted slash commands. Deliberately excludes anything that ends the
# session or is hard to undo from a phone (/exit, /logout, /doctor).
COMMANDS = {
    "clear":   ("/clear",   "wipe context"),
    "compact": ("/compact", "summarise + shrink"),
    "cost":    ("/cost",    "show spend"),
    "context": ("/context", "show context use"),
    "usage":   ("/usage",   "show limits"),
}


async def send_slash(s, text):
    """Type a slash command and submit it.

    The first Enter is eaten by Claude's autocomplete popup, so two are needed —
    see probe_slash.py, where single-Enter silently did nothing.
    """
    await s.async_send_text(text)
    await asyncio.sleep(0.9)
    await s.async_send_text("\r")
    await asyncio.sleep(0.6)
    await s.async_send_text("\r")


@writes("cmd")
async def api_cmd(request):
    body = await request.json()
    uuid, name = body.get("uuid", "").upper(), body.get("cmd")
    if name not in COMMANDS:
        return web.json_response({"error": f"command {name!r} not allowed"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    await send_slash(s, COMMANDS[name][0])
    return web.json_response({"ok": True, "cmd": COMMANDS[name][0]})


@guard
async def api_commands(request):
    return web.json_response(
        {"commands": [{"id": k, "cmd": v[0], "desc": v[1]}
                      for k, v in COMMANDS.items()]})


@writes("model")
async def api_model(request):
    """Fire /model <name>.

    WARNING: /model is NOT session-local. Claude echoes "saved as your default
    for new sessions" — it rewrites ~/.claude/settings.json, so this repoints
    every future session too. The UI requires a second confirming tap.
    """
    body = await request.json()
    uuid, name = body.get("uuid", "").upper(), body.get("model")
    if name not in MODELS:
        return web.json_response({"error": f"bad model {name!r}"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    await send_slash(s, f"/model {MODELS[name]}")
    return web.json_response({"ok": True, "model": name,
                              "note": "also changed the global default"})


# ── usage / CC Dash data ───────────────────────────────────────────────────
# Reuses ~/.claude/cc_history.py — the same module cc-dashboard.py reads, so the
# numbers here and in the TUI come from one source and cannot drift apart.
sys.path.insert(0, os.path.expanduser("~/.claude"))
_usage_cache = {"at": 0, "data": None}
USAGE_TTL = 120


def _compute_usage():
    # Only today. The dashboard is a "what is happening right now" screen — the
    # 30-day chart, per-project spend and all-time totals live in the TUI.
    import cc_history as HIST
    agg = HIST.build()          # build() already returns the aggregate
    tokens = HIST.series(agg, "tokens")
    cost = HIST.series(agg, "cost")
    today = time.strftime("%Y-%m-%d")
    tot = agg.get("tot") or {}
    return {
        "today": {"tokens": tokens.get(today, 0),
                  "cost": round(cost.get(today, 0.0), 2)},
        # all-time roll-up: tokens counts in+out+cache-create (cache reads are the
        # replayed context, not new work), matching cc-dashboard's totals line.
        "all_time": {
            "tokens": (tot.get("in", 0) + tot.get("out", 0) + tot.get("cc", 0)),
            "cost": round(agg.get("cost", 0.0) or 0.0, 2),
            "sessions": agg.get("sessions", 0),
            "active_days": len(HIST.active_days(agg))
                           if hasattr(HIST, "active_days") else 0,
        },
    }


@guard
async def api_usage(request):
    now = time.time()
    if _usage_cache["data"] and now - _usage_cache["at"] < USAGE_TTL:
        data = _usage_cache["data"]
    else:
        try:
            data = await asyncio.to_thread(_compute_usage)
            _usage_cache.update(at=now, data=data)
        except Exception as e:
            print(f"  [usage] failed: {type(e).__name__}: {e}", flush=True)
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    rows, limits = await build_fleet()
    return web.json_response({
        **data,
        "limits": limits,
        "fleet": {"panes": len(rows),
                  "working": sum(1 for r in rows if r["state"] == "working"),
                  "live_cost": round(sum(r.get("cost") or 0 for r in rows), 2)},
        "age": int(now - _usage_cache["at"]),
        # Absolute epoch the data was computed at, so the client can tick the
        # freshness label off its own clock (same pattern as the fleet timers).
        "computed_at": _usage_cache["at"],
    })


async def ws_pane(request):
    if not authed(request):
        return web.json_response({"error": "locked"}, status=401)
    uuid = request.match_info["uuid"].upper()
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    last = None
    sent_history = False
    try:
        while not ws.closed:
            s = (await all_sessions()).get(uuid)
            if not s:
                await ws.send_json({"gone": True})
                break
            if not sent_history:
                # scrollback is expensive and rarely changes at the top; ship it
                # once so the client can render the whole conversation, then
                # stream only the live screen below it
                hist = await pane_history(s)
                await ws.send_json({"history": hist, "cols": await pane_cols(s)})
                sent_history = True
            txt = await pane_text(s)
            if txt != last:                      # only push on change
                last = txt
                await ws.send_json({"text": txt, "cols": await pane_cols(s),
                                    "prompt": detect_prompt(txt)})
            await asyncio.sleep(POLL)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        if not ws.closed:
            await ws.close()
    return ws


async def ws_fleet(request):
    peer = request.remote
    if not authed(request):
        print(f"  [ws/fleet] {peer} REJECTED — no unlocked session", flush=True)
        return web.json_response({"error": "locked"}, status=401)
    print(f"  [ws/fleet] {peer} connected", flush=True)
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    last = None
    try:
        while not ws.closed:
            rows, limits = await build_fleet()
            blob = json.dumps([rows, limits], sort_keys=True)
            if blob != last:
                first = last is None
                last = blob
                await ws.send_json({"sessions": rows, "limits": limits})
                if first:
                    print(f"  [ws/fleet] {peer} first payload sent "
                          f"({len(rows)} panes, {len(blob)} bytes)", flush=True)
            await asyncio.sleep(1.0)
    except (asyncio.CancelledError, ConnectionResetError):
        print(f"  [ws/fleet] {peer} disconnected", flush=True)
    except Exception as e:
        print(f"  [ws/fleet] {peer} ERROR {type(e).__name__}: {e}", flush=True)
    finally:
        if not ws.closed:
            await ws.close()
    return ws


# ── boot ───────────────────────────────────────────────────────────────────
def ts_name():
    """The https host `tailscale serve` is publishing this port on, if any."""
    try:
        st = json.loads(os.popen("tailscale serve status --json 2>/dev/null").read())
    except Exception:
        return ""
    for hostport, conf in (st.get("Web") or {}).items():
        for _, h in (conf.get("Handlers") or {}).items():
            if str(h.get("Proxy", "")).endswith(f":{PORT}"):
                return hostport.split(":")[0]
    return ""


async def main(connection):
    global CONN, APP
    try:
        import icon as icon_art
        icon_art.write_all(HERE / "static" / "icons")
    except Exception as e:
        print(f"  [icons] not regenerated: {type(e).__name__}: {e}", flush=True)
    CONN = connection
    APP = await iterm2.async_get_app(connection)

    app = web.Application()
    app["TOKEN"] = TOKEN
    app.router.add_get("/", index)
    app.router.add_get("/api/fleet", api_fleet)
    app.router.add_get("/api/summary", api_summary)
    app.router.add_post("/api/key", api_key)
    app.router.add_post("/api/send", api_send)
    app.router.add_post("/api/submit", api_submit)
    app.router.add_post("/api/spawn", api_spawn)
    app.router.add_post("/api/kill", api_kill)
    app.router.add_post("/api/effort", api_effort)
    app.router.add_post("/api/mode", api_mode)
    app.router.add_post("/api/model", api_model)
    app.router.add_post("/api/cmd", api_cmd)
    app.router.add_get("/api/commands", api_commands)
    app.router.add_get("/api/usage", api_usage)
    # passkey enrolment and unlock — the only endpoints a locked session may use
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/icons/{name}", icon)
    app.router.add_post("/api/clientlog", client_log)
    app.router.add_post("/auth/bootstrap", auth.bootstrap)
    app.router.add_get("/auth/whoami", auth.whoami)
    app.router.add_post("/auth/register/begin", auth.register_begin)
    app.router.add_post("/auth/register/complete", auth.register_complete)
    app.router.add_post("/auth/login/begin", auth.login_begin)
    app.router.add_post("/auth/login/complete", auth.login_complete)
    app.router.add_post("/auth/logout", auth.logout)
    app.router.add_get("/ws/fleet", ws_fleet)
    app.router.add_get("/ws/pane/{uuid}", ws_pane)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, BIND, PORT).start()

    # Where the phone should actually point: the tailnet name, over TLS, served
    # by `tailscale serve`. Falls back to the raw bind address if the tunnel is
    # not up yet — and says so, loudly, because that path has no passkey.
    ts_host = ts_name()
    if ts_host:
        base = f"https://{ts_host}"
    else:
        base = f"http://{BIND}:{PORT}"
    phone_url = f"{base}/?t={TOKEN}"
    print(f"\n  CC Dispatch — bound to {BIND}:{PORT}\n")
    if BIND not in ("127.0.0.1", "::1", "localhost"):
        print("  !! WARNING: not bound to loopback. Anyone who can reach this\n"
              "     address can attempt the bootstrap token. Prefer loopback +\n"
              "     `tailscale serve`.\n")
    if not ts_host:
        print("  !! `tailscale serve` is not running — no TLS, and passkeys\n"
              "     cannot be registered over a bare IP. Start it with:\n"
              f"       tailscale serve --bg {PORT}\n")
    # Scan to launch: the token rides in the QR, so the phone opens straight into
    # the fleet with no typing. Falls back to the bare URL if qrcode isn't present.
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(phone_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as e:
        print(f"  (no QR — {type(e).__name__}: {e})")
    print(f"  phone :  {phone_url}")
    print(f"  local :  http://127.0.0.1:{PORT}/?t={TOKEN}")
    print(f"  passkeys registered: {len(auth.load_creds())}"
          f"   audit: {auth.AUDIT_FILE}\n")
    fleet, _ = await build_fleet()
    print(f"  {len(fleet)} Claude panes visible, "
          f"{sum(1 for f in fleet if f['sendable'])} sendable\n", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    iterm2.run_until_complete(main)
