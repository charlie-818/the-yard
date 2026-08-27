# CC Dispatch

Phone-first control panel for a fleet of live Claude Code iTerm2 panes. Reads and
drives sessions that already exist — nothing is restarted.

| File | Role |
|---|---|
| `server.py` | aiohttp server + WebSocket fleet feed; drives iTerm2 via its Python API. |
| `auth.py` | WebAuthn passkey registration + session gating. |
| `icon.py` | Generates the PWA home-screen icons (the clawd mark). |
| `static/` | The single-page web app + icons. |

## Run

```bash
../install.sh dispatch          # from repo root: venv + deps + icons
./.venv/bin/python server.py    # serves 127.0.0.1:8788
tailscale serve https / http://127.0.0.1:8788   # reach it from your phone
```

Loopback-only, passkey-gated, audit-logged. See the
[root README](../README.md#security-model) for the full security model.
