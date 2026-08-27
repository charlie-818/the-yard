# ccdash

Live fleet dashboard for Claude Code — a dependency-free TUI. Installed into
`~/.claude` by the repo-root `./install.sh ccdash`.

| File | Role |
|---|---|
| `cc-dashboard.py` | The dashboard itself. `python3 ~/.claude/cc-dashboard.py`. |
| `cc-dash.sh` | Opens the dashboard in a fresh iTerm window, scoped to the window you launch from. |
| `cc_history.py` | Rolling / all-time usage from cached transcripts. |
| `statusline.sh` | Claude Code statusline hook. Renders the context bar **and** drops the per-session JSON feed both tools read. |
| `cc-layout.py` | Small layout helper. |

See the [root README](../README.md) for the full picture and configuration.
