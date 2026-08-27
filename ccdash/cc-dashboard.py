#!/usr/bin/env python3
"""Claude Code info dashboard. Live TUI for iTerm.

Panels: header clock, session-window limit bars, FLEET (one row per live session),
and USAGE — one table: live (this window, exact from Claude Code) beside the rolling
7d/30d windows (cc_history.py; all-time and the calendar live in `cchist`).

No third-party deps. Run: python3 ~/.claude/cc-dashboard.py
Env:
  CC_SESSION_CAP   token cap for the session bar (default 200_000_000)
  CC_WINDOW_HOURS  rolling session window hours   (default 5)
  CC_REFRESH       redraw interval seconds         (default 2)
  CC_ITERM_WINDOW  iTerm window id (w<N>) to scope FLEET/USAGE to; default = the
                   window the dashboard runs in. Empty/non-iTerm → show all.
"""
import os, sys, glob, json, time, random, subprocess, threading, re
from datetime import datetime, timezone, date, timedelta

sys.path.insert(0, os.path.expanduser("~/.claude"))
import cc_history as HIST   # all-time history (transcripts, cached on disk)

PROJECTS = os.path.expanduser("~/.claude/projects")
# Session limit in WEIGHTED tokens (in+out + 0.1×cache_read + 1.25×cache_write) over the
# rolling window. Default calibrated so the bar ≈ matches Claude Code's /usage "current
# session" %. Claude's real limit is private — tune CC_SESSION_LIMIT to your plan if it drifts.
SESSION_LIMIT = int(os.environ.get("CC_SESSION_LIMIT", 90_000_000))
WINDOW_HOURS = float(os.environ.get("CC_WINDOW_HOURS", 5))
GRAPH_H = int(os.environ.get("CC_GRAPH_H", 4))   # rows in the USAGE daily-token chart
GRAPH_W = int(os.environ.get("CC_GRAPH_W", 76))  # hard column cap for that chart
GRAPH_FRAC = float(os.environ.get("CC_GRAPH_FRAC", 0.85))  # …and this share of the panel
REFRESH = float(os.environ.get("CC_REFRESH", 0.25))  # clawd's animation frame rate; also
                                                     # keeps the working timer from skipping a second

# Per-1M-token pricing (input, output). Cache read ≈ 0.1×input, cache write ≈ 1.25×input.
# Source: claude-api skill cached pricing table — refresh here if rates change.
PRICING = {
    "claude-opus-4-8": (5, 25), "claude-opus-4-7": (5, 25), "claude-opus-4-6": (5, 25),
    "claude-opus-4-5": (5, 25), "claude-sonnet-4-6": (3, 15), "claude-haiku-4-5": (1, 5),
    "claude-fable-5": (10, 50),
}
DEFAULT_PRICE = (5, 25)   # unknown model → assume opus-tier

# Exact live session data — Claude Code feeds this JSON to the statusline every render;
# statusline.sh dumps the latest to this path. Carries the true 5h/7d rate-limit %,
# reset times, session cost, and context-window fill — no estimation needed.
LIVE_FILE = os.environ.get("CC_STATUS_JSON", "/tmp/claude-statusline-debug.json")
def read_live():
    # The 5h/7d limit is account-global, but each session only refreshes its copy on its
    # own API calls. Pick the dump from the NEWEST window (max resets_at) so a stale
    # pre-reset reading can't pin the bar after the window rolls over; within the same
    # window take the highest pct (a lagging idle pane reads low until its next call).
    best=None; best_key=(-1,-1)
    for p in glob.glob(os.path.join(FLEET_DIR,"*.json")):
        try:
            if time.time()-os.path.getmtime(p)>FLEET_STALE: continue
            d=json.load(open(p))
        except Exception: continue
        fh=(d.get("rate_limits") or {}).get("five_hour")
        if isinstance(fh,dict):
            key=(fh.get("resets_at",0), fh.get("used_percentage",-1))
            if key>best_key: best_key=key; best=d
    if best is not None: return best
    try:
        return json.load(open(LIVE_FILE))
    except Exception:
        return None

# Limit burn-rate. Derived from the WINDOW ITSELF — a limit window runs from
# resets_at-span to resets_at, so elapsed time and the percentage already spent give
# the average pace directly. That makes it exact on the first frame, unaffected by a
# dashboard restart, and identical in every pane — where the old sampled-delta version
# needed two minutes of warm-up and reset to "—" whenever the process did.
def pace(pct, resets_at, span_h, started=None, now=None):
    """(active_secs, %/hour, hours_to_cap|None, hours_to_reset)

    Rate is measured from when WORK started inside this window, not from the window's
    own start. A limit window is a fixed wall-clock box: sit down two hours into it and
    dividing by the full elapsed box halves your apparent rate and pushes the projected
    cap hours too late. `started` is the first request actually made inside the window."""
    if now is None: now=time.time()
    win_start=resets_at-span_h*3600
    base=max(win_start, started) if started else win_start
    elapsed=max(60.0, now-base)
    rate=pct/(elapsed/3600)                       # %/hour over ACTIVE time
    left=max(0.0,(resets_at-now)/3600)
    return elapsed, rate, (((100-pct)/rate) if rate>0 else None), left

_start_cache={}
def window_started(events, win_start):
    """Timestamp of the first request inside the window (None if there is none).

    Cached per (event-count, window) — the event list is rebuilt only on a scan, and a
    min() over every request ever made is far too costly to run at frame rate."""
    key=(len(events), round(win_start))
    if key in _start_cache: return _start_cache[key]
    first=None
    for t,_ in events:
        if t>=win_start and (first is None or t<first): first=t
    if len(_start_cache)>64: _start_cache.clear()
    _start_cache[key]=first
    return first

_days_cache={}
def window_days(events, win_start, now=None):
    """How many distinct DAYS have seen work since the window opened (>=1).

    A week-long limit has to be paced per day, not per active hour: 4% burned in the
    45 minutes since the window rolled is not "80%/day", it is one day's work so far.
    Counting active days is what keeps the projection stable across a long window."""
    if now is None: now=time.time()
    key=(len(events), round(win_start), int(now//3600))
    if key in _days_cache: return _days_cache[key]
    days={datetime.fromtimestamp(t).strftime("%Y-%m-%d") for t,_ in events if t>=win_start}
    n=max(1,len(days))
    if len(_days_cache)>64: _days_cache.clear()
    _days_cache[key]=n
    return n

# Background agents — `claude agents --json` lists every session; the non-interactive
# (dispatched/background) ones don't own an iTerm pane, so they never appear via the
# /tmp/cc-status dumps. Poll them off-thread and fold them into the fleet.
_agents=[]
def agents_loop():
    global _agents
    while True:
        try:
            r=subprocess.run(["claude","agents","--json"],capture_output=True,text=True,timeout=15)
            _agents=json.loads(r.stdout or "[]")
        except Exception:
            pass
        time.sleep(15)

# Fleet — one JSON per live session, written by statusline.sh into this dir.
FLEET_DIR = os.environ.get("CC_FLEET_DIR", "/tmp/cc-status")
FLEET_STALE = 90    # drop Claude panes whose statusline hasn't rendered in 90s (closed).
GENERIC_FLEET_STALE = 4 * 3600  # fallback retention for generic records without SessionEnd
GENERIC_ENDED_STALE = 15 * 60   # retain an ended row long enough to review its work
GENERIC_EXIT_GRACE = 5          # allow hook writes/process handoff before orphan cleanup
                    # open-but-idle panes keep re-rendering (~1s), so they stay; closed go.
# Scope FLEET/USAGE to ONE iTerm window so a pane only reports its window's sessions
# (~6), not every Claude Code session on the machine. statusline.sh tags each dump
# with iterm_window (w<N>). Default to the window THIS dashboard runs in; cc-dash.sh
# passes CC_ITERM_WINDOW so a dashboard opened in a fresh window still scopes to the
# window it was launched from. Empty (not under iTerm) → no scoping, show all.
_ITERM_SELF = os.environ.get("ITERM_SESSION_ID", "")
FLEET_WINDOW = os.environ.get("CC_ITERM_WINDOW") or (_ITERM_SELF.split("t", 1)[0] if _ITERM_SELF else "")
_NOISE = ("system-reminder","task-notification","<environment_context","<channel","<command-","<local-command")
def tail_lines(path, n, size=524288):
    # Last n lines without reading the whole file — transcripts grow to tens of MB and
    # these scans run per fleet row per ~0.9s frame. A line bigger than `size` just
    # truncates the window further back; the partial first line fails json.loads and
    # is skipped by every caller.
    try:
        with open(path,"rb") as f:
            f.seek(0,2); sz=f.tell()
            f.seek(max(0,sz-size))
            data=f.read()
        return data.decode("utf-8","ignore").splitlines()[-n:]
    except OSError:
        return []

def last_user_msg(path):
    lines=tail_lines(path,200)
    for line in reversed(lines):
        try: o=json.loads(line)
        except Exception: continue
        if o.get("type")!="user" or o.get("isMeta"): continue
        ct=(o.get("message") or {}).get("content")
        if isinstance(ct,str): txt=ct
        elif isinstance(ct,list):
            txt=" ".join(b.get("text","") for b in ct if isinstance(b,dict) and b.get("type")=="text")
        else: continue
        txt=" ".join(txt.split())
        if txt and not any(s in txt for s in _NOISE):
            return txt
    return ""

def prompt_tokens(path):
    # Tokens for the latest individual prompt: sum input+output+cache_creation across the
    # current turn's assistant messages (back to the last real user prompt), deduped by
    # message.id. Excludes cache_read — that's the replayed context, not this prompt's work.
    lines=tail_lines(path,500)
    seen=set(); tot=0
    for line in reversed(lines):
        if '"usage"' not in line and '"type":"user"' not in line: continue
        try: o=json.loads(line)
        except Exception: continue
        t=o.get("type")
        if t=="user" and not o.get("isMeta"):
            ct=(o.get("message") or {}).get("content")
            # a text user message is a new-prompt boundary; tool_result user lines are part
            # of the same turn's tool loop, so don't stop on them
            if isinstance(ct,str) or (isinstance(ct,list) and any(
                    isinstance(b,dict) and b.get("type")=="text" for b in ct)):
                break
            continue
        if t!="assistant": continue
        m=o.get("message") or {}; u=m.get("usage")
        if not isinstance(u,dict): continue
        mid=m.get("id")
        if mid in seen: continue
        seen.add(mid)
        tot+=(u.get("input_tokens",0) or 0)+(u.get("output_tokens",0) or 0)+(u.get("cache_creation_input_tokens",0) or 0)
    return tot

AGENT_FRESH=25   # agent transcript appended within this many seconds → running now
AGENT_QUIET=900  # unfinished agent transcript quiet this long → presume it died
WF_STALE=300     # workflow journal quiet this long → presume the run died (crash backstop)

_done_cache={}   # agent transcript path -> ((mtime,size), finished?)
def _agent_finished(p):
    """True once the agent has delivered its final answer.

    An agent's every intermediate assistant message ends in a tool_use, so the ONE
    line with stop_reason "end_turn" is its return value — a far better done-signal
    than mtime, which can't tell a thinking agent from a finished one."""
    try: st=os.stat(p)
    except OSError: return True
    key=(st.st_mtime, st.st_size)
    ent=_done_cache.get(p)
    if ent and ent[0]==key: return ent[1]
    fin=False
    for line in reversed(tail_lines(p,40)):
        if '"assistant"' not in line: continue
        try: o=json.loads(line)
        except Exception: continue
        if o.get("type")!="assistant": continue
        fin=((o.get("message") or {}).get("stop_reason")=="end_turn"); break
    _done_cache[p]=(key,fin)
    return fin
def _agent_meta(p):
    # meta.json sits beside each agent transcript. Agent-tool metas carry a
    # description + toolUseId; workflow metas are just {"agentType":"workflow-subagent"}.
    try: m=json.load(open(p[:-6]+".meta.json"))
    except Exception: return None,"agent"
    lb=m.get("description") or m.get("agentType") or "agent"
    return m.get("toolUseId"), ("wf" if lb=="workflow-subagent" else lb[:24])

_pend_cache={}   # transcript path -> ((mtime,size), tasks, done)
def _pending(path):
    # Agent/Task tool_use blocks in the transcript tail with no tool_result yet.
    # Cached by (mtime,size) so idle sessions skip the read entirely.
    try: st=os.stat(path)
    except OSError: return {},set()
    key=(st.st_mtime, st.st_size)
    ent=_pend_cache.get(path)
    if ent and ent[0]==key: return ent[1],ent[2]
    tasks={}; done=set()
    for line in tail_lines(path,400):
        if '"tool_use"' not in line and '"tool_result"' not in line: continue
        try: o=json.loads(line)
        except Exception: continue
        ct=(o.get("message") or {}).get("content")
        if not isinstance(ct,list): continue
        for b in ct:
            if not isinstance(b,dict): continue
            if b.get("type")=="tool_use" and b.get("name") in ("Agent","Task"):
                inp=b.get("input") or {}
                tasks[b.get("id")]=(inp.get("subagent_type") or inp.get("description") or "agent")[:24]
            elif b.get("type")=="tool_result":
                done.add(b.get("tool_use_id"))
    _pend_cache[path]=(key,tasks,done)
    return tasks,done

def _wf_running(wd, now):
    # journal.jsonl is the authoritative running set: agentIds with a "started" line
    # and no "result" line. mtime freshness can't do this job — an agent inside a long
    # Bash/WebFetch call stops appending its transcript, and a finished one keeps a
    # fresh mtime for a while. Journal quiet > WF_STALE = the run likely died; then
    # only trust agents whose transcript is still being written.
    j=os.path.join(wd,"journal.jsonl")
    try: jmt=os.path.getmtime(j)
    except OSError: return []
    started=[]; done=set()
    for line in tail_lines(j,2000):
        try: o=json.loads(line)
        except Exception: continue
        if o.get("type")=="started": started.append(o.get("agentId"))
        elif o.get("type")=="result": done.add(o.get("agentId"))
    run=[a for a in started if a and a not in done]
    if run and now-jmt>WF_STALE:
        def fresh(a):
            try: return now-os.path.getmtime(os.path.join(wd,"agent-%s.jsonl"%a))<=AGENT_FRESH
            except OSError: return False
        run=[a for a in run if fresh(a)]
    return run

def _wf_name(base, wfid):
    # The launcher persists each run's script as <session-dir>/workflows/scripts/
    # <meta.name>-<wfid>.js — the only on-disk source for a running workflow's name
    # (its tool_result arrives immediately for background runs, so the transcript
    # can't tell us; workflow agent metas carry no name either).
    for s in glob.glob(os.path.join(base,"workflows","scripts","*-"+wfid+".js")):
        return os.path.basename(s)[:-(len(wfid)+4)].rstrip("-")[:24]
    return "workflow"

def subagents(path, now=None):
    # Every subagent running right now for a session.
    #  Agent-tool agents: pending tool_use in the transcript tail (survives long silent
    #  tool calls) ∪ fresh agent-*.jsonl under <session-dir>/subagents/, deduped by
    #  meta.toolUseId (done ids drop just-finished agents immediately).
    #  Workflow/ultracode agents: started-minus-result from each wf dir's journal —
    #  they never appear as tool_use blocks and their metas have no toolUseId. Each
    #  active run also contributes one "⚙ <name>" entry.
    if now is None: now=time.time()
    tasks,done=_pending(path)
    out=[v for k,v in tasks.items() if k not in done]
    base=path[:-6] if path.endswith(".jsonl") else ""
    if not base: return out
    for p in glob.glob(os.path.join(base,"subagents","agent-*.jsonl")):
        try: mt=os.path.getmtime(p)
        except OSError: continue
        # A BACKGROUND agent (the Agent tool's default, and how Plan/Explore usually
        # run) gets its tool_result the instant it is dispatched, so `done` holds its
        # id for its entire life — suppressing on `done` hid every background agent.
        # The transcript itself is the authority: unfinished + still being written.
        if _agent_finished(p): continue           # returned → drop it the same frame
        if now-mt>AGENT_QUIET: continue           # crashed/killed backstop
        tid,lb=_agent_meta(p)
        # dedupe against the PENDING ids only (tasks still holds ids whose result has
        # already landed — for a background agent that is every id, from dispatch on)
        if tid and tid in tasks and tid not in done: continue
        out.append(lb)
    for wd in glob.glob(os.path.join(base,"subagents","workflows","wf_*")):
        run=_wf_running(wd,now)
        if not run: continue
        out.append("⚙ "+_wf_name(base,os.path.basename(wd)))
        out+=["wf"]*len(run)
    return out

FLEET_FRESH=10   # an assistant line written within this many seconds → generating now
def gen_fresh(path, now):
    # True only if the newest *assistant* line is recent — ignores housekeeping appends
    # (file-history-snapshot / queue-operation / mode) that touch the transcript when idle.
    for line in reversed(tail_lines(path,25)):
        if '"timestamp"' not in line or '"assistant"' not in line: continue
        try: o=json.loads(line)
        except Exception: continue
        if o.get("type")!="assistant": continue
        ts=o.get("timestamp")
        if not ts: return False
        try: ep=datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
        except Exception: return False
        return (now-ep)<FLEET_FRESH
    return False

# "working" = the session's output tokens or API time grew since the last poll — the only
# signals that move solely while the agent is generating (idle panes show zero delta even
# though their statusline keeps re-rendering). Held briefly so gaps between requests /
# tool calls within a turn don't flip the dot. Tune the hold with CC_FLEET_WORKING.
FLEET_WORKING=float(os.environ.get("CC_FLEET_WORKING", 15))
_work_state={}   # sid -> (last_output_tokens, last_api_ms, last_active_ts)
_fleet_last={}   # path -> last successfully-parsed dump (survives mid-write reads)
_io_max={}; _cost_max={}   # sid -> high-water mark; smooths colliding writers / partial reads
_generic_proc_cache=(0.0,set(),False)
def generic_agent_ttys():
    """TTYs that currently own an interactive Codex or Grok process.

    Generic clients only write state at lifecycle events, unlike Claude's periodic
    statusline.  Verifying their pty prevents a crashed/closed session from lingering
    until its conservative stale timeout.
    """
    global _generic_proc_cache
    ts,ttys,checked=_generic_proc_cache
    if time.time()-ts<2: return ttys,checked
    found=set()
    checked=False
    try:
        p=subprocess.run(["ps","-axo","tty=,command="],capture_output=True,
                         text=True,timeout=2)
        if p.returncode!=0: raise OSError("ps failed")
        checked=True; out=p.stdout
        for line in out.splitlines():
            tty,_,cmd=line.strip().partition(" ")
            lc=cmd.lower()
            if tty.startswith("ttys") and ("codex" in lc or "grok" in lc):
                found.add(tty)
    except Exception:
        pass
    _generic_proc_cache=(time.time(),found,checked)
    return found,checked
def _read_fleet():
    now=time.time()
    # Collapse to one dump per iTerm pane (keep newest): /clear and session resume create
    # fresh session_id files for the same pane — without this they'd each show as a row and
    # double-count in USAGE. Falls back to session_id when a dump predates pane tagging.
    best={}   # pane key -> (mtime, path, dump)
    for p in glob.glob(os.path.join(FLEET_DIR,"*.json")):
        try:
            mt=os.path.getmtime(p)
            d=json.load(open(p)); _fleet_last[p]=d
        except Exception:
            d=_fleet_last.get(p)        # mid-write/partial read → reuse last good
            if d is None: continue
            try: mt=os.path.getmtime(p)
            except OSError: continue
        sp0=os.path.join(FLEET_DIR,(d.get("fleet_key") or d.get("session_id") or "")+".state")
        try:
            state0=open(sp0).read().strip(); state_mt=os.path.getmtime(sp0)
        except OSError:
            state0=""; state_mt=mt
        stale=(GENERIC_ENDED_STALE if state0=="ended" else GENERIC_FLEET_STALE) if d.get("provider") in ("codex","grok") else FLEET_STALE
        if now-max(mt,state_mt)>stale:
            if d.get("provider") in ("codex","grok") and state0=="ended":
                key=d.get("fleet_key") or d.get("session_id") or ""
                for suffix in (".json",".state",".prompt"):
                    try: os.unlink(os.path.join(FLEET_DIR,key+suffix))
                    except OSError: pass
            continue
        # A one-shot client can exit without dispatching SessionEnd.  Its saved hook
        # record includes the owning TTY, so remove it only after that *same* Codex/Grok
        # process is gone.  An interactive session waiting for its next prompt stays.
        if d.get("provider") in ("codex","grok"):
            tty=d.get("tty") or ""
            ttys,checked=generic_agent_ttys()
            if state0!="ended" and checked and tty and now-mt>GENERIC_EXIT_GRACE and tty not in ttys:
                key=d.get("fleet_key") or d.get("session_id") or ""
                for suffix in (".json",".state",".prompt"):
                    try: os.unlink(os.path.join(FLEET_DIR,key+suffix))
                    except OSError: pass
                continue
        # Codex/Grok hook children can lose ITERM_SESSION_ID.  Keep those established
        # sessions visible in every dashboard instead of treating a blank ID as closed.
        if FLEET_WINDOW and (d.get("iterm_window") or "")!=FLEET_WINDOW and d.get("provider") not in ("codex","grok"):
            continue
        pane=d.get("iterm_pane") or os.path.basename(p)
        if pane not in best or mt>best[pane][0]: best[pane]=(mt,p,d)
    rows=[]
    for pane,(mt,p,d) in best.items():
        cwd=(d.get("workspace") or {}).get("current_dir") or d.get("cwd") or "?"
        # name = current project dir (live, updates when a pane switches project);
        # session_name is CC's early AI title and goes stale on a project change.
        name=os.path.basename(cwd.rstrip("/")) or "?"
        sid=os.path.basename(p)
        cw=d.get("context_window") or {}
        provider=d.get("provider") or "claude"
        # working dot = the SAME signal that colors the iTerm tab: the UserPromptSubmit
        # (working) / Stop (idle) hooks write <session_id>.state via cc-active.sh.
        realsid=d.get("session_id") or sid.rsplit(".json",1)[0]
        fleet_key=d.get("fleet_key") or realsid
        sp=os.path.join(FLEET_DIR,fleet_key+".state")
        try:
            working=open(sp).read().strip()=="working"
            smt=os.path.getmtime(sp); since=now-smt   # how long in this state
        except OSError:
            working=False; since=None; smt=None
        pp=os.path.join(FLEET_DIR,fleet_key+".prompt")
        try: pmt=os.path.getmtime(pp)
        except OSError: pmt=None
        # Codex/Grok only touch their record at lifecycle events, so the last prompt (or
        # the record itself) is their only activity clock.  Claude's statusline rewrites
        # its dump on EVERY render — timing from it would reset the age to zero forever
        # AND reshuffle the sort on every frame.  Claude keeps its original clock: the
        # state file, which moves only on a real working/idle transition.
        if provider in ("codex","grok"):
            active_at=pmt if pmt is not None else mt
            since=now-active_at
        else:
            active_at=smt if smt is not None else mt
        io=(cw.get("total_input_tokens",0) or 0)+(cw.get("total_output_tokens",0) or 0)
        # tokens for the latest individual prompt (new tokens only, excl. cache replay)
        tp=d.get("transcript_path") or session_ops_path(provider,realsid)
        cur=prompt_tokens(tp)
        subs=subagents(tp, now)   # Agent-tool + workflow subagents currently running
        cost=(d.get("cost") or {}).get("total_cost_usd",0) or 0
        # high-water per session smooths colliding writers / partial reads (two panes
        # sharing a session_id overwrite one file with close values). But a REAL reset —
        # context compaction, /clear, session restart — drops the live count, so let a
        # large drop (below half the mark) clear the mark instead of pinning it forever.
        pj=_io_max.get(sid,0)
        if io<pj*0.5: pj=0
        io=_io_max[sid]=max(pj,io)
        pc=_cost_max.get(sid,0)
        if cost<pc*0.5: pc=0
        cost=_cost_max[sid]=max(pc,cost)
        rows.append({"pane":pane, "sid":sid, "realsid":realsid, "name":name, "cwd":cwd,
                     "provider":provider,
                     "active_at":active_at, "working":working, "since":since, "cur":cur,
                     "io":io, "cost":cost, "bg":False, "subs":subs, "tp":tp,
                     "op_provider":provider})
    # fold in background agents (dispatched sessions with no pane). Tie each to a visible
    # chat by matching cwd when window-scoped; show all when unscoped.
    known={r["realsid"] for r in rows}; cwds={r["cwd"] for r in rows}
    for a in (_agents or []):
        if a.get("kind")=="interactive": continue
        asid=a.get("sessionId"); acwd=a.get("cwd") or "?"
        if not asid or asid in known: continue
        if FLEET_WINDOW and acwd not in cwds: continue
        rows.append({"pane":"~bg:"+asid, "sid":asid, "realsid":asid,
                     "name":a.get("name") or os.path.basename(acwd.rstrip("/")) or asid[:8],
                     "cwd":acwd, "active_at":(a.get("startedAt",0) or 0)/1000 or now,
                     "working":a.get("status")=="busy", "since":None, "cur":0,
                     "io":0, "cost":0, "bg":True, "provider":"claude",
                     "op_provider":"claude", "tp":""})
    # working (running) above idle; within each group, most recently chatted first
    # sid tiebreak keeps equal-timestamp rows from swapping places frame to frame
    rows.sort(key=lambda r:(r["working"], r["active_at"], r["sid"]), reverse=True)
    return rows

# Clawd animates at a few frames a second, but the fleet scan behind it is not cheap
# (a tail-read of every transcript, per row, for prompt tokens + running subagents).
# Cache it: a second of staleness is invisible on the dots and the working timer, and
# it decouples frame rate from scan cost.
# Fleet state is also the iTerm tab-color source for Codex.  Keep its cache below one
# render frame so the dashboard never visibly trails a hook-driven tab transition.
FLEET_TTL=float(os.environ.get("CC_FLEET_TTL", 0.1))
_fleet_cache=(0.0,None)
# Native fallback is only for sessions whose hook record never arrived.  Keep it short
# so a completed unhooked CLI cannot linger in the dashboard.
NATIVE_ACTIVE_SECS=12
CODEX_SESSIONS=os.path.expanduser("~/.codex/sessions")
GROK_SESSIONS=os.path.expanduser("~/.grok/sessions")
_session_paths={}
def session_ops_path(provider, sid):
    """Return the native journal which owns prompt/edit accounting for a session."""
    key=(provider,sid)
    cached=_session_paths.get(key)
    if cached and os.path.exists(cached): return cached
    if provider=="codex":
        hits=glob.glob(os.path.join(CODEX_SESSIONS,"*","*","*",f"*-{sid}.jsonl"))
        path=hits[-1] if hits else ""
    elif provider=="grok":
        hits=glob.glob(os.path.join(GROK_SESSIONS,"*",sid,"chat_history.jsonl"))
        path=hits[-1] if hits else ""
    else:
        path=""
    if path: _session_paths[key]=path
    return path
def native_active_rows(now, known):
    """Fall back to the clients' own append-only session journals.

    This handles clients launched before a hook configuration reload and clients whose
    hook subprocess is detached from iTerm.  Only actively-written turns are included;
    a finished Grok journal is never mistaken for a live session.
    """
    rows=[]
    for path in glob.glob(os.path.join(CODEX_SESSIONS,"*","*","*","*.jsonl")):
        try:
            mt=os.path.getmtime(path)
            if now-mt>NATIVE_ACTIVE_SECS: continue
        except OSError: continue
        sid=os.path.basename(path).rsplit("-",1)[-1].removesuffix(".jsonl")
        # Rollout names end in the full thread id, but the final dash split only gives
        # the last segment.  session_meta is the authoritative id and cwd.
        cwd="?"; realid=""
        try:
            with open(path,errors="ignore") as f: o=json.loads(f.readline())
            p=o.get("payload") or {}; realid=p.get("session_id") or p.get("id") or ""
            cwd=p.get("cwd") or cwd
        except Exception:
            continue
        if not realid or realid in known: continue
        known.add(realid)
        pst=scan_codex_ops(path)
        prompt_at=pst.get("last_prompt_at") or mt
        rows.append({"pane":"~codex:"+realid, "sid":"codex-"+realid,
                     "realsid":realid, "provider":"codex",
                     "name":os.path.basename(cwd.rstrip("/")) or "?", "cwd":cwd,
                     "active_at":prompt_at, "working":True, "since":now-prompt_at, "cur":0,
                     "io":0, "cost":0, "bg":False, "subs":[], "tp":path,
                     "op_provider":"codex"})
    for path in glob.glob(os.path.join(GROK_SESSIONS,"*","*","events.jsonl")):
        try:
            mt=os.path.getmtime(path)
            if now-mt>NATIVE_ACTIVE_SECS: continue
        except OSError: continue
        last=None
        for line in reversed(tail_lines(path,20)):
            try: last=json.loads(line); break
            except Exception: continue
        # A completed/cancelled Grok turn is deliberately absent, even while its file
        # remains recent.  Only a genuine streaming phase is a live agent.
        if not isinstance(last,dict) or last.get("type")!="phase_changed": continue
        if not str(last.get("phase","")).startswith("streaming"): continue
        base=os.path.dirname(path); realid=os.path.basename(base)
        if realid in known: continue
        known.add(realid)
        cwd=os.path.basename(os.path.dirname(base)).replace("%2F","/") or "?"
        rows.append({"pane":"~grok:"+realid, "sid":"grok-"+realid,
                     "realsid":realid, "provider":"grok", "name":cwd,
                     "cwd":cwd, "active_at":mt, "working":True, "since":now-mt,
                     "cur":0, "io":0, "cost":0, "bg":False, "subs":[],
                     "tp":os.path.join(base,"chat_history.jsonl"), "op_provider":"grok"})
    return rows
def read_fleet():
    global _fleet_cache
    ts,rows=_fleet_cache
    if rows is not None and time.time()-ts<FLEET_TTL: return rows
    rows=_read_fleet()
    rows+=native_active_rows(time.time(),{r["realsid"] for r in rows})
    # sid tiebreak keeps equal-timestamp rows from swapping places frame to frame
    rows.sort(key=lambda r:(r["working"], r["active_at"], r["sid"]), reverse=True)
    _fleet_cache=(time.time(),rows)
    return rows

# ── tool-use ops per session ───────────────────────────────────────────────
# Every tool call in a transcript, counted by name, plus the files the editing tools
# touched and the lines they added/removed.
#
# The scan is INCREMENTAL and this is not optional: transcripts reach tens of MB, and
# re-reading them on a 0.25s frame would eat the machine. Each file's byte offset is
# remembered and only bytes appended since the last pass are parsed. A file that
# SHRANK (compaction, /clear, rotation) is rescanned from zero.
EDIT_TOOLS={"Write","Edit","MultiEdit","NotebookEdit"}
def _nlines(s): return len(s.splitlines()) if isinstance(s,str) and s else 0
def _count_edit(name,inp,st):
    fp=inp.get("file_path") or inp.get("notebook_path")
    if fp: st["files"].add(fp)
    if name=="Write":
        st["add"]+=_nlines(inp.get("content"))
    elif name=="Edit":
        # diff semantics: the old lines go, the new lines arrive. replace_all can hit
        # several sites but the transcript doesn't record how many, so this counts one.
        st["add"]+=_nlines(inp.get("new_string")); st["del"]+=_nlines(inp.get("old_string"))
    elif name=="MultiEdit":
        for e in (inp.get("edits") or []):
            if isinstance(e,dict):
                st["add"]+=_nlines(e.get("new_string")); st["del"]+=_nlines(e.get("old_string"))
    elif name=="NotebookEdit":
        st["add"]+=_nlines(inp.get("new_source"))

_ops={}   # transcript path -> {"off","counts","files","add","del","seen"}
def scan_ops(path):
    st=_ops.get(path)
    try: size=os.path.getsize(path)
    except OSError: return st
    if st is None or size<st["off"]:
        st={"off":0,"counts":{},"files":set(),"add":0,"del":0,"seen":set(),"prompts":0,"last_prompt_at":0}
    if size>st["off"]:
        try:
            with open(path,"rb") as f:
                f.seek(st["off"]); data=f.read()
        except OSError:
            data=b""
        # consume only up to the last COMPLETE line; a half-written tail would fail to
        # parse and its tool calls would then be lost for good once the offset moved past
        cut=data.rfind(b"\n")+1
        st["off"]+=cut
        for line in data[:cut].decode("utf-8","ignore").splitlines():
            if '"tool_use"' not in line and '"type":"user"' not in line: continue
            try: o=json.loads(line)
            except Exception: continue
            # A real PROMPT is a user message carrying text. Tool results are also
            # type=user but hold tool_result blocks, and the harness injects meta lines
            # (system-reminders, command output) — neither is something you typed.
            if o.get("type")=="user" and not o.get("isMeta"):
                ct=(o.get("message") or {}).get("content")
                txt=(ct if isinstance(ct,str) else
                     " ".join(b.get("text","") for b in ct
                              if isinstance(b,dict) and b.get("type")=="text")
                     if isinstance(ct,list) else "")
                if txt.strip() and not any(s in txt for s in _NOISE):
                    uid=o.get("uuid")
                    if uid is None or uid not in st["seen"]:
                        if uid is not None: st["seen"].add(uid)
                        st["prompts"]=st.get("prompts",0)+1
            m=o.get("message")
            if not isinstance(m,dict): continue
            for b in (m.get("content") or []):
                if not (isinstance(b,dict) and b.get("type")=="tool_use"): continue
                tid=b.get("id")
                if tid is not None:            # a retried/rewritten message repeats its
                    if tid in st["seen"]: continue   # blocks — count each call once
                    st["seen"].add(tid)
                n=b.get("name") or "?"
                st["counts"][n]=st["counts"].get(n,0)+1
                if n in EDIT_TOOLS: _count_edit(n,b.get("input") or {},st)
    _ops[path]=st
    return st

def _native_state(path):
    st=_ops.get(path)
    if st is None:
        st={"off":0,"counts":{},"files":set(),"add":0,"del":0,"seen":set(),"prompts":0}
    return st

def scan_codex_ops(path):
    """Count actual Codex user messages and patches from its rollout journal."""
    st=_native_state(path)
    try: size=os.path.getsize(path)
    except OSError: return st
    if size<st.get("off",0):
        st={"off":0,"counts":{},"files":set(),"add":0,"del":0,"seen":set(),"prompts":0}
    if size==st.get("off",0): return st
    try:
        with open(path,"rb") as f:
            f.seek(st["off"]); data=f.read()
    except OSError: return st
    cut=data.rfind(b"\n")+1
    st["off"]+=cut
    for line in data[:cut].decode("utf-8","ignore").splitlines():
            try: o=json.loads(line); p=o.get("payload") or {}
            except Exception: continue
            typ=p.get("type")
            if typ=="message" and p.get("role")=="user":
                content=p.get("content") or []
                text=" ".join(x.get("text","") for x in content if isinstance(x,dict) and x.get("type")=="input_text")
                if text.strip() and not any(s in text for s in _NOISE):
                    st["prompts"]+=1
                    try: st["last_prompt_at"]=datetime.fromisoformat(o.get("timestamp","").replace("Z","+00:00")).timestamp()
                    except Exception: pass
                continue
            if typ!="custom_tool_call": continue
            name=p.get("name") or "?"; st["counts"][name]=st["counts"].get(name,0)+1
            raw=p.get("input") or ""
            if not isinstance(raw,str) or "*** Begin Patch" not in raw: continue
            # Wrapper tools preserve a patch as a quoted source string, so its line
            # breaks arrive as literal \\n escapes rather than physical journal newlines.
            raw=raw.replace("\\n","\n")
            for m in re.finditer(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$",raw,re.M): st["files"].add(m.group(1).strip())
            for change in raw.splitlines():
                if change.startswith("+++") or change.startswith("---"): continue
                if change.startswith("+"): st["add"]+=1
                elif change.startswith("-"): st["del"]+=1
    _ops[path]=st
    return st

def scan_grok_ops(path):
    """Grok's chat log has prompts; hunk_records has authoritative file/line deltas."""
    st=_native_state(path)
    hunk=os.path.join(os.path.dirname(path),"hunk_records.jsonl")
    try: stamp=(os.path.getmtime(path),os.path.getsize(path),
                os.path.getmtime(hunk) if os.path.exists(hunk) else 0)
    except OSError: return st
    if st.get("stamp")==stamp: return st
    st={"off":0,"counts":{},"files":set(),"add":0,"del":0,"seen":set(),"prompts":0,"stamp":stamp}
    try:
        with open(path,errors="ignore") as f:
            for line in f:
                try: o=json.loads(line)
                except Exception: continue
                if o.get("type")!="user": continue
                ct=o.get("content") or []
                text=" ".join(x.get("text","") for x in ct if isinstance(x,dict) and x.get("type")=="text")
                if text.strip() and "<system-reminder>" not in text: st["prompts"]+=1
    except OSError: return st
    try:
        with open(hunk,errors="ignore") as f:
            for line in f:
                try: o=json.loads(line)
                except Exception: continue
                # Normal agent hunks are marked `agent`.  Grok's compensating
                # `removed` records omit authorType, but retain agentId; include
                # those while never attributing a human edit to the chat.
                author=o.get("authorType")
                if author=="human" or (author!="agent" and not o.get("agentId")):
                    continue
                if o.get("eventType") not in ("added","updated","removed"):
                    continue
                fp=o.get("filePath")
                if fp: st["files"].add(fp)
                # Grok writes each hunk refinement as a signed delta.  A negative
                # added count is a deletion of earlier inserted lines (and vice versa
                # for removed), so preserve it as a changed line instead of dropping it.
                try:
                    added=int(o.get("linesAdded") or 0)
                    removed=int(o.get("linesRemoved") or 0)
                except (TypeError,ValueError):
                    continue
                if added>=0: st["add"]+=added
                else: st["del"]-=added
                if removed>=0: st["del"]+=removed
                else: st["add"]-=removed
    except OSError: pass
    _ops[path]=st
    return st

def scan_session_ops(provider,path):
    if provider=="codex": return scan_codex_ops(path)
    if provider=="grok": return scan_grok_ops(path)
    return scan_ops(path)

def ops_loop():
    while True:
        try:
            for r in read_fleet():
                if r.get("tp"): scan_session_ops(r.get("op_provider") or r.get("provider"),r["tp"])
        except Exception:
            pass
        time.sleep(2)

# Display names. MultiEdit folds into "edit" and the two Web* tools into "web" — the
# distinction is noise at a glance, and merging keeps the row short on a narrow window.
OPS_SHORT={"Bash":"bash","Edit":"edit","MultiEdit":"edit","Write":"write","Read":"read",
           "Glob":"glob","Grep":"grep","Task":"task","Agent":"agent","TodoWrite":"todo",
           "WebFetch":"web","WebSearch":"web","NotebookEdit":"nb","AskUserQuestion":"ask",
           "ToolSearch":"tool","Workflow":"flow","Skill":"skill","Artifact":"art"}
def opname(n): return OPS_SHORT.get(n) or n.lower()[:6]
def merge_ops(counts):
    """[(short_name, n)] biggest first, tools sharing a short name summed."""
    if not counts: return []
    mg={}
    for k,v in counts.items():
        s=opname(k); mg[s]=mg.get(s,0)+v
    return sorted(mg.items(),key=lambda kv:(-kv[1],kv[0]))

def fleet_ops(rows):
    """(per-session counts, fleet totals) — files/lines are deduped ACROSS sessions,
    since two panes in the same repo routinely edit the same file."""
    per={}; tot={}; files=set(); add=dele=0
    for r in rows:
        st=_ops.get(r.get("tp") or "")
        if not st: continue
        per[r["sid"]]=st["counts"]
        for k,v in st["counts"].items(): tot[k]=tot.get(k,0)+v
        files|=st["files"]; add+=st["add"]; dele+=st["del"]
    return per,{"counts":tot,"files":len(files),"add":add,"del":dele}

# Claude Code's own authoritative lifetime tally (the source behind /stats and the
# "What's up next" card). Deduped, persistent across pruned transcripts. Refreshed by
# CC on startup / when stats are viewed — may lag a few days (see lastComputedDate).
STATS_FILE = os.path.expanduser("~/.claude/stats-cache.json")
def read_stats():
    try:
        d=json.load(open(STATS_FILE))
    except Exception:
        return None
    mu=d.get("modelUsage",{})
    tot={"in":0,"out":0,"cc":0,"cr":0}
    for v in mu.values():
        tot["in"]+=v.get("inputTokens",0); tot["out"]+=v.get("outputTokens",0)
        tot["cc"]+=v.get("cacheCreationInputTokens",0); tot["cr"]+=v.get("cacheReadInputTokens",0)
    fav=max(mu.items(), key=lambda kv: kv[1].get("inputTokens",0)+kv[1].get("outputTokens",0),
            default=(None,{}))[0] or "?"
    hc=d.get("hourCounts",{})
    peak=max(hc.items(), key=lambda kv: kv[1], default=("?",0))[0]
    return {"tot":tot, "sessions":d.get("totalSessions",0), "messages":d.get("totalMessages",0),
            "fav":fav.replace("claude-","").split("-2")[0], "peak":peak,
            "active":len(d.get("dailyActivity",[])), "as_of":d.get("lastComputedDate","?")}

# ── ANSI ──────────────────────────────────────────────────────────────────
def c(s, code): return f"\x1b[{code}m{s}\x1b[0m"
DIM="2";BOLD="1";RED="31";GRN="32";YEL="33";BLU="34";MAG="35";CYN="36";GRY="90"
HOME="\x1b[H";CLEAR="\x1b[2J";HIDE="\x1b[?25l";SHOW="\x1b[?25h";CLRLINE="\x1b[K"
ALT_ON="\x1b[?1049h";ALT_OFF="\x1b[?1049l"   # alternate screen buffer — no scrollback

def human(n):
    n=float(n)
    for unit,div in (("B",1e9),("M",1e6),("K",1e3)):
        if abs(n)>=div: return f"{n/div:.2f}{unit}"
    return f"{int(n)}"

def human0(n):
    """Same scale as human(), but no fractional K — token counts are read as a
    magnitude ("110K"), and two decimals of precision on them is just noise.
    M/B keep one decimal, where dropping it would round 1.5M to 2M."""
    n=float(n)
    if abs(n)>=1e9: return f"{n/1e9:.1f}B"
    if abs(n)>=1e6: return f"{n/1e6:.1f}M"
    if abs(n)>=1e3:
        k=round(n/1e3)
        return f"{k/1e3:.1f}M" if abs(k)>=1e3 else f"{k}K"   # 999_999 reads 1.0M, not 1000K
    return f"{int(n)}"

def dur(secs):
    s=int(secs); h,s=divmod(s,3600); m,s=divmod(s,60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")

def termsize():
    try:
        sz=os.get_terminal_size(); return sz.columns, sz.lines
    except OSError:
        return 80,40

# ── token scan (cached by file mtime+size) ─────────────────────────────────
_cache={}   # path -> (key, totals_dict, per_model_dict, window_events[(ts,in+out)])
def parse_file(path):
    # Dedupe by message.id: one assistant API response is written across several
    # transcript lines (thinking/text/tool_use blocks), each carrying the same
    # message-level usage. Count it once — matches /usage (per-response billing).
    # Subagent (isSidechain) responses ARE included: /usage counts them too, and
    # subagent-heavy sessions are a large share of real usage.
    raw={"in":0,"out":0,"cc":0,"cr":0}   # lifetime per-line sum (matches ccusage-style tools)
    events=[]; per_model={}; seen=set()  # per_model is deduped, for accurate cost
    try:
        for line in open(path, errors="ignore"):
            line=line.strip()
            if not line or '"usage"' not in line: continue
            try: o=json.loads(line)
            except Exception: continue
            m=o.get("message")
            if not isinstance(m,dict): continue
            u=m.get("usage")
            if not isinstance(u,dict): continue
            i=u.get("input_tokens",0) or 0
            ot=u.get("output_tokens",0) or 0
            ucc=u.get("cache_creation_input_tokens",0) or 0
            ucr=u.get("cache_read_input_tokens",0) or 0
            raw["in"]+=i; raw["out"]+=ot; raw["cc"]+=ucc; raw["cr"]+=ucr
            mid=m.get("id")
            if mid is not None:
                if mid in seen: continue   # dedup only the cost/window accounting
                seen.add(mid)
            mdl=m.get("model") or "unknown"
            d=per_model.setdefault(mdl,{"in":0,"out":0,"cc":0,"cr":0})
            d["in"]+=i; d["out"]+=ot; d["cc"]+=ucc; d["cr"]+=ucr
            ts=o.get("timestamp")
            if ts:
                try:
                    ep=datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
                    # tokens produced by this response — replayed cache reads excluded
                    events.append((ep, i+ot+ucc))
                except Exception:
                    pass
    except OSError:
        pass
    return raw, per_model, events

def scan():
    # recursive: subagent and workflow transcripts live at
    # projects/<proj>/<session>/subagents/[workflows/wf_*/]agent-*.jsonl. A shallow glob
    # missed ~1,000 of them, so the 5h figure undercounted subagent work while USAGE's
    # 7d/30d columns (cc_history, which globs recursively) counted it — the two panels
    # disagreed by however much the subagents had done.
    files=glob.glob(os.path.join(PROJECTS,"**","*.jsonl"),recursive=True)
    raw={"in":0,"out":0,"cc":0,"cr":0}; per_model={}; window=[]
    for p in files:
        try: st=os.stat(p)
        except OSError: continue
        key=(st.st_mtime, st.st_size)
        ent=_cache.get(p)
        if not ent or ent[0]!=key:
            r,pm,events=parse_file(p)
            ent=_cache[p]=(key,r,pm,events)
        _,r,pm,events=ent
        for k in raw: raw[k]+=r[k]
        for mdl,d in pm.items():
            agg=per_model.setdefault(mdl,{"in":0,"out":0,"cc":0,"cr":0})
            for k in agg: agg[k]+=d[k]
        window.extend(events)
    return raw, per_model, window, len(files)

def produced(path):
    """Tokens this session produced (in+out+cache write), from the token scan's cache.

    The fleet dump's context_window totals count replayed cache reads too, so a long
    idle session's figure climbs on every turn. This counts only new tokens, matching
    the USAGE panel."""
    ent=_cache.get(path)
    if not ent: return None
    return sum(v["in"]+v["out"]+v["cc"] for v in ent[2].values()) or None

def window_usage(events):
    cutoff=time.time()-WINDOW_HOURS*3600
    used=0; first=None
    for t,v in events:
        if t>=cutoff:
            used+=v
            if first is None or t<first: first=t
    return used, first

def busiest_window(events):
    """Max input+output summed over any WINDOW_HOURS span in history."""
    if not events: return 0
    span=WINDOW_HOURS*3600
    ev=sorted(events)
    best=run=0; lo=0
    for hi in range(len(ev)):
        run+=ev[hi][1]
        while ev[hi][0]-ev[lo][0]>span:
            run-=ev[lo][1]; lo+=1
        if run>best: best=run
    return best

# ── cost ─────────────────────────────────────────────────────────────────────
def price_for(mdl):
    # model ids may carry a date suffix (claude-haiku-4-5-20251001) — match by prefix.
    for k,v in PRICING.items():
        if mdl.startswith(k): return v
    return DEFAULT_PRICE

def cost(per_model):
    """Estimated USD spend. cache read billed 0.1×input, cache write 1.25×input."""
    total=0.0
    for mdl,d in per_model.items():
        pin,pout=price_for(mdl)
        total+=(d["in"]*pin + d["out"]*pout + d["cr"]*0.1*pin + d["cc"]*1.25*pin)/1e6
    return total

# ── git ────────────────────────────────────────────────────────────────────
def git(path,*args):
    try:
        return subprocess.run(["git","-C",path,*args],capture_output=True,
                              text=True,timeout=5).stdout.rstrip("\n")
    except Exception:
        return ""

# One subprocess per repo per BRANCH_TTL instead of one per fleet row per frame —
# at animation frame rates the uncached version was the dashboard's whole CPU cost.
BRANCH_TTL=15.0
_branch_cache={}   # cwd -> (ts, branch)
def git_branch(path):
    hit=_branch_cache.get(path)
    if hit and time.time()-hit[0]<BRANCH_TTL: return hit[1]
    br=git(path,"rev-parse","--abbrev-ref","HEAD")
    _branch_cache[path]=(time.time(),br)
    return br

# ── render ─────────────────────────────────────────────────────────────────
def bar(frac,width):
    frac=max(0.0,min(1.0,frac))
    fill=int(round(frac*width))
    col=GRN if frac<0.6 else (YEL if frac<0.85 else RED)
    return c("█"*fill,col)+c("░"*(width-fill),GRY)

# ── clawd, the Claude Code pet ─────────────────────────────────────────────
# Lifted verbatim from the shipped CLI bundle (@anthropic-ai/claude-code): the pose
# table renders three rows of quadrant blocks, with the middle segments drawn in
# `clawd_body` rgb(215,119,87) on a `clawd_background` rgb(0,0,0) — the black showing
# through the quadrant gaps is what makes the ears and eyes read.
# Row 1: L + E + R   Row 2: 2L + █████ + 2R   Row 3: feet.
CLAWD_BODY="38;2;215;119;87"                  # clawd_body
CLAWD_FACE="38;2;215;119;87;48;2;0;0;0"       # clawd_body on clawd_background
#
# Rows are upstream's own (LogoV2/Clawd.tsx), at its full 9-cell width: row 1 =
# r1L + r1E + r1R, row 2 = r2L + █████ + r2R, row 3 = the feet. The only edit is a
# trailing space on the default r1R so every pose measures 9 and the wordmark beside
# him can't shift by a column when the pose changes.
#
# NB the feet are "▘▘ ▝▝", NOT the " ▗   ▖ " that also appears in the bundle — that
# string is APPLE_EYES, a fallback EYES row for Apple Terminal (which can't do the
# bg-fill trick), never drawn under the body.
CLAWD_LEGS="  ▘▘ ▝▝  "   # top quadrants → flush with the torso row; ink sits under the core
CLAWD={   # each row is 9 cells: L + core(5) + R
    "default":    {"L":" ▐","E":"▛███▜","R":"▌ ","2L":"▝▜","2R":"▛▘"},
    "look-left":  {"L":" ▐","E":"▟███▟","R":"▌ ","2L":"▝▜","2R":"▛▘"},
    "look-right": {"L":" ▐","E":"▙███▙","R":"▌ ","2L":"▝▜","2R":"▛▘"},
    "arms-up":    {"L":"▗▟","E":"▛███▜","R":"▙▖","2L":" ▜","2R":"▛ "},
    # ours, not upstream's: the eyes ARE the black showing through the quadrant gaps
    # of ▛/▜, so a solid █████ covers the background entirely — eyes shut.
    "blink":      {"L":" ▐","E":"█████","R":"▌ ","2L":"▝▜","2R":"▛▘"},
    # more of ours, same trick: the face row's black gaps ARE the expression, and the
    # arm cells (L/R + 2L/2R) are what read as limbs. Every row still measures 9.
    "wink-left":  {"L":" ▐","E":"████▜","R":"▌ ","2L":"▝▜","2R":"▛▘"},
    "wink-right": {"L":" ▐","E":"▛████","R":"▌ ","2L":"▝▜","2R":"▛▘"},
    "squint":     {"L":" ▐","E":"▄███▄","R":"▌ ","2L":"▝▜","2R":"▛▘"},
    "wide":       {"L":" ▐","E":"▗███▖","R":"▌ ","2L":"▝▜","2R":"▛▘"},
    "wave-left":  {"L":"▗▟","E":"▛███▜","R":"▌ ","2L":" ▜","2R":"▛▘"},
    "wave-right": {"L":" ▐","E":"▛███▜","R":"▙▖","2L":"▝▜","2R":"▛ "},
    "shrug":      {"L":"▐ ","E":"▛███▜","R":" ▌","2L":"▝▜","2R":"▛▘"},
    "lean":       {"L":"  ","E":"▛███▜","R":"▌▌","2L":"▝▜","2R":"▛▘"},
    "sleep":      {"L":" ▐","E":"█▄█▄█","R":"▌ ","2L":"▝▜","2R":"▛▘"},
}
def clawd_cells(pose):
    """Clawd as 3 rows of (char, colour) cells, so he can be composited over the
    wordmark. Spaces carry colour None and are painted as TRANSPARENT — otherwise he
    drags a blank rectangle across the logo instead of walking over it."""
    p=CLAWD.get(pose) or CLAWD["default"]
    seg=lambda s,col:[(ch,col if ch!=" " else None) for ch in s]
    return [seg(p["L"],CLAWD_BODY)+seg(p["E"],CLAWD_FACE)+seg(p["R"],CLAWD_BODY),
            seg(p["2L"],CLAWD_BODY)+seg("█████",CLAWD_FACE)+seg(p["2R"],CLAWD_BODY),
            seg(CLAWD_LEGS,CLAWD_BODY)]
CLAWD_W=9        # every pose is exactly 9 cells; asserted in the width tests
CLAWD_H=3

# ── block wordmark ─────────────────────────────────────────────────────────
# A terminal can't scale text, so a "bigger" wordmark has to be drawn: 3 rows tall,
# each glyph 3 cells wide with a 1-cell gap. Only the letters CLAUDE CODE needs.
# Compaction is done by STACKING the two words (23 cells) rather than shrinking the
# glyphs — sub-3-cell letters have to fall back to quadrant blocks, which read mushy.
BLOCK_FONT={
    "C":("███","█  ","███"), "L":("█  ","█  ","███"), "A":("███","███","█ █"),
    "U":("█ █","█ █","███"), "D":("██ ","█ █","██ "), "E":("███","██ ","███"),
    "O":("███","█ █","███"),
}
def banner_lines(text):
    """3 equal-width rows spelling `text`; unknown characters are skipped.

    A space is a 1-cell gap, not a blank 3-cell glyph — the inter-letter gaps either
    side already read as a word break."""
    rows=["","",""]
    for ch in text.upper():
        if ch==" ":
            for i in range(3): rows[i]+=" "
            continue
        g=BLOCK_FONT.get(ch)
        if not g: continue
        for i in range(3): rows[i]+=g[i]+" "
    if not rows[0]: return ["","",""]
    rows=[r[:-1] for r in rows]        # drop the trailing inter-letter gap, not glyph
    w=max(len(r) for r in rows)        # ink (D and L end in a blank column) — rstrip
    return [r.ljust(w) for r in rows]  # would desync the rows

# ── clawd's roaming animation ──────────────────────────────────────────────
# He roams the WHOLE header box — the full inner width and every content row —
# walking across the wordmark rather than being penned beside it. Position (x,y) is
# absolute in the box; actions are lists of (seconds, pose, dx, dy) steps applied to
# it. Sequences are BUILT per action (not table lookups) so walk length, direction and
# climb distance vary, and so a walk can be clipped to the room actually available.
GAP_IDLE=(1.2,5.0); GAP_BUSY=(0.25,1.5)   # seconds of stillness between actions
# Every so often the post-action rest is a LONG one instead — he sits there doing
# nothing, which is what makes the moving bits read as deliberate rather than as a
# loop that never stops twitching.
LOAF_CHANCE=0.35; LOAF_IDLE=(6.0,16.0); LOAF_BUSY=(2.0,5.0)

def _clawd_action(busy,x,maxx,y,maxy,ok=None,band=0):
    """ok(x,y) says which cells he may occupy; `band` is the top floor (above the
    wordmark). Everything below `band` is only reachable where the wordmark isn't —
    the gutters either side of it, which widen as the window does."""
    if ok is None: ok=lambda px,py: 0<=px<=maxx and 0<=py<=maxy
    supported=lambda px,py: py>=maxy or not ok(px,py+1)
    # columns he can stand on at each level: the band is floor everywhere the wordmark
    # is under him, and the gutters are the columns where the floor is the box bottom
    ledges=[px for px in range(maxx+1) if ok(px,band) and supported(px,band)]
    pits  =[px for px in range(maxx+1) if ok(px,band) and not supported(px,band)]
    # Weights, not a uniform pick: idle clawd mostly potters and holds still, busy
    # clawd bounces. The tail kinds are rare on purpose — a trick you see every
    # thirty seconds stops being a surprise.
    kinds=(["walk"]*5+["jump"]*2+["blink"]*4+["look"]+["cheer"]
           +["wave"]*2+["stretch"]*3+["snooze"]*4+["ponder"]*3+["sneeze"]
           +["scan"]
           if not busy else
           ["walk"]*4+["jump"]*5+["cheer"]*2+["blink"]*2+["wave"]
           +["dance"]*3+["spin"]+["stretch"]*2)
    # only offer the two level-changing moves when the geometry actually allows them:
    # a narrow window has no gutter beside the wordmark and he simply stays up top
    if y<=band and pits: kinds+=["drop"]*(4 if not busy else 2)
    if y>band and ledges: kinds+=["climb"]*6
    k=random.choice(kinds)
    if k=="drop":
        # walk to the lip of a gutter and step off — gravity in clawd_state does the
        # falling, so the landing squash is the same one a jump gets
        tgt=min(pits,key=lambda px:abs(px-x))
        d=1 if tgt>x else -1
        walk=[(0.11,"look-right" if d>0 else "look-left",d,0) for _ in range(abs(tgt-x))]
        return walk+[(0.30,"wide",0,0)]      # a beat at the edge before he goes over
    if k=="climb":
        # scramble back up to the wordmark's level, then hop across onto it
        tgt=min(ledges,key=lambda px:abs(px-x))
        d=1 if tgt>x else -1
        up=[(0.09,"arms-up",0,-1) for _ in range(max(0,y-band))]
        across=[(0.09,"arms-up",d,0) for _ in range(abs(tgt-x))]
        return [(0.20,"squint",0,0)]+up+across+[(0.12,"squint",0,0),(0.25,"default",0,0)]
    if k=="wave":
        # one arm up, waggling — alternate the raised arm so it isn't always the left
        side=random.choice(("wave-left","wave-right"))
        n=random.randint(2,4)
        return [(0.20,side if i%2 else "default",0,0) for i in range(n*2)]+[
                (0.30,"default",0,0)]
    elif k=="stretch":
        return [(0.30,"arms-up",0,0),(0.90,"arms-up",0,0),(0.35,"squint",0,0),
                (0.50,"default",0,0)]
    elif k=="snooze":
        # the long one: eyes drift shut and STAY shut. Nothing moves for seconds.
        return [(0.45,"squint",0,0),(0.35,"blink",0,0),
                (random.uniform(2.5,6.0),"sleep",0,0),
                (0.40,"squint",0,0),(0.30,"wide",0,0),(0.40,"default",0,0)]
    elif k=="ponder":
        # a long hold, head cocked, then a slow return — pure stillness with a tilt
        tilt=random.choice(("shrug","lean"))
        return [(0.35,tilt,0,0),(random.uniform(1.5,4.0),tilt,0,0),
                (0.40,"squint",0,0),(0.35,"default",0,0)]
    elif k=="sneeze":
        return [(0.50,"squint",0,0),(0.25,"wide",0,0),(0.12,"blink",0,-1) if y>0
                else (0.12,"blink",0,0),(0.12,"arms-up",0,1) if y>0
                else (0.12,"arms-up",0,0),(0.45,"squint",0,0),(0.30,"default",0,0)]
    elif k=="doubletake":
        # snaps one way, snaps back harder — the timing IS the joke, so keep it tight
        d=random.choice(("look-left","look-right"))
        o="look-right" if d=="look-left" else "look-left"
        return [(0.55,d,0,0),(0.12,o,0,0),(0.10,"wide",0,0),(0.70,"wide",0,0),
                (0.35,"default",0,0)]
    elif k=="scan":
        # slow sweep of the whole box, holding at each end
        return [(0.80,"look-left",0,0),(0.30,"default",0,0),(0.80,"look-right",0,0),
                (random.uniform(0.8,2.0),"default",0,0)]
    elif k=="dance":
        # shuffle in place: step out and back, so he ends where he started
        d=1 if x<=0 else -1 if x>=maxx else random.choice((-1,1))
        beat=[]
        for i in range(random.randint(2,4)):
            beat+=[(0.14,"arms-up",d,0),(0.14,"default",-d,0)]
        return beat+[(0.25,"default",0,0)]
    elif k=="spin":
        return [(0.10,p,0,0) for p in ("look-left","default","look-right","default")*2]+[
                (0.30,"wide",0,0),(0.35,"default",0,0)]
    elif k=="walk":
        d=1 if x<=0 else -1 if x>=maxx else random.choice((-1,1))
        room=(maxx-x) if d>0 else x
        n=min(random.randint(3,12),room)
        if n:                                     # alternate the eyes so he looks
            step=0.10 if busy else 0.14           # like he's stepping, not sliding
            pose=("look-right" if d>0 else "look-left")
            # a glance every fourth step, not every other one — alternating the eyes
            # on every step made him look frantic rather than like he was walking
            seq=[(step, pose if i%4==0 else "default", d,0) for i in range(n)]
            # idle strolls stop halfway to look at something, then carry on — a walk
            # that never breaks stride is the thing that reads as a loop
            if not busy and n>=5 and random.random()<0.45:
                seq.insert(random.randint(2,n-2),
                           (random.uniform(0.6,1.6),random.choice(("look-left","look-right","squint")),0,0))
            return seq+[(random.uniform(0.25,0.9),"default",0,0)]
    elif k=="jump":
        # Straight up and straight back down — he never STAYS off the ground, so the
        # airborne step is always immediately followed by its matching descent.
        if y>0:
            return [(0.14,"arms-up",0,0),(0.22,"arms-up",0,-1),
                    (0.14,"default",0,1),(0.12,"default",0,0)]
        return [(0.18,"arms-up",0,0),(0.20,"default",0,0)]   # no headroom → hop in place
    elif k=="cheer":
        return [(0.40,"arms-up",0,0),(0.22,"default",0,0),(0.40,"arms-up",0,0),
                (0.22,"default",0,0)]
    elif k=="look":
        return [(0.45,random.choice(("look-left","look-right")),0,0),
                (random.uniform(0.4,1.8),"default",0,0)]
    # blink: sometimes a plain double-blink, sometimes a wink held long enough to land
    if random.random()<0.3:
        return [(0.16,"blink",0,0),(0.55,random.choice(("wink-left","wink-right")),0,0),
                (0.30,"default",0,0)]
    return [(0.16,"blink",0,0),(0.18,"default",0,0),(0.14,"blink",0,0),
            (random.uniform(0.3,1.2),"default",0,0)]

def clawd_state(a, now, busy, maxx, maxy, ok=None, band=0):
    """Advance the animation clock; returns (pose, x, y) absolute in the header box.

    `a` is one clawd's state (there is one per live session), `ok(x,y)` is the box's
    collision map — the wordmark is solid, the other clawds are solid, the gutters
    either side are open — and `band` is the floor above the wordmark. Gravity here is
    what makes the level changes read: he never teleports between floors, he falls."""
    if ok is None: ok=lambda px,py: 0<=px<=maxx and 0<=py<=maxy
    if not ok(a["x"],a["y"]):                    # window resized under him
        a["x"]=max(0,min(maxx,a["x"])); a["y"]=max(0,min(maxy,a["y"]))
        while a["y"]>0 and not ok(a["x"],a["y"]): a["y"]-=1
        if not ok(a["x"],a["y"]): a["x"],a["y"]=0,min(band,maxy)
        a["seq"]=[]; a["i"]=0; a["fell"]=0
    while now>=a["until"]:
        if a["i"]<len(a["seq"]):
            dur,pose,dx,dy=a["seq"][a["i"]]; a["i"]+=1
            nx=max(0,min(maxx,a["x"]+dx)); ny=max(0,min(maxy,a["y"]+dy))
            # slide along a wall rather than stopping dead against it
            if   ok(nx,ny):      a["x"],a["y"]=nx,ny
            elif ok(nx,a["y"]):  a["x"]=nx
            elif ok(a["x"],ny):  a["y"]=ny
            a["pose"]=pose; a["until"]=now+dur
        elif a["resting"]:                       # rest over → pick the next action
            a["seq"]=_clawd_action(busy,a["x"],maxx,a["y"],maxy,ok,band)
            a["i"]=0; a["resting"]=False
        elif a["y"]<maxy and ok(a["x"],a["y"]+1):
            # nothing under him → fall, accelerating after the first row
            a["fell"]+=1
            a["seq"]=[(0.11 if a["fell"]<2 else 0.06,"arms-up",0,1)]; a["i"]=0
        elif a["fell"]:                          # landing squash, deeper for a long drop
            n=a["fell"]; a["fell"]=0
            a["seq"]=([(0.10,"squint",0,0)]+([(0.12,"lean",0,0)] if n>1 else [])
                      +[(0.14,"default",0,0)])
            a["i"]=0
        else:                                    # action over → stand still a while
            a["resting"]=True; a["pose"]="default"
            loaf=random.random()<LOAF_CHANCE
            gap=(LOAF_BUSY if busy else LOAF_IDLE) if loaf else (GAP_BUSY if busy else GAP_IDLE)
            a["until"]=now+random.uniform(*gap)
    return a["pose"],a["x"],a["y"]

# ── the troupe: one clawd per live session ─────────────────────────────────
# The header population IS the fleet — a session appearing drops a clawd into the box,
# a session ending walks one out. They are solid to each other, so they queue and
# sidestep instead of merging into a single smear of blocks.
CLAWD_GAP=1        # cells they keep between them
EXIT_SECS=1.2      # how long the goodbye plays before the clawd is dropped
_clawds={}         # session key -> anim state

def _exit_seq():
    """Goodbye: a wave, then he hops up out of the frame."""
    return [(0.18,"wide",0,0),(0.20,"wave-left",0,0),(0.20,"default",0,0),
            (0.20,"wave-left",0,0),(0.16,"arms-up",0,-1),(0.16,"arms-up",0,-1),
            (0.14,"blink",0,0)]

def _spawn(x,y=0,seq=None):
    """Hello. Where he comes from is randomised — always dropping from the same top row
    made every arrival look identical however random the column was."""
    return {"seq":seq if seq is not None else [(0.12,"blink",0,0),(0.12,"wide",0,0)],
            "i":0,"until":0.0,"x":x,"y":y,"pose":"blink","resting":False,
            "fell":0,"bye":None,"greet":0.0}

def _entrance(free, maxx, bottom, band, base_ok):
    """Pick a random arrival: fall in from above, walk in from a side, or climb up out
    of a gutter. Each returns (x, y, opening sequence); gravity finishes the job."""
    pits=[px for px in free if base_ok(px,bottom) and bottom>band]
    roll=random.random()
    if pits and roll<0.25:                       # up out of a gutter, with a stretch
        x=random.choice(pits)
        return x,bottom,[(0.14,"blink",0,0),(0.16,"squint",0,0),(0.20,"arms-up",0,0),
                         (0.18,"wide",0,0)]
    if free and roll<0.50:                       # stroll in from whichever edge is free
        edges=[px for px in (0,maxx) if px in free]
        if edges:
            x=random.choice(edges); d=1 if x==0 else -1
            walk=[(0.12,"look-right" if d>0 else "look-left",d,0)
                  for _ in range(random.randint(2,5))]
            return x,band,[(0.14,"blink",0,0)]+walk
    x=random.choice(free) if free else random.randint(0,maxx)
    return x,0,[(0.12,"blink",0,0),(0.12,"wide",0,0)]     # drop in from the top

def _greet(now):
    """Two clawds that end up side by side notice each other and wave.

    Rate-limited per clawd — a pair that shares a gutter would otherwise wave forever."""
    ks=[k for k,a in _clawds.items() if a.get("bye") is None]
    for i in range(len(ks)):
        for j in range(i+1,len(ks)):
            a,b=_clawds[ks[i]],_clawds[ks[j]]
            if a["y"]!=b["y"] or not (a["resting"] and b["resting"]): continue
            if abs(a["x"]-b["x"])-CLAWD_W>3: continue
            if now<a["greet"] or now<b["greet"] or random.random()>0.25: continue
            left,right=(a,b) if a["x"]<b["x"] else (b,a)
            def hi(who,face,wave):
                who["seq"]=[(0.28,face,0,0),(0.22,wave,0,0),(0.22,face,0,0),
                            (0.22,wave,0,0),(0.35,"default",0,0)]
                who["i"]=0; who["resting"]=False; who["until"]=now
                who["greet"]=now+random.uniform(8,20)
            hi(left,"look-right","wave-right"); hi(right,"look-left","wave-left")

def update_clawds(fleet, now, inner, bottom, band, base_ok):
    """Sync the troupe to the fleet and advance every clawd a frame.

    Returns them in paint order. More sessions than the box can hold are simply not
    given a clawd — nine cells each is a hard floor, and overlapping them would read
    as one broken sprite rather than as a crowd."""
    maxx=max(0,inner-CLAWD_W)
    cap=max(1,(inner+CLAWD_GAP)//(CLAWD_W+CLAWD_GAP))
    # WORKING chats only. Subagents never reach the fleet at all (they are a row's
    # `subs`), and a dispatched background agent is not someone sitting at a terminal —
    # neither gets a clawd. An idle chat doesn't either: the troupe is a picture of what
    # is generating right now, so a session going quiet walks its clawd out and a prompt
    # drops one back in.
    chats=[s for s in fleet if not s.get("bg") and s.get("working")]
    keys=[s["sid"] for s in chats][:cap]
    busy={s["sid"]:s["working"] for s in chats}
    for k,a in list(_clawds.items()):          # sessions that ended → play the goodbye
        if k in keys:
            a["bye"]=None; continue
        if a["bye"] is None:
            a["bye"]=now; a["seq"]=_exit_seq(); a["i"]=0; a["resting"]=False
        elif now-a["bye"]>EXIT_SECS:
            del _clawds[k]
    for k in keys:                             # new sessions → drop one in
        if k in _clawds: continue
        free=[px for px in range(0,maxx+1)
              if base_ok(px,0) and all(abs(px-o["x"])>=CLAWD_W+CLAWD_GAP
                                       for o in _clawds.values())]
        _clawds[k]=_spawn(*_entrance(free,maxx,bottom,band,base_ok))
    for k,a in _clawds.items():
        boxes=[(o["x"],o["y"]) for kk,o in _clawds.items() if kk is not k]
        def ok(px,py,boxes=boxes):
            if not base_ok(px,py): return False
            # solid to one another, with a gap kept so they never visually touch
            return all(not (abs(px-ox)<CLAWD_W+CLAWD_GAP and abs(py-oy)<CLAWD_H)
                       for ox,oy in boxes)
        clawd_state(a,now,busy.get(k,False),maxx,bottom,ok,band)
    _greet(now)
    return list(_clawds.values())

def hr(label,width):
    label=f" {label} "
    # The rule spans the FULL panel width: it starts at column 0, so it has to end at
    # column W or the panel looks lopsided — flush left, short right.
    return c("─── ",GRY)+c(label.strip(),BOLD)+" "+c("─"*max(0,width-len(label)-3),GRY)

LOTR_TOKENS=576_000   # ~Lord of the Rings trilogy in tokens, for the vanity multiple
def render(raw,per_model,window,nfiles,cap):
    cols,lines=termsize()
    W=min(cols,100)
    used,first=window_usage(window)
    out=[]
    now=datetime.now()
    ORANGE="38;5;173"   # Claude terracotta accent
    inner=W-2
    # The header is COMPOSITED on a cell grid rather than assembled from row strings:
    # the wordmark is painted first, then clawd on top, so he can roam the whole box
    # and walk across the logo. Painting per-cell is also what keeps the visible width
    # exact — colour escapes never enter the width arithmetic.
    fleet=read_fleet()
    date_s=now.strftime("%a %d %b"); time_s=now.strftime("%H:%M:%S")
    mark="CLAUDE CODE"
    # Date + time ride IN the top border, right-aligned, interrupting the rule. Too
    # narrow to cut them in? Fall back to a stacked date-over-time block on the right.
    stamp=len(date_s)+2+len(time_s)
    in_border=inner>=stamp+6
    RW=0 if in_border else max(len(date_s),len(time_s))
    big=banner_lines(mark); bw=len(big[0])       # always one line: 3 rows, 40 cells
    gut=2                                        # gutter the wordmark keeps from the frame
    if bw>inner-2*gut:                           # too narrow for the block font
        big=[mark[:max(0,inner-2*gut)]]; bw=len(big[0])
    # The wordmark sits on its own rows at the BOTTOM. Clawd owns the band above it —
    # but the wordmark is centred, so a wide window leaves a gutter either side of it
    # that is empty all the way down. Those gutters are his too: he can walk off the
    # edge of the logo and drop into one, and climb back up.
    BAND=CLAWD_H+1                               # one spare row so he has headroom
    H=BAND+len(big)
    wm_at=((inner-bw)//2, BAND)
    wx0,wy0=wm_at
    band_floor=max(0,BAND-CLAWD_H)               # standing on top of the wordmark
    bottom=max(0,H-CLAWD_H)                      # standing on the floor of the box
    GUT=1                                        # cells of clearance he keeps from it
    def clawd_ok(px,py):
        if not (0<=px<=max(0,inner-CLAWD_W) and 0<=py<=bottom): return False
        if py+CLAWD_H<=wy0: return True          # entirely above the wordmark's rows
        return px+CLAWD_W<=wx0-GUT or px>=wx0+bw+GUT      # else: clear of its columns
    troupe=update_clawds(fleet, time.time(), inner, bottom, band_floor, clawd_ok)

    grid=[[(" ",None) for _ in range(inner)] for _ in range(H)]
    def paint(x,y,cells):
        if not (0<=y<H): return
        for i,(ch,col) in enumerate(cells):
            if col is None or not (0<=x+i<inner): continue   # None == transparent
            grid[y][x+i]=(ch,col)
    wx,wy=wm_at
    for j,line in enumerate(big):                            # wordmark underneath
        paint(wx,wy+j,[(ch,f"{BOLD};{ORANGE}") for ch in line])
    for a in troupe:                                         # the troupe walks above it
        for j,cells in enumerate(clawd_cells(a["pose"])):
            paint(a["x"],a["y"]+j,cells)
    if not in_border:                                        # right-hand clock block —
        paint(inner-RW,1,[(ch,GRY) for ch in date_s.rjust(RW)])          # painted LAST
        paint(inner-RW,2,[(ch,f"{BOLD};{YEL}") for ch in time_s.rjust(RW)])  # so he
                                                             # can't stand in front of it

    if in_border:                                # ╭──── Tue 25 Aug  11:02:23 ──╮
        lead=inner-stamp-4                       # rule + 1 space each side + 2 trailing
        out.append(c("╭"+"─"*lead+" ",ORANGE)+c(date_s,GRY)+"  "
                   +c(time_s,f"{BOLD};{YEL}")+c(" ──╮",ORANGE))
    else:
        out.append(c("╭"+"─"*inner+"╮",ORANGE))
    for row in grid:                             # emit, coalescing same-colour runs
        line=""; buf=""; cur=None
        for ch,col in row:
            if col!=cur:
                if buf: line+=c(buf,cur) if cur else buf
                buf=""; cur=col
            buf+=ch
        if buf: line+=c(buf,cur) if cur else buf
        out.append(c("│",ORANGE)+line+c("│",ORANGE))
    out.append(c("╰"+"─"*inner+"╯",ORANGE))
    out.append("")

    # Every panel row is inset 2 from the section rule's left end; ROW_RIGHT mirrors
    # that inset on the right (the rule now runs the full W, see hr), so rows sit INSIDE
    # their section with equal margins either side. ONE right edge for every panel —
    # SESSION's bars, FLEET's table and USAGE's columns all end here, so the whole
    # dashboard right-justifies on a single line however wide the window is.
    ROW_RIGHT=W-2
    PCTW=4                     # "100%" — fixed, so the bar length can't jitter with it
    # Bars were a hard-coded 40, which overflows a ~46-column window: the row wrapped,
    # and a wrapped row scrolls the alt-screen buffer and permanently shifts the display.
    # Now it fills whatever the window allows, ending flush with every other row.
    BARW=max(10,ROW_RIGHT-2-2-PCTW)
    # session limits — exact, from Claude Code's live statusline feed when available
    live=read_live()
    rl=(live or {}).get("rate_limits") if isinstance(live,dict) else None
    def reset_str(epoch):
        rem=max(0,epoch-time.time())
        return f"resets in {int(rem//3600)}h{int(rem%3600//60):02d}m"
    if rl and isinstance(rl.get("five_hour"),dict):
        fh=rl["five_hour"]; pct=fh.get("used_percentage",0)
        # Each bar is captioned above it: name on the left, the bare time-to-reset
        # ending exactly where the BAR ends (not where the % does) — so the caption
        # reads as that bar's own clock.
        def caption(label,epoch):
            rem=max(0,epoch-time.time())
            rs=f"{int(rem//3600)}h{int(rem%3600//60):02d}m"
            return ("  "+c(label,GRY)
                    +" "*max(1,2+BARW-2-len(label)-len(rs))+c(rs,BOLD))
        def hrs(h):
            # takes HOURS; past two days it reads as days
            if h>=48: return f"{h/24:.1f}d"
            if h>=1:  return f"{int(h)}h{int(h%1*60):02d}m"
            return f"{int(h*60)}m"
        def pace_line(pct,resets_at,span_h,per_day=False):
            """pace · when the cap lands.

            The 5h limit is paced per ACTIVE HOUR (see pace()); the week is paced per
            ACTIVE DAY — today's burn, or the average across the days worked since the
            week rolled — because extrapolating a week from one morning's hours swings
            the projection wildly for no reason."""
            win_start=resets_at-span_h*3600
            left=max(0.0,(resets_at-time.time())/3600)
            if per_day:
                d=window_days(window,win_start)
                rate=pct/d                              # %/day, averaged over days worked
                eta=((100-pct)/rate*24) if rate>0 else None
                r=f"{rate:.1f}%/d"
            else:
                started=window_started(window,win_start)
                el,rate,eta,left=pace(pct,resets_at,span_h,started)
                r=f"{rate:.1f}%/h"
            # the bar above already shows the %, so this line carries only what it
            # can't: the pace (over active time — see pace()) and when the cap lands
            bits=[(r,BOLD)]
            plain=[t for t,_ in bits]
            bits=[c(t,col) for t,col in bits]
            if eta is None:
                tail=[("idle",GRY)]
            elif eta<left:                       # cap arrives BEFORE the window resets
                tail=[(f"⚠ cap in {hrs(eta)}",RED)]
            else:
                tail=[(f"cap in {hrs(eta)}",GRY)]
            for txt,col in tail:
                line=" · ".join(plain+[txt])
                if len(line)+2<=ROW_RIGHT or (txt,col)==tail[-1]:
                    bits=bits+[c(txt,col)]; break
            return "  "+c(" · ",GRY).join(bits)
        out.append(hr("SESSION",W))
        out.append(caption("5h limit",fh.get('resets_at',0)))
        out.append("  "+bar(pct/100,BARW)+"  "+f"{round(pct)}%".rjust(PCTW))
        out.append(pace_line(pct,fh.get('resets_at',0),WINDOW_HOURS))
        sd=rl.get("seven_day") or {}
        if sd:
            spct=sd.get("used_percentage",0)
            out.append(caption("week limit",sd.get('resets_at',0)))
            out.append("  "+bar(spct/100,BARW)+"  "+f"{round(spct)}%".rjust(PCTW))
            out.append(pace_line(spct,sd.get('resets_at',0),24*7,per_day=True))
    else:
        # no live feed — show nothing but a hint (only exact data is displayed)
        out.append(hr("SESSION",W))
        out.append("  "+c("5h limit",GRY))
        out.append(c("  no live data · start a Claude Code session",GRY))
    out.append("")

    # USAGE's own column geometry. FLEET below runs to the full panel width instead —
    # its middle columns need the room, and its "lines" column ends on the panel edge.
    # A 9-cell floor on the value columns overflowed the panel below ~44 columns: the
    # row wrapped, and a wrapped row scrolls the alt-screen buffer permanently. Shrink
    # the values, then the label column, until the whole table fits the width.
    USAGE_LW=12; USAGE_VW=max(5,min(13,(ROW_RIGHT-2-USAGE_LW)//3))
    while 2+USAGE_LW+3*USAGE_VW>ROW_RIGHT:
        if USAGE_LW>6: USAGE_LW-=1
        elif USAGE_VW>4: USAGE_VW-=1
        else: break
    # Value columns cap at 13 cells, so once the window is wide the table stops growing
    # and would float short of the panel edge. Spend the slack on the LABEL column (to a
    # limit) so the figures stay right-flush with the bars above them — but never open
    # the 60-column label-to-number gulf that stretching all the way would.
    slack=ROW_RIGHT-(2+USAGE_LW+3*USAGE_VW)
    if slack>0: USAGE_LW+=min(slack,max(0,20-USAGE_LW))

    # FLEET — one row per session, as a fixed-column TABLE so the counts line up and
    # can be read down. Redundancy dropped on purpose: no branch line (it was "—" for
    # every row), no "idle"/"⏱" words (the dot and the colour already say it), no
    # per-row token figure (USAGE totals it below).
    per_ops,_=fleet_ops(fleet)
    if fleet:
        out.append(hr(f"FLEET ({len(fleet)})",W))
        # Layout, left to right: name · age (both pinned left), then prompt · tokens ·
        # files spread EVENLY across the middle, then lines right-justified to the panel
        # edge. The middle three share one region divided into equal cells so the gaps
        # between them stay identical however wide the window is.
        # OW must EXCEED the widest value (5, e.g. "2.36K") and the widest label, or
        # adjacent cells butt together and "10" + "2.36K" reads as "102.36K".
        SW=6; OW=7; LINEW=7                          # age, each metric, the lines column
        MID=[("tokens",lambda o,st,s: produced(s.get("tp") or "") or s.get("io") or None, human0),
             ("prompt",lambda o,st,s: st.get("prompts") if st else None, human),
             ("files", lambda o,st,s: len(st["files"]) if st else None, human)]
        # Name sized to the longest name present, not to leftover space — otherwise it
        # swallows the slack and opens a gulf before the numbers.
        NW=max(9,min(24,max(len(s["name"]) for s in fleet)+1))
        LEFT=4+NW+1+SW                    # margin + dot + space + name + gap + age
        RIGHT=ROW_RIGHT                   # same right edge as the SESSION bars above
        # If the three middle columns can't fit, take the room out of the NAME first —
        # they are the point of the table. CELL is OW plus one guaranteed space, or a
        # long value butts straight against the age beside it ("1m37s128.21K").
        CELL=OW+1
        if RIGHT-LINEW-LEFT<len(MID)*CELL:
            NW=max(9,RIGHT-LINEW-len(MID)*CELL-(4+1+SW)); LEFT=4+NW+1+SW
        use=MID[:max(0,min(len(MID),(RIGHT-LINEW-LEFT)//CELL))]
        region=max(0,RIGHT-LINEW-LEFT)               # space between age and lines
        CW=region//len(use) if use else 0            # equal cell per middle column
        lead=region-CW*len(use)                      # remainder → just before lines
        def midrow(vals):
            """vals = [(plain, coloured)] — one per middle column.

            Centred in its own equal-width cell, so a header and the figure under it
            share a centreline and every inter-column gap comes out the same."""
            row=""
            for plain,painted in vals:
                # figures are right-justified inside a fixed sub-block, and the BLOCK is
                # what gets centred — so decimals line up down the column instead of
                # drifting with each value's length
                if len(plain)<OW:
                    lp=OW-len(plain); painted=" "*lp+painted; plain=" "*lp+plain
                # odd slack leans LEFT: a full-width figure keeps its space from the
                # column before it rather than touching it
                slack=max(0,CW-len(plain)); l=slack-slack//2
                row+=" "*l+painted+" "*(slack-l)
            return row+" "*lead
        # .get, not [] — a row assembled anywhere but _read_fleet's main loop (the
        # background-agent rows were) can be missing this, and a KeyError here kills
        # the whole dashboard rather than one row
        prov=lambda s: s.get("provider") or "claude"
        multi=len({prov(s) for s in fleet})>1
        groups=([(p,[s for s in fleet if prov(s)==p])
                 for p in ("claude","codex","grok") if any(prov(s)==p for s in fleet)]
                if multi else [("",fleet)])
        def header_row(label):
            return ("  "+c(label.ljust(NW+2),GRY)+" "+c("age".rjust(SW),GRY)
                    +midrow([(h,c(h,GRY)) for h,_,_ in use])
                    +c("lines".rjust(LINEW),GRY))
        for group_i,(provider,items) in enumerate(groups):
            if provider and group_i==0:
                # In a split fleet the provider label identifies the rows, so a
                # separate "session" heading only wastes a line.
                out.append(header_row(provider.upper()))
            elif provider:
                out.append("  "+c(provider.upper(),GRY))
            else:
                out.append(header_row("  session"[:NW+2]))
            for s in items:
                dot=c("●",GRN if s["working"] else RED)
                # A single-framework fleet has no group headings to disambiguate, but it
                # also has nothing to disambiguate FROM — the provider prefix is noise.
                nm=s["name"][:NW]
                age=dur(s["since"]) if s["since"] is not None else "—"
                st=_ops.get(s.get("tp") or "")
                o=dict(merge_ops(per_ops.get(s["sid"])))
                def cell(v,w):
                    return c("·".rjust(w),GRY) if not v else c(human(v).rjust(w),f"{BOLD};{CYN}")
                def mid(v,fmt):
                    txt="·" if not v else fmt(v)
                    return txt,(c(txt,GRY) if not v else c(txt,f"{BOLD};{CYN}"))
                lncell=cell((st["add"]+st["del"]) if st else None,LINEW)
                out.append("  "+dot+" "+c(nm.ljust(NW),f"{BOLD};{MAG}" if s.get("bg") else BOLD)
                           +" "+c(age.rjust(SW),GRN if s["working"] else GRY)
                           +midrow([mid(get(o,st,s),fmt) for _,get,fmt in use])+lncell)
                subs=s.get("subs") or []
                if subs:
                    cnt={}
                    for tn in subs: cnt[tn]=cnt.get(tn,0)+1
                    summ=", ".join((f"{n}× {t}" if n>1 else t) for t,n in cnt.items())
                    line=f"⤷ {len(subs)} agent"+("s" if len(subs)>1 else "")+": "+summ
                    if len(line)>cols-5: line=line[:cols-6]+"…"
                    out.append("    "+c(line,MAG))
        out.append("")

    # USAGE — one panel, two columns: what this window is burning right now (exact, from
    # Claude Code's own statusline feed) beside the all-time totals (cc_history's cached
    # transcript scan). Same rows, same units, so the eye can read across.
    a=_hist["agg"]
    if fleet or a:
        out.append(hr("USAGE",W))
        # Fixed compact table, left-aligned. Stretching the columns to the panel edge
        # opens a 60-column gap between a label and its number in a wide window — the
        # eye can't carry across that, so the block stays tight instead.
        LW,VW=USAGE_LW,USAGE_VW                 # label column, each of 3 value columns
        def row(label,vals,col=CYN):
            # pad the PLAIN text — padding a coloured string counts escape bytes as cells
            return ("  "+c(label.ljust(LW),GRY)
                    +"".join(c(v.rjust(VW),f"{BOLD};{col}") for v in vals))
        # "5h", not "live": this column is the same rolling window the SESSION bar above
        # uses. FLEET's per-row tokens are LIFETIME totals and deliberately do not sum
        # to it — different scope, different question.
        out.append("  "+" "*LW+"".join(c(h.rjust(VW),GRY)
                                       for h in ("5h","7d","30d")))
        if fleet:
            tcost=sum(s["cost"] for s in fleet)
            # `used` is what this 5h window produced — same units as the 7d/30d columns
            # beside it, so the row reads across.
            live=[human(used),"$%.2f"%tcost,str(len(fleet))]
        else:
            live=["—","—","—"]
        # rolling windows — cc_history counts these per transcript, so `sessions` is the
        # count of distinct transcripts touched in the window, not session-days
        def wcol(label):
            if not a: return ["…","…","…"]      # first history scan still running
            w=(a.get("win") or {}).get(label) or {}
            # tokens PRODUCED (in+out+cache write). The raw count is ~97% cache reads —
            # context replayed every call — which reads as a nonsense number.
            return [human(w.get("ntok") or 0),"$%s"%f"{w.get('cost',0):,.0f}",
                    f"{w.get('sessions',0):,}"]
        d7=wcol("7d"); d30=wcol("30d")
        out.append(row("tokens",[live[0],d7[0],d30[0]]))
        out.append(row("cost",[live[1],d7[1],d30[1]],GRN))
        out.append(row("sessions",[live[2],d7[2],d30[2]]))
        if a:
            # daily token chart, one column per day (today rightmost) — as many days as
            # the panel is wide, up to two months
            out.append("")
            s=HIST.series(a,"tokens")
            today=date.today()
            # never draw further back than the oldest day that HAS token data — Claude
            # Code prunes transcripts, so a longer window is just a run of blank cells
            tok_days=[k for k,v in s.items() if v>0]
            span=((today-date.fromisoformat(min(tok_days))).days+1) if tok_days else 30
            # plot area: capped at GRAPH_W / GRAPH_FRAC so a wide window doesn't stretch
            # ~36 days into a wall of 3-cell bars, and centred in the panel.
            # X axis only — no y gutter
            BW=max(8,min(W-2,GRAPH_W,int((W-2)*GRAPH_FRAC)))
            IND=" "*(1+(W-2-BW)//2)                  # left margin that centres the chart
            N=min(60,max(7,span),BW)
            # each day's bar is BW/N cells; the remainder is spread across the bars
            # (some get one cell more) so the chart fills the box edge to edge with no
            # margin on either side
            base,rem=divmod(BW,N)
            widths=[base+(1 if (i*rem)//N != ((i+1)*rem)//N else 0) for i in range(N)]
            vals=[s.get((today-timedelta(days=N-1-i)).isoformat(),0) for i in range(N)]
            mx=max(vals) or 1
            # GRAPH_H rows of eighth-blocks: a taller chart resolves an ordinary day from
            # a big one, which a one-row sparkline cannot
            for r in range(GRAPH_H,0,-1):
                line=""
                for v,w_ in zip(vals,widths):
                    f=v/mx*GRAPH_H                  # column height in rows
                    if f>=r: ch="█"
                    elif f>r-1: ch="▁▂▃▄▅▆▇█"[max(0,min(7,int((f-(r-1))*8)-1))]
                    else: ch=" "
                    line+=ch*w_                     # widen BEFORE colouring
                out.append(IND+c(line,ORANGE))
            # x axis: uniformly spaced weekly ticks, with dates beneath them
            days_at=[]; x=0
            for i,w_ in enumerate(widths):
                days_at.append((today-timedelta(days=N-1-i),x,w_)); x+=w_
            rule=["─"]*BW
            # Use the weekly dates only as labels.  The chart's bars have integer
            # widths (some days are a cell wider than others), so placing ticks at
            # their literal day positions makes a regular weekly cadence look
            # uneven.  Spread the visible tick marks uniformly across the rule.
            # Fixed-width numeric dates make the label row scan as evenly as the
            # marks themselves.  Use day/month to match the dashboard's locale.
            weekly_labels=[]
            for d,x0,w_ in days_at:
                if d.weekday()==0:                  # Monday
                    weekly_labels.append(d.strftime("%d/%m"))
            ticks={}
            count=len(weekly_labels)
            for i,lab in enumerate(weekly_labels):
                # Inset the endpoints by half a fixed-width date label.  That lets
                # every label stay centered under its tick (including the first and
                # last), while the tick intervals remain equal.
                half_label=len(lab)//2
                if count<=1:
                    pos=(BW-1)//2
                else:
                    first=half_label; last=BW-1-half_label
                    pos=round(first+i*(last-first)/(count-1))
                rule[pos]="┴"; ticks[pos]=lab
            out.append(IND+c("".join(rule),GRY))
            axis=[" "]*BW
            for pos in sorted(ticks):                # drop a label that would collide
                # centre on the tick, but slide left rather than drop when the label
                # would run past the right edge — that is what was eating the last date
                lab=ticks[pos]
                # Odd-length labels can be perfectly centred; for an even-length
                # label this intentionally favours the right half-cell, matching
                # the left-middle tick selected above.
                st=max(0,min(pos-(len(lab)-1)//2,BW-len(lab)))
                if all(axis[j]==" " for j in range(max(0,st-1),min(BW,st+len(lab)+1))):
                    for j,ch in enumerate(lab): axis[st+j]=ch
            out.append(IND+c("".join(axis).rstrip(),GRY))
        out.append("")

    # paint — clamp to the window BOX so nothing overflows into a scroll. Height is a
    # slice; width needs a visible-cell clip, since a wrapped row scrolls the alt-screen
    # buffer and permanently shifts the display. Panels size themselves to fit, but
    # below ~30 columns even the tightest table can't, so this is the backstop.
    def clip(line,width):
        vis=0; kept=[]; i=0
        while i<len(line):
            if line[i]=="\x1b":                       # copy escapes, they cost no cells
                j=line.find("m",i)
                if j<0: break
                kept.append(line[i:j+1]); i=j+1; continue
            if vis>=width: return "".join(kept)+"\x1b[0m"
            kept.append(line[i]); vis+=1; i+=1
        return "".join(kept)
    out=[clip(l,cols) for l in out[:max(1,lines-1)]]
    buf=HOME
    for i,l in enumerate(out):
        buf+=l+CLRLINE+("\n" if i<len(out)-1 else "")
    buf+="\x1b[J"  # clear below
    sys.stdout.write(buf); sys.stdout.flush()

# All-time history. The first build reads every transcript (~8s over 2.6 GB), so it runs
# off-thread and the panel simply doesn't appear until it lands; later passes re-read only
# the transcripts that changed (~0.1s), which is why this can poll on a minute.
_hist={"agg":None}
def hist_loop():
    while True:
        try: _hist["agg"]=HIST.build()
        except Exception: pass
        time.sleep(60)

def main():
    sys.stdout.write(ALT_ON+CLEAR+HIDE)   # alt screen: no scrollback, restored on exit
    threading.Thread(target=agents_loop, daemon=True).start()   # background-agent discovery
    threading.Thread(target=hist_loop, daemon=True).start()     # all-time usage
    threading.Thread(target=ops_loop, daemon=True).start()      # per-session tool counts
    try:
        last_scan=0; raw=per_model=window=None; nfiles=0
        while True:
            if time.time()-last_scan>8 or raw is None:
                raw,per_model,window,nfiles=scan(); last_scan=time.time()
            render(raw,per_model,window,nfiles,SESSION_LIMIT)
            time.sleep(REFRESH)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW+ALT_OFF)   # leave alt screen, restore prior terminal

if __name__=="__main__":
    main()
