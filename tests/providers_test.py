#!/usr/bin/env python3
"""Unit tests for per-user provider settings, key handling, and model discovery.

Run: python3 tests/providers_test.py
Exit 0 = every check passed.
"""
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    (PASS if ok else FAIL).append(name)


# Build a pre-upgrade database: one shared provider chain and two existing
# users. Importing server.py must copy the chain to both accounts once.
work = tempfile.mkdtemp(prefix="macrolog-providers-")
os.environ["MACROLOG_DATA"] = work
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("QWENPLAN_API_KEY", "")
db_path = os.path.join(work, "macrolog.sqlite")
con = sqlite3.connect(db_path)
con.executescript("""
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL UNIQUE,
  email TEXT, name TEXT, picture TEXT, created_at INTEGER NOT NULL,
  last_login INTEGER);
""")
con.execute("INSERT INTO settings VALUES(?,?)", ("settings", json.dumps({
    "units": "metric",
    "providers": [
        {"kind": "qwenplan", "model": "qwen3.8-flash"},
        {"kind": "openrouter", "model": "qwen/qwen3.8-flash",
         "api_key": "sk-legacy-secret"},
    ],
})))
con.execute("INSERT INTO users(subject,email,created_at) VALUES('alice','a@x',1)")
con.execute("INSERT INTO users(subject,email,created_at) VALUES('bob','b@x',1)")
con.commit()
con.close()

sys.path.insert(0, ROOT)
import server  # noqa: E402
from app import vision  # noqa: E402

# ---------------------------------------------------------------- migration
shared = server.db.get_setting("settings", {}) or {}
check("shared blob no longer carries providers", "providers" not in shared,
      str(shared)[:100])
alice = (server.db.get_setting("settings:alice", {}) or {}).get("providers") or []
bob = (server.db.get_setting("settings:bob", {}) or {}).get("providers") or []
default = (server.db.get_setting("settings:default", {}) or {}).get("providers") or []
check("existing user alice got the chain", len(alice) == 2, str(alice)[:120])
check("existing user bob got the chain", len(bob) == 2, str(bob)[:120])
check("no-auth default got the chain", len(default) == 2, str(default)[:120])
check("migrated entries have stable ids",
      all(p.get("id") for p in alice), str(alice)[:120])
check("migration preserves the saved key",
      alice[1].get("api_key") == "sk-legacy-secret")
check("default settings are empty for future users",
      server.DEFAULT_SETTINGS.get("providers") == [])
check("a brand-new account starts with no providers",
      server.get_settings("carol").get("providers") == [])

# ------------------------------------------------------- shared providers
# Alice marks one of her providers as shared; Bob (and any new account) sees it
# as a fallback. The key stays server-side and only the owner can change it.
alice_shared = alice + [{
    "id": "sh1", "kind": "openrouter", "model": "shared/model",
    "base_url": "https://openrouter.ai/api/v1", "enabled": True,
    "shared": True, "api_key": "sk-shared",
}]
server.save_user_settings("alice", {"providers": alice_shared})
bob_settings = server.get_settings("bob")
bob_shared = bob_settings.get("shared_providers") or []
check("another user sees a shared provider", len(bob_shared) == 1,
      str(bob_shared)[:160])
check("shared entry carries the owner label and keeps its key server-side",
      bob_shared and bob_shared[0].get("shared_by") == "a@x"
      and bob_shared[0].get("api_key") == "sk-shared", str(bob_shared)[:180])
public_bob = server._public_settings(bob_settings)
check("public settings never expose the shared key",
      "sk-shared" not in json.dumps(public_bob), str(public_bob)[:180])
check("public shared entry is read-only and reports key presence",
      public_bob["shared_providers"][0].get("editable") is False
      and public_bob["shared_providers"][0].get("has_key") is True
      and public_bob["shared_providers"][0].get("shared_by") == "a@x")
check("shared providers are used after personal ones",
      vision.build_chain(bob_settings)[0].name == "openrouter")
alice_settings = server.get_settings("alice")
check("the owner does not see their own provider duplicated",
      alice_settings.get("shared_providers") == [],
      str(alice_settings.get("shared_providers"))[:120])
check("the owner's row reports the shared flag",
      any(p.get("shared") is True for p in alice_settings.get("providers") or []))
check("a new account can use the shared fallback",
      len(server.get_settings("carol").get("shared_providers") or []) == 1)
# Unsharing is live: once Alice flips the flag, Bob loses it.
server.save_user_settings("alice", {"providers": [
    {**p, "shared": False} for p in alice_shared]})
check("unsharing removes the provider for everyone else",
      server.get_settings("bob").get("shared_providers") == [])

# ------------------------------------------------------- key sanitization
existing = [
    {"id": "p1", "kind": "openrouter", "model": "qwen/qwen3.8-flash",
     "api_key": "sk-saved"},
]
same = server._clean_providers(
    [{"id": "p1", "kind": "openrouter", "model": "qwen/qwen3.8-flash"}], existing)
check("blank key keeps the saved key", same[0].get("api_key") == "sk-saved",
      str(same)[0][:120])
changed = server._clean_providers(
    [{"id": "p1", "kind": "custom", "model": "m", "base_url": "http://x/v1"}],
    existing)
check("changing kind does not carry the old key", "api_key" not in changed[0])
replaced = server._clean_providers(
    [{"id": "p1", "kind": "openrouter", "api_key": "sk-new"}], existing)
check("a new key replaces the old one", replaced[0].get("api_key") == "sk-new")
cleared = server._clean_providers(
    [{"id": "p1", "kind": "openrouter", "clear_key": True}], existing)
check("clear_key removes the saved key", "api_key" not in cleared[0])
both = server._clean_providers(
    [{"id": "p1", "kind": "openrouter", "api_key": "sk-new", "clear_key": True}],
    existing)
check("a provided key wins over clear_key", both[0].get("api_key") == "sk-new")
shared_existing = [{"id": "p1", "kind": "openrouter", "shared": True}]
kept_shared = server._clean_providers(
    [{"id": "p1", "kind": "openrouter"}], shared_existing)
check("omitting the share flag preserves it",
      kept_shared[0].get("shared") is True)
unshared = server._clean_providers(
    [{"id": "p1", "kind": "openrouter", "shared": False}], shared_existing)
check("an explicit false unshares", unshared[0].get("shared") is False)
new_kind = server._clean_providers(
    [{"id": "p1", "kind": "custom", "model": "m", "base_url": "http://x/v1"}],
    shared_existing)
check("changing provider kind does not keep sharing on",
      new_kind[0].get("shared") is False)
check("unknown kinds are discarded",
      server._clean_providers([{"kind": "evil"}], []) == [])
check("URL credentials are rejected",
      not server._valid_provider_url("https://user:pass@example.com/v1"))
check("loopback URLs are allowed (Ollama/LAN use case)",
      server._valid_provider_url("http://127.0.0.1:11434"))
try:
    server._clean_providers([{"kind": "custom", "model": "m"}], [])
    missing_base = False
except ValueError:
    missing_base = True
check("custom provider requires a base URL", missing_base)
try:
    server._clean_providers([{"kind": "custom", "model": "m",
                              "base_url": "ftp://x"}], [])
    bad_scheme = False
except ValueError:
    bad_scheme = True
check("custom provider rejects non-http URLs", bad_scheme)
try:
    server._clean_providers([{"kind": "stub"}] * 13, [])
    too_many = False
except ValueError:
    too_many = True
check("provider count is bounded", too_many)

# ------------------------------------------------------- public redaction
public = server._public_settings({"providers": [{
    "id": "p1", "kind": "openrouter", "model": "m", "base_url": "https://x/v1",
    "enabled": True, "api_key": "sk-secret",
}]})
check("public settings omit the key",
      "sk-secret" not in json.dumps(public), str(public)[:140])
check("public settings report has_key and key_source",
      public["providers"][0].get("has_key") is True
      and public["providers"][0].get("key_source") == "saved")
legacy = server._public_settings({"api_key": "sk-legacy-top", "providers": []})
check("legacy top-level api_key is stripped",
      "sk-legacy-top" not in json.dumps(legacy), str(legacy)[:120])
try:
    server._clean_providers([{"kind": "custom", "model": "m",
                              "base_url": "https://example.com/" + "a" * 600}], [])
    long_url = False
except ValueError:
    long_url = True
check("over-long base URLs are rejected rather than truncated", long_url)

# ------------------------------------------------------- model discovery
openrouter_fixture = {"data": [
    {"id": "vendor/vision", "name": "Vision", "context_length": 128000,
     "architecture": {"input_modalities": ["text", "image"]},
     "pricing": {"prompt": "0.00000015"}},
    {"id": "vendor/text", "name": "Text",
     "architecture": {"input_modalities": ["text"]}},
    {"id": "vendor/vision:batch", "name": "Batch",
     "architecture": {"input_modalities": ["text", "image"]}},
]}
gemini_fixture = {"models": [
    {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash",
     "supportedGenerationMethods": ["generateContent"], "inputTokenLimit": 1000000},
    {"name": "models/text-embedding-004", "displayName": "Embedding",
     "supportedGenerationMethods": ["embedContent"]},
]}
ollama_fixture = {"models": [{
    "name": "llama3.2-vision:11b", "model": "llama3.2-vision:11b",
    "details": {"parameter_size": "11B"},
}]}
calls = []


def fake_open_json(url, headers=None, timeout=20):
    calls.append((url, headers or {}))
    if "/v1beta/models" in url:
        return gemini_fixture
    if url.endswith("/api/tags"):
        return ollama_fixture
    return openrouter_fixture


server._open_json = fake_open_json
models = server._fetch_models("openrouter", "https://openrouter.ai/api/v1", "sk-x")
check("OpenRouter discovery keeps only image-input, non-batch models",
      [m["id"] for m in models] == ["vendor/vision"], str(models)[:160])
check("OpenRouter discovery sends the bearer key",
      calls and calls[-1][1].get("Authorization") == "Bearer sk-x")
models = server._fetch_models("custom", "http://127.0.0.1:9/v1", "")
check("generic discovery returns every model",
      [m["id"] for m in models] == ["vendor/vision", "vendor/text",
                                    "vendor/vision:batch"])
models = server._fetch_models("gemini", "https://generativelanguage.googleapis.com", "k")
check("Gemini discovery keeps generateContent models",
      [m["id"] for m in models] == ["gemini-2.5-flash"], str(models)[:120])
models = server._fetch_models("ollama", "http://127.0.0.1:11434", "")
check("Ollama discovery lists installed tags",
      [m["id"] for m in models] == ["llama3.2-vision:11b"], str(models)[:120])
check("Gemini has a default model for key-only providers",
      server.provider_default_model("gemini") == "gemini-2.5-flash")

# ------------------------------------------------------- DeepSeek reasoning
# DeepSeek Flash is multimodal but is also a reasoning model. Without
# reasoning_effort=none it spends the whole max_tokens budget on hidden
# reasoning and returns empty content, which breaks the JSON contract.
deepseek = vision.OpenAICompatProvider({
    "kind": "custom", "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash", "api_key": "sk-x"}, "custom")
check("DeepSeek direct disables hidden reasoning",
      deepseek.reasoning_effort == "none", deepseek.reasoning_effort)
orouter_deepseek = vision.OpenAICompatProvider({
    "kind": "openrouter", "model": "deepseek/deepseek-v4.1-flash",
    "api_key": "sk-x"}, "openrouter")
check("OpenRouter DeepSeek also disables hidden reasoning",
      orouter_deepseek.reasoning_effort == "none", orouter_deepseek.reasoning_effort)
qwen = vision.OpenAICompatProvider({
    "kind": "openrouter", "model": "qwen/qwen3.8-flash",
    "api_key": "sk-x"}, "openrouter")
check("non-DeepSeek models keep their default reasoning behavior",
      not qwen.reasoning_effort, repr(qwen.reasoning_effort))
captured = {}
_orig_post = vision._post


def _fake_post(url, payload, headers, timeout=90):
    captured.clear()
    captured.update(payload)
    return {"choices": [{"message": {"content": "{\"ok\": true}"}}]}


vision._post = _fake_post
try:
    deepseek.estimate_text("ping")
    check("DeepSeek request carries reasoning_effort none",
          captured.get("reasoning_effort") == "none", str(captured)[:120])
    qwen.estimate_text("ping")
    check("Qwen request does not carry a reasoning flag",
          "reasoning_effort" not in captured, str(captured)[:120])
finally:
    vision._post = _orig_post

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failures:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
