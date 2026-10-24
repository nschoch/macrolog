"""MacroLog - self-hosted photo calorie & macro tracker.

Stdlib-only web server (http.server + sqlite3) so deployment is one command on
any machine with Python 3.9+: Mac, Linux, Proxmox LXC, Docker, a Pi.

    python3 server.py --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import socket
import sys
import threading
import time
import uuid
from datetime import date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.adaptive import (accuracy_summary, adapt_targets, apply_calibration,
                          calibration_multipliers, macronutrient_targets,
                          record_correction, tdee_from_formula, weight_trend)
from app.database import Database
from app.vision import ProviderError, estimate_photo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("MACROLOG_DATA", os.path.join(ROOT, "data"))
STATIC_DIR = os.path.join(ROOT, "static")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
DB_PATH = os.path.join(DATA_DIR, "macrolog.sqlite")

DEFAULT_SETTINGS = {
    "units": "metric",              # metric | imperial
    "sex": "male",
    "age": 35,
    "height_cm": 178,
    "activity_factor": 1.4,
    "weekly_rate_kg": -0.5,         # negative = cut
    "protein_g_per_kg": 1.8,
    "fat_calorie_pct": 0.25,
    "calorie_target": None,
    "start_date": None,
    "starting_weight_kg": None,
    "auto_adapt": True,
    "calibrate_estimates": True,
    # Provider chain, tried in order. First entry wins; missing keys are skipped.
    "providers": [
        {"kind": "qwenplan", "model": "qwen3.8-max"},
        {"kind": "groq",   "model": "meta-llama/llama-4-scout-17b-16e-instruct"},
        {"kind": "gemini", "model": "gemini-2.5-flash"},
        {"kind": "openai", "model": "gpt-4o-mini"},
        {"kind": "ollama", "model": "llama3.2-vision:11b"},
    ],
}

db = Database(DB_PATH)
_LOG_LOCK = threading.Lock()


def load_env():
    """Read KEY=VALUE lines from .env next to the project, without overriding env."""
    for path in (os.path.join(ROOT, ".env"), os.path.join(os.path.dirname(ROOT), ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass


def get_settings() -> dict:
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    stored = db.get_setting("settings", {}) or {}
    if isinstance(stored, dict):
        stored.pop("api_key", None)          # never carry secrets into settings blob
        merged.update(stored)
    return merged


def today_str() -> str:
    return date.today().isoformat()


def local_ts_for(date_str: str, fallback_hour: int = 12) -> int:
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return int(time.time())
    now = datetime.now()
    return int(datetime(d.year, d.month, d.day, now.hour or fallback_hour,
                        now.minute).timestamp())


# ============================================================== API handlers
class Api:
    """Each method returns (status_code, payload_dict)."""

    # ---- bootstrap / state -------------------------------------------
    @staticmethod
    def GET_state(h, q, body):
        s = get_settings()
        t = today_str()
        meals = db.meals_for_date(t)
        consumed = {k: round(sum(float(m[k]) for m in meals), 1)
                    for k in ("calories", "protein", "carbs", "fat")}
        rows = db.query("SELECT date, value FROM weights ORDER BY date")
        trend = weight_trend(rows)
        target = s.get("calorie_target") or tdee_from_formula(
            s, trend["current_kg"]) or 2200
        tmp = dict(s); tmp["_current_weight_kg"] = trend["current_kg"]
        targets = macronutrient_targets(target, tmp)
        ready = [p["kind"] for p in s.get("providers", []) if _provider_ready(p)]
        providers = [{"kind": p["kind"], "model": p.get("model"),
                     "configured": (p["kind"] == "stub") or (_provider_ready(p) and not p.get("unreachable"))}
                    for p in s.get("providers", [])]
        return 200, {
            "date": t, "meals": [_meal_dict(m) for m in meals],
            "consumed": consumed, "targets": targets, "trend": trend,
            "settings": _public_settings(s), "providers": providers,
            "active_provider": ready[0] if ready else "stub",
            "exercise": _exercise_total(t),
            "needs_setup": not s.get("start_date"),
            "weights_count": len(rows),
        }

    # ---- meals --------------------------------------------------------
    @staticmethod
    def POST_estimate(h, q, body):
        """Photo -> estimate, preview only. Nothing is written until /meals."""
        upload = getattr(h, "_upload", None)
        if not upload:
            return 400, {"error": "no image received (send multipart form-data field 'photo')"}
        path, mime = upload
        try:
            meal, provider, model = estimate_photo(path, get_settings())
        except ProviderError as e:
            _unlink(path)
            return 502, {"error": f"all providers failed: {e}"}
        except Exception as e:
            _unlink(path)
            return 500, {"error": f"{type(e).__name__}: {e}"}
        if not meal["items"] and not meal["calories"]:
            _unlink(path)
            return 422, {"error": meal.get("notes") or "no food detected in image"}
        s = get_settings()
        changed = False
        if s.get("calibrate_estimates"):
            meal, changed = apply_calibration(meal, calibration_multipliers(db))
        return 200, {"estimate": meal, "provider": provider, "model": model,
                     "calibrated": changed, "temp_photo": path}

    @staticmethod
    def POST_meals(h, q, body):
        s = get_settings()
        photo = body.get("photo_path")
        source = body.get("source", "photo")
        if photo and not os.path.abspath(photo).startswith(os.path.abspath(PHOTO_DIR)):
            return 400, {"error": "invalid photo path"}
        d = body.get("date") or today_str()
        meal = {
            "description": str(body.get("description") or "")[:200],
            "calories": float(body.get("calories") or 0),
            "protein": float(body.get("protein") or 0),
            "carbs": float(body.get("carbs") or 0),
            "fat": float(body.get("fat") or 0),
        }
        if meal["calories"] <= 0:
            if photo and os.path.exists(photo):
                os.remove(photo)
            return 400, {"error": "calories must be > 0"}
        final_path = _promote_photo(photo) if photo else None
        mid = db.add_meal(
            date=d, ts=int(body.get("ts") or local_ts_for(d)),
            kind=str(body.get("kind") or "other")[:20],
            description=meal["description"], calories=meal["calories"],
            protein=meal["protein"], carbs=meal["carbs"], fat=meal["fat"],
            confidence=float(body.get("confidence") or 0.5),
            photo_path=final_path, source=source,
            provider=str(body.get("provider") or "")[:30],
            model=str(body.get("model") or "")[:60],
            raw_json=json.dumps(body.get("raw") or {})[:8000])
        _maybe_adapt(s)
        return 201, {"id": mid, **_day_summary(d)}

    @staticmethod
    def PATCH_meal(h, q, body, meal_id):
        old = db.one("SELECT * FROM meals WHERE id=?", (meal_id,))
        if not old:
            return 404, {"error": "meal not found"}
        fields = {k: v for k, v in body.items()
                  if k in ("kind", "description", "calories", "protein", "carbs", "fat")}
        if not fields:
            return 400, {"error": "no editable fields supplied"}
        db.update_meal(meal_id, **fields)
        new = dict(old)
        new.update(fields)
        n = record_correction(db, int(meal_id), {
            "calories": old["calories"], "protein": old["protein"],
            "carbs": old["carbs"], "fat": old["fat"]}, new, int(time.time()))
        s = get_settings()
        _maybe_adapt(s)
        return 200, {"updated": True, "corrections_recorded": n,
                     "multipliers": calibration_multipliers(db),
                     **_day_summary(old["date"])}

    @staticmethod
    def DELETE_meal(h, q, body, meal_id):
        row = db.one("SELECT * FROM meals WHERE id=?", (meal_id,))
        if not row:
            return 404, {"error": "meal not found"}
        db.delete_meal(int(meal_id))
        if row["photo_path"] and os.path.exists(row["photo_path"]):
            try:
                os.remove(row["photo_path"])
            except OSError:
                pass
        return 200, {"deleted": True, **_day_summary(row["date"])}

    # ---- weight / composition -----------------------------------------
    @staticmethod
    def POST_weight(h, q, body):
        val = float(body.get("value") or 0)
        if val <= 0 or val > 700:
            return 400, {"error": "weight out of range"}
        d = body.get("date") or today_str()
        if body.get("unit") == "lb" or get_settings().get("units") == "imperial":
            val = val / 2.2046226
        db.set_weight(d, local_ts_for(d), round(val, 2))
        if body.get("body_fat") is not None:
            bf = float(body["body_fat"])
            if 1 <= bf <= 70:
                db.set_body_fat(d, local_ts_for(d), bf)
        s = get_settings()
        if not s.get("start_date"):
            s["start_date"] = d
            s["starting_weight_kg"] = round(val, 2)
            db.set_setting("settings", s)
        result = _maybe_adapt(s, persist=True)
        return 201, {"logged": {"date": d, "kg": round(val, 2)}, **result}

    @staticmethod
    def GET_history(h, q, body):
        days = int((q.get("days") or ["90"])[0])
        start = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
        meals = db.query(
            "SELECT date, SUM(calories) c, SUM(protein) p, SUM(carbs) cb, "
            "SUM(fat) f, COUNT(*) n FROM meals WHERE date >= ? GROUP BY date "
            "ORDER BY date", (start,))
        weights = db.query(
            "SELECT date, value FROM weights WHERE date >= ? ORDER BY date", (start,))
        bf = db.query("SELECT date, value FROM body_fat WHERE date >= ? ORDER BY date",
                      (start,))
        ex = db.query("SELECT date, SUM(calories) c FROM exercises WHERE date >= ? "
                      "GROUP BY date", (start,))
        s = get_settings()
        target = s.get("calorie_target") or tdee_from_formula(s) or 2200
        return 200, {
            "days": days, "target": target,
            "intake": [{"date": r["date"], "calories": round(r["c"] or 0),
                        "protein": round(r["p"] or 0), "carbs": round(r["cb"] or 0),
                        "fat": round(r["f"] or 0), "meals": r["n"]} for r in meals],
            "weights": [{"date": r["date"], "kg": r["value"]} for r in weights],
            "body_fat": [{"date": r["date"], "pct": r["value"]} for r in bf],
            "exercise": [{"date": r["date"], "calories": round(r["c"] or 0)} for r in ex],
            "accuracy": accuracy_summary(db),
        }

    @staticmethod
    def POST_exercise(h, q, body):
        cal = float(body.get("calories") or 0)
        if cal <= 0:
            return 400, {"error": "calories must be > 0"}
        d = body.get("date") or today_str()
        db.execute("INSERT INTO exercises(ts,date,name,calories) VALUES(?,?,?,?)",
                   (local_ts_for(d), d, str(body.get("name") or "workout")[:60], cal))
        return 201, _day_summary(d)

    @staticmethod
    def POST_adapt(h, q, body):
        s = get_settings()
        res = _maybe_adapt(s, persist=True, force=True)
        return 200, res

    # ---- settings ------------------------------------------------------
    @staticmethod
    def GET_settings(h, q, body):
        s = get_settings()
        return 200, {"settings": _public_settings(s),
                     "accuracy": accuracy_summary(db),
                     "multipliers": calibration_multipliers(db),
                     "has_keys": {k: bool(os.environ.get(k)) for k in
                                  ("GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "QWENPLAN_API_KEY")}}

    @staticmethod
    def PUT_settings(h, q, body):
        cur = db.get_setting("settings", {}) or {}
        allowed = {"units", "sex", "age", "height_cm", "activity_factor",
                   "weekly_rate_kg", "protein_g_per_kg", "fat_calorie_pct",
                   "calorie_target", "auto_adapt", "calibrate_estimates",
                   "providers", "start_date", "starting_weight_kg"}
        for k, v in body.items():
            if k in allowed:
                cur[k] = v
        if "height_cm" in body and str(body.get("units")) == "imperial":
            pass  # UI converts before sending; store canonical metric
        db.set_setting("settings", cur)
        s = get_settings()
        return 200, {"settings": _public_settings(s), "targets": macronutrient_targets(
            s.get("calorie_target") or tdee_from_formula(s) or 2200, s)}

    @staticmethod
    def GET_export(h, q, body):
        """Full data dump as JSON. Secrets are never included."""
        s = get_settings()
        return 200, {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "settings": _public_settings(s),
            "meals": [_meal_dict(m) for m in db.query("SELECT * FROM meals ORDER BY ts")],
            "weights": [dict(r) for r in db.query("SELECT date,value FROM weights ORDER BY date")],
            "body_fat": [dict(r) for r in db.query("SELECT date,value FROM body_fat ORDER BY date")],
            "exercises": [dict(r) for r in db.query("SELECT date,name,calories FROM exercises ORDER BY date")],
            "corrections": [dict(r) for r in db.query(
                "SELECT m.date, m.description, e.field, e.predicted, e.actual "
                "FROM estimates e JOIN meals m ON m.id=e.meal_id ORDER BY e.logged_at")],
            "accuracy": accuracy_summary(db),
            "multipliers": {f: calibration_multipliers(db, f)
                            for f in ("calories", "protein", "carbs", "fat")},
        }

    @staticmethod
    def POST_test_provider(h, q, body):
        """Ping each configured provider with a tiny text request."""
        results = []
        for p in get_settings().get("providers", []):
            kind = p.get("kind")
            try:
                if kind == "stub":
                    raise ProviderError("stub provider has no remote endpoint")
                key_env = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY",
                           "groq": "GROQ_API_KEY", "qwenplan": "QWENPLAN_API_KEY"}.get(kind)
                key = p.get("api_key") or (os.environ.get(key_env) if key_env else "x")
                if not key:
                    raise ProviderError("no API key configured")
                base = p.get("base_url") or {
                    "gemini": "https://generativelanguage.googleapis.com",
                    "openai": "https://api.openai.com/v1",
                    "groq": "https://api.groq.com/openai/v1",
                    "qwenplan": os.environ.get("QWENPLAN_BASE_URL",
                            "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                    "ollama": os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
                }.get(kind)
                url, payload, headers = _ping_request(kind, base, p.get("model"))
                import urllib.request as U
                req = U.Request(
                    url, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", **headers})
                t0 = time.time()
                with U.urlopen(req, timeout=30) as r:
                    json.loads(r.read())
                results.append({"kind": kind, "model": p.get("model"), "ok": True,
                                "ms": int((time.time() - t0) * 1000)})
            except Exception as e:
                msg = str(e)
                if hasattr(e, "read"):
                    try:
                        msg = e.read().decode()[:200]
                    except Exception:
                        pass
                results.append({"kind": kind, "model": p.get("model"), "ok": False,
                                "error": type(e).__name__ + ": " + msg[:220]})
        return 200, {"results": results}


def _ping_request(kind, base, model):
    import urllib.request as U
    if kind == "gemini":
        return (f"{base}/v1beta/models/{model}:generateContent",
                {"contents": [{"parts": [{"text": "ping"}]}]},
                {"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")})
    if kind == "ollama":
        return (f"{base.rstrip('/')}/api/tags", {}, {})
    return (f"{base}/chat/completions",
            {"model": model, "messages": [{"role": "user", "content": "ping"}],
             "max_tokens": 5},
            {"Authorization": "Bearer " + os.environ.get(
                {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY",
                 "qwenplan": "QWENPLAN_API_KEY"}.get(kind, ""), "")})


def _provider_ready(entry: dict) -> bool:
    """Mirror vision._has_credentials so the UI badge matches real behavior."""
    from app.vision import _has_credentials
    return _has_credentials(entry)


def _public_settings(s: dict) -> dict:
    out = dict(s)
    out["providers"] = [{"kind": p.get("kind"), "model": p.get("model"),
                         "base_url": p.get("base_url")}
                        for p in s.get("providers", [])]
    return out


def _unlink(path) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def guess_mime(ext: str) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".heic": "image/heic",
            ".gif": "image/gif"}.get(ext.lower(), "image/jpeg")


def sweep_temp_photos(max_age_hours: int = 6) -> int:
    """Delete abandoned tmp-* uploads (user cancelled, or estimate failed)."""
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        for name in os.listdir(PHOTO_DIR):
            if not name.startswith("tmp-"):
                continue
            full = os.path.join(PHOTO_DIR, name)
            try:
                if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                    os.remove(full)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def _meal_dict(m) -> dict:
    return {"id": m["id"], "ts": m["ts"], "date": m["date"], "kind": m["kind"],
            "description": m["description"], "calories": m["calories"],
            "protein": m["protein"], "carbs": m["carbs"], "fat": m["fat"],
            "confidence": m["confidence"], "source": m["source"],
            "provider": m["provider"], "model": m["model"],
            "photo": os.path.basename(m["photo_path"]) if m["photo_path"] else None}


def _exercise_total(d: str) -> float:
    row = db.one("SELECT SUM(calories) c FROM exercises WHERE date=?", (d,))
    return round(row["c"] or 0) if row else 0.0


def _day_summary(d: str) -> dict:
    meals = db.meals_for_date(d)
    return {"date": d, "consumed": {
        k: round(sum(float(m[k]) for m in meals), 1)
        for k in ("calories", "protein", "carbs", "fat")},
        "meals": [_meal_dict(m) for m in meals],
        "exercise": _exercise_total(d)}


def _promote_photo(tmp_path: str) -> str | None:
    """Move an accepted temp photo into the permanent dated store."""
    if not tmp_path or not os.path.exists(tmp_path):
        return None
    d = today_str()
    dest_dir = os.path.join(PHOTO_DIR, d)
    os.makedirs(dest_dir, exist_ok=True)
    ext = os.path.splitext(tmp_path)[1] or ".jpg"
    dest = os.path.join(dest_dir, f"{int(time.time())}-{uuid.uuid4().hex[:8]}{ext}")
    try:
        shutil.move(tmp_path, dest)
        return dest
    except (OSError, shutil.Error):
        return None


def _maybe_adapt(settings: dict, persist: bool = False, force: bool = False) -> dict:
    """Run the weight->target loop when appropriate; optionally save result."""
    if not (force or settings.get("auto_adapt", True)):
        return {"adapted": False, "reason": "auto-adapt disabled"}
    snapshot = json.loads(json.dumps(settings))
    res = adapt_targets(db, settings)
    if res.get("adjusted") and persist:
        cur = db.get_setting("settings", {}) or {}
        cur["calorie_target"] = settings["calorie_target"]
        db.set_setting("settings", cur)
    res["adapted"] = bool(res.get("adjusted"))
    res["targets"] = macronutrient_targets(
        res.get("target") or snapshot.get("calorie_target") or 2200,
        {**snapshot, "_current_weight_kg": (res.get("trend") or {}).get("current_kg")})
    return res


# ================================================================ HTTP layer
MAX_BODY = 20 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "MacroLog/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        with _LOG_LOCK:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers ---------------------------------------------------------
    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return None, "body too large"
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            fields, upload = self._parse_multipart(raw, ctype)
            self._upload = upload          # consumed by POST_estimate
            return fields, None
        self._upload = None
        if not raw:
            return {}, None
        try:
            return json.loads(raw.decode()), None
        except (ValueError, UnicodeDecodeError):
            return None, "invalid JSON body"

    def _parse_multipart(self, raw: bytes, ctype: str):
        """Minimal multipart/form-data reader.

        Returns (text_fields, (photo_path, mime) | None). The photo is written
        to a temp file in PHOTO_DIR; it is promoted on save or deleted if the
        estimate fails / the user cancels.
        """
        boundary = None
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part.split("=", 1)[1].strip('"')
        if not boundary:
            return {}, None
        fields = {}
        upload = None
        for chunk in raw.split(b"--" + boundary.encode()):
            chunk = chunk.strip(b"\r\n")
            if not chunk or chunk == b"--":
                continue
            head, sep, body = chunk.partition(b"\r\n\r\n")
            if not sep:
                continue
            name = filename = None
            for line in head.decode(errors="replace").split("\r\n"):
                if not line.lower().startswith("content-disposition"):
                    continue
                for bit in line.split(";"):
                    bit = bit.strip()
                    if bit.startswith("name="):
                        name = bit.split("=", 1)[1].strip('"')
                    elif bit.startswith("filename="):
                        filename = bit.split("=", 1)[1].strip('"')
            if name is None:
                continue
            if filename is None:
                fields[name] = body.decode(errors="replace").strip()
            elif filename and body:
                ext = (os.path.splitext(filename)[1] or ".jpg").lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"):
                    ext = ".jpg"
                os.makedirs(PHOTO_DIR, exist_ok=True)
                tmp = os.path.join(PHOTO_DIR, f"tmp-{uuid.uuid4().hex}{ext}")
                with open(tmp, "wb") as fh:
                    fh.write(body)
                upload = (tmp, guess_mime(ext))
                fields["photo_path"] = tmp
        return fields, upload

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/"):
            return self._route("GET", u.path, q, {})
        return self._static(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        body, err = self._read_body()
        if err:
            return self._json(400, {"error": err})
        return self._route("POST", u.path, parse_qs(u.query), body or {})

    def do_PATCH(self):
        return self._write("PATCH")

    def do_PUT(self):
        return self._write("PUT")

    def do_DELETE(self):
        return self._write("DELETE")

    def _write(self, verb):
        u = urlparse(self.path)
        body, err = self._read_body()
        if err:
            return self._json(400, {"error": err})
        return self._route(verb, u.path, parse_qs(u.query), body or {})

    def _route(self, verb, path, q, body):
        parts = [p for p in path.strip("/").split("/") if p]
        try:
            if len(parts) < 2:
                return self._json(404, {"error": "unknown endpoint"})
            resource = parts[1]
            tail = parts[2] if len(parts) > 2 else None

            if resource == "state" and verb == "GET":
                st, pl = Api.GET_state(self, q, body)
            elif resource == "estimate" and verb == "POST":
                st, pl = Api.POST_estimate(self, q, body)
            elif resource == "meals" and verb == "POST":
                st, pl = Api.POST_meals(self, q, body)
            elif resource == "meals" and tail and verb == "PATCH":
                st, pl = Api.PATCH_meal(self, q, body, tail)
            elif resource == "meals" and tail and verb == "DELETE":
                st, pl = Api.DELETE_meal(self, q, body, tail)
            elif resource == "weight" and verb == "POST":
                st, pl = Api.POST_weight(self, q, body)
            elif resource == "exercise" and verb == "POST":
                st, pl = Api.POST_exercise(self, q, body)
            elif resource == "history" and verb == "GET":
                st, pl = Api.GET_history(self, q, body)
            elif resource == "adapt" and verb == "POST":
                st, pl = Api.POST_adapt(self, q, body)
            elif resource == "settings" and verb == "GET":
                st, pl = Api.GET_settings(self, q, body)
            elif resource == "settings" and verb == "PUT":
                st, pl = Api.PUT_settings(self, q, body)
            elif resource == "export" and verb == "GET":
                st, pl = Api.GET_export(self, q, body)
            elif resource == "test-provider" and verb == "POST":
                st, pl = Api.POST_test_provider(self, q, body)
            elif resource == "photo" and tail and verb == "GET":
                return self._photo(tail)
            else:
                st, pl = 404, {"error": "unknown endpoint"}
        except ValueError as e:
            st, pl = 400, {"error": f"bad input: {e}"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            st, pl = 500, {"error": f"{type(e).__name__}: {e}"}
        return self._json(st, pl)

    def _photo(self, name):
        safe = os.path.basename(name)
        for d in (PHOTO_DIR, *[os.path.join(PHOTO_DIR, x) for x in os.listdir(PHOTO_DIR)
                               if os.path.isdir(os.path.join(PHOTO_DIR, x))]):
            candidate = os.path.join(d, safe)
            if os.path.isfile(candidate):
                mime = mimetypes.guess_type(candidate)[0] or "image/jpeg"
                with open(candidate, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
        self._json(404, {"error": "photo not found"})

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            full = os.path.join(STATIC_DIR, "index.html")
            if not os.path.isfile(full):
                return self._json(404, {"error": "not found"})
        mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "image/svg+xml"):
            mime += "; charset=utf-8"
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def main():
    ap = argparse.ArgumentParser(description="MacroLog self-hosted calorie tracker")
    ap.add_argument("--host", default=os.environ.get("MACROLOG_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MACROLOG_PORT", 8765)))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    load_env()
    os.makedirs(PHOTO_DIR, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    shown = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    print(f"\n  MacroLog  ->  http://{shown}:{args.port}")
    if args.host == "0.0.0.0":
        for ip in lan_ips():
            print(f"              http://{ip}:{args.port}   (phone, same network)")
    s = get_settings()
    ready = [p["kind"] for p in s.get("providers", [])
             if os.environ.get({"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY",
                                "groq": "GROQ_API_KEY",
                                "qwenplan": "QWENPLAN_API_KEY"}.get(p["kind"], ""))]
    print(f"  vision:   {', '.join(ready) if ready else 'NONE - running offline stub'}")
    print("  Ctrl-C to stop\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
