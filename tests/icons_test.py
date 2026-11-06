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

# First frame: the shell must paint dark from the HTML itself. Without this,
# a cold iOS start shows the browser's white canvas (or its white launch
# screen) until app.css arrives, which is the "white screen until I tap" bug.
check("index.html has an inline dark first-frame rule",
      bool(re.search(r"<style>[^<]*html\s*\{\s*background:\s*#0f1115", html)),
      "no inline <style> painting #0f1115 on <html>")
check("index.html declares color-scheme dark", 'name="color-scheme" content="dark"' in html)
check("app.js is preloaded", bool(re.search(r'<link[^>]+rel="preload"[^>]+href="/app\.js"', html)))

# iOS launch images: a home-screen app with no matching startup image paints a
# plain white screen for the whole launch. Every declared link must resolve to
# a PNG of exactly the device pixel size its media query describes, or iOS
# silently ignores it.
splash = re.findall(r'<link[^>]+rel="apple-touch-startup-image"[^>]*>', html)
check("index.html declares iOS launch images", bool(splash), "no apple-touch-startup-image")
seen = set()
for tag in splash:
    href = re.search(r'href="([^"]+)"', tag).group(1)
    media = re.search(r'media="([^"]+)"', tag)
    if not media:
        check(f"{href} has a media query", False)
        continue
    mw = re.search(r"device-width:\s*(\d+)px", media.group(1))
    mh = re.search(r"device-height:\s*(\d+)px", media.group(1))
    md = re.search(r"-webkit-device-pixel-ratio:\s*(\d+)", media.group(1))
    path = STATIC / href.lstrip("/")
    ok = path.is_file()
    check(f"launch image {href} exists", ok, str(path))
    if ok and mw and mh and md:
        want = (int(mw.group(1)) * int(md.group(1)), int(mh.group(1)) * int(md.group(1)))
        got = Image.open(path).size
        check(f"launch image {href} is {want[0]}x{want[1]}", got == want, str(got))
        check(f"launch image {href} is opaque", Image.open(path).mode in ("RGB", "P"), "")
    seen.add((mw.group(1), mh.group(1), md.group(1)) if mw and mh and md else None)
check("launch images cover the main iPhone sizes",
      {("390", "844", "3"), ("393", "852", "3"), ("430", "932", "3")} <= seen, str(sorted(x for x in seen if x)))

# The service worker should ship a bumped shell so clients pick up the new head.
sw = (STATIC / "sw.js").read_text(encoding="utf-8")
check("service worker cache is versioned", bool(re.search(r"macrolog-shell-v\d+", sw)))
# `cache.addAll(['/', ...])` aborts the whole install when `/` is a 302 to the
# login page — which is the normal state of a home-screen web app on iOS, whose
# cookie jar is separate. Chrome then drops the registration entirely, so the
# app is left with no worker at all. Entries must be cached individually.
check("service worker install survives a failed shell entry", "addAll(" not in sw,
      "install uses addAll(); one redirect can abort it")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
