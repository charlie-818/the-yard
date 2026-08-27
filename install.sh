#!/usr/bin/env bash
# The Yard — installer for ccdash (fleet dashboard) and CC Dispatch (phone control).
#
#   ./install.sh            install both
#   ./install.sh ccdash     dashboard + statusline only
#   ./install.sh dispatch   phone-control server only
#
# Idempotent. Backs up ~/.claude/settings.json before touching it.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
CLAUDE="$HOME/.claude"
BLUE='\033[36m'; GREEN='\033[32m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
say() { printf "${BLUE}▸${RESET} %s\n" "$*"; }
ok()  { printf "${GREEN}✓${RESET} %s\n" "$*"; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1"; exit 1; }; }

install_ccdash() {
  say "Installing ccdash → ${CLAUDE}"
  need python3; need jq
  mkdir -p "$CLAUDE"
  for f in cc-dashboard.py cc_history.py cc-dash.sh statusline.sh cc-layout.py; do
    cp "$REPO/ccdash/$f" "$CLAUDE/$f"
    chmod +x "$CLAUDE/$f"
  done
  ok "copied dashboard + statusline scripts"

  # Wire the statusline into settings.json without clobbering existing config.
  local S="$CLAUDE/settings.json"
  [ -f "$S" ] || echo '{}' > "$S"
  cp "$S" "$S.bak.$(date +%s 2>/dev/null || echo backup)" 2>/dev/null || true
  python3 - "$S" <<'PY'
import json, sys
p = sys.argv[1]
try:    cfg = json.load(open(p))
except Exception: cfg = {}
cur = cfg.get("statusLine")
want = {"type": "command", "command": "~/.claude/statusline.sh", "padding": 0, "refreshInterval": 2}
if cur != want:
    cfg["statusLine"] = want
    json.dump(cfg, open(p, "w"), indent=2)
    print("  statusLine wired into settings.json (backup written)")
else:
    print("  statusLine already configured")
PY
  ok "ccdash ready — open it with:  bash ~/.claude/cc-dash.sh   (or: python3 ~/.claude/cc-dashboard.py)"
}

install_dispatch() {
  say "Installing CC Dispatch → ${REPO}/dispatch"
  need python3
  cd "$REPO/dispatch"
  [ -d .venv ] || python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
  ok "dependencies installed"
  ./.venv/bin/python icon.py >/dev/null 2>&1 && ok "PWA icons generated" || true
  printf "${BOLD}Run it:${RESET}  cd dispatch && ./.venv/bin/python server.py\n"
  printf "${DIM}Then expose it to your phone over your tailnet:  tailscale serve https / http://127.0.0.1:8788${RESET}\n"
}

case "${1:-all}" in
  ccdash)   install_ccdash ;;
  dispatch) install_dispatch ;;
  all)      install_ccdash; echo; install_dispatch ;;
  *) echo "usage: ./install.sh [all|ccdash|dispatch]"; exit 1 ;;
esac

echo
ok "Done. Welcome to The Yard."
