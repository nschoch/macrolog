"""End-to-end API tests against a running MacroLog server.

Usage: python3 tests/api_test.py http://127.0.0.1:8790
Exits non-zero on any failure. Uses a throwaway DB via MACROLOG_DATA in the
caller's environment; deletes nothing you care about.
"""
import json
import os
import threading
import time
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def req(path, method="GET", body=None, headers=None, raw_body=None):
    url = BASE + path
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=40) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        payload = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(payload)
        except ValueError:
            return e.code, {"error": payload[:200]}


def multipart(path, filename, filebytes):
    boundary = "----macrologtest"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + filebytes + f"\r\n--{boundary}--\r\n".encode()
    return req(path, "POST", raw_body=body,
               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


# Pin the server to the offline stub provider before anything else: a real
# vision key in the server's .env must NOT make this suite hit live APIs
# (slow, nondeterministic, costs tokens). Providers with no api_key and no
# server-side env key are skipped by build_chain(); stub is always appended.
st, ss = req("/api/settings", "PUT", {"providers": [{"kind": "stub"}]})
assert st == 200, f"could not pin stub provider: {st} {ss}"

print(f"\nMacroLog API tests -> {BASE} (stub-pinned)\n")

# 1. state
st, s = req("/api/state")
check("state returns 200", st == 200, str(st))
check("state has targets.calories", isinstance(s.get("targets", {}).get("calories"), int))
KNOWN_PROVIDERS = {"qwenplan", "groq", "gemini", "openai", "openrouter",
                   "custom", "ollama", "stub"}
check("active provider is known", s.get("active_provider") in KNOWN_PROVIDERS,
      str(s.get("active_provider")))

# 2. estimate from a photo
fake_jpeg = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300") + b"\x00" * 2000 + b"ffd9"
st, e = multipart("/api/estimate", "meal.jpg", fake_jpeg)
check("estimate 200", st == 200, str(e)[:120])
est = e.get("estimate", {})
check("estimate has calories>0", est.get("calories", 0) > 0, str(est))
check("estimate temp_photo present", bool(e.get("temp_photo")))
check("estimate items use generic names (no weights/parentheses)",
      est.get("items") and all(
          not any(ch.isdigit() for ch in i.get("name", "")) and "(" not in i.get("name", "")
          for i in est["items"]), str(est.get("items"))[:160])
check("estimate items carry a detail field",
      all("detail" in i for i in est.get("items", [])), str(est.get("items"))[:160])
temp_path = e.get("temp_photo")

# 3. save meal
st, m = req("/api/meals", "POST", {
    "description": "grilled chicken rice bowl", "calories": 620, "protein": 45,
    "carbs": 70, "fat": 18, "kind": "lunch", "source": "photo",
    "confidence": 0.7, "provider": e.get("provider", ""), "model": e.get("model", ""),
    "photo_path": temp_path, "raw": est})
check("save meal 201", st == 201, str(m)[:120])
meal_id = m.get("id")
check("consumed reflects meal", m.get("consumed", {}).get("calories") == 620)
if meal_id:
    st, s2 = req("/api/state")
    photos = [x["photo"] for x in s2["meals"] if x["id"] == meal_id]
    check("photo promoted to permanent store", bool(photos[0]) if photos else False)

# 3b. Copy a saved meal (including its photo) to today without moving the original.
today_copy = time.strftime("%Y-%m-%d")
st, cp = req(f"/api/meals/{meal_id}/copy", "POST", {"date": today_copy})
copy_id = cp.get("id")
check("copy meal -> 201", st == 201 and copy_id and copy_id != meal_id, str(cp)[:140])
st, orig = req(f"/api/meals/{meal_id}")
st, dup = req(f"/api/meals/{copy_id}")
check("copy keeps the description and totals",
      dup.get("description") == orig.get("description")
      and abs(float(dup.get("calories") or 0) - float(orig.get("calories") or 0)) < 0.1,
      str(dup)[:140])
check("copy lands on the requested date", dup.get("date") == today_copy,
      f"{dup.get('date')} vs {today_copy}")
check("copy has its own photo file", bool(dup.get("photos")) and bool(orig.get("photos"))
      and dup["photos"][0] != orig["photos"][0],
      f"{dup.get('photos')} vs {orig.get('photos')}")
req(f"/api/meals/{copy_id}", "DELETE")
st, orig2 = req(f"/api/meals/{meal_id}")
check("deleting the copy leaves the original intact",
      st == 200 and orig2.get("id") == meal_id and bool(orig2.get("photos")),
      str(orig2)[:120])

# 4. edit records a correction (the calibration training signal)
st, u = req(f"/api/meals/{meal_id}", "PATCH", {"calories": 700, "protein": 50})
check("patch meal 200", st == 200, str(u)[:120])
check("correction recorded", u.get("corrections_recorded", 0) >= 1, str(u.get("corrections_recorded")))

# 5. accuracy summary now populated
st, hist = req("/api/history?days=90")
acc = hist.get("accuracy", {}).get("calories", {})
check("accuracy tracks edits", acc.get("n", 0) >= 1, str(acc))

# 6. weights drive the adaptive loop
# Two subtleties that made this check flap on reruns against a reused database:
#  * values are keyed to days-before-today, so a second run upserts identical
#    numbers instead of sliding the series;
#  * unit="kg" is explicit because check 8b switches settings to imperial and
#    the server reads an unlabelled value in the stored unit (90 posted as 90 lb
#    became ~41 kg, halving the slope).
import datetime as _dtw
def _trend_weight(days_ago):
    return round(90 - (20 - days_ago) * 0.22, 2)
for _days_ago in range(20, -1, -1):
    _d = _dtw.date.today() - _dtw.timedelta(days=_days_ago)
    req("/api/weight", "POST", {"value": _trend_weight(_days_ago),
                                "date": _d.isoformat(), "unit": "kg"})
st, ar = req("/api/adapt", "POST", {})
tr = ar.get("trend", {})
check("weight trend reliable after 21 days", tr.get("reliable") is True, str(tr))
check("slope near -1.5 kg/wk", tr.get("slope_kg_per_week") and abs(tr["slope_kg_per_week"] + 1.5) < 0.5, str(tr.get("slope_kg_per_week")))
check("adapt produced target", isinstance(ar.get("target"), int), str(ar.get("reason")))

# 7. exercise credit
st, ex = req("/api/exercise", "POST", {"name": "run", "calories": 400})
check("exercise logged", st == 201 and ex.get("exercise", 0) >= 400 or ex.get("consumed"), str(ex)[:100])

# 8. settings round-trip
st, sv = req("/api/settings", "PUT", {"weekly_rate_kg": -0.75, "protein_g_per_kg": 2.0})
check("settings saved", st == 200 and sv.get("settings", {}).get("weekly_rate_kg") == -0.75, str(sv)[:120])

# 8a. Photo retention is a personal setting that round-trips.
st, pr = req("/api/settings", "PUT", {"photo_retention_months": 6})
check("photo retention setting saved",
      st == 200 and (pr.get("settings") or {}).get("photo_retention_months") == 6,
      str(pr.get("settings"))[:120])
st, bad = req("/api/settings", "PUT", {"photo_retention_months": "nope"})
check("invalid photo retention rejected without breaking settings",
      st == 200 and (bad.get("settings") or {}).get("photo_retention_months") == 6,
      str(bad.get("settings"))[:120])
req("/api/settings", "PUT", {"photo_retention_months": 12})

# 8b. REGRESSION: body-profile settings must PERSIST across a fresh state read
#     (guards the OIDC per-user migration bug where saved prefs reverted to defaults).
req("/api/settings", "PUT", {"units": "imperial", "age": 41, "height_cm": 183, "activity_factor": 1.35})
st, s2 = req("/api/state")
p = s2.get("settings", {})
check("units persist after save+reload", p.get("units") == "imperial", str(p.get("units")))
check("age persists after save+reload", p.get("age") == 41, str(p.get("age")))
check("height persists after save+reload", p.get("height_cm") == 183, str(p.get("height_cm")))
check("profile-set clears needs_setup nag", s2.get("needs_setup") is False, str(s2.get("needs_setup")))

# 8c. Per-user provider config: keys are write-only and survive reorder by id.
SECRET = "test-key-or-do-not-leak"
st, pv = req("/api/settings", "PUT", {"providers": [
    {"kind": "stub"},
    {"kind": "openrouter", "model": "qwen/qwen3.8-flash",
     "base_url": "https://openrouter.ai/api/v1", "enabled": False,
     "shared": True, "api_key": SECRET},
]})
provs = (pv.get("settings") or {}).get("providers") or []
check("provider settings save", st == 200 and len(provs) == 2, str(pv)[:140])
check("saved provider key is never returned", SECRET not in json.dumps(pv))
check("provider reports a write-only saved key",
      len(provs) == 2 and provs[1].get("has_key") is True
      and provs[1].get("key_source") == "saved", str(provs)[:180])
check("provider reports its share flag", provs[1].get("shared") is True,
      str(provs[1])[:140])
check("a user's own shared provider is not in shared_providers",
      (pv.get("settings") or {}).get("shared_providers") == [],
      str((pv.get("settings") or {}).get("shared_providers"))[:120])
st, sd_prov = req("/api/state")
check("state never carries the provider key", SECRET not in json.dumps(sd_prov))
check("state marks the personal provider as shared",
      any(p.get("shared") for p in sd_prov.get("providers") or []),
      str(sd_prov.get("providers"))[:160])
st, pv2 = req("/api/settings", "PUT", {"providers": [
    {"id": provs[1]["id"], "kind": "openrouter", "model": "qwen/qwen3.8-flash",
     "base_url": "https://openrouter.ai/api/v1", "enabled": False},
    {"id": provs[0]["id"], "kind": "stub", "enabled": True},
]})
provs2 = (pv2.get("settings") or {}).get("providers") or []
check("reordering preserves the saved key by stable id",
      len(provs2) == 2 and provs2[0].get("has_key") is True,
      str(provs2)[:180])
# Saving an unrelated body/goal field must not clobber the provider chain.
req("/api/settings", "PUT", {"weekly_rate_kg": -0.75})
st, pv3 = req("/api/settings")
provs3 = (pv3.get("settings") or {}).get("providers") or []
check("saving body settings keeps the provider chain",
      len(provs3) == 2 and provs3[0].get("has_key") is True, str(provs3)[:160])

# 9. export
st, xp = req("/api/export")
check("export has meals", st == 200 and len(xp.get("meals", [])) >= 1, str(st))
check("export carries no api keys", "api_key" not in json.dumps(xp).lower())
check("export does not carry the saved key value", SECRET not in json.dumps(xp))
check("export omits connections shared by other users",
      "shared_providers" not in json.dumps(xp))

# Restore the offline stub for the remaining estimate tests.
req("/api/settings", "PUT", {"providers": [{"kind": "stub"}]})

# 10. delete
st, d = req(f"/api/meals/{meal_id}", "DELETE")
check("delete meal 200", st == 200, str(d)[:100])

# 11. bad input rejected. NOTE: 0 kcal is now VALID (supplements) as long as the
# entry carries a description or components; only a truly empty one is refused.
st, _ = req("/api/meals", "POST", {"calories": 0})
check("empty calorie-less meal rejected", st == 400, str(st))
st, zc = req("/api/meals", "POST", {"calories": 0, "description": "creatine", "source": "manual"})
check("0 kcal with a description is accepted", st in (200, 201), str(zc)[:80])
if zc.get("id"):
    req(f"/api/meals/{zc['id']}", "DELETE")

# ---- components / items ---------------------------------------------------
# 12. create a meal WITH components; totals must equal sum of components
st, m = req("/api/meals", "POST", {
    "description": "Spaghetti dinner", "kind": "dinner",
    "date": today_str if False else None,
    "items": [
        {"name": "Pasta", "grams": 80, "calories": 280, "protein": 10, "carbs": 56, "fat": 1.5},
        {"name": "Meatballs", "grams": 120, "calories": 300, "protein": 18, "carbs": 6, "fat": 20},
        {"name": "Marinara", "grams": 120, "calories": 120, "protein": 3, "carbs": 12, "fat": 6}]})
check("create meal with components -> 201", st == 201, str(m)[:120])
comp_mid = m.get("id")
expect_kcal = 280 + 300 + 120
saved = next((x for x in m.get("meals", []) if x["id"] == comp_mid), {})
check("meal total == sum(components)", abs(saved.get("calories", 0) - expect_kcal) < 0.5,
      f"{saved.get('calories')} vs {expect_kcal}")
check("components persisted on read-back", len(saved.get("items", [])) == 3, str(len(saved.get("items", []))))

# 13. edit a component's grams -> total recomputes proportionally
st, u = req(f"/api/meals/{comp_mid}", "PATCH", {"items": [
    {"name": "Pasta", "grams": 160, "calories": 560, "protein": 20, "carbs": 112, "fat": 3},
    {"name": "Meatballs", "grams": 120, "calories": 300, "protein": 18, "carbs": 6, "fat": 20},
    {"name": "Marinara", "grams": 120, "calories": 120, "protein": 3, "carbs": 12, "fat": 6}]})
new_saved = next((x for x in u.get("meals", []) if x["id"] == comp_mid), {})
check("component edit recomputes total", abs(new_saved.get("calories", 0) - (560+300+120)) < 0.5,
      str(new_saved.get("calories")))
check("edited grams stored", any(i["grams"] == 160 for i in new_saved.get("items", [])), str(new_saved.get("items")))

# 14. date/time editing
st, u = req(f"/api/meals/{comp_mid}", "PATCH", {"date": "2026-01-15", "time": "07:30"})
check("date edit accepted", st == 200, str(u)[:80])
import datetime as _dt
moved = db_get_meal = None
# verify via history that the meal now sits on 2026-01-15
st, hist = req("/api/history?days=400")
on_day = [d for d in hist.get("intake", []) if d["date"] == "2026-01-15"]
check("meal moved to edited date", bool(on_day), str([d['date'] for d in hist.get('intake',[])][:5]))

# 15. delete the component meal; its items should cascade away (no orphans)
st, _ = req(f"/api/meals/{comp_mid}", "DELETE")
check("delete component meal -> 200", st == 200, str(st))

# 16. day browsing: GET /api/state?date=YYYY-MM-DD
import datetime as _dt2
yesterday = (_dt2.date.today() - _dt2.timedelta(days=1)).isoformat()
st, sd = req("/api/state")
check("state without date -> today", st == 200 and sd.get("date") == _dt.date.today().isoformat(), str(sd.get("date")))
st, sd = req(f"/api/state?date={yesterday}")
check("state ?date=yesterday accepted", st == 200 and sd.get("date") == yesterday, str(st))
st, sd = req("/api/state?date=not-a-date")
check("state bad date -> 400", st == 400, str(st))
# log a meal onto the viewed day, then confirm it appears there and not today
st, _ = req("/api/meals", "POST", {"description": "past-day test", "calories": 100,
                                   "protein": 5, "carbs": 10, "fat": 3, "date": yesterday})
mid_y = None
st, sd = req(f"/api/state?date={yesterday}")
mid_y = next((m["id"] for m in sd.get("meals", []) if m["description"] == "past-day test"), None)
check("meal visible on its date", mid_y is not None, str([m['description'] for m in sd.get('meals', [])]))
st, sd = req("/api/state")
check("past-day meal absent from today", all(m["id"] != mid_y for m in sd.get("meals", [])))

# 17. single-meal fetch (how past-day taps open the editor)
st, one = req(f"/api/meals/{mid_y}")
check("GET meal by id -> 200 + fields", st == 200 and one.get("id") == mid_y
      and one.get("date") == yesterday and "items" in one, str(st))
st, _ = req("/api/meals/999999")
check("GET unknown meal -> 404", st == 404, str(st))
st, _ = req(f"/api/meals/{mid_y}", "DELETE")

# 17b. model resolution precedence: QWENPLAN_MODEL must beat a stale model
# persisted in the settings DB, or editing .env would silently do nothing.
# Import the provider module by path so this works regardless of cwd.
import importlib.util as _ilu
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = _ilu.spec_from_file_location("_vision", os.path.join(_root, "app", "vision.py"))
_v = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_v)
def _resolved(env_model, cfg_model):
    if env_model is None:
        os.environ.pop("QWENPLAN_MODEL", None)
    else:
        os.environ["QWENPLAN_MODEL"] = env_model
    cfg = {"kind": "qwenplan"}
    if cfg_model is not None:
        cfg["model"] = cfg_model
    return _v.OpenAICompatProvider(cfg, "qwenplan").model
# Precedence: Settings-saved value > QWENPLAN_MODEL env > built-in default.
# A UI edit must win, otherwise saving a model in Settings appears to do nothing
# whenever the deploy sets the env var (the inverse trap also applies: env alone
# must still work for installs that never touch the UI).
check("stored (UI) config beats env var", _resolved("qwen3.8-flash", "qwen3.8-max") == "qwen3.8-max")
check("env used when no stored value", _resolved("qwen3.8-flash", None) == "qwen3.8-flash")
check("env ignored once user clears stored field", _resolved("qwen3.8-flash", "") == "qwen3.8-flash")
check("default is the cheap flash tier", _resolved(None, None) == "qwen3.8-flash")
# restore whatever the server process had (this is an in-proc import, so no
# effect on the running server, but keep the test client tidy for later checks)
os.environ.pop("QWENPLAN_MODEL", None)

# 18. text->AI estimate (describe-a-meal)
st, te = req("/api/estimate-text", "POST", {"text": "100g of rice and 100g of cooked 85% lean ground beef"})
est2 = te.get("estimate", {})
check("estimate-text 200", st == 200, str(te)[:120])
check("estimate-text has items", len(est2.get("items", [])) >= 2, str(est2.get("items"))[:150])
check("estimate-text totals positive", est2.get("calories", 0) > 0 and est2.get("protein", 0) > 0, str(est2.get("calories")))
# With a real key configured but failing/quota-limited, the server must say so
# instead of silently serving placeholder numbers. The stub path in this suite
# is pinned, so assert the contract only when a warning is present.
check("estimate-text warning field is a string when present",
      te.get("warning") is None or isinstance(te.get("warning"), str),
      str(te.get("warning"))[:80])
st, _ = req("/api/estimate-text", "POST", {"text": ""})
check("estimate-text empty -> 400", st == 400, str(st))
st, _ = req("/api/estimate-text", "POST", {"text": "ab"})
check("estimate-text too short -> 400", st == 400, str(st))
# save one from the estimate and confirm it lands with source=text
if est2.get("calories"):
    st, mm = req("/api/meals", "POST", {"description": est2.get("description", "text meal"),
        "calories": est2["calories"], "protein": est2["protein"], "carbs": est2["carbs"],
        "fat": est2["fat"], "source": "text", "items": est2.get("items", [])})
    mid_t = (mm or {}).get("id")
    check("save text-estimated meal", st in (200, 201) and mid_t, str(st))
    if mid_t:
        st, one = req(f"/api/meals/{mid_t}")
        check("text meal carries source + components", one.get("source") == "text"
              and len(one.get("items", [])) >= 2, f"{one.get('source')} items={len(one.get('items', []))}")
        req(f"/api/meals/{mid_t}", "DELETE")

# 19. merged estimate-meal: photos + text in one request
import datetime as _dt3
jpg = (bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300")
       + b"\x00" * 1500 + b"ffd9")
# multipart with two images and a text field
def multi(path, parts):
    boundary = "----mltest"
    body = b""
    for name, filename, data in parts:
        body += f"--{boundary}\r\n".encode()
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        body += (disp + "\r\n" if not filename else disp + "\r\n").encode()
        if filename:
            body += b"Content-Type: image/jpeg\r\n"
        body += b"\r\n" + (data if isinstance(data, bytes) else data.encode()) + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return req(path, "POST", raw_body=body,
               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

st, r2 = multi("/api/estimate-meal", [("photos", "plate.jpg", jpg),
                                      ("photos", "label.jpg", jpg),
                                      ("text", None, "about half the box")])
check("estimate-meal 2 photos + text -> 200", st == 200, str(r2)[:120])
temps = r2.get("temp_photos") or []
check("estimate-meal returns temp_photos list", len(temps) == 2, str(temps))
est_items = (r2.get("estimate") or {}).get("items", [])
check("estimate-meal produced items", len(est_items) >= 1, str(est_items)[:120])
st, r3 = multi("/api/estimate-meal", [("text", None, "just text no photo")])
check("estimate-meal text-only accepted", st == 200, str(st))
st, r4 = multi("/api/estimate-meal", [("text", None, "ab")])
check("estimate-meal empty-ish -> 400", st == 400, str(st))

# 20. save a meal carrying BOTH photos and confirm they round-trip
if temps:
    st, saved = req("/api/meals", "POST", {"description": "two-photo meal",
        "calories": 400, "protein": 30, "carbs": 20, "fat": 18,
        "photos": temps, "source": "photo"})
    mp = (saved or {}).get("id")
    check("save meal with 2 photos", st in (200, 201) and mp, str(st))
    if mp:
        st, one = req(f"/api/meals/{mp}")
        ph = one.get("photos") or []
        check("meal reports both photos", len(ph) == 2, str(ph))
        check("legacy single 'photo' still populated", bool(one.get("photo")))
        try:  # binary endpoint: cannot go through req()'s JSON decode
            with urllib.request.urlopen(BASE + f"/api/photo/{ph[0]}", timeout=15) as pr:
                img_ok = pr.status == 200 and pr.read(2) == b"\xff\xd8"
        except Exception:
            img_ok = False
        check("first photo served as image bytes", img_ok)
        req(f"/api/meals/{mp}", "DELETE")

# 21. auto meal grouping
def mk(desc, mins_ago, kcal=100):
    """Log a meal at a specific offset from now so cluster gaps are exact."""
    import datetime as _d4
    t = _d4.datetime.now() - _d4.timedelta(minutes=mins_ago)
    return req("/api/meals", "POST", {"description": desc, "calories": kcal,
        "protein": 5, "carbs": 5, "fat": 5,
        "date": _d4.date.today().isoformat(),
        "time": t.strftime("%H:%M")})

_, rA = mk("gA", 5); _, rB = mk("gB", 2)     # 3 min apart -> one sitting
_, rC = mk("gC", 200)                        # hours later -> its own sitting
idsAB = None
st, gs = req("/api/state")
byDesc = {m["description"]: m["id"] for m in gs["meals"]}
wantAB = {byDesc.get("gA"), byDesc.get("gB")}
groups = gs.get("groups") or []
names = [g["label"] for g in groups]
check("state includes derived groups", len(groups) >= 2, str(names))
# Assert on OUR two meals specifically — other checks leave meals logged near
# "now", so counting group size would be flaky; membership is what matters.
pair = next((g for g in groups if wantAB <= set(g["meal_ids"])), None)
check("close-in-time meals clustered together", pair is not None, str(names))
if pair:
    both = wantAB <= set(pair["meal_ids"])
    check("cluster contains both nearby meals", both, str(sorted(wantAB)))
    mine = [m for m in gs["meals"] if m["id"] in wantAB]
    check("cluster calories cover its members",
          pair["calories"] >= sum(int(m["calories"]) for m in mine) - 1,
          f'{pair["calories"]} vs {sum(int(m["calories"]) for m in mine)}')
gC_id = byDesc.get("gC")
solo = [g for g in groups if gC_id in g["meal_ids"]]
check("far-apart meal is its own group", len(solo) == 1,
      str([(g["label"], g["count"]) for g in groups]))
check("group labels are time-derived strings", all(isinstance(g["label"], str) and g["label"] for g in groups))
for m in gs["meals"]:
    if m["description"].startswith("g"):
        req(f"/api/meals/{m['id']}", "DELETE")

# 23. last_weight drives the weight-dock prefill + staleness nudge.
import datetime as _dt5
def log_w(kg, days_ago):
    d = (_dt5.date.today() - _dt5.timedelta(days=days_ago)).isoformat()
    return req("/api/weight", "POST", {"value": kg, "date": d})

# Deliberately placed before the adaptive-loop checks and confined to dates
# outside TREND_WINDOW_DAYS: overwriting a recent day changes the regression's
# endpoint and would make the slope assertions below flap.
st, s0 = req("/api/state")
lw0 = s0.get("last_weight") or {}
check("state exposes last_weight object", bool(lw0), str(s0.get("last_weight")))
check("last_weight carries date/kg/days_ago",
      all(k in lw0 for k in ("date", "kg", "days_ago")), str(lw0))
# Whatever the newest stored weigh-in is, days_ago must match its date.
_exp_gap = (_dt5.date.today() - _dt5.date.fromisoformat(lw0["date"])).days if lw0.get("date") else None
check("days_ago matches the reported date", lw0.get("days_ago") == _exp_gap, str(lw0))
# kg must be the raw stored value for that date, not a trend-smoothed figure.
st, hist0 = req("/api/history?days=900")
row = next((w for w in hist0.get("weights", []) if w["date"] == lw0.get("date")), None)
check("last_weight kg equals the raw stored value (not smoothed)",
      row is not None and abs(lw0["kg"] - row["kg"]) < 0.011,
      f"payload={lw0.get('kg')} row={row and row['kg']}")

# Staleness must be computable from the payload alone: log a far-past weight on
# a date nothing else touches, then confirm a client could detect >7 days by
# comparing dates (the newest row still wins, which is the point).
STALE = (_dt5.date.today() - _dt5.timedelta(days=400)).isoformat()
st, _ = log_w(81.5, 400)
st, s1 = req("/api/state")
lw1 = s1.get("last_weight") or {}
check("far-past entry does not displace a newer row",
      lw1.get("date") == lw0.get("date"), f"{lw1} vs {lw0}")
st, hist = req("/api/history?days=900")
dates = [w["date"] for w in hist.get("weights", [])]
check("past-dated weigh-in persisted", STALE in dates, f"{STALE} not in {len(dates)} rows")
gap_days = (_dt5.date.fromisoformat(dates[-1]) - _dt5.date.fromisoformat(dates[0])).days
check("history spans enough to compute staleness", gap_days >= 400, str(gap_days))



# 24. fiber: a SUBSET of carbs, tracked per item and summed per day.
st, fm = req("/api/meals", "POST", {
    "description": "fiber probe", "calories": 300, "protein": 9, "carbs": 50,
    "fat": 6, "fiber": 9, "source": "manual",
    "items": [{"name": "oats", "grams": 50, "calories": 190, "protein": 6,
               "carbs": 32, "fat": 3, "fiber": 5},
              {"name": "berries", "grams": 80, "calories": 110, "protein": 3,
               "carbs": 18, "fat": 3, "fiber": 4}]})
fmid = (fm or {}).get("id")
check("meal with fiber saves", st in (200, 201) and fmid, str(st))
if fmid:
    st, one = req(f"/api/meals/{fmid}")
    check("item fiber persists", [i.get("fiber") for i in one.get("items", [])] == [5.0, 4.0],
          str([i.get("fiber") for i in one.get("items", [])]))
    check("meal fiber is the item sum", one.get("fiber") == 9.0, str(one.get("fiber")))
    check("fiber never exceeds carbs", one["fiber"] <= one["carbs"],
          f"fiber={one['fiber']} carbs={one['carbs']}")
    st, sd = req("/api/state?date=" + one["date"])
    check("state consumed includes fiber",
          abs((sd.get("consumed") or {}).get("fiber", 0) - 9.0) < 0.01,
          str(sd.get("consumed")))
    check("targets expose a fiber goal", isinstance((sd.get("targets") or {}).get("fiber"), int),
          str((sd.get("targets") or {}).get("fiber")))
    # fiber is editable on an existing meal
    st, up = req(f"/api/meals/{fmid}", "PATCH", {"fiber": 12})
    check("fiber editable via PATCH", st == 200 and (up.get("fiber") == 12
          or req(f"/api/meals/{fmid}")[1].get("fiber") == 12), str(st))
    req(f"/api/meals/{fmid}", "DELETE")

# 25. history aggregates fiber per day
st, hist2 = req("/api/history?days=5")
check("history intake rows carry fiber",
      all("fiber" in d for d in hist2.get("intake", [])), str(hist2.get("intake"))[:120])

# 26. vision.normalize clamps fiber inside carbs
import importlib.util as _ilu2
_root2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec2 = _ilu2.spec_from_file_location("_vision2", os.path.join(_root2, "app", "vision.py"))
_v2 = _ilu2.module_from_spec(_spec2); _spec2.loader.exec_module(_v2)
n = _v2.normalize({"items": [
    {"name": "oats", "grams": 50, "calories": 190, "protein": 6, "carbs": 32, "fat": 3, "fiber": 5},
    {"name": "impossible", "grams": 10, "calories": 20, "protein": 0, "carbs": 2, "fat": 1, "fiber": 9}],
    "total_calories": 210, "total_protein": 6, "total_carbs": 34, "total_fat": 4,
    "total_fiber": 14, "confidence": 0.8})
check("normalize clamps per-item fiber to its carbs",
      n["items"][1]["fiber"] == 2.0, str(n["items"][1]))
check("normalize sums fiber rather than trusting a wild total",
      n["fiber"] == 7.0, str(n.get("fiber")))

# 27. Client timezone: wall-clock times must be stored as typed, not shifted.
import datetime as _dt6
from zoneinfo import ZoneInfo as _ZI
PT = _ZI("America/Los_Angeles")

def req_tz(path, method="GET", body=None, tz="America/Los_Angeles"):
    headers = {"X-Client-TZ": tz, "Content-Type": "application/json"}
    r = urllib.request.Request(BASE + path, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

# A fixed past date keeps these meals out of any other test's grouping window.
today_pt = "2026-01-15"
made = []
for hm, kcal in (("07:25", 90), ("12:35", 400), ("19:10", 700)):
    st, out = req_tz("/api/meals", "POST", {
        "description": "tz " + hm, "calories": kcal, "protein": 10, "carbs": 20,
        "fat": 8, "date": today_pt, "time": hm, "source": "manual"})
    made.append((out.get("id"), hm))
    check(f"meal at {hm} accepted", st in (200, 201) and out.get("id"), str(st))

st, sd = req_tz("/api/state?date=" + today_pt)
byid = {m["id"]: m for m in sd.get("meals", [])}
offsets = []
for mid, hm in made:
    m = byid.get(mid)
    if not m:
        offsets.append(None); continue
    local = _dt6.datetime.fromtimestamp(m["ts"], PT).strftime("%H:%M")
    offsets.append(local)
check("stored epoch equals the wall-clock time the user typed",
      offsets == ["07:25", "12:35", "19:10"], str(offsets))

# Grouping follows the user's clock, which was the visible symptom.
groups = {mid: next((g["label"] for g in sd.get("groups", []) if mid in g["meal_ids"]), None)
          for mid, _ in made}
check("07:25 groups as Breakfast", groups.get(made[0][0]) == "Breakfast", str(groups))
check("12:35 groups as Lunch", groups.get(made[1][0]) == "Lunch", str(groups))
check("19:10 groups as Dinner", groups.get(made[2][0]) == "Dinner", str(groups))

# The same rows read from another zone must shift by 8h, proving the zone is
# per-request rather than baked into the stored data.
st, sd_ldn = req_tz("/api/state?date=" + today_pt, tz="Europe/London")
london = {m["id"]: m for m in sd_ldn.get("meals", [])}
LDN = _ZI("Europe/London")
lunch_ts = byid[made[1][0]]["ts"]
ldn_local = _dt6.datetime.fromtimestamp(lunch_ts, LDN).strftime("%H:%M")
pt_local = _dt6.datetime.fromtimestamp(lunch_ts, PT).strftime("%H:%M")
# Derive the expected London wall clock rather than hardcoding it: DST means the
# offset varies by season (a hardcoded 20:35 was wrong once BST ended).
expected = (_dt6.datetime.fromtimestamp(lunch_ts, PT)
            .astimezone(LDN).strftime("%H:%M"))
check("the same meal reads differently from another zone",
      ldn_local == expected and ldn_local != pt_local, f"{ldn_local} vs PT {pt_local}")
check("payload identical regardless of viewer zone",
      london[made[1][0]]["ts"] == lunch_ts, str(ldn_local))

for mid, _ in made:
    if mid:
        req_tz(f"/api/meals/{mid}", "DELETE")

# 28. time_format is a per-user display preference
st, tf = req("/api/settings", "PUT", {"time_format": "24h"})
check("time_format saves", st == 200 and (tf.get("settings") or {}).get("time_format") == "24h",
      str((tf.get("settings") or {}).get("time_format")))
st, sd = req("/api/state")
check("time_format persists across a fresh state read",
      (sd.get("settings") or {}).get("time_format") == "24h",
      str((sd.get("settings") or {}).get("time_format")))
# It is presentation only: no stored timestamp semantics change.
check("time_format does not alter stored dates",
      all(len(m["date"]) == 10 for m in sd.get("meals", [])), "date fields malformed")
st, tf = req("/api/settings", "PUT", {"time_format": "12h"})
check("time_format switches back", (tf.get("settings") or {}).get("time_format") == "12h",
      str((tf.get("settings") or {}).get("time_format")))

# 29. Rescan: re-estimate from images we already hold (no re-upload needed).
st, rem = multi("/api/estimate-meal", [("photos", "plate.jpg", jpg),
                                       ("text", None, "about half")])
temps29 = [os.path.basename(t) for t in (rem.get("temp_photos") or [])]
check("estimate-meal produced a temp photo to rescan", len(temps29) == 1, str(temps29))
st, r1 = req("/api/rescan", "POST", {"photos": temps29, "text": "about half"})
check("rescan of a temp upload -> 200", st == 200, str(r1)[:120])
check("rescan returns an estimate with items",
      len((r1.get("estimate") or {}).get("items", [])) >= 1, str(r1.get("estimate"))[:120])
check("rescan reports which photos it used", r1.get("photos") == temps29, str(r1.get("photos")))

# ...and from a SAVED meal's stored photo, which is the case that matters when
# revisiting an old entry from the meal list.
est29 = rem.get("estimate") or {}
st, saved29 = req("/api/meals", "POST", {
    "description": "rescan probe", "calories": est29.get("calories") or 100,
    "protein": est29.get("protein") or 1, "carbs": est29.get("carbs") or 1,
    "fat": est29.get("fat") or 1, "photos": rem.get("temp_photos"), "source": "photo"})
mid29 = (saved29 or {}).get("id")
if mid29:
    st, one29 = req(f"/api/meals/{mid29}")
    stored = one29.get("photos") or []
    st, r2 = req("/api/rescan", "POST", {"photos": stored})
    check("rescan of a stored meal photo -> 200", st == 200, str(r2)[:120])
    check("rescan from storage yields an estimate",
          len((r2.get("estimate") or {}).get("items", [])) >= 1, str(r2)[:120])
    req(f"/api/meals/{mid29}", "DELETE")

st, r3 = req("/api/rescan", "POST", {"photos": ["nope-missing.jpg"]})
check("rescan with no usable photo -> 400", st == 400, str(r3)[:100])
st, r4 = req("/api/rescan", "POST", {"photos": ["../../etc/passwd"]})
check("rescan refuses path traversal", st == 400, str(r4)[:100])

# 30. Zero-calorie entries (supplements) are legitimate.
st, z1 = req("/api/meals", "POST", {"description": "Creatine", "calories": 0, "source": "manual",
    "items": [{"name": "Creatine", "grams": 5, "calories": 0, "protein": 0, "carbs": 0, "fat": 0}]})
check("0 kcal entry with a component saves", st in (200, 201) and z1.get("id"), str(z1)[:90])
st, z2 = req("/api/meals", "POST", {"description": "Black coffee", "calories": 0, "source": "manual"})
check("0 kcal entry with only a description saves", st in (200, 201), str(z2)[:90])
st, z3 = req("/api/meals", "POST", {"calories": 0, "source": "manual"})
check("a completely empty entry is still rejected", st == 400, str(z3)[:90])
st, z4 = req("/api/meals", "POST", {"description": "x", "calories": -5, "source": "manual"})
check("negative calories rejected with its own message",
      st == 400 and "negative" in str(z4.get("error")), str(z4)[:90])
for zid in (z1.get("id"), z2.get("id")):
    if zid: req(f"/api/meals/{zid}", "DELETE")

# 31. Quick foods: names fold to the generic food, then rank by frequency.
for _ in range(3):
    req("/api/meals", "POST", {"description": "Sunfiber", "calories": 0, "source": "manual",
        "items": [{"name": "Sunfiber", "grams": 7, "calories": 0, "protein": 0,
                   "carbs": 7, "fat": 0, "fiber": 7}]})
# The same yogurt scanned with a different brand wording / weight each day.
yogurt_variants = [
    ("Chobani Nonfat Plain Greek Yogurt (protein rich)", 134, 134, 23, 5, 0, 0),
    ("Chobani Whole Milk Plain Greek Yogurt", 150, 170, 25, 6, 3, 0),
    ("Chobani Whole Milk Plain Greek Yogurt", 134, 170, 25, 6, 3, 0),
]
for i, (name, grams, cal, pro, carb, fat, fib) in enumerate(yogurt_variants):
    req("/api/meals", "POST", {"description": name, "calories": cal,
        "source": "manual", "items": [{"name": name, "detail": "Chobani whole milk",
        "grams": grams, "calories": cal, "protein": pro, "carbs": carb,
        "fat": fat, "fiber": fib}]})
# The same oats with cooking/weight wording the model has actually produced.
oats_variants = [
    ("Steel cut oats (cooked)", 100, 180, 7, 32, 3, 5),
    ("cooked steel cut oats", 110, 198, 7, 35, 3, 6),
    ("steel cut oatmeal (dry weight 66g, cooked)", 66, 250, 9, 44, 4, 6),
]
for name, grams, cal, pro, carb, fat, fib in oats_variants:
    req("/api/meals", "POST", {"description": name, "calories": cal,
        "source": "manual", "items": [{"name": name, "grams": grams,
        "calories": cal, "protein": pro, "carbs": carb, "fat": fat, "fiber": fib}]})
st, qf = req("/api/quick-foods?limit=12")
foods = qf.get("foods") or []
by_name = {f["name"]: f for f in foods}
check("quick foods returns entries", st == 200 and len(foods) >= 2, str(foods)[:120])
check("frequency ranking puts the most-used group first",
      foods and foods[0]["uses"] >= 3, str(foods[0])[:90])
check("brand/weight variants fold into one Greek yogurt chip",
      "Greek yogurt" in by_name and sum(f["name"] == "Greek yogurt" for f in foods) == 1,
      str([f["name"] for f in foods])[:160])
check("folded yogurt reports all three uses",
      by_name.get("Greek yogurt", {}).get("uses") == 3, str(by_name.get("Greek yogurt")))
check("cooked/weight oat variants fold into one Steel cut oats chip",
      "Steel cut oats" in by_name and sum(f["name"] == "Steel cut oats" for f in foods) == 1,
      str([f["name"] for f in foods])[:160])
check("folded oats reports all three uses",
      by_name.get("Steel cut oats", {}).get("uses") == 3, str(by_name.get("Steel cut oats")))
check("folded oats carries the latest macros + portion",
      by_name.get("Steel cut oats", {}).get("grams") == 66.0
      and by_name.get("Steel cut oats", {}).get("fiber") == 6.0,
      str(by_name.get("Steel cut oats")))
check("folded chip keeps a specific detail line",
      by_name.get("Greek yogurt", {}).get("detail") == "Chobani whole milk",
      str(by_name.get("Greek yogurt")))
check("single-name staples still appear", "Sunfiber" in by_name, str(by_name.keys())[:120])
st, qf1 = req("/api/quick-foods?limit=1")
check("limit is honoured", len((qf1.get("foods") or [])) == 1, str(qf1)[:90])

# Detail is a first-class field: it must survive save -> read -> chip.
st, dm = req("/api/meals", "POST", {"description": "Greek yogurt", "calories": 170,
    "source": "manual", "items": [{"name": "Greek yogurt", "detail": "Chobani whole milk",
    "grams": 150, "calories": 170, "protein": 25, "carbs": 6, "fat": 3, "fiber": 0}]})
mid_d = (dm or {}).get("id")
st, dmeal = req(f"/api/meals/{mid_d}")
ditems = dmeal.get("items") or []
check("item detail survives save -> read",
      ditems and ditems[0].get("detail") == "Chobani whole milk", str(ditems)[:120])
if mid_d: req(f"/api/meals/{mid_d}", "DELETE")

# 32. The Jev cross-check must be invisible when no key is configured.
st, sd = req("/api/state")
check("crosscheck field is absent without JEV_API_KEY",
      "crosscheck" not in sd, str(sorted(sd.keys()))[:120])

# 33. Apple Health Shortcut token: read-only, hashed, revocable.
st, ht = req("/api/health/token", "POST", {})
ht_token = ht.get("token") or ""
check("health token generated", st == 200 and len(ht_token) >= 32, str(ht)[:80])
st, hs = req("/api/settings")
check("settings reports a configured health token",
      (hs.get("settings") or {}).get("health_token_set") is True,
      str((hs.get("settings") or {}).get("health_token_set")))
st, xp_health = req("/api/export")
check("health token/hash never appears in settings or export",
      ht_token not in json.dumps(hs) and "health_token_hash" not in json.dumps(xp_health))
st, no_auth = req("/health/day")
check("health endpoint requires a bearer token", st == 401, str(no_auth)[:100])
health_date = time.strftime("%Y-%m-%d")
st, hd = req("/health/day?date=" + health_date,
             headers={"Authorization": "Bearer " + ht_token})
check("health day returns totals and meals",
      st == 200 and hd.get("date") == health_date
      and isinstance(hd.get("totals"), dict)
      and isinstance(hd.get("meals"), list),
      str(hd)[:180])
check("health day exposes gram units for Health samples",
      (hd.get("units") or {}).get("protein") == "g"
      and (hd.get("units") or {}).get("calories") == "kcal", str(hd.get("units")))
check("first fetch is not flagged as already exported",
      (hd.get("export") or {}).get("already_requested") is False
      and (hd.get("export") or {}).get("already_confirmed") is False,
      str(hd.get("export")))
st, pending = req("/health/pending", headers={"Authorization": "Bearer " + ht_token})
check("pending endpoint lists unconfirmed data days",
      st == 200 and isinstance(pending.get("pending"), list)
      and health_date in [d.get("date") for d in pending.get("pending") or []],
      str(pending)[:180])
st, no_pending = req("/health/pending")
check("pending endpoint requires a bearer token", st == 401, str(no_pending)[:100])
st, hd2 = req("/health/day?date=" + health_date,
              headers={"Authorization": "Bearer " + ht_token})
check("second fetch warns it was already requested",
      (hd2.get("export") or {}).get("already_requested") is True
      and (hd2.get("export") or {}).get("already_confirmed") is False,
      str(hd2.get("export")))
st, ack = req("/health/ack", "POST", {"dates": [health_date]},
              headers={"Authorization": "Bearer " + ht_token})
check("health acknowledgement confirms the day",
      st == 200 and health_date in (ack.get("confirmed") or []), str(ack)[:120])
st, hd3 = req("/health/day?date=" + health_date,
              headers={"Authorization": "Bearer " + ht_token})
check("confirmed day warns against duplicate export",
      (hd3.get("export") or {}).get("already_confirmed") is True,
      str(hd3.get("export")))
st, pending2 = req("/health/pending", headers={"Authorization": "Bearer " + ht_token})
check("confirmed day is removed from pending backfill",
      health_date not in [d.get("date") for d in pending2.get("pending") or []]
      and pending2.get("last_export_date") == health_date,
      str({k: pending2.get(k) for k in ("last_export_date", "count")}))
st, state_health = req("/api/state")
check("state exposes health export status",
      (state_health.get("health_export") or {}).get("today_confirmed") is True
      and (state_health.get("health_export") or {}).get("last_export_date") == health_date,
      str(state_health.get("health_export"))[:160])
st, bad_ack = req("/health/ack", "POST", {"dates": ["not-a-date"]},
                  headers={"Authorization": "Bearer " + ht_token})
check("health ack rejects invalid dates", st == 400, str(bad_ack)[:100])
st, no_ack = req("/health/ack", "POST", {"dates": [health_date]})
check("health ack requires a bearer token", st == 401, str(no_ack)[:100])
st, _ = req("/api/health/token", "DELETE")
st, old_token = req("/health/day?date=" + health_date,
                    headers={"Authorization": "Bearer " + ht_token})
check("revoking the health token invalidates it", st == 401, str(old_token)[:100])

# 34. Model discovery: OpenRouter gets filtered to image-capable, non-batch
#     models; a generic OpenAI-compatible endpoint returns everything. The mock
#     server keeps this deterministic and offline.
MODEL_FIXTURE = {"data": [
    {"id": "vendor/vision-1", "name": "Vision One", "context_length": 128000,
     "architecture": {"input_modalities": ["text", "image"]},
     "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
    {"id": "vendor/text-1", "name": "Text One", "context_length": 32000,
     "architecture": {"input_modalities": ["text"]},
     "pricing": {"prompt": "0.0000001"}},
    {"id": "vendor/vision-1:batch", "name": "Vision batch", "context_length": 128000,
     "architecture": {"input_modalities": ["text", "image"]}},
]}


class ModelProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if not self.path.endswith("/models"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(MODEL_FIXTURE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


mock = ThreadingHTTPServer(("127.0.0.1", 0), ModelProvider)
threading.Thread(target=mock.serve_forever, daemon=True).start()
mock_base = f"http://127.0.0.1:{mock.server_address[1]}/v1"
try:
    st, ml = req("/api/provider/models", "POST",
                 {"kind": "custom", "base_url": mock_base})
    check("generic endpoint lists every model", st == 200
          and len(ml.get("models", [])) == 3, str(ml)[:160])
    st, ml = req("/api/provider/models", "POST",
                 {"kind": "openrouter", "base_url": mock_base})
    ids = [m.get("id") for m in ml.get("models", [])]
    check("OpenRouter list hides text-only and batch models",
          st == 200 and ids == ["vendor/vision-1"], str(ids))
    st, bad = req("/api/provider/models", "POST",
                  {"kind": "openrouter", "base_url": "ftp://bad"})
    check("model discovery rejects non-http URLs", st == 400, str(bad)[:100])
    st, bad = req("/api/settings", "PUT",
                  {"providers": [{"kind": "custom", "model": "m"}]})
    check("custom provider rejects a missing base URL", st == 400, str(bad)[:120])
finally:
    mock.shutdown()
    mock.server_close()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failures:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
