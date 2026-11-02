"""Static icon wiring checks.

The iPhone home-screen icon is easy to silently break: iOS does not use the
manifest's SVG icon, it wants a 180x180 opaque `apple-touch-icon`. These checks
run without a server and validate the HTML/manifest links and the PNGs they
point at.
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


html = (STATIC / "index.html").read_text(encoding="utf-8")
manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))

# iOS: an explicit 180x180 apple-touch-icon link, not just a manifest entry.
m = re.search(r'<link[^>]+rel="apple-touch-icon"[^>]*>', html)
check("index.html has an apple-touch-icon link", bool(m), "no <link rel=apple-touch-icon>")
href = re.search(r'href="([^"]+)"', m.group(0)).group(1) if m else ""
check("apple-touch-icon points at a local PNG", href.startswith("/") and href.endswith(".png"), href)
touch = STATIC / href.lstrip("/")
check("apple-touch-icon file exists", touch.is_file(), str(touch))
if touch.is_file():
    im = Image.open(touch)
    check("apple-touch-icon is 180x180", im.size == (180, 180), str(im.size))
    # iOS fills transparent pixels with black; the icon must be opaque.
    check("apple-touch-icon is opaque", im.mode in ("RGB", "L"), im.mode)

# Manifest: PNG icons in the sizes Android/Chrome expect, plus a maskable one.
icons = manifest.get("icons") or []
png_icons = [i for i in icons if i.get("type") == "image/png"]
check("manifest lists PNG icons", bool(png_icons), json.dumps(icons))
check("manifest has a 192x192 icon", any(i.get("sizes") == "192x192" for i in png_icons))
check("manifest has a 512x512 icon", any(i.get("sizes") == "512x512" for i in png_icons))
check("manifest has a maskable icon", any("maskable" in str(i.get("purpose", "")) for i in png_icons))
for icon in png_icons:
    path = STATIC / str(icon.get("src", "")).lstrip("/")
    ok = path.is_file()
    check(f"manifest icon {icon.get('src')} exists", ok, str(path))
    if ok and "x" in str(icon.get("sizes", "")):
        want = tuple(int(n) for n in str(icon["sizes"]).split("x"))
        got = Image.open(path).size
        check(f"{icon.get('src')} is {icon['sizes']}", got == want, str(got))

# SVG favicon stays as the browser-tab asset.
svg = STATIC / "icon.svg"
check("icon.svg exists", svg.is_file(), str(svg))
if svg.is_file():
    try:
        ET.parse(svg)
        check("icon.svg is valid XML", True)
    except ET.ParseError as exc:
        check("icon.svg is valid XML", False, str(exc))

# The service worker should ship a bumped shell so clients pick up the new head.
sw = (STATIC / "sw.js").read_text(encoding="utf-8")
check("service worker cache is versioned", bool(re.search(r"macrolog-shell-v\d+", sw)))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
