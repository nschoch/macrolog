"""MacroLog - self-hosted photo calorie & macro tracker.

Stdlib-only web server (http.server + sqlite3) so deployment is one command on
any machine with Python 3.9+: Mac, Linux, Proxmox LXC, Docker, a Pi.

    python3 server.py --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote as _urlquote, unquote as _urlunquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load_env():
    """Read KEY=VALUE lines from .env next to the project, without overriding env."""
    root = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(root, ".env"), os.path.join(os.path.dirname(root), ".env")):
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


# Load .env BEFORE the app modules import: several of them (and DATA_DIR,
# APP_TZ, argparse defaults below) read os.environ at import time. Only when run
# as a program, so test imports never pick up a developer's .env.
if __name__ == "__main__":
    load_env()


from app.adaptive import (accuracy_summary, adapt_targets, apply_calibration,
                          calibration_multipliers, macronutrient_targets,
                          record_correction, tdee_from_formula, weight_trend)
from app.database import Database
from app.vision import (PROVIDER_KINDS, ProviderError, estimate_meal,
                        estimate_photo, estimate_text, provider_default_base,
                        env_key_for, _same_base,
                        provider_default_model, provider_env_key,
                        provider_key_source)
from app import auth as OIDC
from app import foods as FOODS
from app import jev as JEVC

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
    "calorie_target_locked": False,  # true freezes the target against adaptation
    "target_adjusted_on": None,      # YYYY-MM-DD of the last adaptive move
    "health_token_hash": None,       # SHA-256 only; the token is shown once
    "health_token_created_at": None,
    "start_date": None,
    "starting_weight_kg": None,
    "auto_adapt": True,
    "calibrate_estimates": True,
    "time_format": "12h",            # 12h | 24h (display only; storage is epochs)
    "meal_gap_minutes": 45,          # meals logged within this gap = one sitting
    # Meal photos are files, not DB blobs. 0 keeps them forever; otherwise the
    # maintenance sweep deletes photos older than this many months (macros stay).
    "photo_retention_months": 12,
    # Provider chain, tried in order. Empty by default: every account brings
    # its own provider/key rather than inheriting the operator's. The offline
    # stub is appended automatically when no real provider is ready.
    "providers": [],
    # Derived at read time from providers other users marked shared=true. It is
    # never written into a user's settings blob.
    "shared_providers": [],
}

db = Database(DB_PATH)
_LOG_LOCK = threading.Lock()

# Canonical storage is metric; these convert the imperial presentation edge.
LB_PER_KG = 2.2046226
CM_PER_IN = 2.54


def _new_provider_id() -> str:
    """Stable per-entry id so reordering never makes one key adopt another's row."""
    return uuid.uuid4().hex[:12]


def _migrate_shared_providers() -> None:
    """Move the old server-wide provider chain into each existing account.

    Before providers became per-user, `settings.providers` was shared by every
    OIDC account. Copy it once to each account that existed at upgrade time (and
    to `default` for no-auth installs), then delete it from the shared blob.
    Accounts created afterwards start empty -- that is what stops a new family
    member from silently spending the operator's Qwen/OpenRouter key.
    """
    shared = db.get_setting("settings", {}) or {}
    if not isinstance(shared, dict) or "providers" not in shared:
        return
    providers = shared.pop("providers") or []
    if not isinstance(providers, list):
        providers = []
    for p in providers:
        if isinstance(p, dict) and not p.get("id"):
            p["id"] = _new_provider_id()

    owners = {"default"}
    try:
        owners.update(str(r["subject"]) for r in
                      db.query("SELECT subject FROM users"))
    except Exception:
        pass
    moved = 0
    for owner in owners:
        key = f"settings:{owner}"
        personal = db.get_setting(key, {}) or {}
        if not isinstance(personal, dict):
            personal = {}
        if "providers" not in personal:
            personal["providers"] = json.loads(json.dumps(providers))
            db.set_setting(key, personal)
            moved += 1
    db.set_setting("settings", shared)
    if providers:
        print(f"[migrate] moved shared provider chain to {moved} account(s)")


_migrate_shared_providers()


# Settings keys that are per-person vs shared across all users.
USER_SETTING_KEYS = {"units", "sex", "age", "height_cm", "activity_factor",
                     "weekly_rate_kg", "protein_g_per_kg", "fat_calorie_pct",
                     "calorie_target", "calorie_target_locked",
                     "target_adjusted_on", "health_token_hash",
                     "health_token_created_at", "start_date", "starting_weight_kg",
                     "auto_adapt", "calibrate_estimates", "_profile_set",
                     "meal_gap_minutes", "time_format", "providers",
                     "photo_retention_months"}


def _shared_owner_label(subject: str) -> str:
    """Human label for the person who shared a connection."""
    if subject == "default":
        return "the server"
    try:
        user = db.get_user(subject)
    except Exception:
        user = None
    if user:
        label = str(user.get("name") or user.get("email") or "").strip()
        if label:
            return label[:80]
    return "another user"


def _shared_providers_for(owner: str) -> list[dict]:
    """Providers other users marked `shared: true`, with their owner's label.

    Sharing is a live flag on the owner's own provider entry, not a copy: edits,
    key changes, disabling, and unsharing propagate immediately. The key is kept
    in memory for the estimator; `_public_settings` strips it before any response.
    """
    out = []
    try:
        rows = db.query(
            "SELECT key, value FROM settings WHERE key LIKE 'settings:%' ORDER BY key")
    except Exception:
        return out
    for row in rows:
        key = str(row["key"])
        subject = key[len("settings:"):] if key.startswith("settings:") else ""
        if not subject or subject == owner:
            continue
        try:
            blob = json.loads(row["value"])
        except Exception:
            continue
        providers = blob.get("providers") if isinstance(blob, dict) else None
        if not isinstance(providers, list):
            continue
        label = _shared_owner_label(subject)
        for p in providers:
            if not isinstance(p, dict) or p.get("enabled") is False:
                continue
            if p.get("shared") is not True or p.get("kind") not in PROVIDER_KINDS:
                continue
            if p.get("kind") == "stub":
                continue
            entry = json.loads(json.dumps(p))
            entry["shared"] = True
            entry["shared_by"] = label
            entry["_shared_owner"] = subject
            out.append(entry)
    return out


def _hash_health_token(token: str) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def _owner_for_health_token(token: str) -> str | None:
    """Resolve a Shortcut token to its owner. Tokens are stored hashed only."""
    if not token:
        return None
    digest = _hash_health_token(token)
    for row in db.query("SELECT key, value FROM settings WHERE key LIKE 'settings:%'"):
        try:
            blob = json.loads(row["value"])
        except Exception:
            continue
        stored = blob.get("health_token_hash") if isinstance(blob, dict) else None
        if stored and hmac.compare_digest(str(stored), digest):
            return str(row["key"])[len("settings:"):] or "default"
    return None


def get_settings(owner: str = "default") -> dict:
    """Merge defaults <- shared settings <- this user's personal settings."""
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    shared = db.get_setting("settings", {}) or {}
    if isinstance(shared, dict):
        shared.pop("api_key", None)
        # Only non-user keys are inherited from the shared blob. Providers are
        # per-user now; the startup migration moved the legacy shared chain.
        for k, v in shared.items():
            if k not in USER_SETTING_KEYS:
                merged[k] = v
    personal = db.get_setting(f"settings:{owner}", {}) or {}
    if isinstance(personal, dict):
        merged.update(personal)
    # A user cannot persist this derived list; always rebuild it from the other
    # accounts' current flags so unsharing/removal takes effect immediately.
    merged["shared_providers"] = _shared_providers_for(owner)
    return merged


def save_user_settings(owner: str, updates: dict) -> None:
    cur = db.get_setting(f"settings:{owner}", {}) or {}
    for k, v in updates.items():
        if k in USER_SETTING_KEYS:
            cur[k] = v
    db.set_setting(f"settings:{owner}", cur)


# The app is single-household and its users live in one timezone, but the LXC
# runs UTC -- so after ~5pm Pacific the server's "today" rolled to tomorrow and
# the whole day's meals vanished from the Today view (they were never deleted,
# just filed under a date the UI no longer asked for). Resolve "today" through
# an explicit zone instead of the host clock.
try:
    from zoneinfo import ZoneInfo
    APP_TZ = ZoneInfo(os.environ.get("MACROLOG_TZ", "America/Los_Angeles"))
except Exception:                                     # pragma: no cover
    APP_TZ = None


def resolve_tz(name: str | None):
    """Zone for a client-supplied IANA name, falling back to the household one.

    Each request may carry the browser's own zone, because a family member
    travelling (or a device with a different zone) must not have their meal
    times reinterpreted in someone else's timezone.
    """
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return APP_TZ


def now_local(tz=None) -> datetime:
    """Wall-clock time in the given zone (or the household zone)."""
    zone = tz or APP_TZ
    return datetime.now(zone) if zone else datetime.now()


def today_str(tz=None) -> str:
    return now_local(tz).date().isoformat()


def local_ts_for(date_str: str, fallback_hour: int = 12, tz=None) -> int:
    """Epoch for "now" on `date_str`, interpreted in `tz`.

    The datetime must be timezone-AWARE. A naive datetime is interpreted by the
    host clock, and the LXC runs UTC, so a 12:35 lunch was stored as 12:35 UTC
    (= 05:35 Pacific) -- which then displayed and grouped seven hours early.
    """
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return int(time.time())
    now = now_local(tz)
    return int(datetime(d.year, d.month, d.day, now.hour or fallback_hour,
                        now.minute, tzinfo=tz or APP_TZ).timestamp())


# ============================================================== API handlers
def _effective_providers(settings: dict) -> list[dict]:
    """A user's own chain followed by connections shared by other accounts."""
    return list(settings.get("providers") or []) + \
        list(settings.get("shared_providers") or [])


class Api:
    """Each method returns (status_code, payload_dict)."""

    # ---- bootstrap / state -------------------------------------------
    @staticmethod
    def GET_state(h, q, body):
        owner = getattr(h, "owner", "default")
        s = get_settings(owner)
        # ?date=YYYY-MM-DD browses a past day; absent/invalid = today in the
        # caller's own timezone.
        tz = h._tz()
        t = today_str(tz)
        want = (q.get("date") or [""])[0]
        if want:
            try:
                t = date.fromisoformat(want[:10]).isoformat()
            except ValueError:
                return 400, {"error": "invalid date (use YYYY-MM-DD)"}
        meals = db.meals_for_date(t, owner)
        item_map = db.items_for_date(t, owner)
        consumed = {k: round(sum(float(m[k]) for m in meals), 1)
                    for k in ("calories", "protein", "carbs", "fat", "fiber")}
        rows = db.query("SELECT date, value FROM weights WHERE owner=? ORDER BY date", (owner,))
        trend = weight_trend(rows)
        target = s.get("calorie_target") or tdee_from_formula(
            s, trend["current_kg"]) or 2200
        tmp = dict(s); tmp["_current_weight_kg"] = trend["current_kg"]
        targets = macronutrient_targets(target, tmp)
        # Disabled entries must not count as ready or become active_provider.
        effective = _effective_providers(s)
        ready = [p["kind"] for p in effective
                 if p.get("enabled", True) and _provider_ready(p)]
        providers = [{"id": p.get("id"), "kind": p.get("kind"),
                      "model": p.get("model"),
                      "shared": bool(p.get("shared")),
                      "shared_by": p.get("shared_by"),
                      "editable": not p.get("shared_by"),
                     "configured": (p.get("kind") == "stub") or (_provider_ready(p) and not p.get("unreachable"))}
                    for p in effective]
        dicts = [_meal_dict(m, item_map.get(m["id"], [])) for m in meals]
        return 200, {
            "date": t, "meals": dicts,
            # Derived eatings (see group_meals); gap is user-tunable.
            "groups": group_meals(dicts, int(s.get("meal_gap_minutes") or 45), tz=tz),
            "consumed": consumed, "targets": targets, "trend": trend,
            "settings": _public_settings(s), "providers": providers,
            "health_export": _health_export_status(owner),
            "active_provider": ready[0] if ready else "stub",
            "exercise": _exercise_total(t, owner),
            "user": getattr(h, "user", None),
            "auth_enabled": OIDC.enabled(),
            # needs_setup = user hasn't customized their body profile yet (still on
            # defaults). Distinct from having logged a weight (weights_count).
            "needs_setup": not s.get("_profile_set"),
            "weights_count": len(rows),
            # Newest weigh-in, so the dock can prefill it and warn when the
            # user has gone too long without one (trend quality decays fast).
            "last_weight": ({
                "date": rows[-1]["date"],
                "kg": float(rows[-1]["value"]),
                "days_ago": max(0, (date.fromisoformat(str(t)[:10])
                                    - date.fromisoformat(str(rows[-1]["date"])[:10])).days),
            } if rows else None),
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
        meal = _genericize_estimate(meal)
        if not meal["items"] and not meal["calories"]:
            _unlink(path)
            return 422, {"error": meal.get("notes") or "no food detected in image"}
        s = get_settings(owner)
        changed = False
        if s.get("calibrate_estimates"):
            meal, changed = apply_calibration(meal, calibration_multipliers(db, owner=owner))
        out = {"estimate": meal, "provider": provider, "model": model,
               "calibrated": changed, "temp_photo": path}
        errs = meal.get("_provider_errors")
        if errs:
            out["warning"] = _provider_warning(errs)
        return 200, out

    @staticmethod
    def POST_estimate_meal(h, q, body):
        """Merged capture: any number of photos (food and/or Nutrition Facts
        label) plus optional free text, estimated as ONE meal. Preview only —
        nothing is written until the client posts /meals.

        Supersedes choosing between photo-only and text-only at capture time,
        which is what lets a user photograph a label *and* say "I ate half".
        """
        owner = getattr(h, "owner", "default")
        images = [path for path, _mime in (getattr(h, "_uploads", None) or [])]
        if not images:                       # legacy single-photo field
            one = getattr(h, "_upload", None)
            if one:
                images = [one[0]]
        text = str((body or {}).get("text") or "").strip()[:2000]
        if not images and len(text) < 3:
            return 400, {"error": "add a photo or describe what you ate"}
        try:
            meal, provider, model = estimate_meal(images, text or None,
                                                  get_settings(owner))
            meal = _genericize_estimate(meal)
        except ProviderError as e:
            for path in images:
                _unlink(path)
            return 502, {"error": f"all providers failed: {e}"}
        except Exception as e:  # noqa: BLE001
            for path in images:
                _unlink(path)
            return 500, {"error": f"{type(e).__name__}: {e}"}
        if not meal["items"] and not meal["calories"]:
            for path in images:
                _unlink(path)
            return 422, {"error": meal.get("notes") or "no food recognized"}
        cur = get_settings(owner)
        changed = False
        if cur.get("calibrate_estimates"):
            meal, changed = apply_calibration(meal, calibration_multipliers(db, owner=owner))
        out = {"estimate": meal, "provider": provider, "model": model,
               "calibrated": changed,
               # Client keeps these and hands them back on save so every photo
               # ends up attached to the meal rather than orphaned in tmp.
               "temp_photos": images}
        # Optional second opinion: Jev is discriminative (typed questions, no
        # free text), so it cannot produce an estimate -- but it is a fast,
        # well-calibrated judge of whether this total is plausible for the
        # photo. Adds ~150 ms and fails open.
        try:
            if images:
                out["crosscheck"] = JEVC.crosscheck(
                    images, meal.get("calories") or 0, meal.get("description") or "")
        except Exception:                             # noqa: BLE001
            pass
        errs = meal.get("_provider_errors")
        if errs:
            out["warning"] = _provider_warning(errs)
        return 200, out

    @staticmethod
    def POST_rescan(h, q, body):
        """Re-estimate from photos we already hold (temp uploads or saved meals).

        Lets the user retry an estimate without re-photographing -- useful after
        a poor read, a provider change, or when label/notes context is added
        later. Writes nothing; the client decides whether to keep the result.
        """
        owner = getattr(h, "owner", "default")
        names = [str(x) for x in (body.get("photos") or [])][:4]
        paths = [p for p in (_resolve_photo(n) for n in names) if p]
        if not paths:
            return 400, {"error": "no photo available to rescan"}
        text = str(body.get("text") or "").strip()[:2000]
        try:
            meal, provider, model = estimate_meal(paths, text or None,
                                                  get_settings(owner))
            meal = _genericize_estimate(meal)
        except ProviderError as e:
            return 502, {"error": f"all providers failed: {e}"}
        except Exception as e:  # noqa: BLE001
            return 500, {"error": f"{type(e).__name__}: {e}"}
        if not meal["items"] and not meal["calories"]:
            return 422, {"error": meal.get("notes") or "no food recognized in image"}
        cur = get_settings(owner)
        changed = False
        if cur.get("calibrate_estimates"):
            meal, changed = apply_calibration(meal, calibration_multipliers(db, owner=owner))
        out = {"estimate": meal, "provider": provider, "model": model,
               "calibrated": changed, "photos": names}
        try:
            if paths:
                out["crosscheck"] = JEVC.crosscheck(
                    paths, meal.get("calories") or 0, meal.get("description") or "")
        except Exception:                             # noqa: BLE001
            pass
        errs = meal.get("_provider_errors")
        if errs:
            out["warning"] = _provider_warning(errs)
        return 200, out

    @staticmethod
    def POST_estimate_text(h, q, body):
        """Text -> AI estimate for described meals. Preview only; nothing is
        written until the client posts /meals with the returned estimate."""
        owner = getattr(h, "owner", "default")
        text = str(body.get("text") or "").strip()[:2000]
        if len(text) < 3:
            return 400, {"error": "describe your meal first"}
        try:
            meal, provider, model = estimate_text(text, get_settings(owner))
            meal = _genericize_estimate(meal)
        except ProviderError as e:
            return 502, {"error": f"all providers failed: {e}"}
        except Exception as e:
            return 500, {"error": f"{type(e).__name__}: {e}"}
        if not meal["items"] and not meal["calories"]:
            return 422, {"error": meal.get("notes") or "no food recognized in that description"}
        # NOTE: don't reuse `s` for the settings dict — it would shadow the
        # Content-Length value computed by _json() (late-binding closure bug).
        cur = get_settings(owner)
        changed = False
        if cur.get("calibrate_estimates"):
            meal, changed = apply_calibration(meal, calibration_multipliers(db, owner=owner))
        out = {"estimate": meal, "provider": provider, "model": model,
               "calibrated": changed}
        errs = meal.get("_provider_errors")
        if errs:
            out["warning"] = _provider_warning(errs)
        return 200, out

    @staticmethod
    def POST_meals(h, q, body):
        owner = getattr(h, "owner", "default")
        s = get_settings(owner)
        photo = body.get("photo_path")
        source = body.get("source", "photo")
        for cand in [photo, *(body.get("photos") or [])]:
            if cand and not os.path.abspath(str(cand)).startswith(
                    os.path.abspath(PHOTO_DIR)):
                return 400, {"error": "invalid photo path"}
        tz = h._tz()
        d = body.get("date") or today_str(tz)
        ts = _parse_when(body, default_date=d, tz=tz)
        items = _sanitize_items(body.get("items"))
        meal = {
            "description": str(body.get("description") or "")[:200],
            "calories": float(body.get("calories") or 0),
            "protein": float(body.get("protein") or 0),
            "carbs": float(body.get("carbs") or 0),
            "fat": float(body.get("fat") or 0),
            "fiber": float(body.get("fiber") or 0),
        }
        # When components are supplied, the meal total IS their sum (single source
        # of truth); otherwise fall back to the flat totals the client sent.
        if items:
            meal.update(_sum_items(items))
        # Zero-calorie entries are legitimate (creatine, sunfiber, black coffee,
        # supplements). Only reject when there is nothing at all to record.
        has_content = bool(str(meal.get("description") or "").strip()) or bool(items)
        if meal["calories"] < 0:
            if photo and os.path.exists(photo):
                os.remove(photo)
            return 400, {"error": "calories cannot be negative"}
        if meal["calories"] == 0 and not has_content:
            if photo and os.path.exists(photo):
                os.remove(photo)
            return 400, {"error": "add a description or components "
                                  "(calories may be 0 for supplements)"}
        # Accept several photos (e.g. the plate AND its Nutrition Facts label):
        # `photos` is a list, `photo_path` a single temp file for older clients.
        tmp_list = [str(x) for x in (body.get("photos") or []) if x]
        if not tmp_list and photo:
            tmp_list = [photo]
        tmp_list = [x for x in tmp_list
                    if os.path.abspath(x).startswith(os.path.abspath(PHOTO_DIR))]
        final_path = _store_photos([_promote_photo(x) for x in tmp_list])
        mid = db.add_meal(
            date=d, ts=ts,
            # "auto" = user declined to name it; grouping decides from time, so
            # persist a neutral value that won't override the derived label.
            kind=("other" if str(body.get("kind") or "other") == "auto"
                  else str(body.get("kind") or "other"))[:20],
            description=meal["description"], calories=meal["calories"],
            protein=meal["protein"], carbs=meal["carbs"], fat=meal["fat"],
            fiber=meal.get("fiber") or 0,
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
    def POST_copy_meal(h, q, body, meal_id):
        """Copy a saved meal to a target date (today by default).

        The copy is independent: its photos are duplicated so deleting either
        meal cannot remove the other's image. The original is never modified.
        """
        owner = getattr(h, "owner", "default")
        try:
            mid = int(meal_id)
        except (TypeError, ValueError):
            return 400, {"error": "invalid meal id"}
        old = db.get_meal(mid, owner)
        if not old:
            return 404, {"error": "meal not found"}
        tz = h._tz()
        d = str(body.get("date") or today_str(tz))[:10]
        try:
            d = date.fromisoformat(d).isoformat()
        except ValueError:
            return 400, {"error": "invalid date (use YYYY-MM-DD)"}
        # No time supplied -> keep the original's clock time on the new date, so
        # a copied lunch still groups as lunch.
        ts = _parse_when({"date": d, "time": body.get("time")},
                         default_date=d, fallback_ts=old["ts"], tz=tz)
        items = db.items_for_meal(mid)
        photo = _copy_photos(old["photo_path"], d)
        new_id = db.add_meal(
            date=d, ts=ts, kind=old["kind"], description=old["description"],
            calories=old["calories"], protein=old["protein"],
            carbs=old["carbs"], fat=old["fat"], fiber=old["fiber"],
            confidence=old["confidence"], photo_path=photo,
            source=old["source"], provider=old["provider"],
            model=old["model"], raw_json=old["raw_json"], owner=owner)
        if items:
            db.replace_meal_items(new_id, items)
        _maybe_adapt(get_settings(owner), owner=owner)
        return 201, {"id": new_id, "copied_from": mid, "date": d,
                     **_day_summary(d, owner)}

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
                  if k in ("kind", "description", "calories", "protein",
                           "carbs", "fat", "fiber")}
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
                                 tz=h._tz(),
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
        # A meal can own several photos (plate + label), stored as a JSON list;
        # deleting only the raw column string would orphan all of them.
        _delete_meal_photos(row["photo_path"])
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
        d = body.get("date") or today_str(h._tz())
        db.set_weight(d, local_ts_for(d, tz=h._tz()), round(val, 2), owner)
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
        start = (now_local(h._tz()).date() - timedelta(days=days)).isoformat()
        meals = db.query(
            "SELECT date, SUM(calories) c, SUM(protein) p, SUM(carbs) cb, "
            "SUM(fat) f, SUM(fiber) fb, COUNT(*) n FROM meals "
            "WHERE date >= ? AND owner=? GROUP BY date "
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
                        "fat": round(r["f"] or 0), "fiber": round(r["fb"] or 0),
                        "meals": r["n"]} for r in meals],
            "weights": [{"date": r["date"], "kg": r["value"]} for r in weights],
            "body_fat": [{"date": r["date"], "pct": r["value"]} for r in bf],
            "exercise": [{"date": r["date"], "calories": round(r["c"] or 0)} for r in ex],
            "accuracy": accuracy_summary(db, owner),
        }

    @staticmethod
    def GET_quick_foods(h, q, body):
        """Foods this user logs most often, for one-tap re-logging.

        Derived from their own meal_items history rather than a favourites
        table: supplements and staples reappear on their own, so there is
        nothing extra to curate. Names are folded to their generic food before
        counting (``steel cut oatmeal (66g, cooked)`` and ``cooked steel cut
        oats`` are one food), because otherwise a recurring bowl with a new
        weight each day never accumulates enough uses to reach the list.
        Ranking is frequency first, most-recent second; each entry carries the
        macros of its latest occurrence, so re-adding reproduces the last log.
        """
        owner = getattr(h, "owner", "default")
        try:
            limit = max(1, min(24, int((q.get("limit") or ["12"])[0])))
        except ValueError:
            limit = 12
        try:
            rows = db.query(
                "SELECT mi.name, mi.detail, mi.grams, mi.calories, mi.protein, "
                "mi.carbs, mi.fat, mi.fiber, m.ts AS last_ts "
                "FROM meal_items mi JOIN meals m ON m.id = mi.meal_id "
                "WHERE m.owner=? AND TRIM(mi.name) != '' "
                "ORDER BY m.ts ASC, mi.id ASC", (owner,))
        except Exception:                                  # noqa: BLE001
            return 200, {"foods": []}
        groups: dict[str, dict] = {}
        for row in rows:
            key = FOODS.food_key(row["name"])
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"uses": 0, "last_ts": 0, "latest": row,
                                   "labels": {}}
            g["uses"] += 1
            label, derived = FOODS.canonicalize(row["name"])
            g["labels"][label] = g["labels"].get(label, 0) + 1
            if row["last_ts"] >= g["last_ts"]:
                g["last_ts"] = row["last_ts"]
                g["latest"] = row
                g["latest_label"] = label
                g["latest_detail"] = derived
        ordered = sorted(groups.values(),
                         key=lambda g: (-g["uses"], -g["last_ts"]))
        foods = []
        for g in ordered[:limit]:
            latest = g["latest"]
            # The group's most common label reads more naturally than whichever
            # odd variant happened to be logged last.
            label = max(g["labels"].items(),
                        key=lambda kv: (kv[1], kv[0] == g.get("latest_label")))[0]
            detail = str(latest["detail"] or "").strip() or g.get("latest_detail") or ""
            foods.append({
                "name": label or latest["name"],
                "detail": detail,
                "grams": round(latest["grams"] or 0, 1),
                "calories": round(latest["calories"] or 0, 1),
                "protein": round(latest["protein"] or 0, 1),
                "carbs": round(latest["carbs"] or 0, 1),
                "fat": round(latest["fat"] or 0, 1),
                "fiber": round((latest["fiber"] if "fiber" in latest.keys() else 0) or 0, 1),
                "uses": g["uses"],
            })
        return 200, {"foods": foods}

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
                                  ("GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
                                   "QWENPLAN_API_KEY", "OPENROUTER_API_KEY")}}

    @staticmethod
    def PUT_settings(h, q, body):
        owner = getattr(h, "owner", "default")
        # Providers (and their keys) are personal now; a request can only ever
        # write the signed-in user's own blob. Provider entries are sanitized so
        # a client cannot inject arbitrary keys or non-http URLs.
        current = get_settings(owner)
        personal = {k: v for k, v in body.items() if k in USER_SETTING_KEYS}
        if "photo_retention_months" in personal:
            try:
                personal["photo_retention_months"] = max(
                    0, min(120, int(personal["photo_retention_months"])))
            except (TypeError, ValueError):
                personal.pop("photo_retention_months", None)
        if "providers" in body:
            personal["providers"] = _clean_providers(
                body.get("providers"), current.get("providers") or [])
        if personal.get("calorie_target_locked") is True:
            # A lock is a freeze: auto-adapt must not remain enabled in the UI.
            personal["auto_adapt"] = False
            if "calorie_target" not in personal:
                rate = float(current.get("weekly_rate_kg") or 0.0)
                personal["calorie_target"] = round(
                    tdee_from_formula(current) + rate * 7700.0 / 7.0)
        # Saving any body/goal field marks the profile as configured (clears nag).
        if personal.get("units") or personal.get("age") or personal.get("height_cm") \
                or personal.get("sex") or personal.get("activity_factor"):
            personal["_profile_set"] = True
        if personal:
            save_user_settings(owner, personal)
        s = get_settings(owner)
        return 200, {"settings": _public_settings(s), "targets": macronutrient_targets(
            s.get("calorie_target") or tdee_from_formula(s) or 2200, s)}

    @staticmethod
    def POST_health_token(h, q, body):
        """Create a fresh read-only Shortcut token, shown once."""
        owner = getattr(h, "owner", "default")
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        save_user_settings(owner, {"health_token_hash": _hash_health_token(token),
                                   "health_token_created_at": now})
        return 200, {"token": token, "created_at": now,
                     "endpoint": "/health/day"}

    @staticmethod
    def DELETE_health_token(h, q, body):
        owner = getattr(h, "owner", "default")
        save_user_settings(owner, {"health_token_hash": None,
                                   "health_token_created_at": None})
        return 200, {"revoked": True}

    @staticmethod
    def POST_provider_models(h, q, body):
        """List models for a provider using a draft or already-saved key.

        The browser never talks to the provider directly: that would expose the
        key to CORS preflights and third-party origins, and some providers do
        not send CORS headers at all.
        """
        owner = getattr(h, "owner", "default")
        current = get_settings(owner).get("providers") or []
        saved = next((p for p in current
                      if isinstance(p, dict) and p.get("id") == body.get("provider_id")), None)
        kind = str(body.get("kind") or (saved or {}).get("kind") or "").strip().lower()
        if kind not in PROVIDER_KINDS:
            return 400, {"error": "unknown provider type"}
        if kind == "stub":
            return 200, {"models": []}

        saved_base = str((saved or {}).get("base_url") or "").strip().rstrip("/")
        base = str(body.get("base_url") or saved_base or
                   provider_default_base(kind)).strip().rstrip("/")
        if not _valid_provider_url(base):
            return 400, {"error": "base URL must be a full http(s) URL"}
        # A key the caller did not type in this request may only travel to the
        # host it was saved for (or, for the operator's env key, the provider's
        # default host). Otherwise a draft base_url could exfiltrate it.
        key = str(body.get("api_key") or "").strip()
        if not key and saved and saved.get("kind") == kind and saved.get("api_key") \
                and _same_base(base, saved_base or provider_default_base(kind)):
            key = str(saved["api_key"]).strip()
        if not key:
            key = env_key_for(kind, base)
        try:
            models = _fetch_models(kind, base, key)
        except Exception as e:
            # Provider error bodies can be verbose; keep the response short and
            # redact the key if a gateway echoes it back.
            detail = str(e)
            if key:
                detail = detail.replace(key, "[redacted]")
            return 400, {"error": f"could not load models: {detail[:240]}"}
        return 200, {"models": models, "kind": kind, "base_url": base}

    @staticmethod
    def GET_export(h, q, body):
        """Full data dump as JSON for the current user. Secrets are never included."""
        owner = getattr(h, "owner", "default")
        s = get_settings(owner)
        settings = _public_settings(s)
        # Connections shared by other accounts are not part of this user's
        # backup; they remain owned by whoever shared them.
        settings.pop("shared_providers", None)
        return 200, {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "settings": settings,
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
            "health_exports": [dict(r) for r in db.query(
                "SELECT date, requested_at, confirmed_at FROM health_exports "
                "WHERE owner=? ORDER BY date", (owner,))],
        }

    @staticmethod
    def POST_test_provider(h, q, body):
        """Ping each enabled provider this user saved with a tiny request."""
        owner = getattr(h, "owner", "default")
        results = []
        for p in _effective_providers(get_settings(owner)):
            if p.get("enabled") is False:
                continue
            kind = p.get("kind")
            if kind == "stub":
                # The offline placeholder has no endpoint to ping; it is not a
                # connection failure worth showing next to a real provider.
                continue
            model = p.get("model") or provider_default_model(kind)
            shared_by = p.get("shared_by")
            try:
                key = str(p.get("api_key") or
                          env_key_for(kind, p.get("base_url"))).strip()
                if not key and kind not in ("custom", "ollama"):
                    raise ProviderError("no API key configured")
                base = str(p.get("base_url") or
                           provider_default_base(kind)).strip().rstrip("/")
                if not _valid_provider_url(base):
                    raise ProviderError("base URL must be a full http(s) URL")
                url, payload, headers = _ping_request(kind, base, model, key)
                data = json.dumps(payload).encode() if payload is not None else None
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json", **headers})
                t0 = time.time()
                # A user-supplied base URL must not be able to redirect the
                # server into an unrelated internal address; _NoRedirect makes
                # the request fail on any 3xx instead of following it.
                opener = urllib.request.build_opener(_NoRedirect())
                with opener.open(req, timeout=30) as r:
                    json.loads(r.read())
                results.append({"id": p.get("id"), "kind": kind, "model": model,
                                "shared_by": shared_by, "ok": True,
                                "ms": int((time.time() - t0) * 1000)})
            except Exception as e:
                msg = str(e)
                if hasattr(e, "read"):
                    try:
                        msg = e.read().decode()[:200]
                    except Exception:
                        pass
                if key:
                    msg = msg.replace(key, "[redacted]")
                results.append({"id": p.get("id"), "kind": kind, "model": model,
                                "shared_by": shared_by, "ok": False,
                                "error": type(e).__name__ + ": " + msg[:220]})
        return 200, {"results": results}


def _valid_provider_url(url: str) -> bool:
    """Only plain http(s) URLs with a host; no credentials in the URL.

    We intentionally allow loopback/private hosts: Ollama and LAN OpenAI-
    compatible servers are a first-class use case. Redirects are disabled by
    _open_json so a user-supplied host cannot bounce the server into an
    unrelated internal address.
    """
    if not url or len(url) > 500:
        return False
    if any(ord(c) < 32 for c in url):
        return False
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return (u.scheme in ("http", "https") and bool(u.hostname)
            and not u.username and not u.password
            and not u.query and not u.fragment)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_json(url: str, headers: dict | None = None, timeout: int = 20):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise ValueError(f"HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise ValueError(f"network: {e.reason}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError("provider returned non-JSON") from None


def _fetch_models(kind: str, base: str, key: str) -> list[dict]:
    """Normalize a provider's model list for the Settings picker.

    OpenRouter gets special treatment: its /models payload is public and carries
    an `architecture.input_modalities` array, so we can hide the ~25% of models
    that cannot accept a photo. Other OpenAI-compatible gateways usually expose
    only ids; manual entry remains available in the UI.
    """
    base = base.rstrip("/")
    if kind == "gemini":
        data = _open_json(f"{base}/v1beta/models?pageSize=1000",
                          {"x-goog-api-key": key} if key else {}, timeout=20)
        rows = data.get("models") if isinstance(data, dict) else []
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            methods = row.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue
            mid = str(row.get("name") or "").split("/")[-1]
            if mid:
                out.append({"id": mid, "name": row.get("displayName") or mid,
                            "context_length": row.get("inputTokenLimit"),
                            "vision": None, "pricing": {}})
        return out[:500]

    if kind == "ollama":
        data = _open_json(f"{base}/api/tags", {}, timeout=10)
        rows = data.get("models") if isinstance(data, dict) else []
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            mid = str(row.get("model") or row.get("name") or "")
            if mid:
                out.append({"id": mid, "name": mid,
                            "context_length": None,
                            "vision": None, "pricing": {}})
        return out[:500]

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if kind == "openrouter":
        headers.update({"X-Title": "MacroLog",
                        "HTTP-Referer": "https://github.com/nschoch/macrolog"})
    data = _open_json(f"{base}/models", headers, timeout=20)
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("data") or data.get("models") or []
    else:
        rows = []
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or row.get("name") or "").strip()
        if not mid:
            continue
        if kind == "openrouter" and mid.endswith(":batch"):
            # Batch endpoints are cheaper but not interactive; they would make
            # a photo estimate wait behind a queued job.
            continue
        arch = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
        modalities = arch.get("input_modalities") or []
        has_arch = bool(modalities) or bool(arch.get("modality"))
        vision = (("image" in modalities) or
                  ("image" in str(arch.get("modality") or ""))) if has_arch else None
        if kind == "openrouter" and vision is not True:
            continue
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        clean_pricing = {k: pricing[k] for k in ("prompt", "completion", "image")
                         if pricing.get(k) is not None}
        out.append({
            "id": mid,
            "name": str(row.get("name") or mid)[:160],
            "context_length": row.get("context_length") or row.get("context_window"),
            "vision": vision,
            "pricing": clean_pricing,
        })
    return out[:500]


def _ping_request(kind, base, model, key=""):
    """Build a tiny provider-specific request used by the Test button."""
    base = base.rstrip("/")
    if kind == "gemini":
        return (f"{base}/v1beta/models/{model}:generateContent",
                {"contents": [{"parts": [{"text": "ping"}]}]},
                {"x-goog-api-key": key})
    if kind == "ollama":
        return (f"{base}/api/tags", None, {})
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if kind == "openrouter":
        headers.update({"X-Title": "MacroLog",
                        "HTTP-Referer": "https://github.com/nschoch/macrolog"})
    return (f"{base}/chat/completions",
            {"model": model, "messages": [{"role": "user", "content": "ping"}],
             "max_tokens": 5},
            headers)


def _provider_ready(entry: dict) -> bool:
    """Mirror vision.build_chain()'s selection so the UI badge matches what the
    estimator will actually do: switched-off entries are never ready."""
    if entry.get("enabled") is False:
        return False
    from app.vision import _has_credentials
    return _has_credentials(entry)


def _clean_providers(raw, existing=None) -> list[dict]:
    """Validate a client-supplied provider chain without ever logging its keys.

    Stable ids let the UI reorder/edit rows while the server keeps the matching
    saved key. An absent `api_key` preserves the existing key; an explicit empty
    string (or `clear_key`) removes it, which backs the "Remove saved key"
    control.
    """
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("providers must be a list")
    if len(raw) > 12:
        raise ValueError("too many providers (max 12)")
    existing = [p for p in (existing or []) if isinstance(p, dict)]
    by_id = {str(p.get("id")): p for p in existing if p.get("id")}
    out, seen = [], set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in PROVIDER_KINDS:
            continue
        raw_id = str(item.get("id") or "").strip()
        src = by_id.get(raw_id) if raw_id else (existing[i] if i < len(existing) else None)
        pid = raw_id or str((src or {}).get("id") or "")
        if not pid or pid in seen:
            pid = _new_provider_id()
        seen.add(pid)

        model = str(item.get("model") or "").strip()[:200]
        base = str(item.get("base_url") or "").strip()
        if len(base) > 500:
            raise ValueError(f"{kind}: base URL is too long")
        if base and not _valid_provider_url(base):
            raise ValueError(f"{kind}: base URL must be a full http(s) URL")
        if kind == "custom" and (not base or not model):
            raise ValueError("custom provider needs a base URL and a model")
        enabled = item.get("enabled") is not False
        same_kind = src is not None and src.get("kind") == kind
        # An explicit flag wins. An old client that does not send it keeps the
        # existing share state, but a kind change does not silently re-share the
        # new provider with everyone.
        if "shared" in item:
            shared = item.get("shared") is True
        else:
            shared = bool((src or {}).get("shared")) and same_kind
        if kind == "stub":
            shared = False
        entry = {"id": pid, "kind": kind, "model": model,
                 "base_url": base, "enabled": enabled, "shared": shared}

        # Only carry a key across an edit of the same provider kind; changing
        # OpenAI -> OpenRouter must not silently reuse the old credential. A
        # non-empty api_key always wins over clear_key so the two controls
        # cannot cancel each other out in a surprising order.
        carries_key = same_kind
        new_key = str(item.get("api_key") or "").strip()
        if len(new_key) > 500:
            raise ValueError("API key is too long")
        if new_key:
            entry["api_key"] = new_key
        elif item.get("clear_key") or "api_key" in item:
            pass
        elif carries_key and src.get("api_key"):
            entry["api_key"] = str(src["api_key"])[:500]
        out.append(entry)
    return out


def _public_provider(p: dict, *, shared_by=None, editable=True) -> dict:
    return {
        "id": p.get("id") or "",
        "kind": p.get("kind"),
        "model": p.get("model"),
        "base_url": p.get("base_url"),
        "enabled": p.get("enabled", True),
        "has_key": bool(p.get("api_key")),
        "key_source": provider_key_source(p),
        "shared": bool(p.get("shared")),
        "shared_by": shared_by,
        "editable": editable,
    }


def _public_settings(s: dict) -> dict:
    out = dict(s)
    # A legacy top-level key (from before providers owned their own keys) must
    # not leak either. Provider keys are stripped below.
    out.pop("api_key", None)
    out["health_token_set"] = bool(out.pop("health_token_hash", None))
    # api_key is deliberately NOT included: secrets stay on the server and are
    # write-only from the browser's perspective. `has_key` distinguishes a saved
    # key from one inherited via the server's environment (`key_source: env`).
    out["providers"] = [_public_provider(p)
                        for p in s.get("providers", []) if isinstance(p, dict)]
    out["shared_providers"] = [
        _public_provider(p, shared_by=p.get("shared_by"), editable=False)
        for p in s.get("shared_providers", []) if isinstance(p, dict)
    ]
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
         "fiber": (m["fiber"] if "fiber" in m.keys() else 0),
         "confidence": m["confidence"], "source": m["source"],
         "provider": m["provider"], "model": m["model"],
         "photo": _photo_list(m["photo_path"])[0] if m["photo_path"] else None,
        "photos": _photo_list(m["photo_path"])}
    if items is not None:
        d["items"] = items
    return d


def _genericize_estimate(meal: dict) -> dict:
    """Give estimator items a generic food label plus a specific detail line.

    Models name a scan after what they see ("Chobani Nonfat Plain Greek Yogurt
    (protein rich)"), which makes the same breakfast a different "food" every
    day. The review sheet and the quick-log chips key off the generic label;
    the original brand/variant wording rides along in `detail` so it is not
    lost when the item is saved.
    """
    for item in meal.get("items") or []:
        raw = str(item.get("name") or "")
        label, derived = FOODS.canonicalize(raw)
        if label:
            item["name"] = label
        detail = str(item.get("detail") or "").strip()
        if not detail or detail.casefold() == raw.strip().casefold():
            item["detail"] = derived
        else:
            item["detail"] = detail
    return meal


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
        out.append({"name": name, "detail": str(it.get("detail") or "").strip()[:120],
                    "grams": g, "calories": cal,
                    "protein": max(0.0, float(it.get("protein") or 0)),
                    "carbs": max(0.0, float(it.get("carbs") or 0)),
                    "fat": max(0.0, float(it.get("fat") or 0)),
                    "fiber": max(0.0, float(it.get("fiber") or 0))})
    return out


def _sum_items(items: list[dict]) -> dict:
    tot = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "fiber": 0.0}
    for it in items:
        for k in tot:
            tot[k] += float(it.get(k) or 0)
    return {k: round(v, 1) for k, v in tot.items()}


def _parse_when(body: dict, default_date: str, fallback_ts: int | None = None,
                tz=None) -> int:
    """Resolve a meal's epoch ts from optional date (YYYY-MM-DD) + time (HH:MM).

    Every conversion is tz-aware so the wall-clock time the user typed is the
    time actually stored -- see local_ts_for for why naive datetimes are wrong
    on this host.
    """
    zone = tz or APP_TZ
    d = body.get("date") or default_date
    t = body.get("time")
    try:
        dt = date.fromisoformat(str(d)[:10])
    except ValueError:
        return fallback_ts if fallback_ts is not None else local_ts_for(default_date, tz=zone)
    if t and re.match(r"^\d{1,2}:\d{2}", str(t)):
        hh, mm = str(t)[:5].split(":")
        return int(datetime(dt.year, dt.month, dt.day,
                            min(23, int(hh)), min(59, int(mm)),
                            tzinfo=zone).timestamp())
    if fallback_ts is not None:
        # Keep the existing clock time-of-day when only the date changed.
        base = datetime.fromtimestamp(fallback_ts, zone)
        return int(datetime(dt.year, dt.month, dt.day, base.hour, base.minute,
                            tzinfo=zone).timestamp())
    now = now_local(zone)
    return int(datetime(dt.year, dt.month, dt.day, now.hour, now.minute,
                        tzinfo=zone).timestamp())


def _provider_warning(errors: list) -> str:
    """Short, human-readable reason the real providers were skipped."""
    blob = " ".join(errors)
    if "insufficient_quota" in blob or "quota" in blob.lower():
        return ("Your vision provider's quota is exhausted, so this is an "
                "offline placeholder estimate — not real nutrition data. "
                "Editing it now would train the calibrator on fake numbers.")
    if "429" in blob:
        return ("Your vision provider is rate-limiting requests, so this is an "
                "offline placeholder estimate — not real nutrition data.")
    if "401" in blob or "403" in blob:
        return ("Your vision provider rejected the API key, so this is an "
                "offline placeholder estimate — not real nutrition data.")
    return ("No vision provider answered (" + errors[0][:120] + "), so this is "
            "an offline placeholder estimate — not real nutrition data.")


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


def _health_day_payload(d: str, owner: str) -> dict:
    """Read-only day payload for the Apple Health Shortcut.

    Daily totals are the simple path; `meals` is included for people who want
    per-meal Health samples, and weight/exercise are optional extras.
    """
    meals = db.meals_for_date(d, owner)
    item_map = db.items_for_date(d, owner)
    totals = {k: round(sum(float(m[k]) for m in meals), 1)
              for k in ("calories", "protein", "carbs", "fat", "fiber")}
    rows = []
    for m in meals:
        rows.append({
            "time": datetime.fromtimestamp(m["ts"], APP_TZ).strftime("%H:%M"),
            "timestamp": m["ts"],
            "kind": m["kind"],
            "description": m["description"],
            "calories": m["calories"], "protein": m["protein"],
            "carbs": m["carbs"], "fat": m["fat"], "fiber": m["fiber"],
            "items": item_map.get(m["id"], []),
        })
    w = db.one("SELECT date, value FROM weights WHERE owner=? AND date=?",
               (owner, d))
    bf = db.one("SELECT date, value FROM body_fat WHERE owner=? AND date=?",
                (owner, d))
    sample_at = datetime.fromisoformat(d + "T12:00:00")
    if APP_TZ is not None:
        sample_at = sample_at.replace(tzinfo=APP_TZ)
    return {
        "date": d,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "totals": totals,
        "meals": rows,
        "weight": ({"date": w["date"], "kg": round(float(w["value"]), 2)}
                   if w else None),
        "body_fat_pct": round(float(bf["value"]), 1) if bf else None,
        "exercise_calories": _exercise_total(d, owner),
        "sample_at": sample_at.isoformat(),
        "units": {"calories": "kcal", "protein": "g", "carbs": "g",
                  "fat": "g", "fiber": "g", "weight": "kg",
                  "body_fat": "%"},
    }


def _health_pending_dates(owner: str) -> list[str]:
    """Every date with food/weight/body-fat/exercise that is not confirmed."""
    rows = db.query(
        "SELECT date FROM meals WHERE owner=? AND date<=? "
        "UNION SELECT date FROM weights WHERE owner=? AND date<=? "
        "UNION SELECT date FROM body_fat WHERE owner=? AND date<=? "
        "UNION SELECT date FROM exercises WHERE owner=? AND date<=? "
        "ORDER BY date",
        (owner, today_str(), owner, today_str(), owner, today_str(),
         owner, today_str()))
    confirmed = {r["date"] for r in db.query(
        "SELECT date FROM health_exports WHERE owner=? AND confirmed_at IS NOT NULL",
        (owner,))}
    return [r["date"] for r in rows if r["date"] not in confirmed]


def _health_export_status(owner: str) -> dict:
    pending = _health_pending_dates(owner)
    today = today_str()
    row = db.one("SELECT requested_at, confirmed_at FROM health_exports "
                 "WHERE owner=? AND date=?", (owner, today))
    last = db.one("SELECT MAX(date) d, MAX(confirmed_at) t FROM health_exports "
                  "WHERE owner=? AND confirmed_at IS NOT NULL", (owner,))
    return {
        "today": today,
        "today_requested": bool(row and row["requested_at"]),
        "today_confirmed": bool(row and row["confirmed_at"]),
        "last_export_date": (last["d"] if last else None),
        "last_export_at": (
            datetime.fromtimestamp(int(last["t"]), APP_TZ).isoformat(timespec="seconds")
            if last and last["t"] else None),
        "pending_count": len(pending),
        "pending_dates": pending[:10],
        "pending_oldest": pending[0] if pending else None,
    }


# Clock windows used to NAME an auto-detected sitting. A meal's start time picks
# the label; consecutive meals that land in the same window get numbered, so a
# late snack after dinner reads "Dinner 2" rather than silently reusing Dinner.
MEAL_WINDOWS = (("breakfast", 0, 10.5), ("lunch", 10.5, 15.0),
                ("dinner", 15.0, 21.0), ("snack", 21.0, 24.0))


def group_meals(meals: list[dict], gap_minutes: int = 45, tz=None) -> list[dict]:
    """Cluster a day's meals into eatings by time proximity.

    Deliberately derived, not stored: the user logs one dish at a time (a photo
    per course, or a plate now and its label later), so requiring them to pick
    "Lunch" each time is friction that also produces wrong groupings. Two
    records within `gap_minutes` of each other are treated as one sitting —
    generous enough that a restaurant meal spanning courses still groups, while
    a mid-afternoon snack stays separate.

    Returns [{"id", "start_ts", "meal_ids", "label", "calories", ...}] ordered
    through the day. `kind` from any record only overrides when the user set it
    explicitly (auto|None means "we decide").
    """
    if not meals:
        return []
    ordered = sorted(meals, key=lambda m: (m.get("ts") or 0, m.get("id") or 0))
    clusters: list[list[dict]] = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        gap = (cur.get("ts", 0) - prev.get("ts", 0)) / 60.0
        if gap <= gap_minutes:
            clusters[-1].append(cur)
        else:
            clusters.append([cur])

    out = []
    seen_labels: dict[str, int] = {}
    for idx, cluster in enumerate(clusters, start=1):
        start_ts = min((m.get("ts") or 0) for m in cluster)
        # Interpret the epoch in the household zone so Breakfast/Lunch/Dinner
        # boundaries match the user's day, not the UTC host clock.
        _zone = tz or APP_TZ
        _local = (datetime.fromtimestamp(start_ts, _zone) if _zone
                  else datetime.fromtimestamp(start_ts))
        hour = _local.hour + _local.minute / 60.0
        name = next((lbl for lbl, lo, hi in MEAL_WINDOWS if lo <= hour < hi),
                    "snack")
        explicit = {m.get("kind") for m in cluster
                    if m.get("kind") and m["kind"] not in ("auto", "other")}
        if len(explicit) == 1:              # user already named this sitting
            name = explicit.pop()
        seen_labels[name] = seen_labels.get(name, 0) + 1
        label = name if seen_labels[name] == 1 else f"{name} {seen_labels[name]}"
        out.append({
            "id": idx,
            "label": label.capitalize(),
            "start_ts": start_ts,
            "meal_ids": [m["id"] for m in cluster],
            "count": len(cluster),
            "calories": round(sum(float(m.get("calories") or 0) for m in cluster)),
            "protein": round(sum(float(m.get("protein") or 0) for m in cluster)),
            "carbs": round(sum(float(m.get("carbs") or 0) for m in cluster)),
            "fat": round(sum(float(m.get("fat") or 0) for m in cluster)),
        })
    return out


def _resolve_photo(name) -> str | None:
    """Find a stored/temp photo by basename, guarding against traversal.

    Photos live either directly in PHOTO_DIR (temp uploads) or in a dated
    subdirectory (saved meals), so a basename lookup has to search both.
    """
    safe = os.path.basename(str(name or ""))
    if not safe or safe in (".", ".."):
        return None
    candidates = [os.path.join(PHOTO_DIR, safe)]
    try:
        for entry in os.listdir(PHOTO_DIR):
            sub = os.path.join(PHOTO_DIR, entry)
            if os.path.isdir(sub):
                candidates.append(os.path.join(sub, safe))
    except OSError:
        pass
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _photo_list(stored) -> list[str]:
    """Meals store one path or a JSON list of them (plate + label photo);
    always return basenames in order."""
    if not stored:
        return []
    if isinstance(stored, str) and stored.lstrip().startswith("["):
        try:
            items = json.loads(stored)
        except ValueError:
            items = [stored]
    else:
        items = [stored]
    return [os.path.basename(p) for p in items if p]


def _store_photos(paths: list[str]) -> str | None:
    """Persist promoted photo paths: plain string when one (back-compat with
    rows written before multi-photo support), JSON array when several."""
    kept = [p for p in paths if p]
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else json.dumps(kept)


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
        # Phone originals are multi-MB; the model only ever reads a 1024px
        # version, so keep a 1600px copy on disk instead of the raw upload.
        return _downscale_photo(dest)
    except (OSError, shutil.Error):
        return None


def _stored_photo_paths(stored) -> list[str]:
    """Full stored paths for a meal's photo column (one path or a JSON list)."""
    if not stored:
        return []
    if isinstance(stored, str) and stored.lstrip().startswith("["):
        try:
            items = json.loads(stored)
        except ValueError:
            items = [stored]
    else:
        items = [stored]
    return [str(p) for p in items if p]


def _find_photo_file(path: str) -> str | None:
    """Resolve a stored photo path, including legacy basename-only rows."""
    if os.path.isfile(path):
        return path
    base = os.path.basename(path)
    for root, _dirs, files in os.walk(PHOTO_DIR):
        if base in files:
            return os.path.join(root, base)
    return None


def _copy_photos(stored, target_date: str) -> str | None:
    """Duplicate a meal's photo(s) into target_date's store.

    A copy must own its files: DELETE_meal removes the photo, so sharing one
    path between the original and the copy would break one of them.
    """
    paths = _stored_photo_paths(stored)
    if not paths:
        return None
    dest_dir = os.path.join(PHOTO_DIR, target_date)
    os.makedirs(dest_dir, exist_ok=True)
    copied = []
    for src in paths:
        actual = _find_photo_file(src)
        if not actual:
            continue
        ext = os.path.splitext(actual)[1] or ".jpg"
        dest = os.path.join(dest_dir, f"{int(time.time())}-{uuid.uuid4().hex[:8]}{ext}")
        try:
            shutil.copy2(actual, dest)
            copied.append(dest)
        except OSError:
            continue
    return _store_photos(copied)


# ------------------------------------------------------- photo maintenance
def _downscale_photo(path: str, max_dim: int = 1600, quality: int = 85) -> str:
    """Shrink a stored photo in place without changing its filename.

    The DB stores the path, so the filename must stay stable when maintenance
    rewrites a file. JPEG/PNG/WebP are re-encoded; HEIC and anything Pillow
    cannot open are left alone. The model reads at most 1024px, so 1600px is
    plenty for a later rescan while cutting phone originals by ~80-90%.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        return path
    try:
        from PIL import Image, ImageOps
    except Exception:
        return path
    tmp = path + ".tmp"
    try:
        before = os.path.getsize(path)
        with Image.open(path) as source:
            img = ImageOps.exif_transpose(source)
            width, height = img.size
            if max(width, height) <= max_dim and before <= 700 * 1024:
                return path              # already web-sized; avoid recompression
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            if ext in (".jpg", ".jpeg"):
                img.save(tmp, "JPEG", quality=quality, optimize=True,
                         progressive=True)
            elif ext == ".webp":
                img.save(tmp, "WEBP", quality=quality, method=6)
            else:
                img.save(tmp, "PNG", optimize=True)
        if os.path.getsize(tmp) >= before and max(width, height) <= max_dim:
            os.remove(tmp)               # recompression made it bigger
            return path
        os.replace(tmp, path)
        return path
    except Exception:                    # noqa: BLE001
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return path


def _delete_meal_photos(stored) -> int:
    """Delete every file a meal owns (one path or a JSON list)."""
    removed = 0
    for path in _stored_photo_paths(stored):
        actual = _find_photo_file(path)
        if actual:
            _unlink(actual)
            removed += 1
    return removed


def _photo_refs() -> set[str]:
    """Basenames referenced by any meal, across all users."""
    refs: set[str] = set()
    try:
        rows = db.query("SELECT photo_path FROM meals "
                        "WHERE photo_path IS NOT NULL AND photo_path != ''")
    except Exception:                    # noqa: BLE001
        return refs
    for row in rows:
        refs.update(os.path.basename(p) for p in _stored_photo_paths(row["photo_path"]))
    return refs


def _photo_stats() -> dict:
    files = 0
    total = 0
    for root, _dirs, names in os.walk(PHOTO_DIR):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass
    return {"files": files, "bytes": total}


def _prune_orphan_photos(older_than_minutes: int = 60) -> tuple[int, int]:
    """Delete permanent files no meal points at.

    `tmp-*` is intentionally skipped: the temp sweep owns it (6h). A one-hour
    grace period protects a file between promotion and the meal INSERT.
    """
    refs = _photo_refs()
    cutoff = time.time() - older_than_minutes * 60
    removed = 0
    freed = 0
    for root, _dirs, names in os.walk(PHOTO_DIR):
        for name in names:
            if name.startswith("tmp-"):
                continue
            full = os.path.join(root, name)
            if name in refs:
                continue
            try:
                if os.path.getmtime(full) > cutoff:
                    continue
                size = os.path.getsize(full)
                os.remove(full)
                removed += 1
                freed += size
            except OSError:
                pass
    return removed, freed


def _shrink_referenced_photos() -> dict:
    """Re-encode every photo a meal still references (one-time maintenance)."""
    refs = _photo_refs()
    before = after = 0
    changed = 0
    for name in refs:
        full = _find_photo_file(name)
        if not full:
            continue
        try:
            old = os.path.getsize(full)
        except OSError:
            continue
        new_path = _downscale_photo(full)
        try:
            new = os.path.getsize(new_path)
        except OSError:
            new = old
        before += old
        after += new
        if new < old:
            changed += 1
    return {"files": len(refs), "before": before, "after": after, "changed": changed}


def sweep_photos(verbose: bool = False) -> dict:
    """Temp cleanup + per-user retention. Runs on startup and every 6 hours."""
    tmp_removed = sweep_temp_photos()
    photos_removed = 0
    meals_cleared = 0
    try:
        owners = [r["owner"] for r in db.query(
            "SELECT DISTINCT owner FROM meals "
            "WHERE photo_path IS NOT NULL AND photo_path != ''")]
    except Exception:                    # noqa: BLE001
        owners = []
    for owner in owners:
        try:
            months = int(get_settings(owner).get("photo_retention_months") or 0)
        except (TypeError, ValueError):
            months = 0
        if months <= 0:
            continue                     # keep forever
        cutoff = (date.today() - timedelta(days=30 * months)).isoformat()
        rows = db.query(
            "SELECT id, photo_path FROM meals WHERE owner=? AND date < ? "
            "AND photo_path IS NOT NULL AND photo_path != ''", (owner, cutoff))
        for row in rows:
            photos_removed += _delete_meal_photos(row["photo_path"])
            db.execute("UPDATE meals SET photo_path='' WHERE id=? AND owner=?",
                       (row["id"], owner))
            meals_cleared += 1
    out = {"tmp_removed": tmp_removed, "photos_removed": photos_removed,
           "meals_cleared": meals_cleared}
    if verbose and (tmp_removed or photos_removed):
        print(f"[photos] sweep: {tmp_removed} temp, {photos_removed} expired "
              f"photos from {meals_cleared} meals")
    return out


def _photo_maintenance_loop(interval: int = 6 * 3600) -> None:
    while True:
        time.sleep(interval)
        try:
            sweep_photos(verbose=True)
        except Exception as e:           # noqa: BLE001
            print(f"[photos] maintenance failed: {type(e).__name__}: {e}")


def maintain_photos(dry_run: bool = False) -> dict:
    """CLI one-shot: sweep, prune orphans, then shrink referenced photos."""
    before = _photo_stats()
    print(f"[photos] before: {before['files']} files, {before['bytes']/1e6:.1f} MB")
    if dry_run:
        print("[photos] dry run — no files changed")
        return {"before": before, "after": before}
    sweep = sweep_photos(verbose=True)
    orphans, orphan_bytes = _prune_orphan_photos()
    shrunk = _shrink_referenced_photos()
    after = _photo_stats()
    print(f"[photos] after:  {after['files']} files, {after['bytes']/1e6:.1f} MB")
    print(f"[photos] orphans removed: {orphans} ({orphan_bytes/1e6:.1f} MB); "
          f"shrunk {shrunk['changed']} of {shrunk['files']} referenced files "
          f"({shrunk['before']/1e6:.1f} -> {shrunk['after']/1e6:.1f} MB)")
    return {"before": before, "after": after, "sweep": sweep,
            "orphans": orphans, "orphan_bytes": orphan_bytes, "shrunk": shrunk}


def _maybe_adapt(settings: dict, persist: bool = False, force: bool = False,
                 owner: str = "default") -> dict:
    """Run the weight->target loop when appropriate; optionally save result."""
    if not (force or settings.get("auto_adapt", True)):
        return {"adapted": False, "reason": "auto-adapt disabled"}
    snapshot = json.loads(json.dumps(settings))
    res = adapt_targets(db, settings, today=date.fromisoformat(today_str()),
                        owner=owner, force=force)
    if res.get("adjusted") and persist:
        updates = {"calorie_target": settings["calorie_target"]}
        if settings.get("target_adjusted_on"):
            updates["target_adjusted_on"] = settings["target_adjusted_on"]
        save_user_settings(owner, updates)
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
    def _tz(self):
        """The requesting browser's timezone, so a meal typed at 12:35 local is
        stored at 12:35 local no matter what zone the server runs in."""
        return resolve_tz(self.headers.get("X-Client-TZ"))

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

    def _reject_unread(self, status, error):
        """Answer without reading the body; the connection can't be reused."""
        self.close_connection = True
        return self._json(status, {"error": error})

    def _cross_site(self) -> bool:
        """True for a state-changing request a browser sent from another site.

        Session cookies are SameSite=Lax, which still lets *sibling subdomains*
        (same site) POST with them, and no-auth mode has no cookie at all.
        Sec-Fetch-Site is sent by every current browser; Origin is the fallback.
        Non-browser clients (curl, tests, Shortcuts) send neither and pass.
        """
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site:
            return site not in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            o = urlparse(origin)
        except ValueError:
            return True
        allowed = {h.lower() for h in (self.headers.get("Host"),
                                       self.headers.get("X-Forwarded-Host"),
                                       urlparse(os.environ.get("OIDC_REDIRECT_BASE", "")).netloc)
                   if h}
        return (o.netloc or "").lower() not in allowed

    def _read_body(self, multipart_ok=False):
        self._upload = None
        self._uploads = []
        raw_len = (self.headers.get("Content-Length") or "0").strip()
        if not raw_len.isdigit():
            return None, "invalid Content-Length"
        length = int(raw_len)
        if length > MAX_BODY:
            return None, "body too large"
        ctype = self.headers.get("Content-Type", "").lower()
        if ctype.startswith("multipart/form-data"):
            if not multipart_ok:
                return None, "multipart bodies are only accepted for photo uploads"
            raw = self.rfile.read(length) if length else b""
            fields, upload = self._parse_multipart(raw, self.headers.get("Content-Type", ""))
            self._upload = upload          # consumed by POST_estimate
            return fields, None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}, None
        # Only JSON. A text/plain or form-encoded body is what a cross-site
        # <form> or no-cors fetch can send without a CORS preflight.
        if not ctype.startswith("application/json"):
            return None, "Content-Type must be application/json"
        try:
            return json.loads(raw.decode()), None
        except (ValueError, UnicodeDecodeError):
            return None, "invalid JSON body"

    def _parse_multipart(self, raw: bytes, ctype: str):
        """Minimal multipart/form-data reader.

        Returns (text_fields, (photo_path, mime) | None) where the tuple is the
        FIRST image, for the single-photo callers; every image additionally
        lands on self._uploads in send order so multi-image endpoints can see
        them all. Photos are written to temp files in PHOTO_DIR and promoted on
        save or deleted if the estimate fails / the user cancels.
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
        uploads: list[tuple[str, str]] = []   # every image part, in send order
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
                pair = (tmp, guess_mime(ext))
                uploads.append(pair)
                if upload is None:
                    upload = pair          # legacy single-photo callers
                fields.setdefault("photo_path", tmp)
        self._uploads = uploads
        return fields, upload

    # -- routing ---------------------------------------------------------
    def _begin(self):
        self._pending_cookies = []

    def do_GET(self):
        self._begin()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/health/day", "/health/today"):
            return self._health_day(q)
        if u.path == "/health/pending":
            return self._health_pending(q)
        if u.path.startswith("/auth/"):
            return self._auth(u.path, q)
        if u.path.startswith("/api/"):
            return self._route("GET", u.path, q, {})
        # Unauthenticated users hitting the app root get bounced to login when
        # OIDC is on; otherwise serve the SPA.
        if u.path in ("/", "") and OIDC.enabled() and not self._owner():
            return self._redirect("/auth/login")
        return self._static(u.path)

    # Routes that take multipart photo uploads. Everything else is JSON-only.
    MULTIPART_ROUTES = ("/api/estimate", "/api/estimate-meal")

    def do_POST(self):
        return self._write("POST")

    def do_PATCH(self):
        return self._write("PATCH")

    def do_PUT(self):
        return self._write("PUT")

    def do_DELETE(self):
        return self._write("DELETE")

    def _write(self, verb):
        """Authenticate and route-check BEFORE touching the body, so anonymous
        clients can't make the server buffer 20 MB or write temp photos."""
        self._begin()
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if verb == "POST" and path == "/health/ack":
            # Bearer token, never an ambient cookie: no CSRF exposure.
            if not self._health_token_owner():
                return self._reject_unread(401, "invalid health token")
            body, err = self._read_body()
            if err:
                return self._reject_unread(400, err)
            return self._health_ack(body or {})
        if not path.startswith("/api/"):
            return self._reject_unread(404, "unknown endpoint")
        if OIDC.enabled() and self._owner() is None:
            self.close_connection = True
            return self._json(401, {"error": "authentication required",
                                    "login": "/auth/login"})
        if self._cross_site():
            return self._reject_unread(403, "cross-site request refused")
        body, err = self._read_body(
            multipart_ok=(verb == "POST" and path in self.MULTIPART_ROUTES))
        if err:
            return self._reject_unread(413 if "too large" in err else 400, err)
        return self._route(verb, u.path, parse_qs(u.query), body or {})

    # -- authentication --------------------------------------------------
    def _owner(self) -> str | None:
        """Resolved owner key for this request: OIDC subject, or 'default' when
        auth is disabled. Returns None if auth is on but no valid session."""
        if not OIDC.enabled():
            return "default"
        sess = OIDC.read_session_cookie(self._cookies().get(SESSION_COOKIE))
        # Re-check the allowlist on every request so removing an email from
        # OIDC_ALLOWED_EMAILS takes effect without waiting for cookie expiry.
        if not sess or not OIDC.allowed(sess.get("email")):
            return None
        return sess["sub"]

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

    def _health_token_owner(self):
        auth = self.headers.get("Authorization", "")
        token = auth.split(None, 1)[1].strip() if auth.lower().startswith("bearer ") else ""
        return _owner_for_health_token(token)

    def _health_day(self, q):
        owner = self._health_token_owner()
        if not owner:
            return self._json(401, {"error": "invalid health token"})
        d = (q.get("date") or [today_str()])[0]
        try:
            d = date.fromisoformat(str(d)[:10]).isoformat()
        except ValueError:
            return self._json(400, {"error": "invalid date (use YYYY-MM-DD)"})
        row = db.one("SELECT requested_at, confirmed_at FROM health_exports "
                     "WHERE owner=? AND date=?", (owner, d))
        already_requested = bool(row and row["requested_at"])
        already_confirmed = bool(row and row["confirmed_at"])
        now = int(time.time())
        db.execute(
            "INSERT INTO health_exports(owner,date,requested_at) VALUES(?,?,?) "
            "ON CONFLICT(owner,date) DO UPDATE SET requested_at=excluded.requested_at",
            (owner, d, now))
        payload = _health_day_payload(d, owner)
        payload["export"] = {
            "requested_at": now,
            "confirmed_at": row["confirmed_at"] if row else None,
            "already_requested": already_requested,
            "already_confirmed": already_confirmed,
        }
        return self._json(200, payload)

    def _health_pending(self, q):
        owner = self._health_token_owner()
        if not owner:
            return self._json(401, {"error": "invalid health token"})
        try:
            limit = int((q.get("limit") or ["366"])[0])
        except ValueError:
            limit = 366
        limit = max(1, min(366, limit))
        pending = _health_pending_dates(owner)
        days = [_health_day_payload(d, owner) for d in pending[:limit]]
        status = _health_export_status(owner)
        return self._json(200, {
            **status,
            "count": len(pending),
            "returned": len(days),
            "truncated": len(pending) > len(days),
            "pending": days,
        })

    def _health_ack(self, body):
        owner = self._health_token_owner()
        if not owner:
            return self._json(401, {"error": "invalid health token"})
        raw = body.get("dates") if isinstance(body, dict) else None
        if raw is None and isinstance(body, dict) and body.get("date"):
            raw = [body["date"]]
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return self._json(400, {"error": "dates must be a YYYY-MM-DD array"})
        dates = []
        for value in raw[:366]:
            try:
                d = date.fromisoformat(str(value)[:10]).isoformat()
            except ValueError:
                return self._json(400, {"error": "invalid date (use YYYY-MM-DD)"})
            if d not in dates:
                dates.append(d)
        if not dates:
            return self._json(400, {"error": "dates must not be empty"})
        now = int(time.time())
        for d in dates:
            db.execute(
                "INSERT INTO health_exports(owner,date,requested_at,confirmed_at) "
                "VALUES(?,?,?,?) ON CONFLICT(owner,date) DO UPDATE SET "
                "confirmed_at=excluded.confirmed_at, "
                "requested_at=COALESCE(health_exports.requested_at, excluded.requested_at)",
                (owner, d, now, now))
        return self._json(200, {"confirmed": dates, "confirmed_at": now})

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
            elif resource == "estimate-text" and verb == "POST":
                st, pl = Api.POST_estimate_text(self, q, body)
            elif resource == "estimate-meal" and verb == "POST":
                st, pl = Api.POST_estimate_meal(self, q, body)
            elif resource == "quick-foods" and verb == "GET":
                st, pl = Api.GET_quick_foods(self, q, body)
            elif resource == "rescan" and verb == "POST":
                st, pl = Api.POST_rescan(self, q, body)
            elif resource == "meals" and tail and len(parts) > 3 \
                    and parts[3] == "copy" and verb == "POST":
                st, pl = Api.POST_copy_meal(self, q, body, tail)
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
            elif resource == "health" and tail == "token" and verb == "POST":
                st, pl = Api.POST_health_token(self, q, body)
            elif resource == "health" and tail == "token" and verb == "DELETE":
                st, pl = Api.DELETE_health_token(self, q, body)
            elif resource == "settings" and verb == "GET":
                st, pl = Api.GET_settings(self, q, body)
            elif resource == "settings" and verb == "PUT":
                st, pl = Api.PUT_settings(self, q, body)
            elif resource == "export" and verb == "GET":
                st, pl = Api.GET_export(self, q, body)
            elif resource == "test-provider" and verb == "POST":
                st, pl = Api.POST_test_provider(self, q, body)
            elif resource == "provider" and tail == "models" and verb == "POST":
                st, pl = Api.POST_provider_models(self, q, body)
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
            # A visible build marker makes "am I looking at a stale cached
            # shell?" answerable without DevTools. Cached HTML keeps the literal
            # placeholder; a live server replaces it with the asset token.
            data = data.replace(b"__APP_BUILD__", token)
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


def _is_loopback_host(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        import ipaddress
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def main():
    ap = argparse.ArgumentParser(description="MacroLog self-hosted calorie tracker")
    ap.add_argument("--host", default=os.environ.get("MACROLOG_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MACROLOG_PORT", 8765)))
    ap.add_argument("--tls-cert", default=os.environ.get("MACROLOG_TLS_CERT"),
                    help="PEM cert; enables HTTPS (required for iPhone camera over LAN)")
    ap.add_argument("--tls-key", default=os.environ.get("MACROLOG_TLS_KEY"),
                    help="PEM private key matching --tls-cert")
    ap.add_argument("--maintain-photos", action="store_true",
                    help="sweep temp/orphan photos, apply retention, shrink "
                         "stored photos, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --maintain-photos: report only, change nothing")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    load_env()
    os.makedirs(PHOTO_DIR, exist_ok=True)
    if OIDC.enabled():
        print(f"  session key: {OIDC.init_session_key(DATA_DIR)}")
    elif not _is_loopback_host(args.host) and not args.maintain_photos \
            and os.environ.get("MACROLOG_ALLOW_NO_AUTH", "") != "1":
        # No-auth mode makes every visitor the same full-access user (read,
        # export, delete, spend the operator's provider keys). Never do that on
        # a reachable interface by accident.
        sys.exit(f"refusing to listen on {args.host} without login: configure "
                 "OIDC (see .env.example) or set MACROLOG_ALLOW_NO_AUTH=1 to "
                 "accept a shared no-auth instance on a trusted network.")
    if args.maintain_photos:
        maintain_photos(dry_run=args.dry_run)
        return
    # One startup sweep, then a light 6-hour loop in the serving process.
    sweep_photos(verbose=True)
    threading.Thread(target=_photo_maintenance_loop, daemon=True).start()

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
    ready = [p["kind"] for p in _effective_providers(s)
             if p.get("enabled", True) and _provider_ready(p)]
    print(f"  vision:   {', '.join(ready) if ready else 'NONE - running offline stub'}")
    if not s.get("providers") and not s.get("shared_providers"):
        print("            configure a provider per user in Settings → AI provider")
    if use_tls:
        print("  tls:      on (HTTPS — iPhone camera will work)")
    print("  Ctrl-C to stop\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
