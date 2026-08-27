#!/usr/bin/env python3
"""All-time Claude Code usage history — the data layer.

Claude Code's own ~/.claude/stats-cache.json is not usable for this: it only refreshes
when CC recomputes it (ours was 11 weeks stale), keeps ~95 days, and carries no per-day
tokens or cost. So history is rebuilt from the transcripts themselves
(~/.claude/projects/*/*.jsonl), which are the same source /usage bills from.

Scanning 2.6 GB every run would be absurd, so every file's contribution is cached in
~/.claude/cc-history-cache.json keyed by (mtime, size). Only files that changed since
the last build are re-read — a warm rebuild touches the handful of live sessions.

Public API:
  build(force=False) -> dict   scan + cache, returns the aggregate
  load() -> dict | None        read the cached aggregate without scanning
"""
import os, glob, json, time, tempfile
from datetime import datetime, date, timedelta

PROJECTS   = os.path.expanduser("~/.claude/projects")
CACHE_FILE = os.path.expanduser("~/.claude/cc-history-cache.json")
CACHE_V    = 4      # v4 adds per-response records, for cross-file dedupe

# Per-1M-token pricing (input, output); cache read 0.1×input, cache write 1.25×input.
# Kept in sync with cc-dashboard.py's table.
PRICING = {
    "claude-opus-4-8": (5, 25), "claude-opus-4-7": (5, 25), "claude-opus-4-6": (5, 25),
    "claude-opus-4-5": (5, 25), "claude-sonnet-4-6": (3, 15), "claude-haiku-4-5": (1, 5),
    "claude-fable-5": (10, 50), "claude-opus-5": (5, 25), "claude-sonnet-5": (3, 15),
}
DEFAULT_PRICE = (5, 25)

def price_for(mdl):
    for k, v in PRICING.items():
        if mdl.startswith(k): return v
    return DEFAULT_PRICE

def model_cost(mdl, i, o, cc, cr):
    pin, pout = price_for(mdl)
    return (i*pin + o*pout + cr*0.1*pin + cc*1.25*pin) / 1e6

def short_model(m):
    return (m or "unknown").replace("claude-", "").split("-2")[0]

def project_of(path):
    # The transcript's own "cwd" field is the only lossless source: the directory name
    # (~/.claude/projects/<encoded-cwd>/) flattens every '/' AND every '-' to '-', so
    # "GP-Beauty/.claude" and "GP/Beauty/.claude" encode identically. Read cwd off the
    # first line and fall back to the encoded dir only if the file has none.
    try:
        with open(path, errors="ignore") as f:
            for _ in range(5):
                line = f.readline()
                if not line: break
                if '"cwd"' not in line: continue
                cwd = json.loads(line).get("cwd")
                if cwd: return os.path.basename(cwd.rstrip("/")) or cwd
    except Exception:
        pass
    # fallback: the first path component under projects/ is the encoded cwd; for a
    # subagent transcript the immediate parent is a session uuid, which names nothing
    rel = os.path.relpath(path, PROJECTS).split(os.sep)
    d = rel[0] if rel else ""
    return d.rstrip("-").split("-")[-1] or d

# ── per-file scan ──────────────────────────────────────────────────────────
# A file's contribution:
#   days   {"YYYY-MM-DD": [in, out, cc, cr, responses]}
#   hours  {"0".."23": responses}
#   models {model: [in, out, cc, cr]}
#   cost   float   (priced per model at scan time)
#   first/last  epoch of the first/last timestamped usage line
def scan_file(path):
    days = {}; hours = {}; models = {}; seen = set(); recs = []
    first = last = None; proj = None
    try:
        f = open(path, errors="ignore")
    except OSError:
        return None
    with f:
        for line in f:
            if proj is None and '"cwd"' in line:
                try:
                    cwd = json.loads(line).get("cwd")
                    if cwd: proj = os.path.basename(cwd.rstrip("/")) or cwd
                except Exception:
                    pass
            if '"usage"' not in line: continue
            try: o = json.loads(line)
            except Exception: continue
            m = o.get("message")
            if not isinstance(m, dict): continue
            u = m.get("usage")
            if not isinstance(u, dict): continue
            # Dedupe by message.id: one API response is written across several transcript
            # lines (thinking / text / tool_use), each repeating the message-level usage.
            mid = m.get("id")
            if mid is not None:
                if mid in seen: continue
                seen.add(mid)
            i  = u.get("input_tokens", 0) or 0
            ot = u.get("output_tokens", 0) or 0
            cc = u.get("cache_creation_input_tokens", 0) or 0
            cr = u.get("cache_read_input_tokens", 0) or 0
            mdl = m.get("model") or "unknown"
            md = models.setdefault(mdl, [0, 0, 0, 0])
            md[0] += i; md[1] += ot; md[2] += cc; md[3] += cr
            ts = o.get("timestamp")
            if not ts: continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            except Exception:
                continue
            ep = dt.timestamp()
            if first is None or ep < first: first = ep
            if last is None or ep > last: last = ep
            k = dt.strftime("%Y-%m-%d")
            d = days.setdefault(k, [0, 0, 0, 0, 0])
            d[0] += i; d[1] += ot; d[2] += cc; d[3] += cr; d[4] += 1
            h = str(dt.hour); hours[h] = hours.get(h, 0) + 1
            # One record per API response, so aggregate() can drop responses that a
            # RESUMED or forked session copied into a second transcript. The per-file
            # `seen` set above cannot see those — same message.id, different file — and
            # they were inflating every total by ~2%.
            if mid is not None:
                recs.append([mid[-14:], k, dt.hour, mdl, i, ot, cc, cr])
    cost = sum(model_cost(m, *v) for m, v in models.items())
    return {"days": days, "hours": hours, "models": models, "recs": recs,
            "cost": cost, "first": first, "last": last,
            "proj": proj or project_of(path)}

def _dedupe(cache):
    """Per-file contributions with cross-file duplicate responses removed.

    Files are walked oldest-first (by first timestamp), so the transcript that
    ORIGINALLY made a request keeps it and any later copy of it is dropped. Entries
    scanned by an older cache version carry no records and pass through untouched."""
    eff = {}
    order = sorted(cache["files"].items(),
                   key=lambda kv: (kv[1].get("first") is None, kv[1].get("first") or 0))
    claimed = set()
    for p, r in order:
        recs = r.get("recs")
        if recs is None:
            eff[p] = r; continue
        days = {}; hours = {}; models = {}
        for mid, k, hr, mdl, i, ot, cc, cr in recs:
            if mid in claimed: continue
            claimed.add(mid)
            d = days.setdefault(k, [0, 0, 0, 0, 0])
            d[0] += i; d[1] += ot; d[2] += cc; d[3] += cr; d[4] += 1
            md = models.setdefault(mdl, [0, 0, 0, 0])
            md[0] += i; md[1] += ot; md[2] += cc; md[3] += cr
            h = str(hr); hours[h] = hours.get(h, 0) + 1
        eff[p] = dict(r, days=days, hours=hours, models=models,
                      cost=sum(model_cost(m, *v) for m, v in models.items()))
    return eff

# ── build / cache ──────────────────────────────────────────────────────────
def _load_cache():
    try:
        c = json.load(open(CACHE_FILE))
        if c.get("v") == CACHE_V: return c
    except Exception:
        pass
    return {"v": CACHE_V, "files": {}}

def _save_cache(cache):
    # atomic — the dashboard reads this file on its own schedule
    d = os.path.dirname(CACHE_FILE)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cc-history-")
    try:
        with os.fdopen(fd, "w") as f: json.dump(cache, f, separators=(",", ":"))
        os.replace(tmp, CACHE_FILE)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass

def build(force=False, progress=None):
    """Scan every transcript (changed ones only, unless force) and aggregate."""
    cache = {"v": CACHE_V, "files": {}} if force else _load_cache()
    # recursive: subagent and workflow transcripts live at
    # projects/<proj>/<session-uuid>/subagents/[workflows/wf_*/]agent-*.jsonl — 733 files
    # here, and their usage is real (/usage bills it too). A shallow glob misses all of it.
    files = glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True)
    live = set(); scanned = 0
    for n, p in enumerate(files):
        try: st = os.stat(p)
        except OSError: continue
        live.add(p)
        key = [st.st_mtime, st.st_size]
        ent = cache["files"].get(p)
        if ent and ent.get("k") == key:
            ent.pop("gone", None)          # back on disk (or never left)
            continue
        r = scan_file(p)
        if r is None: continue
        r["k"] = key
        cache["files"][p] = r
        scanned += 1
        if progress and scanned % 25 == 0: progress(n + 1, len(files), scanned)
    # Claude Code prunes transcripts (cleanupPeriodDays), so a file vanishing is NORMAL
    # and does not mean its usage never happened. Keep its already-scanned aggregate and
    # mark it retired — that is what makes "all-time" keep meaning all-time instead of
    # silently collapsing to the prune horizon. Retired entries are never rescanned.
    for p, r in cache["files"].items():
        if p not in live: r["gone"] = True
    _save_cache(cache)
    return aggregate(cache)

def load():
    c = _load_cache()
    return aggregate(c) if c["files"] else None

def cache_age():
    try: return time.time() - os.path.getmtime(CACHE_FILE)
    except OSError: return None

# ── the pre-transcript tail ────────────────────────────────────────────────
# Claude Code prunes transcripts (cleanupPeriodDays, ~30 by default), so the .jsonl
# history only reaches back a few weeks. ~/.claude/stats-cache.json keeps a longer
# dailyActivity list — message/session/tool counts only, no tokens and no cost, and it
# only refreshes when CC recomputes it. Used strictly for days the transcripts no longer
# cover, so the heatmap shows the real span instead of starting at the prune horizon.
PROMPTS_FILE = os.path.expanduser("~/.claude/history.jsonl")
def read_prompts():
    """date -> prompts you typed that day, from ~/.claude/history.jsonl.

    This file is never pruned — it reaches back to the first day you used Claude Code
    (24k lines here, back to January) — so it is the only EXACT long-span activity
    series on disk. It carries no tokens, so it cannot fill in cost or usage; it tells
    you which days were active and how hard, nothing more."""
    days = {}; projects = {}
    try:
        f = open(PROMPTS_FILE, errors="ignore")
    except OSError:
        return days, projects
    with f:
        for line in f:
            if '"timestamp"' not in line: continue
            try: o = json.loads(line)
            except Exception: continue
            ts = o.get("timestamp")
            if not ts: continue
            k = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            days[k] = days.get(k, 0) + 1
            pr = o.get("project") or ""
            if pr:
                n = os.path.basename(pr.rstrip("/")) or pr
                projects[n] = projects.get(n, 0) + 1
    return days, projects

STATS_FILE = os.path.expanduser("~/.claude/stats-cache.json")
def read_archive():
    try:
        d = json.load(open(STATS_FILE))
    except Exception:
        return {}, {}
    days = {}
    for e in d.get("dailyActivity", []):
        k = e.get("date")
        if k: days[k] = [e.get("messageCount", 0), e.get("sessionCount", 0),
                         e.get("toolCallCount", 0)]
    meta = {"sessions": d.get("totalSessions", 0), "messages": d.get("totalMessages", 0),
            "as_of": d.get("lastComputedDate", "?"), "hours": d.get("hourCounts", {})}
    return days, meta

# ── aggregate ──────────────────────────────────────────────────────────────
def aggregate(cache):
    cache = {"v": cache.get("v"), "files": _dedupe(cache)}
    days = {}      # date -> [in, out, cc, cr, responses, sessions, cost]
    hours = {}
    models = {}
    projects = {}  # name -> [tokens, cost, sessions]
    tot = [0, 0, 0, 0, 0]
    cost = 0.0
    first = last = None
    for p, r in cache["files"].items():
        cost += r.get("cost", 0.0)
        proj = projects.setdefault(r.get("proj", "?"), [0, 0.0, 0])
        proj[1] += r.get("cost", 0.0); proj[2] += 1
        for m, v in r.get("models", {}).items():
            md = models.setdefault(m, [0, 0, 0, 0])
            for j in range(4): md[j] += v[j]
            proj[0] += v[0] + v[1] + v[2] + v[3]
        for h, n in r.get("hours", {}).items():
            hours[h] = hours.get(h, 0) + n
        for k, v in r.get("days", {}).items():
            d = days.setdefault(k, [0, 0, 0, 0, 0, 0, 0.0])
            for j in range(5): d[j] += v[j]
            d[5] += 1                                    # this file = one session that day
            for j in range(5): tot[j] += v[j]
        f, l = r.get("first"), r.get("last")
        if f and (first is None or f < first): first = f
        if l and (last is None or l > last): last = l
    # cost per day, apportioned from each file's priced total by that day's token share
    for p, r in cache["files"].items():
        dd = r.get("days", {})
        tk = sum(sum(v[:4]) for v in dd.values())
        if not tk: continue
        c_ = r.get("cost", 0.0)
        for k, v in dd.items():
            days[k][6] += c_ * (sum(v[:4]) / tk)
    # Rolling windows, computed per FILE so "sessions" is a count of distinct transcripts
    # touched in the window — summing the per-day session counter instead would count a
    # session once per day it was active and make 30d ≈ all-time.
    today = date.today()
    win = {}
    for label, back in (("24h", 0), ("7d", 6), ("30d", 29)):
        cut = (today - timedelta(days=back)).isoformat()
        w = {"tok": 0, "wtok": 0, "ntok": 0, "cost": 0.0, "responses": 0, "sessions": 0, "days": 0}
        for p, r in cache["files"].items():
            dd = r.get("days", {})
            inw = [v for k, v in dd.items() if k >= cut]
            if not inw: continue
            tk_file = sum(sum(v[:4]) for v in dd.values())
            tk_win = sum(sum(v[:4]) for v in inw)
            w["sessions"] += 1
            w["tok"] += tk_win
            # Weighted like a limit window counts it (cache read 0.1x, cache write
            # 1.25x). Raw `tok` is ~97% cache_read — context replayed on every call —
            # so it climbs with TURNS, not with work, and reads as a nonsense number.
            w["wtok"] += sum(v[0] + v[1] + 1.25*v[2] + 0.1*v[3] for v in inw)
            w["ntok"] += sum(v[0] + v[1] + v[2] for v in inw)     # produced
            w["responses"] += sum(v[4] for v in inw)
            # the file's priced cost apportioned by the window's share of its tokens
            if tk_file: w["cost"] += r.get("cost", 0.0) * (tk_win / tk_file)
        w["days"] = len([k for k in days if k >= cut])
        win[label] = w

    arch, meta = read_archive()
    prompts, prompt_projects = read_prompts()
    # the pruned tail: days that happened but whose transcripts are gone. history.jsonl
    # covers the whole span exactly; stats-cache only fills days it predates.
    archive = {k: [0, arch.get(k, [0, 0, 0])[1], 0] for k in prompts if k not in days}
    for k, v in arch.items():
        if k not in days and k not in archive: archive[k] = v
    return {"days": days, "archive": archive, "stats": meta, "win": win,
            "prompts": prompts, "prompt_projects": prompt_projects,
            "hours": hours, "models": models, "projects": projects,
            "tot": {"in": tot[0], "out": tot[1], "cc": tot[2], "cr": tot[3],
                    "responses": tot[4]},
            "cost": cost, "sessions": len(cache["files"]),
            "first": first, "last": last}

# ── derived series ─────────────────────────────────────────────────────────
# "tokens" everywhere means tokens PRODUCED — input + output + cache write. Cache
# READS are the same conversation replayed on every request (~97% of the raw count);
# they climb with turns rather than with work, so they are not part of this figure.
def day_tokens(v): return v[0] + v[1] + v[2]

def series(agg, metric="tokens"):
    """date-string -> number, for the chosen metric.

    messages/sessions also cover the pruned tail (stats-cache); tokens/cost cannot —
    nothing outside the transcripts records them."""
    if metric == "prompts":                       # exact over the whole span
        return dict(agg.get("prompts") or {})
    pick = {"tokens":   day_tokens,
            "cost":     lambda v: v[6],
            "messages": lambda v: v[4],
            "sessions": lambda v: v[5]}[metric]
    s = {k: pick(v) for k, v in agg["days"].items()}
    if metric in ("messages", "sessions"):
        j = 0 if metric == "messages" else 1
        for k, v in agg.get("archive", {}).items(): s[k] = v[j]
    return s

def active_days(agg):
    return set(agg["days"]) | set(agg.get("archive", {})) | set(agg.get("prompts", {}))

def streaks(dates):
    """(current, longest) run of consecutive active days."""
    ds = sorted(date.fromisoformat(d) for d in dates)
    if not ds: return 0, 0
    longest = run = 1
    for a, b in zip(ds, ds[1:]):
        run = run + 1 if (b - a).days == 1 else 1
        if run > longest: longest = run
    today = date.today()
    cur = 0
    d = today if ds[-1] == today else today - timedelta(days=1)
    have = set(ds)
    while d in have:
        cur += 1; d -= timedelta(days=1)
    return cur, longest

def monthly(agg, metric="tokens"):
    s = series(agg, metric)
    out = {}
    for k, v in s.items():
        out[k[:7]] = out.get(k[:7], 0) + v
    return out

if __name__ == "__main__":
    t0 = time.time()
    a = build(progress=lambda n, tot, s: print(f"\r  {n}/{tot} files ({s} rescanned)",
                                               end="", flush=True))
    print(f"\r  {len(a['days'])} active days, ${a['cost']:.2f}, "
          f"{a['sessions']} sessions in {time.time()-t0:.1f}s")
