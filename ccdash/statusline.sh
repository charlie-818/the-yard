#!/usr/bin/env bash

# Read JSON state from stdin
input=$(cat)

# Debug: log raw JSON to file
echo "$input" > /tmp/claude-statusline-debug.json

# Per-session dump for the fleet dashboard (cc-dashboard.py reads /tmp/cc-status/*.json)
SID=$(echo "$input" | jq -r '.session_id // ""')
if [ -n "$SID" ] && [ "$SID" != "null" ]; then
  mkdir -p /tmp/cc-status
  # Tag the dump with this pane's iTerm window: w<N> from ITERM_SESSION_ID like
  # "w2t0p5:GUID" (strip from the first 't'). Lets cc-dashboard.py scope FLEET to
  # one iTerm window. Empty when not under iTerm — dashboard then shows all.
  IWIN="${ITERM_SESSION_ID%%t*}"
  IPANE="$ITERM_SESSION_ID"   # full id incl. GUID — stable & unique per pane. The wNtNpN
                              # coordinate reflows/reuses when panes close, which collided
                              # two live sessions onto one key and flickered the fleet row.
  # atomic: write temp then rename, so the dashboard never reads a half-written file
  TMP="/tmp/cc-status/$SID.json.tmp"
  echo "$input" | jq --arg iw "$IWIN" --arg ip "$IPANE" '. + {iterm_window: $iw, iterm_pane: $ip}' > "$TMP" 2>/dev/null || echo "$input" > "$TMP"
  mv -f "$TMP" "/tmp/cc-status/$SID.json"
fi

# Extract fields from Claude's JSON
PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)
DIR=$(echo "$input" | jq -r '.workspace.current_dir // "~"')
COST=$(echo "$input" | jq -r '.cost.total_cost_usd // 0')
MODEL=$(echo "$input" | jq -r '.model.display_name // ""')
EFFORT=$(echo "$input" | jq -r '.effort.level // ""')
SESSION_NAME=$(echo "$input" | jq -r '.session_name // ""')
TRANSCRIPT=$(echo "$input" | jq -r '.transcript_path // ""')

# Last user request from transcript (for recap line)
LAST_MSG=""
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
  LAST_MSG=$(tail -300 "$TRANSCRIPT" 2>/dev/null | jq -r '
    select(.type=="user" and (.isMeta // false | not)) | .message.content |
    if type=="string" then . else (map(select(.type=="text") | .text) | join(" ")) end
  ' 2>/dev/null | grep -v '^\s*$' | grep -vE 'system-reminder|task-notification|<channel' | tail -1 | tr '\n\t' '  ')
fi
LINES_ADDED=$(echo "$input" | jq -r '.cost.total_lines_added // 0')
LINES_REMOVED=$(echo "$input" | jq -r '.cost.total_lines_removed // 0')
TOKENS=$(echo "$input" | jq -r '(.context_window.total_input_tokens // 0) + (.context_window.total_output_tokens // 0)')

# Terminal width. A hook has no controlling terminal, so COLUMNS is unset and `tput
# cols` reports nothing usable. Walk up the process tree to the ancestor that owns a
# real pty (same trick as tabcolor.sh) and ask that device for its size.
term_cols() {
  [ -n "$COLUMNS" ] && { echo "$COLUMNS"; return; }
  local pid=$$ t dev="" sz
  while [ "$pid" -gt 1 ]; do
    t=$(ps -o tty= -p "$pid" 2>/dev/null | tr -d ' ')
    case "$t" in ttys*) dev="/dev/$t"; break ;; esac
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    [ -z "$pid" ] && break
  done
  if [ -n "$dev" ] && [ -r "$dev" ]; then
    sz=$(stty size < "$dev" 2>/dev/null | awk '{print $2}')
    [ -n "$sz" ] && [ "$sz" -gt 20 ] 2>/dev/null && { echo "$sz"; return; }
  fi
  echo 80
}
cols=$(term_cols)

[ "$PCT" -lt 0 ] && PCT=0
[ "$PCT" -gt 100 ] && PCT=100

# ── layout: MODEL on the left · bar in the middle · % on the right ──────────
# The whole line is sized to the terminal so the percentage ends exactly on the last
# usable column. CC_STATUS_PAD trims columns off the right: Claude Code renders the
# statusline inside its own chrome, which is narrower than the raw pty, and at a
# 2-column trim the percentage wrapped. Raise it if the % is still cut off, lower it
# if the line stops short of the edge.
PAD=${CC_STATUS_PAD:-6}

# Model: the feed sometimes gives a raw id ("claude-opus-5") and sometimes a display
# name ("Opus 4.8"). Normalise the id form to the readable one.
case "$MODEL" in
  claude-*)
    MODEL=$(echo "$MODEL" | sed -E 's/^claude-//; s/-([0-9]+)-([0-9]+)$/ \1.\2/; s/-([0-9]+)$/ \1/')
    MODEL="$(tr '[:lower:]' '[:upper:]' <<< "${MODEL:0:1}")${MODEL:1}" ;;
esac
[ -z "$MODEL" ] && MODEL="claude"

SUFFIX="${PCT}%"
WIDTH=$(( cols - PAD ))
# one space either side of the bar is the minimum that keeps the three parts distinct
BAR_WIDTH=$(( WIDTH - ${#MODEL} - ${#SUFFIX} - 2 ))
[ "$BAR_WIDTH" -lt 8 ] && BAR_WIDTH=8
FILLED=$((PCT * BAR_WIDTH / 100))
[ "$FILLED" -gt "$BAR_WIDTH" ] && FILLED=$BAR_WIDTH
EMPTY=$((BAR_WIDTH - FILLED))
printf -v FILL "%${FILLED}s"
printf -v PAD_E "%${EMPTY}s"
BAR="${FILL// /█}${PAD_E// /░}"

# Colors
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
DIM='\033[2m'
WHITE='\033[97m'
RESET='\033[0m'

# Context bar color
if [ "$PCT" -ge 90 ]; then BAR_COLOR="$RED"
elif [ "$PCT" -ge 70 ]; then BAR_COLOR="$YELLOW"
else BAR_COLOR="$GREEN"; fi

# Any slack left over (a short model name on a wide window) goes between the model and
# the bar, so the bar and the percentage stay pinned to the right edge.
GAP=$(( WIDTH - ${#MODEL} - ${#SUFFIX} - BAR_WIDTH - 1 ))
[ "$GAP" -lt 1 ] && GAP=1
printf -v LEAD "%${GAP}s"

printf "%b%s%b%s%b%s%b %b%s%b\n" \
  "$WHITE" "$MODEL" "$RESET" "$LEAD" \
  "$BAR_COLOR" "$BAR" "$RESET" \
  "$WHITE" "$SUFFIX" "$RESET"
