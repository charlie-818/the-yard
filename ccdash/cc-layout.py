#!/usr/bin/env python3
"""Build iTerm2 7-pane layout: three 2-row columns + one tall right column.

    +----+----+----+------+
    | 1  | 2  | 3  |      |
    +----+----+----+  7   |
    | 4  | 5  | 6  |      |
    +----+----+----+------+

Launches cc-dashboard.py in the tall right pane (7).

Requires:
  - iTerm2 Python API enabled: Prefs > General > Magic > Enable Python API
  - pip3 install iterm2

Run:  python3 ~/.claude/cc-layout.py
"""
import iterm2

DASH = "python3 ~/.claude/cc-dashboard.py ~/vaulto-protocol ~/vaulto-api ~/vaulto-tokenization"


async def main(connection):
    await iterm2.async_get_app(connection)      # wires Session.delegate (registry)
    window = await iterm2.Window.async_create(connection)
    tab = window.tabs[0]
    c1 = tab.sessions[0]                         # top-left -> pane 1

    # tall right column first, so it spans full height -> pane 7
    right = await c1.async_split_pane(vertical=True)

    # carve three left columns: [c1 | c2 | c3 | right]
    c2 = await c1.async_split_pane(vertical=True)
    c3 = await c2.async_split_pane(vertical=True)

    # split each left column into two rows
    await c1.async_split_pane(vertical=False)   # pane 1 / pane 4
    await c2.async_split_pane(vertical=False)   # pane 2 / pane 5
    await c3.async_split_pane(vertical=False)   # pane 3 / pane 6

    # run dashboard in the tall right pane (do this first so a resize
    # failure below can never block the launch)
    await right.async_send_text(DASH + "\n")
    await right.async_activate()

    # equalize column widths: best-effort. iTerm2 rejects ("IMPOSSIBLE")
    # grid sizes that don't fit the tiled tab, so swallow per-pane failures.
    cols = [c1, c2, c3, right]
    total = sum(s.grid_size.width for s in cols)
    target = total // len(cols)
    for s in cols:
        try:
            await s.async_set_grid_size(iterm2.util.Size(target, s.grid_size.height))
        except iterm2.rpc.RPCException:
            pass


iterm2.run_until_complete(main)
