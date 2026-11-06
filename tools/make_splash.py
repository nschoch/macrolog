"""Generate iOS launch images (apple-touch-startup-image) for MacroLog.

Without these, iOS paints its own plain white launch screen while the web
content process starts; on a cold launch that white frame can sit there for
seconds (and on some iOS builds only clears when the user taps). These images
are dark and branded, so the launch frame is never the browser's white sheet.

Run with a Python that has Pillow:
    python3 tools/make_splash.py            # writes static/splash/*.png
The media queries printed at the end are pasted into index.html.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

STATIC = Path(__file__).resolve().parent.parent / "static"
OUT = STATIC / "splash"
BG = (15, 17, 21)          # --bg, matches the app shell
FG = (232, 234, 240)       # --text
DIM = (139, 147, 165)      # --muted
ACCENT = (74, 222, 128)    # --accent

# (css width, css height, dpr) — the exact point sizes iOS reports per device.
DEVICES = [
    (375, 667, 2),    # iPhone 8 / SE2 / SE3
    (375, 812, 3),    # X / XS / 11 Pro / 12 mini / 13 mini
    (414, 896, 2),    # XR / 11
    (414, 896, 3),    # XS Max / 11 Pro Max
    (390, 844, 3),    # 12 / 13 / 14
    (428, 926, 3),    # 12 & 13 Pro Max / 14 Plus
    (393, 852, 3),    # 14 Pro / 15 / 15 Pro / 16
    (402, 874, 3),    # 16 Pro
    (430, 932, 3),    # 14 Pro Max / 15 Plus / 15 Pro Max
    (440, 956, 3),    # 16 Pro Max
    (810, 1080, 2),   # iPad 10.2
    (834, 1194, 2),   # iPad Pro 11
    (1024, 1366, 2),  # iPad Pro 12.9
]

FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    "/System/Library/Fonts/Geneva.ttf",
]


def load_font(size):
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(w, h, dpr):
    px_w, px_h = w * dpr, h * dpr
    img = Image.new("RGB", (px_w, px_h), BG)
    d = ImageDraw.Draw(img)

    # Flat background on purpose: a gradient bands badly on OLED and turns a
    # 15 KB launch image into a 200 KB one. The mark alone is enough.
    icon = Image.open(STATIC / "icon-512.png").convert("RGBA")
    side = int(px_w * 0.36)
    icon = icon.resize((side, side), Image.LANCZOS)
    ix = (px_w - side) // 2
    iy = int(px_h * 0.5 - side * 0.5 - px_h * 0.045)
    # The icon art carries its own radial background, so pasting the square
    # leaves a visible seam. A squircle mask makes it read as the app icon.
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, side - 1, side - 1], radius=int(side * 0.225), fill=255)
    img.paste(icon, (ix, iy), mask)

    font = load_font(int(px_w * 0.082))
    text = "MacroLog"
    tw = d.textbbox((0, 0), text, font=font)
    d.text(((px_w - (tw[2] - tw[0])) / 2 - tw[0], iy + side + int(px_h * 0.045)),
           text, font=font, fill=FG)

    sub = load_font(int(px_w * 0.038))
    label = "food log"
    sw = d.textbbox((0, 0), label, font=sub)
    d.text(((px_w - (sw[2] - sw[0])) / 2 - sw[0], iy + side + int(px_h * 0.115)),
           label, font=sub, fill=DIM)

    # A thin accent rule keeps the frame visually tied to the icon.
    rule_w = int(px_w * 0.12)
    ry = iy + side + int(px_h * 0.165)
    d.rectangle([(px_w - rule_w) // 2, ry, (px_w + rule_w) // 2, ry + max(2, dpr)], fill=ACCENT)
    return img


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    lines = []
    for w, h, dpr in DEVICES:
        img = render(w, h, dpr)
        name = f"{w * dpr}x{h * dpr}.png"
        # 64-colour palette: the launch image is a gradient icon on flat
        # dark, so quantization is invisible and cuts each file ~70%.
        img.convert("P", palette=Image.ADAPTIVE, colors=64).save(
            OUT / name, "PNG", optimize=True)
        kb = (OUT / name).stat().st_size / 1024
        lines.append(
            f'<link rel="apple-touch-startup-image" href="/splash/{name}"\n'
            f'      media="(device-width: {w}px) and (device-height: {h}px) '
            f'and (-webkit-device-pixel-ratio: {dpr})">'
        )
        print(f"wrote splash/{name}  {kb:.0f} KB")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
