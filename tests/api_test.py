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
check("stub fallback flagged", s.get("active_provider") in ("stub", "ollama", "groq", "gemini", "openai"))

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

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failures:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
