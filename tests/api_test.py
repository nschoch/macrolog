"""End-to-end API tests against a running MacroLog server.

Usage: python3 tests/api_test.py http://127.0.0.1:8790
Exits non-zero on any failure. Uses a throwaway DB via MACROLOG_DATA in the
caller's environment; deletes nothing you care about.
"""
import json
import os
import sys
import urllib.error
import urllib.request

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


print(f"\nMacroLog API tests -> {BASE}\n")

# 1. state
st, s = req("/api/state")
check("state returns 200", st == 200, str(st))
check("state has targets.calories", isinstance(s.get("targets", {}).get("calories"), int))
KNOWN_PROVIDERS = {"qwenplan", "groq", "gemini", "openai", "ollama", "stub"}
check("active provider is known", s.get("active_provider") in KNOWN_PROVIDERS,
      str(s.get("active_provider")))

# 2. estimate from a photo
fake_jpeg = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300") + b"\x00" * 2000 + b"ffd9"
st, e = multipart("/api/estimate", "meal.jpg", fake_jpeg)
check("estimate 200", st == 200, str(e)[:120])
est = e.get("estimate", {})
check("estimate has calories>0", est.get("calories", 0) > 0, str(est))
check("estimate temp_photo present", bool(e.get("temp_photo")))
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

# 4. edit records a correction (the calibration training signal)
st, u = req(f"/api/meals/{meal_id}", "PATCH", {"calories": 700, "protein": 50})
check("patch meal 200", st == 200, str(u)[:120])
check("correction recorded", u.get("corrections_recorded", 0) >= 1, str(u.get("corrections_recorded")))

# 5. accuracy summary now populated
st, hist = req("/api/history?days=90")
acc = hist.get("accuracy", {}).get("calories", {})
check("accuracy tracks edits", acc.get("n", 0) >= 1, str(acc))

# 6. weights drive the adaptive loop
for i in range(21):
    day = _day(i) if False else __import__("datetime").date.today() - __import__("datetime").timedelta(days=20 - i)
    w = round(90 - i * 0.22, 2)
    req("/api/weight", "POST", {"value": w, "date": day.isoformat()})
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

# 8b. REGRESSION: body-profile settings must PERSIST across a fresh state read
#     (guards the OIDC per-user migration bug where saved prefs reverted to defaults).
req("/api/settings", "PUT", {"units": "imperial", "age": 41, "height_cm": 183, "activity_factor": 1.35})
st, s2 = req("/api/state")
p = s2.get("settings", {})
check("units persist after save+reload", p.get("units") == "imperial", str(p.get("units")))
check("age persists after save+reload", p.get("age") == 41, str(p.get("age")))
check("height persists after save+reload", p.get("height_cm") == 183, str(p.get("height_cm")))
check("profile-set clears needs_setup nag", s2.get("needs_setup") is False, str(s2.get("needs_setup")))

# 9. export
st, xp = req("/api/export")
check("export has meals", st == 200 and len(xp.get("meals", [])) >= 1, str(st))
check("export carries no api keys", "api_key" not in json.dumps(xp).lower())

# 10. delete
st, d = req(f"/api/meals/{meal_id}", "DELETE")
check("delete meal 200", st == 200, str(d)[:100])

# 11. bad input rejected
st, _ = req("/api/meals", "POST", {"calories": 0, "description": "empty"})
check("zero-calorie meal rejected", st == 400, str(st))

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

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failures:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
