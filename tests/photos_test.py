#!/usr/bin/env python3
"""Photo lifecycle tests: delete, temp sweep, retention, orphans, downscale.

Run: python3 tests/photos_test.py
Exit 0 = every check passed. Downscale checks are skipped if Pillow is absent
(the production host has it; the app intentionally treats it as optional).
"""
import json
import os
import sys
import tempfile
import time
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="macrolog-photos-")
os.environ["MACROLOG_DATA"] = WORK
sys.path.insert(0, ROOT)

import server  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    (PASS if ok else FAIL).append(name)


os.makedirs(server.PHOTO_DIR, exist_ok=True)


def make_file(name, age_hours=0, size=64):
    path = os.path.join(server.PHOTO_DIR, name)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    t = time.time() - age_hours * 3600
    os.utime(path, (t, t))
    return path


def add_meal(owner, day, photo_path):
    return server.db.add_meal(
        date=day, ts=int(time.time()) - 400 * 86400, kind="other",
        description="photo test", calories=100, protein=1, carbs=1, fat=1,
        confidence=0.5, photo_path=photo_path, source="photo",
        provider="", model="", raw_json="", owner=owner)


# ------------------------------------------------------- multi-photo delete
p1 = make_file("one.jpg")
p2 = make_file("two.jpg")
removed = server._delete_meal_photos(json.dumps([p1, p2]))
check("meal delete removes every photo in a JSON list",
      removed == 2 and not os.path.exists(p1) and not os.path.exists(p2),
      f"removed={removed}")

# ----------------------------------------------------------- temp sweeping
old_tmp = make_file("tmp-old.jpg", age_hours=7)
new_tmp = make_file("tmp-new.jpg", age_hours=1)
n = server.sweep_temp_photos()
check("temp sweep removes only expired tmp uploads",
      n >= 1 and not os.path.exists(old_tmp) and os.path.exists(new_tmp),
      f"removed={n}")

# ------------------------------------------------------- retention pruning
expired = make_file("expired.jpg")
old_day = (date.today() - timedelta(days=400)).isoformat()
expired_id = add_meal("tester", old_day, expired)
server.save_user_settings("tester", {"photo_retention_months": 12})
res = server.sweep_photos()
row = server.db.get_meal(expired_id, "tester")
check("retention deletes an expired photo",
      res["photos_removed"] >= 1 and not os.path.exists(expired), str(res))
check("retention keeps the meal and its macros",
      row is not None and row["calories"] == 100 and row["photo_path"] == "",
      str(dict(row)) if row else "meal missing")

# Keep-forever must override the default.
forever = make_file("forever.jpg")
add_meal("keeper", old_day, forever)
server.save_user_settings("keeper", {"photo_retention_months": 0})
server.sweep_photos()
check("retention=0 keeps photos forever", os.path.exists(forever))

# --------------------------------------------------------- orphan pruning
ref = make_file("referenced.jpg", age_hours=2)
orphan = make_file("orphan.jpg", age_hours=2)
fresh = make_file("fresh-orphan.jpg", age_hours=0)
add_meal("keeper", date.today().isoformat(), ref)
removed, freed = server._prune_orphan_photos(older_than_minutes=60)
check("orphan prune removes old unreferenced permanent files",
      removed >= 1 and not os.path.exists(orphan), f"removed={removed}")
check("orphan prune keeps referenced and in-flight files",
      os.path.exists(ref) and os.path.exists(fresh))

# ------------------------------------------------------------ downscaling
try:
    from PIL import Image
except Exception:  # Pillow is optional by design
    print("  skip  downscale checks (Pillow not installed)")
else:
    big = os.path.join(server.PHOTO_DIR, "big.jpg")
    Image.effect_noise((3000, 2000), 100).convert("RGB").save(
        big, "JPEG", quality=95)
    before = os.path.getsize(big)
    out = server._downscale_photo(big)
    with Image.open(out) as im:
        dims = im.size
    check("downscale keeps the stored filename stable", out == big, out)
    check("downscale caps the long edge at 1600", max(dims) == 1600, str(dims))
    check("downscale shrinks the file", os.path.getsize(big) < before,
          f"{before} -> {os.path.getsize(big)}")
    # A second pass must not recompress an already-small file forever.
    size_after = os.path.getsize(big)
    server._downscale_photo(big)
    check("downscale is idempotent for web-sized files",
          os.path.getsize(big) == size_after)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failed:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
