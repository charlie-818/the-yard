#!/bin/bash
# Open the Claude Code dashboard in a fresh iTerm window.
# The dashboard scopes FLEET to one iTerm window. It opens in a NEW window (its own
# id), so capture the window you launch FROM (w<N> from ITERM_SESSION_ID "w2t0p5:GUID")
# and pass it through, so the dashboard reports the sessions in your working window.
IWIN="${ITERM_SESSION_ID%%t*}"
ENVPFX=""; [ -n "$IWIN" ] && ENVPFX="env CC_ITERM_WINDOW='$IWIN' "
osascript <<APPLESCRIPT
tell application "iTerm"
  create window with default profile
  tell current session of current window
    set name to "CC Info"
    write text "clear; exec ${ENVPFX}python3 ~/.claude/cc-dashboard.py"
  end tell
end tell
APPLESCRIPT
