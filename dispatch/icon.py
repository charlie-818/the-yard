"""Home-screen icon: the yard's clawd head, orange on the app's grey surface.

Drawn here rather than shipped as a binary so the mark stays in sync with the
one in the header — same geometry, same two colours, one place to change it.
"""
from PIL import Image, ImageDraw

BG = (34, 38, 45)          # --panel2, the app's chrome grey
FG = (215, 119, 87)        # the clawd terracotta
SIZES = (180, 192, 512)    # apple-touch, android, maskable


def render(size, maskable=False):
    S = 4                                   # supersample, then downscale
    im = Image.new("RGBA", (size * S, size * S), BG + (255,))
    d = ImageDraw.Draw(im)
    n = size * S
    # a maskable icon gets squeezed by the launcher's mask, so it needs padding
    pad = n * (0.28 if maskable else 0.19)
    w = n - pad * 2
    head_top = pad + w * 0.20
    head_h = w * 0.66
    ear_w, ear_h = w * 0.12, w * 0.24

    for x in (pad + w * 0.13, pad + w * 0.75):     # ears
        d.rounded_rectangle([x, pad, x + ear_w, pad + ear_h],
                            radius=ear_w / 2, fill=FG)
    d.rounded_rectangle([pad, head_top, pad + w, head_top + head_h],
                        radius=w * 0.22, fill=FG)
    eye_w, eye_h = w * 0.15, w * 0.21              # eyes are notches of the bg
    eye_y = head_top + head_h * 0.30
    for x in (pad + w * 0.22, pad + w * 0.63):
        d.rounded_rectangle([x, eye_y, x + eye_w, eye_y + eye_h],
                            radius=eye_w / 2.4, fill=BG)
    return im.resize((size, size), Image.LANCZOS)


def write_all(out_dir):
    out_dir.mkdir(exist_ok=True)
    made = []
    for s in SIZES:
        p = out_dir / f"icon-{s}.png"
        render(s).save(p)
        made.append(p)
    p = out_dir / "icon-maskable-512.png"
    render(512, maskable=True).save(p)
    made.append(p)
    return made


if __name__ == "__main__":
    import pathlib
    print(*write_all(pathlib.Path(__file__).parent / "static" / "icons"), sep="\n")
