"""TypeSafe Jev cross-check for vision estimates.

Jev is a *discriminative* model, not a chat model: it takes a state (text, JSON
and -- experimentally -- images) plus typed questions, and returns typed answers
with calibrated probabilities in a few hundred milliseconds. It cannot name
arbitrary foods or return an itemised breakdown, so it is NOT a substitute for
the vision provider used to build an estimate.

What it is genuinely good at, and why it is here: judging whether a proposed
calorie total is plausible for the photo. Measured on a ~60 kcal nut snack, the
plausibility of a claim decayed monotonically -- 40/60/90 kcal -> 0.67/0.71/0.66,
150 -> 0.40, 235 -> 0.20, 600 -> 0.04 -- which is exactly the signal needed to
catch gross portion errors without a second generative pass.

Design rules:
  * never block an estimate: any failure returns None and the app carries on;
  * opt-in via JEV_API_KEY (no key -> no calls);
  * only the first photo is sent, downscaled, to keep tokens small.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai").rstrip("/")
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
TIMEOUT = float(os.environ.get("JEV_TIMEOUT", "8"))
MAX_EDGE = 768

# Images are gated behind this opt-in header upstream; without it every image
# request is rejected with an explicit api_usage_error.
_IMAGE_HEADER = {"TypeSafe-Danger-Zone-Images": "true"}

PLATE_THRESHOLD = 0.30   # below this the total looks wrong for the photo


def api_key() -> str:
    return (os.environ.get("JEV_API_KEY") or "").strip()


def available() -> bool:
    return bool(api_key())


def _encode(path: str) -> str | None:
    """Base64 JPEG bytes, downscaled when Pillow is available."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((MAX_EDGE, MAX_EDGE))
            import io
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode()
    except Exception:                                  # noqa: BLE001
        try:
            with open(path, "rb") as fh:
                return base64.b64encode(fh.read()).decode()
        except OSError:
            return None


def _ask(state, questions, image_path, timeout=TIMEOUT):
    key = api_key()
    if not key:
        return None
    b64 = _encode(image_path)
    if not b64:
        return None
    placeholder = "photo_1"
    body = {
        "model": MODEL,
        "state": state.replace("{PH}", placeholder),
        "questions": json.loads(json.dumps(questions).replace("{PH}", placeholder)),
        "assets": {placeholder: {"type": "image", "format": "jpeg", "data_base64": b64}},
    }
    req = urllib.request.Request(
        f"{BASE}/v1/systemone", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}", **_IMAGE_HEADER})
    # Never follow redirects: urllib would replay the Bearer key to the new host.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read(4 * 1024 * 1024))


def crosscheck(image_paths: list[str], calories: float,
               description: str = "") -> dict | None:
    """Ask Jev whether `calories` is plausible for the photo.

    Returns {plausible, kind, kind_confidence, portion, portion_label, ms,
    model} or None when unavailable/failed. Never raises.
    """
    if not available() or not image_paths or not calories or calories <= 0:
        return None
    path = image_paths[0]
    claim = f"{round(float(calories))} kcal"
    hint = (description or "").strip()[:160]
    questions = {
        "plausible": {
            "type": "noul",
            "instructions": (f"The user states this meal totals {claim}"
                             + (f" ({hint})" if hint else "")
                             + ". Is that total plausible for the food actually visible?"),
            "criteria": {
                "true": "Consistent with the visible portion size and food.",
                "false": "Clearly too low or too high for the visible portion and food.",
            },
        },
        "kind": {
            "type": "choice",
            "instructions": "What kind of image is {PH}?",
            "criteria": {
                "plate_of_food": "Prepared food on a plate, bowl or tray.",
                "nutrition_label": "A Nutrition Facts or ingredients panel.",
                "menu": "A restaurant or printed menu.",
                "packaged_product": "Packaging of a food product.",
                "not_food": "No food or drink is shown.",
            },
        },
    }
    started = time.time()
    try:
        data = _ask("meal photo: {PH}", questions, path)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None
    if not data:
        return None
    answers = data.get("answers") or {}
    plausible = (answers.get("plausible") or {}).get("noul")
    kind = answers.get("kind") or {}
    if plausible is None and not kind.get("choice"):
        return None
    out = {
        "plausible": round(float(plausible), 3) if plausible is not None else None,
        "kind": kind.get("choice"),
        "kind_confidence": round(float(kind.get("confidence") or 0), 3),
        "model": data.get("model") or MODEL,
        "ms": int((time.time() - started) * 1000),
        "suspect": (plausible is not None and float(plausible) < PLATE_THRESHOLD),
    }
    if kind.get("choice") == "not_food" and float(kind.get("confidence") or 0) > 0.6:
        out["suspect"] = True
        out["reason"] = "no food visible"
    elif out["suspect"]:
        out["reason"] = "total looks off for the portion shown"
    return out
