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
import re
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
    {"name": str, "detail": str, "grams": number, "calories": number,
     "protein": number, "carbs": number, "fat": number, "fiber": number}
  ],
  "total_calories": number, "total_protein": number,
  "total_carbs": number, "total_fat": number, "total_fiber": number,
  "confidence": number,
  "description": str,
  "notes": str
}

Rules:
- All numbers are per-item absolute amounts for the WHOLE photographed portion, \
not per 100g.
- fiber is the portion of "carbs" that comes from fibre: report it as a NUMBER WITHIN the carb figure, never on top of it (carbs 30 and fiber 8 means 30 g total carbohydrate of which 8 g is fibre). Use 0 when unknown.
- name: the generic food only -- no brand, weight, or cooking method (e.g. "greek yogurt", "steel cut oats"); detail: the specific product/brand and variant, including fat level (e.g. "Chobani nonfat plain"). Put the weight in grams, never in the name.
- total_* must equal the sum of the item values.
- confidence is 0.0-1.0: how sure you are given what is actually visible. Use \
low confidence when the plate is crowded, food is mixed/unidentifiable, or \
portion depth is ambiguous.
- description: one short human phrase for the whole meal, e.g. "Grilled chicken \
rice bowl with avocado".
- If there is no recognizable food, return items: [] with confidence 0 and \
describe what you see in notes.
"""

MEAL_PROMPT = """You are a dietitian estimating nutrition from one or more inputs about a single meal: photos of the food, photos of a packaged product's Nutrition Facts panel, and/or a written description. Use every input you are given; they complement each other.

For each photo of FOOD: identify every distinct item and estimate its COOKED edible weight in grams using visual anchors (plate diameter ~26cm, fork tines ~4mm, standard glass 240ml). Be conservative: restaurant portions are larger than home portions, and dressing/oil/sugar add hidden calories.

For each photo of a NUTRITION FACTS LABEL: transcribe the serving size and the per-serving values (calories, protein, carbs incl. fiber, fat, sodium if shown), plus the product name and total servings per package if visible. These are MEASURED numbers and outrank your visual guess for that item. If the description or another photo says how much was eaten ("about half the box", "1.5 servings", "90g"), scale the per-serving figures to that amount; report the resulting absolute amounts for what was actually consumed.

If a written description is provided, treat it as ground truth for intent: it names ambiguous foods ("the duck"), fixes quantities, and resolves what the photos cannot. When description and photo disagree on identity, prefer the description; on portion size, prefer an explicit stated amount over a visual estimate.

Return ONLY a JSON object, no markdown fences, exactly this shape:
{
  "items": [
    {"name": str, "detail": str, "grams": number, "calories": number,
     "protein": number, "carbs": number, "fat": number, "fiber": number}
  ],
  "total_calories": number, "total_protein": number,
  "total_carbs": number, "total_fat": number, "total_fiber": number,
  "confidence": number,
  "description": str,
  "notes": str
}

Rules:
- All numbers are per-item ABSOLUTE amounts for the WHOLE portion actually eaten, never per 100g and never per serving.
- fiber is the portion of "carbs" that comes from fibre: report it as a NUMBER WITHIN the carb figure, never on top of it (carbs 30 and fiber 8 means 30 g total carbohydrate of which 8 g is fibre). Use 0 when unknown.
- name: the generic food only -- no brand, weight, or cooking method (e.g. "greek yogurt", "steel cut oats"); detail: the specific product/brand and variant, including fat level (e.g. "Chobani nonfat plain"). Put the weight in grams, never in the name.
- total_* must equal the sum of the item values.
- confidence is 0.0-1.0. Use HIGH confidence when a Nutrition Facts label or an explicit stated amount pins the numbers; use LOW confidence when the plate is crowded, food is mixed/unidentifiable, or portion depth is ambiguous.
- notes: state anything you had to assume, and flag any label values used.
- description: one short human phrase for the whole meal.
- If there is nothing recognizable, return items: [] with confidence 0 and explain in notes.
"""

TEXT_PROMPT = """You are a dietitian estimating nutrition from a text \
description of a meal or its ingredients.

Parse the description into distinct food items. Honor amounts as written \
("100g of rice", "a medium banana", "2 slices"); when none is given, assume \
a sensible standard portion and say so in notes. Use cooked weights unless \
the wording clearly refers to raw/package form. For each item report absolute \
amounts for the WHOLE portion (not per 100g), including fiber inside carbs.

Return ONLY a JSON object, no markdown fences, exactly this shape:
{
  "items": [
    {"name": str, "detail": str, "grams": number, "calories": number,
     "protein": number, "carbs": number, "fat": number, "fiber": number}
  ],
  "total_calories": number, "total_protein": number,
  "total_carbs": number, "total_fat": number, "total_fiber": number,
  "confidence": number,
  "description": str,
  "notes": str
}

Rules:
- fiber is the portion of "carbs" that comes from fibre: report it as a NUMBER WITHIN the carb figure, never on top of it (carbs 30 and fiber 8 means 30 g total carbohydrate of which 8 g is fibre). Use 0 when unknown.
- name: the generic food only -- no brand, weight, or cooking method (e.g. "greek yogurt", "steel cut oats"); detail: the specific product/brand and variant, including fat level (e.g. "Chobani nonfat plain"). Put the weight in grams, never in the name.
- total_* must equal the sum of the item values.
- confidence 0.0-1.0: high when amounts and foods are explicit; low when you \
had to guess portions or identify vague items ("some sauce").
- description: one short human phrase for the whole meal.
- If the text contains no recognizable food, return items: [] with \
confidence 0 and explain in notes.
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Fail on 3xx: urllib would otherwise replay Authorization to the new host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())
MAX_RESPONSE_BYTES = 4 * 1024 * 1024      # a meal estimate is a few KB


def _read_capped(resp) -> bytes:
    raw = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderError("provider response too large")
    return raw


def _post(url: str, payload: dict, headers: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(_read_capped(resp))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise ProviderError(f"HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"network: {e.reason}") from None


def _get_json(url: str, headers: dict, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={**headers})
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(_read_capped(resp))


def _structured(payload: dict, name: str) -> dict:
    """Ask for JSON and, on hybrid reasoning models, turn thinking OFF.

    Measured on a plate-of-nuts photo with qwen3.8-flash: the default reply spent
    ~3,300 output tokens (~35 s) largely on hidden reasoning_content, while
    json_object + enable_thinking=false returned in ~8 s with ~400 tokens and the
    same items. That is the difference between a usable photo log and a timeout.
    Only providers known to accept these fields get them.
    """
    if name == "groq":
        return payload                  # rejects response_format outright
    payload["response_format"] = {"type": "json_object"}
    if name == "qwenplan":
        # DashScope hybrid-reasoning switch; ignored (harmlessly) by models that
        # do not reason.
        payload["enable_thinking"] = False
    return payload


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
    tot = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "fiber": 0.0}
    clean_items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = {
            "name": str(it.get("name") or "").strip()[:120],
            # Brand/variant wording the model may have supplied alongside the
            # generic name; the server keeps it as the item's detail line.
            "detail": str(it.get("detail") or "").strip()[:120],
            "grams": _num(it.get("grams")),
            "calories": max(0.0, _num(it.get("calories"))),
            "protein": max(0.0, _num(it.get("protein"))),
            "carbs": max(0.0, _num(it.get("carbs"))),
            "fat": max(0.0, _num(it.get("fat"))),
            # Fiber is a SUBSET of carbs. Clamp defensively: models occasionally
            # report fiber > carbs, which would render as a nonsense "fiber 30 of
            # 20 g carbs" and wrongly imply carbs should be raised.
            "fiber": max(0.0, min(_num(it.get("fiber")),
                                  max(0.0, _num(it.get("carbs"))))),
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
        # pick() already prefers the summed value when the model's total is far
        # off, and the per-item values were clamped to carbs above.
        "fiber": round(pick("total_fiber"), 1),
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
        self.key = str(cfg.get("api_key") or
                       env_key_for("gemini", cfg.get("base_url"))).strip()
        self.model = str(cfg.get("model") or "gemini-2.5-flash").strip()
        self.base = str(cfg.get("base_url") or
                        "https://generativelanguage.googleapis.com").rstrip("/")

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


    def estimate_meal(self, images: list[str], text: str | None = None) -> dict:
        """One or more food/label photos plus an optional description."""
        if not self.key:
            raise ProviderError("Gemini API key not set")
        parts: list[dict] = [{"text": MEAL_PROMPT}]
        for path in images:
            b64, mime = encode_image(path)
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        if text:
            parts.append({"text": "Written description of what I ate:\n" + text})
        payload = {"contents": [{"parts": parts}],
                   "generationConfig": {"temperature": 0.1,
                                        "responseMimeType": "application/json"}}
        data = _post(f"{self.base}/v1beta/models/{self.model}:generateContent",
                     payload, {"x-goog-api-key": self.key},
                     timeout=45 if not images else 60)
        try:
            got = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            raise ProviderError(f"unexpected reply: {json.dumps(data)[:250]}") from None
        return _extract_json("".join(p.get("text", "") for p in got))

    def estimate_text(self, text: str) -> dict:
        if not self.key:
            raise ProviderError("Gemini API key not set")
        url = f"{self.base}/v1beta/models/{self.model}:generateContent"
        # Text-only calls answer in seconds; don't inherit the image timeout.
        url_try = url
        try:
            data = _post(url_try, {
                "contents": [{"parts": [{"text": TEXT_PROMPT + "\n\nMeal description:\n" + text}]}],
                "generationConfig": {"temperature": 0.1,
                                     "responseMimeType": "application/json"},
            }, {"x-goog-api-key": self.key}, timeout=45)
        except urllib.error.HTTPError as e:
            if e.code != 405:
                raise
            # Some gateways only accept GET:generateContent with a JSON body.
            data = _get_json(url_try, {"x-goog-api-key": self.key})
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            raise ProviderError(f"unexpected reply: {json.dumps(data)[:250]}") from None
        return _extract_json("".join(p.get("text", "") for p in parts))


openai_compat_defaults = {
    "groq": ("https://api.groq.com/openai/v1",
             "meta-llama/llama-4-scout-17b-16e-instruct"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    # Qwen Token Plan ("qwenplan"): DashScope-compatible OpenAI endpoint.
    # Override host with QWENPLAN_BASE_URL (token-plan.maas.* hosts vary by region).
    "qwenplan": ("https://dashscope.aliyuncs.com/compatible-mode/v1",
                 "qwen3.8-flash"),
    "openrouter": ("https://openrouter.ai/api/v1", "qwen/qwen3.8-flash"),
    # Custom endpoints have no sensible default: the user must supply both.
    "custom": ("", ""),
}

openai_compat_env_keys = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "qwenplan": "QWENPLAN_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

openai_compat_env_models = {
    "qwenplan": "QWENPLAN_MODEL",
    "openrouter": "OPENROUTER_MODEL",
}

openai_compat_env_bases = {
    "qwenplan": "QWENPLAN_BASE_URL",
    "openrouter": "OPENROUTER_BASE_URL",
}


class OpenAICompatProvider:
    """Any OpenAI-compatible /chat/completions endpoint that accepts images.

    Covers OpenAI, Groq, OpenRouter, Alibaba DashScope Token Plan (Qwen), and
    user-supplied endpoints. Stored settings beat env vars, which beat the
    built-in defaults -- the Settings UI must not be silently ignored.
    """

    def __init__(self, cfg: dict, name: str = "openai"):
        self.name = name
        self.key = str(cfg.get("api_key") or
                       env_key_for(name, cfg.get("base_url"))).strip()
        base, model = openai_compat_defaults.get(name, ("", ""))
        stored_model = str(cfg.get("model") or "").strip()
        stored_base = str(cfg.get("base_url") or "").strip()
        env_model = os.environ.get(openai_compat_env_models.get(name, ""), "")
        env_base = os.environ.get(openai_compat_env_bases.get(name, ""), "")
        self.model = stored_model or env_model or model
        self.base = (stored_base or env_base or base).rstrip("/")
        # DeepSeek's reasoning variants can consume the whole token budget on
        # hidden reasoning and return an empty content field, which breaks the
        # JSON contract. Meal estimates want speed and JSON, not chain-of-
        # thought, so opt out by default. A config value can override this.
        self.reasoning_effort = str(cfg.get("reasoning_effort") or "").strip().lower()
        if not self.reasoning_effort:
            host = self.base.lower()
            deepseek = (self.name == "custom" and
                        ("deepseek" in host or self.model.startswith("deepseek"))) or \
                       (self.name == "openrouter" and self.model.startswith(("deepseek/", "~deepseek/")))
            if deepseek:
                self.reasoning_effort = "none"

    def _headers(self) -> dict:
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        if self.name == "openrouter":
            # OpenRouter uses these only for attribution/analytics; they are not
            # credentials and are safe to send from a self-hosted app.
            headers.setdefault("X-Title", "MacroLog")
            headers.setdefault("HTTP-Referer", "https://github.com/nschoch/macrolog")
        return headers

    def _chat(self, payload: dict, timeout: int = 90) -> dict:
        """POST /chat/completions, retrying without JSON-mode fields on 400/422.

        OpenRouter and custom gateways front hundreds of models; some reject
        ``response_format`` even though the rest of the OpenAI contract works.
        Retrying once without the optional fields turns a hard failure into a
        plain-text JSON reply that ``_extract_json`` can still parse.
        """
        payload = _structured(payload, self.name)
        if self.reasoning_effort:
            payload.setdefault("reasoning_effort", self.reasoning_effort)
        url = f"{self.base}/chat/completions"
        try:
            return _post(url, payload, self._headers(), timeout=timeout)
        except ProviderError as e:
            if self.name not in ("openrouter", "custom"):
                raise
            if not str(e).startswith(("HTTP 400", "HTTP 422")):
                raise
            payload.pop("response_format", None)
            payload.pop("enable_thinking", None)
            payload.pop("reasoning_effort", None)
            return _post(url, payload, self._headers(), timeout=timeout)

    def estimate(self, image_path: str) -> dict:
        if not self.key and self.name != "custom":
            raise ProviderError(f"{self.name} API key not set")
        if not self.base:
            raise ProviderError(f"{self.name} base URL not set")
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
        data = self._chat(payload)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise ProviderError(f"unexpected reply: {json.dumps(data)[:250]}") from None
        return _extract_json(text)

    def estimate_meal(self, images: list[str], text: str | None = None) -> dict:
        """One or more food/label photos plus an optional description."""
        if not self.key and self.name != "custom":
            raise ProviderError(f"{self.name} API key not set")
        if not self.base:
            raise ProviderError(f"{self.name} base URL not set")
        content: list[dict] = [{"type": "text", "text": MEAL_PROMPT}]
        for path in images:
            b64, mime = encode_image(path)
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"}})
        if text:
            content.append({"type": "text",
                            "text": "Written description of what I ate:\n" + text})
        payload = {"model": self.model, "temperature": 0.1, "max_tokens": 1500,
                   "messages": [{"role": "user", "content": content}]}
        data = self._chat(payload, timeout=45 if not images else 60)
        try:
            out = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise ProviderError(f"unexpected reply: {json.dumps(data)[:250]}") from None
        return _extract_json(out)

    def estimate_text(self, text: str) -> dict:
        if not self.key and self.name != "custom":
            raise ProviderError(f"{self.name} API key not set")
        if not self.base:
            raise ProviderError(f"{self.name} base URL not set")
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content":
                          TEXT_PROMPT + "\n\nMeal description:\n" + text}],
        }
        data = self._chat(payload, timeout=45)
        try:
            out = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise ProviderError(f"unexpected reply: {json.dumps(data)[:250]}") from None
        return _extract_json(out)


class OllamaProvider:
    """Local vision model through Ollama. No account, no network egress."""

    name = "ollama"

    def __init__(self, cfg: dict):
        self.base = str(cfg.get("base_url") or
                        os.environ.get("OLLAMA_HOST",
                                       "http://127.0.0.1:11434")).rstrip("/")
        self.model = str(cfg.get("model") or "llama3.2-vision:11b").strip()

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

    def estimate_meal(self, images: list[str], text: str | None = None) -> dict:
        imgs = [encode_image(p)[0] for p in images]
        body = MEAL_PROMPT
        if text:
            body += "\n\nWritten description of what I ate:\n" + text
        msg = {"role": "user", "content": body}
        if imgs:
            msg["images"] = imgs
        payload = {"model": self.model, "raw": True, "stream": False,
                   "format": "json", "options": {"temperature": 0.1},
                   "messages": [msg]}
        data = _post(f"{self.base}/api/chat", payload, {}, timeout=300)
        out = (data.get("message") or {}).get("content", "")
        if not out:
            raise ProviderError(f"empty reply: {json.dumps(data)[:200]}")
        return _extract_json(out)

    def estimate_text(self, text: str) -> dict:
        payload = {"model": self.model, "raw": True, "stream": False,
                   "format": "json", "options": {"temperature": 0.1},
                   "messages": [{"role": "user",
                                 "content": TEXT_PROMPT + "\n\nMeal description:\n" + text}]}
        data = _post(f"{self.base}/api/chat", payload, {}, timeout=300)
        out = (data.get("message") or {}).get("content", "")
        if not out:
            raise ProviderError(f"empty reply: {json.dumps(data)[:200]}")
        return _extract_json(out)


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

    def estimate_meal(self, images: list[str], text: str | None = None) -> dict:
        """Offline fallback mirroring the merged contract so tests can exercise
        the multi-input endpoint without any network."""
        if text:
            return self.estimate_text(text)
        # No text: fold image sizes into the placeholder so different uploads
        # still produce distinct, deterministic output.
        total = sum(os.path.getsize(p) for p in images if os.path.exists(p))
        kcal = round(320 + (total % 400), 1)
        return {
            "items": [{"name": f"Mixed plate ({len(images)} photo(s), offline)",
                       "grams": 350, "calories": kcal,
                       "protein": kcal * 0.12 / 4, "carbs": kcal * 0.45 / 4,
                       "fat": kcal * 0.40 / 9}],
            "total_calories": kcal, "total_protein": round(kcal * 0.12 / 4, 1),
            "total_carbs": round(kcal * 0.45 / 4, 1),
            "total_fat": round(kcal * 0.40 / 9, 1),
            "confidence": 0.15,
            "description": "Offline placeholder estimate",
            "notes": "No vision provider configured. Add an API key in Settings.",
        }

    def estimate_text(self, text: str) -> dict:
        """Offline fallback: split on commas/newlines/"and", one flat item per
        fragment with a fixed 250 kcal split. Deterministic so tests can assert
        the shape without any network."""
        # Non-capturing split: a capturing group would inject the matched "and"
        # separators into the fragment list as pseudo-items.
        frags = [f.strip(" .;") for f in re.split(r",|\n|\s+and\s+", text) if f.strip(" .;")]
        frags = frags[:8] or ["Described meal"]
        each = round(250.0 * len(frags), 1)
        items = [{"name": f[:60], "grams": 0, "calories": 250.0,
                  "protein": 12.5, "carbs": 28.0, "fat": 11.0} for f in frags]
        return {
            "items": items,
            "total_calories": each, "total_protein": 12.5 * len(frags),
            "total_carbs": 28.0 * len(frags), "total_fat": 11.0 * len(frags),
            "confidence": 0.15,
            "description": (text[:60] or "Offline placeholder"),
            "notes": "Text estimate by offline stub — add a provider key for real numbers.",
        }


PROVIDERS = {
    "gemini": GeminiProvider,
    "openai": lambda cfg: OpenAICompatProvider(cfg, "openai"),
    "groq": lambda cfg: OpenAICompatProvider(cfg, "groq"),
    "qwenplan": lambda cfg: OpenAICompatProvider(cfg, "qwenplan"),
    "openrouter": lambda cfg: OpenAICompatProvider(cfg, "openrouter"),
    "custom": lambda cfg: OpenAICompatProvider(cfg, "custom"),
    "ollama": OllamaProvider,
    "stub": StubProvider,
}

# Kinds the Settings UI may save. Keep this in sync with PROVIDERS: a kind with
# no class is harmless but confusing, and an unknown string is never executed.
PROVIDER_KINDS = tuple(PROVIDERS)


def provider_env_key(kind: str) -> str:
    """Server-side env var that can back this provider kind, or ''."""
    if kind == "gemini":
        return "GEMINI_API_KEY"
    return openai_compat_env_keys.get(kind, "")


def provider_default_base(kind: str) -> str:
    """Built-in / env / explicit default base URL for a provider kind."""
    if kind == "gemini":
        return "https://generativelanguage.googleapis.com"
    if kind == "ollama":
        return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    env_name = openai_compat_env_bases.get(kind, "")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].rstrip("/")
    return openai_compat_defaults.get(kind, ("", ""))[0].rstrip("/")


def _same_base(a: str | None, b: str | None) -> bool:
    return (a or "").strip().rstrip("/").lower() == (b or "").strip().rstrip("/").lower()


def env_key_for(kind: str, base_url: str | None = None) -> str:
    """The operator's env key for `kind`, but ONLY for the provider's own
    default (or operator-set env) base URL.

    A user-chosen base URL must never receive the operator's key: otherwise
    anyone who can save a provider or ask for a model list can point it at a
    host they control and read the key off the Authorization header.
    """
    var = provider_env_key(kind)
    key = os.environ.get(var, "").strip() if var else ""
    if not key:
        return ""
    if base_url and str(base_url).strip() and \
            not _same_base(str(base_url), provider_default_base(kind)):
        return ""
    return key


def provider_default_model(kind: str) -> str:
    """Built-in / env / explicit default model for a provider kind."""
    if kind == "gemini":
        return "gemini-2.5-flash"
    if kind == "ollama":
        return "llama3.2-vision:11b"
    env_name = openai_compat_env_models.get(kind, "")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return openai_compat_defaults.get(kind, ("", ""))[1]


def provider_key_source(entry: dict) -> str | None:
    """Where a usable key comes from: 'saved', 'env', or None.

    The UI uses this to distinguish the user's own key from one the operator
    exposed in the server environment; both are usable, but only the former can
    be cleared from Settings.
    """
    if entry.get("api_key"):
        return "saved"
    if env_key_for(entry.get("kind", ""), entry.get("base_url")):
        return "env"
    return None


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
        with _OPENER.open(req, timeout=1.5) as r:
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
    if kind == "custom":
        # Local OpenAI-compatible servers often have no auth layer. The base URL
        # is the only hard requirement; the estimator sends an empty Bearer only
        # when no key is stored.
        return bool(entry.get("base_url"))
    if entry.get("api_key"):
        return True
    if provider_env_key(kind or ""):
        return bool(env_key_for(kind or "", entry.get("base_url")))
    if kind == "ollama":
        base = entry.get("base_url") or os.environ.get(
            "OLLAMA_HOST", "http://127.0.0.1:11434")
        return _ollama_reachable(base)
    return False


def build_chain(settings: dict) -> list:
    """Instantiate usable providers in configured order.

    Providers with no key are skipped rather than failing the whole request,
    so a default config on a fresh install still works via the offline stub.
    Entries switched off in Settings carry enabled=False and are skipped --
    that is how a user turns a provider off without deleting its model/base
    configuration.
    """
    chain = []
    # Personal providers first; connections shared by other accounts are
    # fallbacks after them so a user's own key/priority always wins.
    entries = list(settings.get("providers") or []) + \
        list(settings.get("shared_providers") or [])
    for entry in entries:
        if entry.get("enabled") is False:
            continue
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


def _run_chain(call, settings: dict) -> tuple[dict, str, str]:
    """Try each provider in order with call(provider); returns
    (meal_dict, provider_name, model). Shared by photo and text paths.

    If a real provider failed and we ended up on the offline stub, the earlier
    failures ride along in meal["_provider_errors"] so callers can tell the user
    *why* the numbers are placeholders instead of silently serving fake ones.
    """
    errors = []
    for prov in build_chain(settings):
        try:
            raw = call(prov)
            meal = normalize(raw)
            meal["_raw"] = raw
            if prov.name == "stub" and errors:
                meal["_provider_errors"] = errors
            model = getattr(prov, "model", prov.name)
            return meal, prov.name, model
        except Exception as e:
            msg = str(e)
            key = str(getattr(prov, "key", "") or "")
            if key:
                # A provider (or a misconfigured gateway) may echo the request
                # in an error body; never let a shared owner's key surface in a
                # recipient's warning string.
                msg = msg.replace(key, "[redacted]")
            errors.append(f"{prov.name}: {type(e).__name__}: {msg[:180]}")
    raise ProviderError(" | ".join(errors) or "no providers configured")


def estimate_photo(image_path: str, settings: dict) -> tuple[dict, str, str]:
    """Try each provider in order. Returns (meal_dict, provider, model)."""
    return _run_chain(lambda p: p.estimate(image_path), settings)


def estimate_text(text: str, settings: dict) -> tuple[dict, str, str]:
    """Describe-a-meal path: same chain, text-only prompts."""
    return _run_chain(lambda p: p.estimate_text(text), settings)


def estimate_meal(images: list[str], text: str | None,
                  settings: dict) -> tuple[dict, str, str]:
    """Merged path: any number of photos (food and/or label) plus optional
    free-text context, resolved by the same provider chain."""
    return _run_chain(lambda p: p.estimate_meal(images, text), settings)
