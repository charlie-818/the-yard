#!/usr/bin/env python3
"""Render the GitHub header banner for The Yard.

Same clawd mark and two-colour palette as dispatch/icon.py, so the header and the
home-screen icon stay one brand. Pure Pillow — no SVG toolchain needed.

    python3 assets/make_banner.py        # writes assets/banner.png
"""
import pathlib
from PIL import Image, ImageDraw, ImageFont

# ── palette (from the app) ──────────────────────────────────────────────────
BG       = (18, 21, 26)      # near --bg
PANEL    = (26, 29, 35)      # --panel
LINE     = (51, 57, 68)      # --line
TERRA    = (215, 119, 87)    # clawd terracotta  (--c/#d77757)
TERRA_D  = (176, 92, 64)
FG       = (242, 245, 249)   # --fg
DIM      = (168, 178, 191)   # --dim
DIMMER   = (120, 130, 143)   # --dimmer
GREEN    = (61, 220, 92)     # --green

S = 2                        # supersample factor → downscale for crisp edges
W, H = 1280 * S, 400 * S

FONTS = [
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]
def font(path, size):
    return ImageFont.truetype(path, size)
SF   = lambda s: font(FONTS[0], s)
MONO = lambda s: font(FONTS[1], s)


def clawd(d, x, y, w):
    """The clawd head — terracotta on transparent, matching icon.py geometry."""
    head_top = y + w * 0.20
    head_h   = w * 0.66
    ear_w, ear_h = w * 0.12, w * 0.24
    for ex in (x + w * 0.13, x + w * 0.75):                     # ears
        d.rounded_rectangle([ex, y, ex + ear_w, y + ear_h], radius=ear_w / 2, fill=TERRA)
    d.rounded_rectangle([x, head_top, x + w, head_top + head_h],
                        radius=w * 0.22, fill=TERRA)
    eye_w, eye_h = w * 0.15, w * 0.21                           # eyes = bg notches
    eye_y = head_top + head_h * 0.30
    for ex in (x + w * 0.22, x + w * 0.63):
        d.rounded_rectangle([ex, eye_y, ex + eye_w, eye_y + eye_h],
                            radius=eye_w / 2.4, fill=BG)


def pill(d, x, y, label, accent):
    f = MONO(20 * S)
    padx, pady = 14 * S, 8 * S
    tw = d.textlength(label, font=f)
    box = [x, y, x + tw + padx * 2, y + 20 * S + pady * 2]
    d.rounded_rectangle(box, radius=10 * S, fill=PANEL, outline=accent, width=max(1, S))
    d.text((x + padx, y + pady - 2 * S), label, font=f, fill=DIM)
    return box[2]


def main():
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    # subtle vertical panel gradient for depth
    for i in range(H):
        t = i / H
        c = tuple(int(BG[k] + (PANEL[k] - BG[k]) * (t * 0.6)) for k in range(3))
        d.line([(0, i), (W, i)], fill=c)
    # thin terracotta rule down the left margin — the app's accent edge
    d.rectangle([0, 0, 6 * S, H], fill=TERRA)

    # mark
    mark_w = 150 * S
    clawd(d, 70 * S, (H - mark_w) // 2, mark_w)

    # wordmark
    tx = 265 * S
    d.text((tx, 118 * S), "THE YARD", font=SF(92 * S), fill=FG)
    # tagline
    d.text((tx + 4 * S, 214 * S),
           "Command your fleet of Claude Code agents.",
           font=MONO(27 * S), fill=DIM)

    # component pills
    py = 268 * S
    x2 = pill(d, tx + 4 * S, py, "dispatch · phone control", TERRA)
    pill(d, x2 + 16 * S, py, "ccdash · live fleet TUI", GREEN)

    # faux statusline in the top-right — a brand callback to the real one
    bar_x, bar_y, bar_w = W - 470 * S, 150 * S, 360 * S
    d.text((bar_x, bar_y - 30 * S), "OPUS 4.8", font=MONO(20 * S), fill=FG)
    filled = int(bar_w * 0.62)
    d.rectangle([bar_x, bar_y, bar_x + filled, bar_y + 12 * S], fill=GREEN)
    d.rectangle([bar_x + filled, bar_y, bar_x + bar_w, bar_y + 12 * S], fill=LINE)
    d.text((bar_x + bar_w + 12 * S, bar_y - 6 * S), "62%", font=MONO(20 * S), fill=DIMMER)

    out = pathlib.Path(__file__).parent / "banner.png"
    im.resize((W // S, H // S), Image.LANCZOS).save(out)
    print(out)


if __name__ == "__main__":
    main()
