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
import re
import shutil
import socket
import sys
import threading
import time
import uuid
from datetime import date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote as _urlquote, unquote as _urlunquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.adaptive import (accuracy_summary, adapt_targets, apply_calibration,
                          calibration_multipliers, macronutrient_targets,
                          record_correction, tdee_from_formula, weight_trend)
from app.database import Database
from app.vision import ProviderError, estimate_photo
from app import auth as OIDC

SESSION_COOKIE = "macrolog_session"
STATE_COOKIE = "macrolog_oidc_state"

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

# Canonical storage is metric; these convert the imperial presentation edge.
LB_PER_KG = 2.2046226
CM_PER_IN = 2.54


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


# Settings keys that are per-person vs shared across all users.
USER_SETTING_KEYS = {"units", "sex", "age", "height_cm", "activity_factor",
                     "weekly_rate_kg", "protein_g_per_kg", "fat_calorie_pct",
                     "calorie_target", "start_date", "starting_weight_kg",
                     "auto_adapt", "calibrate_estimates", "_profile_set"}


def get_settings(owner: str = "default") -> dict:
    """Merge defaults <- shared settings <- this user's personal settings."""
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    shared = db.get_setting("settings", {}) or {}
    if isinstance(shared, dict):
        shared.pop("api_key", None)
        # Only provider/vision config is shared; ignore stray legacy keys here.
        for k, v in shared.items():
            if k not in USER_SETTING_KEYS:
                merged[k] = v
    personal = db.get_setting(f"settings:{owner}", {}) or {}
    if isinstance(personal, dict):
        merged.update(personal)
    return merged


def save_user_settings(owner: str, updates: dict) -> None:
    cur = db.get_setting(f"settings:{owner}", {}) or {}
    for k, v in updates.items():
        if k in USER_SETTING_KEYS:
            cur[k] = v
    db.set_setting(f"settings:{owner}", cur)


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
        owner = getattr(h, "owner", "default")
        s = get_settings(owner)
        # ?date=YYYY-MM-DD browses a past day; absent/invalid = today.
        t = today_str()
        want = (q.get("date") or [""])[0]
        if want:
            try:
                t = date.fromisoformat(want[:10]).isoformat()
            except ValueError:
                return 400, {"error": "invalid date (use YYYY-MM-DD)"}
        meals = db.meals_for_date(t, owner)
        item_map = db.items_for_date(t, owner)
        consumed = {k: round(sum(float(m[k]) for m in meals), 1)
                    for k in ("calories", "protein", "carbs", "fat")}
        rows = db.query("SELECT date, value FROM weights WHERE owner=? ORDER BY date", (owner,))
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
            "date": t, "meals": [_meal_dict(m, item_map.get(m["id"], [])) for m in meals],
            "consumed": consumed, "targets": targets, "trend": trend,
            "settings": _public_settings(s), "providers": providers,
            "active_provider": ready[0] if ready else "stub",
            "exercise": _exercise_total(t, owner),
            "user": getattr(h, "user", None),
            "auth_enabled": OIDC.enabled(),
            # needs_setup = user hasn't customized their body profile yet (still on
            # defaults). Distinct from having logged a weight (weights_count).
            "needs_setup": not s.get("_profile_set"),
            "weights_count": len(rows),
        }

    # ---- meals --------------------------------------------------------
    @staticmethod
    def GET_meal(h, q, body, meal_id):
        """One meal + its components by id (owner-scoped), so a past-day tap can
        open the review sheet without shipping whole-day payloads."""
        owner = getattr(h, "owner", "default")
        try:
            mid = int(meal_id)
        except (TypeError, ValueError):
            return 400, {"error": "invalid meal id"}
        m = db.get_meal(mid, owner)
        if not m:
            return 404, {"error": "meal not found"}
        return 200, _meal_dict(m, db.items_for_meal(mid))

    @staticmethod
    def POST_estimate(h, q, body):
        """Photo -> estimate, preview only. Nothing is written until /meals."""
        owner = getattr(h, "owner", "default")
        upload = getattr(h, "_upload", None)
        if not upload:
            return 400, {"error": "no image received (send multipart form-data field 'photo')"}
        path, mime = upload
        try:
            meal, provider, model = estimate_photo(path, get_settings(owner))
        except ProviderError as e:
            _unlink(path)
            return 502, {"error": f"all providers failed: {e}"}
        except Exception as e:
            _unlink(path)
            return 500, {"error": f"{type(e).__name__}: {e}"}
        if not meal["items"] and not meal["calories"]:
            _unlink(path)
            return 422, {"error": meal.get("notes") or "no food detected in image"}
        s = get_settings(owner)
        changed = False
        if s.get("calibrate_estimates"):
            meal, changed = apply_calibration(meal, calibration_multipliers(db, owner=owner))
        return 200, {"estimate": meal, "provider": provider, "model": model,
                     "calibrated": changed, "temp_photo": path}

    @staticmethod
    def POST_meals(h, q, body):
        owner = getattr(h, "owner", "default")
        s = get_settings(owner)
        photo = body.get("photo_path")
        source = body.get("source", "photo")
        if photo and not os.path.abspath(photo).startswith(os.path.abspath(PHOTO_DIR)):
            return 400, {"error": "invalid photo path"}
        d = body.get("date") or today_str()
        ts = _parse_when(body, default_date=d)
        items = _sanitize_items(body.get("items"))
        meal = {
            "description": str(body.get("description") or "")[:200],
            "calories": float(body.get("calories") or 0),
            "protein": float(body.get("protein") or 0),
            "carbs": float(body.get("carbs") or 0),
            "fat": float(body.get("fat") or 0),
        }
        # When components are supplied, the meal total IS their sum (single source
        # of truth); otherwise fall back to the flat totals the client sent.
        if items:
            meal.update(_sum_items(items))
        if meal["calories"] <= 0:
            if photo and os.path.exists(photo):
                os.remove(photo)
            return 400, {"error": "calories must be > 0"}
        final_path = _promote_photo(photo) if photo else None
        mid = db.add_meal(
            date=d, ts=ts,
            kind=str(body.get("kind") or "other")[:20],
            description=meal["description"], calories=meal["calories"],
            protein=meal["protein"], carbs=meal["carbs"], fat=meal["fat"],
            confidence=float(body.get("confidence") or 0.5),
            photo_path=final_path, source=source,
            provider=str(body.get("provider") or "")[:30],
            model=str(body.get("model") or "")[:60],
            raw_json=json.dumps(body.get("raw") or {})[:8000], owner=owner)
        if items:
            db.replace_meal_items(mid, items)
        _maybe_adapt(s, owner=owner)
        return 201, {"id": mid, **_day_summary(d, owner)}

    @staticmethod
    def PATCH_meal(h, q, body, meal_id):
        owner = getattr(h, "owner", "default")
        mid = int(meal_id)
        old = db.get_meal(mid, owner)
        if not old:
            return 404, {"error": "meal not found"}
        # Components supplied -> replace them and derive totals from the sum.
        items = _sanitize_items(body.get("items")) if "items" in body else None
        fields = {k: v for k, v in body.items()
                  if k in ("kind", "description", "calories", "protein", "carbs", "fat")}
        new_date = old["date"]
        if items is not None:
            summed = _sum_items(items)
            fields.update(summed)          # totals follow components
            db.replace_meal_items(mid, items)
        # Date/time edits (only when provided).
        upd_ts = None
        if body.get("date") or body.get("time"):
            new_date = body.get("date") or old["date"]
            upd_ts = _parse_when({**body, "date": new_date}, default_date=new_date,
                                 fallback_ts=old["ts"])
            db.execute("UPDATE meals SET date=?, ts=? WHERE id=? AND owner=?",
                       (new_date, upd_ts, mid, owner))
        if fields:
            db.update_meal(mid, owner=owner, **fields)
        if not fields and items is None and upd_ts is None:
            return 400, {"error": "no editable fields supplied"}
        new = dict(old)
        new.update(fields)
        n = record_correction(db, mid, {
            "calories": old["calories"], "protein": old["protein"],
            "carbs": old["carbs"], "fat": old["fat"]}, new, int(time.time()))
        s = get_settings(owner)
        _maybe_adapt(s, owner=owner)
        summary_dates = {old["date"], new_date}
        result = {"updated": True, "corrections_recorded": n,
                  "multipliers": calibration_multipliers(db, owner=owner)}
        result.update(_day_summary(list(summary_dates)[0], owner))
        return 200, result

    @staticmethod
    def DELETE_meal(h, q, body, meal_id):
        owner = getattr(h, "owner", "default")
        row = db.delete_meal(int(meal_id), owner)
        if not row:
            return 404, {"error": "meal not found"}
        if row["photo_path"] and os.path.exists(row["photo_path"]):
            try:
                os.remove(row["photo_path"])
            except OSError:
                pass
        return 200, {"deleted": True, **_day_summary(row["date"], owner)}

    # ---- weight / composition -----------------------------------------
    @staticmethod
    def POST_weight(h, q, body):
        owner = getattr(h, "owner", "default")
        raw = float(body.get("value") or 0)
        # Trust the unit the CLIENT declared for this value; fall back to the
        # stored preference only if it omitted one. Never guess silently.
        unit = str(body.get("unit") or get_settings(owner).get("units", "metric")).lower()
        val = raw / LB_PER_KG if unit in ("lb", "imperial", "lbs") else raw
        if val <= 0 or val > 700:      # validated in canonical kg
            return 400, {"error": "weight out of range"}
        d = body.get("date") or today_str()
        db.set_weight(d, local_ts_for(d), round(val, 2), owner)
        if body.get("body_fat") is not None:
            bf = float(body["body_fat"])
            if 1 <= bf <= 70:
                db.set_body_fat(d, local_ts_for(d), bf, owner)
        s = get_settings(owner)
        if not s.get("start_date"):
            s["start_date"] = d
            s["starting_weight_kg"] = round(val, 2)
            save_user_settings(owner, {"start_date": d, "starting_weight_kg": round(val, 2)})
        result = _maybe_adapt(s, persist=True, owner=owner)
        return 201, {"logged": {"date": d, "kg": round(val, 2)}, **result}

    @staticmethod
    def GET_history(h, q, body):
        owner = getattr(h, "owner", "default")
        days = int((q.get("days") or ["90"])[0])
        start = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
        meals = db.query(
            "SELECT date, SUM(calories) c, SUM(protein) p, SUM(carbs) cb, "
            "SUM(fat) f, COUNT(*) n FROM meals WHERE date >= ? AND owner=? GROUP BY date "
            "ORDER BY date", (start, owner))
        weights = db.query(
            "SELECT date, value FROM weights WHERE date >= ? AND owner=? ORDER BY date", (start, owner))
        bf = db.query("SELECT date, value FROM body_fat WHERE date >= ? AND owner=? ORDER BY date",
                      (start, owner))
        ex = db.query("SELECT date, SUM(calories) c FROM exercises WHERE date >= ? AND owner=? "
                      "GROUP BY date", (start, owner))
        s = get_settings(owner)
        target = s.get("calorie_target") or tdee_from_formula(s) or 2200
        return 200, {
            "days": days, "target": target,
            "intake": [{"date": r["date"], "calories": round(r["c"] or 0),
                        "protein": round(r["p"] or 0), "carbs": round(r["cb"] or 0),
                        "fat": round(r["f"] or 0), "meals": r["n"]} for r in meals],
            "weights": [{"date": r["date"], "kg": r["value"]} for r in weights],
            "body_fat": [{"date": r["date"], "pct": r["value"]} for r in bf],
            "exercise": [{"date": r["date"], "calories": round(r["c"] or 0)} for r in ex],
            "accuracy": accuracy_summary(db, owner),
        }

    @staticmethod
    def POST_exercise(h, q, body):
        owner = getattr(h, "owner", "default")
        cal = float(body.get("calories") or 0)
        if cal <= 0:
            return 400, {"error": "calories must be > 0"}
        d = body.get("date") or today_str()
        db.execute("INSERT INTO exercises(owner,ts,date,name,calories) VALUES(?,?,?,?,?)",
                   (owner, local_ts_for(d), d, str(body.get("name") or "workout")[:60], cal))
        return 201, _day_summary(d, owner)

    @staticmethod
    def POST_adapt(h, q, body):
        owner = getattr(h, "owner", "default")
        s = get_settings(owner)
        res = _maybe_adapt(s, persist=True, force=True, owner=owner)
        return 200, res

    # ---- settings ------------------------------------------------------
    @staticmethod
    def GET_settings(h, q, body):
        owner = getattr(h, "owner", "default")
        s = get_settings(owner)
        return 200, {"settings": _public_settings(s),
                     "accuracy": accuracy_summary(db, owner),
                     "multipliers": calibration_multipliers(db, owner=owner),
                     "has_keys": {k: bool(os.environ.get(k)) for k in
                                  ("GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "QWENPLAN_API_KEY")}}

    @staticmethod
    def PUT_settings(h, q, body):
        owner = getattr(h, "owner", "default")
        # Per-person keys go to the user's own blob; shared keys (providers) stay global.
        personal = {k: v for k, v in body.items() if k in USER_SETTING_KEYS}
        shared = {k: v for k, v in body.items() if k == "providers"}
        # Saving any body/goal field marks the profile as configured (clears nag).
        if personal.get("units") or personal.get("age") or personal.get("height_cm") \
                or personal.get("sex") or personal.get("activity_factor"):
            personal["_profile_set"] = True
        if personal:
            save_user_settings(owner, personal)
        if shared:
            cur = db.get_setting("settings", {}) or {}
            cur.update(shared)
            db.set_setting("settings", cur)
        s = get_settings(owner)
        return 200, {"settings": _public_settings(s), "targets": macronutrient_targets(
            s.get("calorie_target") or tdee_from_formula(s) or 2200, s)}

    @staticmethod
    def GET_export(h, q, body):
        """Full data dump as JSON for the current user. Secrets are never included."""
        owner = getattr(h, "owner", "default")
        s = get_settings(owner)
        return 200, {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "settings": _public_settings(s),
            "meals": [_meal_dict(m) for m in db.query("SELECT * FROM meals WHERE owner=? ORDER BY ts", (owner,))],
            "weights": [dict(r) for r in db.query("SELECT date,value FROM weights WHERE owner=? ORDER BY date", (owner,))],
            "body_fat": [dict(r) for r in db.query("SELECT date,value FROM body_fat WHERE owner=? ORDER BY date", (owner,))],
            "exercises": [dict(r) for r in db.query("SELECT date,name,calories FROM exercises WHERE owner=? ORDER BY date", (owner,))],
            "corrections": [dict(r) for r in db.query(
                "SELECT m.date, m.description, e.field, e.predicted, e.actual "
                "FROM estimates e JOIN meals m ON m.id=e.meal_id WHERE m.owner=? ORDER BY e.logged_at", (owner,))],
            "accuracy": accuracy_summary(db, owner),
            "multipliers": {f: calibration_multipliers(db, f, owner=owner)
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


def _meal_dict(m, items=None) -> dict:
    d = {"id": m["id"], "ts": m["ts"], "date": m["date"], "kind": m["kind"],
         "description": m["description"], "calories": m["calories"],
         "protein": m["protein"], "carbs": m["carbs"], "fat": m["fat"],
         "confidence": m["confidence"], "source": m["source"],
         "provider": m["provider"], "model": m["model"],
         "photo": os.path.basename(m["photo_path"]) if m["photo_path"] else None}
    if items is not None:
        d["items"] = items
    return d


def _sanitize_items(raw) -> list[dict]:
    """Coerce client-supplied components into safe rows (name + grams + macros)."""
    out = []
    for it in (raw or [])[:40]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:120]
        g = max(0.0, float(it.get("grams") or 0))
        cal = max(0.0, float(it.get("calories") or 0))
        if not name and g == 0 and cal == 0:
            continue
        out.append({"name": name, "grams": g, "calories": cal,
                    "protein": max(0.0, float(it.get("protein") or 0)),
                    "carbs": max(0.0, float(it.get("carbs") or 0)),
                    "fat": max(0.0, float(it.get("fat") or 0))})
    return out


def _sum_items(items: list[dict]) -> dict:
    tot = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
    for it in items:
        for k in tot:
            tot[k] += float(it.get(k) or 0)
    return {k: round(v, 1) for k, v in tot.items()}


def _parse_when(body: dict, default_date: str, fallback_ts: int | None = None) -> int:
    """Resolve a meal's epoch ts from optional date (YYYY-MM-DD) + time (HH:MM).

    Missing time keeps the current clock time on that day; missing both falls
    back to local_ts_for(default_date) so behaviour matches the old path.
    """
    d = body.get("date") or default_date
    t = body.get("time")
    try:
        dt = date.fromisoformat(str(d)[:10])
    except ValueError:
        return fallback_ts if fallback_ts is not None else local_ts_for(default_date)
    if t and re.match(r"^\d{1,2}:\d{2}", str(t)):
        hh, mm = str(t)[:5].split(":")
        return int(datetime(dt.year, dt.month, dt.day,
                            min(23, int(hh)), min(59, int(mm))).timestamp())
    if fallback_ts is not None:
        # Keep the existing clock time-of-day when only the date changed.
        base = datetime.fromtimestamp(fallback_ts)
        return int(datetime(dt.year, dt.month, dt.day, base.hour, base.minute).timestamp())
    now = datetime.now()
    return int(datetime(dt.year, dt.month, dt.day, now.hour, now.minute).timestamp())


def _exercise_total(d: str, owner: str = "default") -> float:
    row = db.one("SELECT SUM(calories) c FROM exercises WHERE date=? AND owner=?", (d, owner))
    return round(row["c"] or 0) if row else 0.0


def _day_summary(d: str, owner: str = "default") -> dict:
    meals = db.meals_for_date(d, owner)
    item_map = db.items_for_date(d, owner)
    return {"date": d, "consumed": {
        k: round(sum(float(m[k]) for m in meals), 1)
        for k in ("calories", "protein", "carbs", "fat")},
        "meals": [_meal_dict(m, item_map.get(m["id"], [])) for m in meals],
        "exercise": _exercise_total(d, owner)}


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


def _maybe_adapt(settings: dict, persist: bool = False, force: bool = False,
                 owner: str = "default") -> dict:
    """Run the weight->target loop when appropriate; optionally save result."""
    if not (force or settings.get("auto_adapt", True)):
        return {"adapted": False, "reason": "auto-adapt disabled"}
    snapshot = json.loads(json.dumps(settings))
    res = adapt_targets(db, settings, owner=owner)
    if res.get("adjusted") and persist:
        save_user_settings(owner, {"calorie_target": settings["calorie_target"]})
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
    def _cookies(self) -> dict:
        raw = self.headers.get("Cookie", "")
        jar = {}
        for chunk in raw.split(";"):
            if "=" in chunk:
                k, _, v = chunk.strip().partition("=")
                jar[k] = _urlunquote(v)
        return jar

    def _base_url(self) -> str:
        """Externally-visible origin, honoring reverse-proxy headers (Caddy)."""
        override = os.environ.get("OIDC_REDIRECT_BASE")
        if override:
            return override.rstrip("/")
        proto = self.headers.get("X-Forwarded-Proto", "https" if hasattr(self, "request_version") else "http")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        return f"{proto}://{host}"

    def _set_cookie(self, name, value, max_age=None, path="/"):
        bits = [f"{name}={_urlquote(value)}", f"Path={path}", "HttpOnly"]
        # Auth cookies are always served over HTTPS (behind Caddy). iOS Safari
        # drops a SameSite=Lax cookie that lacks Secure when it survives a
        # cross-site OAuth redirect, so we must set Secure + Lax explicitly.
        secure = self._is_https()
        if secure:
            bits.append("Secure")
        bits.append("SameSite=Lax")
        if max_age is not None:
            bits.append(f"Max-Age={max_age}")
        self._pending_cookies.append("; ".join(bits))

    def _is_https(self) -> bool:
        if os.environ.get("OIDC_REDIRECT_BASE", "").startswith("https://"):
            return True
        return self.headers.get("X-Forwarded-Proto", "") == "https"

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        for c in getattr(self, "_pending_cookies", []):
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for c in getattr(self, "_pending_cookies", []):
            self.send_header("Set-Cookie", c)
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
    def _begin(self):
        self._pending_cookies = []

    def do_GET(self):
        self._begin()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/auth/"):
            return self._auth(u.path, q)
        if u.path.startswith("/api/"):
            return self._route("GET", u.path, q, {})
        # Unauthenticated users hitting the app root get bounced to login when
        # OIDC is on; otherwise serve the SPA.
        if u.path in ("/", "") and OIDC.enabled() and not self._owner():
            return self._redirect("/auth/login")
        return self._static(u.path)

    def do_POST(self):
        self._begin()
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
        self._begin()
        u = urlparse(self.path)
        body, err = self._read_body()
        if err:
            return self._json(400, {"error": err})
        return self._route(verb, u.path, parse_qs(u.query), body or {})

    # -- authentication --------------------------------------------------
    def _owner(self) -> str | None:
        """Resolved owner key for this request: OIDC subject, or 'default' when
        auth is disabled. Returns None if auth is on but no valid session."""
        if not OIDC.enabled():
            return "default"
        sess = OIDC.read_session_cookie(self._cookies().get(SESSION_COOKIE))
        return sess["sub"] if sess else None

    def _auth(self, path, q):
        base = self._base_url()
        if path == "/auth/login":
            if not OIDC.enabled():
                return self._redirect("/")
            nxt = (q.get("next") or ["/"])[0]
            if not nxt.startswith("/"):        # only allow same-origin paths
                nxt = "/"
            url, meta = OIDC.build_login_url(base, redirect_after=nxt)
            self._set_cookie(STATE_COOKIE, meta["state"], max_age=600)
            return self._redirect(url)
        if path == "/auth/callback":
            flat = {k: v[0] for k, v in q.items()}
            expected = self._cookies().get(STATE_COOKIE)
            try:
                user, after = OIDC.complete_login(base, flat, expected)
            except OIDC.AuthError as e:
                return self._json(400, {"error": f"login failed: {e}"})
            db.upsert_user(user["subject"], user.get("email"),
                           user.get("name"), user.get("picture"))
            self._set_cookie(SESSION_COOKIE, OIDC.make_session_cookie(user),
                             max_age=7 * 86400)
            self._set_cookie(STATE_COOKIE, "", max_age=0)   # clear state cookie
            return self._redirect(after if str(after).startswith("/") else "/")
        if path == "/auth/logout":
            self._set_cookie(SESSION_COOKIE, "", max_age=0)
            body = b'<html><body style="font-family:sans-serif;background:#0f1115;color:#e8eaf0;text-align:center;padding-top:20vh">Signed out.<p><a href="/auth/login" style="color:#4ade80">Log back in</a></p></body></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for c in self._pending_cookies:
                self.send_header("Set-Cookie", c)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._json(404, {"error": "unknown auth route"})

    def _route(self, verb, path, q, body):
        parts = [p for p in path.strip("/").split("/") if p]
        # Enforce auth on all API endpoints (except none today) when enabled.
        owner = self._owner()
        if OIDC.enabled() and owner is None:
            return self._json(401, {"error": "authentication required",
                                    "login": "/auth/login"})
        self.owner = owner or "default"
        sess = OIDC.read_session_cookie(self._cookies().get(SESSION_COOKIE)) if OIDC.enabled() else None
        self.user = ({"email": sess.get("email"), "name": sess.get("name")}
                     if sess else None)
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
            elif resource == "meals" and tail and verb == "GET":
                st, pl = Api.GET_meal(self, q, body, tail)
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
        # Stamp asset URLs with a build token so a stale service-worker cache can
        # never serve old JS/CSS after a deploy — the browser sees a new URL and
        # refetches. Token is the app.js mtime (changes on every code push).
        if full.endswith("index.html"):
            token = str(int(os.path.getmtime(os.path.join(STATIC_DIR, "app.js")))).encode()
            # Replace only the URL portion, keeping the HTML attribute's closing
            # quote intact — an earlier version swallowed it and broke both tags.
            data = re.sub(rb'(?:href|src)="/app\.(?:css|js)"',
                          lambda m: m.group(0)[:-1] + b'?v=' + token + b'"',
                          data)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        # HTML + assets must revalidate every load; photos can cache long.
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
    ap.add_argument("--tls-cert", default=os.environ.get("MACROLOG_TLS_CERT"),
                    help="PEM cert; enables HTTPS (required for iPhone camera over LAN)")
    ap.add_argument("--tls-key", default=os.environ.get("MACROLOG_TLS_KEY"),
                    help="PEM private key matching --tls-cert")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    load_env()
    os.makedirs(PHOTO_DIR, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    use_tls = bool(args.tls_cert and args.tls_key)
    if use_tls:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=args.tls_cert, keyfile=args.tls_key)
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):
            pass
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    elif args.tls_cert or args.tls_key:
        print("  ! both --tls-cert and --tls-key are required for HTTPS; running plain HTTP")

    scheme = "https" if use_tls else "http"
    shown = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    print(f"\n  MacroLog  ->  {scheme}://{shown}:{args.port}")
    if args.host == "0.0.0.0":
        for ip in lan_ips():
            print(f"              {scheme}://{ip}:{args.port}   (phone, same network)")
    s = get_settings()
    ready = [p["kind"] for p in s.get("providers", [])
             if os.environ.get({"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY",
                                "groq": "GROQ_API_KEY",
                                "qwenplan": "QWENPLAN_API_KEY"}.get(p["kind"], ""))]
    print(f"  vision:   {', '.join(ready) if ready else 'NONE - running offline stub'}")
    if use_tls:
        print("  tls:      on (HTTPS — iPhone camera will work)")
    print("  Ctrl-C to stop\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
