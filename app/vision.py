"""Vision providers for MacroLog.

Each provider turns a meal photo into structured macro estimates. They share
one prompt and one output contract, so swapping backends never touches the
rest of the app. Providers are tried in configured order until one succeeds.

Supported kinds:
  gemini  - Google AI Studio / Vertex (needs an AIza-style key)
  openai  - OpenAI chat completions with image_url content
  groq    - Groq (free tier, Llama 4 Scout vision) - OpenAI-compatible
  ollama  - local models via Ollama, no network, no account
  stub    - deterministic fake, for tests and first-run demo
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Prompt: portion size is the hard part, so it is asked for explicitly and
# the model must show its work on quantities before totals.
# --------------------------------------------------------------------------
PROMPT = """You are a dietitian estimating nutrition from a photo of food.

Identify every distinct food/drink item visible. For each, estimate the COOKED \
edible weight in grams using visual anchors (plate diameter ~26cm, fork tines \
~4mm, standard glass 240ml). Be conservative: restaurant portions are larger \
than home portions, and dressing/oil/sugar add hidden calories.

Return ONLY a JSON object, no markdown fences, exactly this shape:
{
  "items": [
    {"name": str, "grams": number, "calories": number,
     "protein": number, "carbs": number, "fat": number}
  ],
  "total_calories": number, "total_protein": number,
  "total_carbs": number, "total_fat": number,
  "confidence": number,
  "description": str,
  "notes": str
}

Rules:
- All numbers are per-item absolute amounts for the WHOLE photographed portion, \
not per 100g.
- total_* must equal the sum of the item values.
- confidence is 0.0-1.0: how sure you are given what is actually visible. Use \
low confidence when the plate is crowded, food is mixed/unidentifiable, or \
portion depth is ambiguous.
- description: one short human phrase for the whole meal, e.g. "Grilled chicken \
rice bowl with avocado".
- If there is no recognizable food, return items: [] with confidence 0 and \
describe what you see in notes.
"""


class ProviderError(Exception):
    pass


# --------------------------------------------------------------------------
# Image helpers
# --------------------------------------------------------------------------
def encode_image(path: str, max_dim: int = 1024, quality: int = 82) -> tuple[str, str]:
    """Return (base64_data, mime_type), downscaling with Pillow when present."""
    raw_bytes = None
    try:
        from PIL import Image  # type: ignore
        img = Image.open(path)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max(img.size) > max_dim:
            ratio = max_dim / float(max(img.size))
            img = img.resize((max(1, int(img.width * ratio)),
                              max(1, int(img.height * ratio))))
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        raw_bytes, mime = buf.getvalue(), "image/jpeg"
    except Exception:
        # Pillow missing or unreadable: send the original bytes untouched.
        with open(path, "rb") as fh:
            raw_bytes = fh.read()
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "heic": "image/heic",
                "gif": "image/gif"}.get(ext, "image/jpeg")
    return base64.b64encode(raw_bytes).decode(), mime


def _post(url: str, payload: dict, headers: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise ProviderError(f"HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"network: {e.reason}") from None


def _extract_json(text: str) -> dict:
    """Models sometimes wrap JSON in prose or fences; dig out the object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ProviderError(f"no JSON in response: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ProviderError(f"bad JSON: {e} :: {text[start:start+200]}") from None


def normalize(data: dict) -> dict:
    """Coerce arbitrary model output into the internal meal contract."""
    items = data.get("items") or []
    tot = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
    clean_items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = {
            "name": str(it.get("name") or "").strip()[:120],
            "grams": _num(it.get("grams")),
            "calories": max(0.0, _num(it.get("calories"))),
            "protein": max(0.0, _num(it.get("protein"))),
            "carbs": max(0.0, _num(it.get("carbs"))),
            "fat": max(0.0, _num(it.get("fat"))),
        }
        if row["name"]:
            clean_items.append(row)
            for k in tot:
                tot[k] += row[k]

    def pick(total_key: str) -> float:
        field = total_key[len("total_"):] if total_key.startswith("total_") else total_key
        v = _num(data.get(total_key))
        summed = tot.get(field, 0.0)
        # Trust the sum unless the model gave a sane standalone total.
        if v <= 0:
            return summed
        if summed > 0 and abs(v - summed) / max(summed, 1) > 0.35:
            return summed
        return v

    out = {
        "items": clean_items,
        "calories": round(pick("total_calories"), 1),
        "protein": round(pick("total_protein"), 1),
        "carbs": round(pick("total_carbs"), 1),
        "fat": round(pick("total_fat"), 1),
        "confidence": min(1.0, max(0.05, _num(data.get("confidence"), 0.5))),
        "description": str(data.get("description") or "").strip()[:200],
        "notes": str(data.get("notes") or "").strip()[:400],
    }
    if not out["description"] and clean_items:
        out["description"] = ", ".join(i["name"] for i in clean_items[:4])
    return out


def _num(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(str(v).strip().split()[0]) if isinstance(v, str) else float(v)
    except (ValueError, IndexError, TypeError):
        return default


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
class GeminiProvider:
    name = "gemini"

    def __init__(self, cfg: dict):
        self.key = cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
        self.model = cfg.get("model", "gemini-2.5-flash")
        self.base = cfg.get("base_url", "https://generativelanguage.googleapis.com")

    def estimate(self, image_path: str) -> dict:
        if not self.key:
            raise ProviderError("Gemini API key not set")
        b64, mime = encode_image(image_path)
        url = f"{self.base}/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ]}],
            "generationConfig": {"temperature": 0.1,
                                 "responseMimeType": "application/json"},
        }
        data = _post(url, payload, {"x-goog-api-key": self.key})
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            raise ProviderError(f"unexpected reply: {json.dumps(data)[:250]}") from None
        text = "".join(p.get("text", "") for p in parts)
        return _extract_json(text)


class OpenAICompatProvider:
    """Any OpenAI-compatible /chat/completions endpoint that accepts images.

    Covers OpenAI, Groq, and Alibaba DashScope Token Plan (Qwen). The
    `base_url` and `env_key` args let a provider opt into a different host and
    credential variable without subclassing.
    """

    def __init__(self, cfg: dict, name: str = "openai"):
        self.name = name
        env_var = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY",
                   "qwen": "DASHSCOPE_API_KEY"}.get(name, f"{name.upper()}_API_KEY")
        self.key = cfg.get("api_key") or os.environ.get(env_var, "")
        defaults = {
            "groq": ("https://api.groq.com/openai/v1", "meta-llama/llama-4-scout-17b-16e-instruct"),
            "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
            # DashScope OpenAI-compatible endpoint; qwen-vl-max is their flagship
            # multimodal model. Override region with DASHSCOPE_BASE_URL (e.g. a
            # token-plan.maas.* host).
            "qwen": (os.environ.get("DASHSCOPE_BASE_URL")
                     or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                     os.environ.get("QWEN_VL_MODEL", "qwen-vl-max")),
        }
        base, model = defaults.get(name, ("", "gpt-4o-mini"))
        self.base = (cfg.get("base_url") or base).rstrip("/")
        self.model = cfg.get("model", model)

    def estimate(self, image_path: str) -> dict:
        if not self.key:
            raise ProviderError(f"{self.name} API key not set")
        b64, mime = encode_image(image_path)
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}],
        }
        # groq rejects response_format; DashScope wants the word "JSON" in the
        # prompt (already present) and tolerates json_object on vl models.
        if self.name not in ("groq", "qwen"):
            payload["response_format"] = {"type": "json_object"}
        data = _post(f"{self.base}/chat/completions", payload,
                     {"Authorization": f"Bearer {self.key}"})
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise ProviderError(f"unexpected reply: {json.dumps(data)[:250]}") from None
        return _extract_json(text)


class OllamaProvider:
    """Local vision model through Ollama. No account, no network egress."""

    name = "ollama"

    def __init__(self, cfg: dict):
        self.base = cfg.get("base_url",
                            os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")).rstrip("/")
        self.model = cfg.get("model", "llama3.2-vision:11b")

    def estimate(self, image_path: str) -> dict:
        b64, _ = encode_image(image_path)
        payload = {"model": self.model, "raw": True, "stream": False,
                   "format": "json", "options": {"temperature": 0.1},
                   "messages": [{"role": "user", "content": PROMPT,
                                 "images": [b64]}]}
        data = _post(f"{self.base}/api/chat", payload, {}, timeout=300)
        text = (data.get("message") or {}).get("content", "")
        if not text:
            raise ProviderError(f"empty reply: {json.dumps(data)[:200]}")
        return _extract_json(text)


class StubProvider:
    """Deterministic offline estimator so the app is testable with no keys."""

    name = "stub"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}

    def estimate(self, image_path: str) -> dict:
        size = os.path.getsize(image_path) if os.path.exists(image_path) else 0
        kcal = round(320 + (size % 400), 1)
        return {
            "items": [{"name": "Mixed plate (offline estimate)", "grams": 350,
                       "calories": kcal, "protein": kcal * 0.12 / 4,
                       "carbs": kcal * 0.45 / 4, "fat": kcal * 0.40 / 9}],
            "total_calories": kcal,
            "total_protein": round(kcal * 0.12 / 4, 1),
            "total_carbs": round(kcal * 0.45 / 4, 1),
            "total_fat": round(kcal * 0.40 / 9, 1),
            "confidence": 0.15,
            "description": "Offline placeholder estimate",
            "notes": "No vision provider configured. Add an API key in Settings.",
        }


PROVIDERS = {
    "gemini": GeminiProvider,
    "openai": lambda cfg: OpenAICompatProvider(cfg, "openai"),
    "groq": lambda cfg: OpenAICompatProvider(cfg, "groq"),
    "qwen": lambda cfg: OpenAICompatProvider(cfg, "qwen"),
    "ollama": OllamaProvider,
    "stub": StubProvider,
}


_OLLAMA_REACH_CACHE = {"ts": 0.0, "ok": False}


def _ollama_reachable(base_url: str) -> bool:
    """Cached (30s) liveness probe so we don't mark ollama ready when down."""
    import time as _t
    now = _t.time()
    if now - _OLLAMA_REACH_CACHE["ts"] < 30:
        return _OLLAMA_REACH_CACHE["ok"]
    ok = False
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    _OLLAMA_REACH_CACHE.update(ts=now, ok=ok)
    return ok


def _has_credentials(entry: dict) -> bool:
    """Can this provider actually be used right now?"""
    kind = entry.get("kind")
    if kind == "stub":
        return True
    if entry.get("api_key"):
        return True
    env = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY",
           "groq": "GROQ_API_KEY", "qwen": "DASHSCOPE_API_KEY"}.get(kind)
    if env:
        return bool(os.environ.get(env))
    if kind == "ollama":
        base = entry.get("base_url") or os.environ.get(
            "OLLAMA_HOST", "http://127.0.0.1:11434")
        return _ollama_reachable(base)
    return False


def build_chain(settings: dict) -> list:
    """Instantiate usable providers in configured order.

    Providers with no key are skipped rather than failing the whole request,
    so a default config on a fresh install still works via the offline stub.
    """
    chain = []
    for entry in settings.get("providers") or []:
        if not _has_credentials(entry):
            continue
        cls = PROVIDERS.get(entry.get("kind"))
        if cls:
            chain.append(cls(entry))
    if not chain:
        chain.append(StubProvider())
    elif not any(isinstance(p, StubProvider) for p in chain):
        # Keep a last-resort offline path so the app never dead-ends on logging.
        chain.append(StubProvider())
    return chain


def estimate_photo(image_path: str, settings: dict) -> tuple[dict, str, str]:
    """Try each provider in order. Returns (meal_dict, provider, model)."""
    errors = []
    for prov in build_chain(settings):
        try:
            raw = prov.estimate(image_path)
            meal = normalize(raw)
            meal["_raw"] = raw
            model = getattr(prov, "model", prov.name)
            return meal, prov.name, model
        except Exception as e:
            errors.append(f"{prov.name}: {type(e).__name__}: {str(e)[:180]}")
    raise ProviderError(" | ".join(errors) or "no providers configured")
