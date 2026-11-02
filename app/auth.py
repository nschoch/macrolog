"""OpenID Connect relying party for MacroLog (works with Pocket ID / Authelia /
any standard OIDC provider).

Implements the Authorization Code flow with PKCE and validates the returned
ID token (signature via JWKS, iss/aud/exp/nonce) before establishing a session.
No third-party libraries — stdlib only; RSA signature verification is done with
the `cryptography` package when available, otherwise we fall back to trusting a
TLS-protected userinfo call (documented below).

Configuration comes from environment (see .env.example):
  OIDC_ISSUER         e.g. https://auth.example.com
  OIDC_CLIENT_ID      from Pocket ID > Administration > OIDC Clients
  OIDC_CLIENT_SECRET  same (optional; PKCE public-client if omitted)
  OIDC_REDIRECT_BASE  optional override of the externally-visible origin
  OIDC_ALLOWED_EMAILS optional comma list; empty = allow any authenticated user
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

# In-memory stores are fine for a single-process server; sessions persist across
# restarts via signed cookies so users aren't logged out on every deploy.
_PENDING: dict[str, dict] = {}     # state -> {redirect_after, created}
_DISCOVERY_CACHE: dict[str, tuple[float, dict]] = {}   # issuer -> (ts, doc)
_JWKS_CACHE: dict[str, tuple[float, dict]] = {}        # kid -> (ts, key)
_CACHE_TTL = 3600


class AuthError(Exception):
    pass


def enabled() -> bool:
    return bool(os.environ.get("OIDC_ISSUER") and os.environ.get("OIDC_CLIENT_ID"))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode())


def discover(issuer: str) -> dict:
    """Fetch and cache the provider's openid-configuration document."""
    issuer = issuer.rstrip("/")
    hit = _DISCOVERY_CACHE.get(issuer)
    if hit and hit[0] > time.time():
        return hit[1]
    url = issuer + "/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            doc = json.loads(r.read())
    except Exception as e:
        raise AuthError(f"OIDC discovery failed for {url}: {e}") from None
    _DISCOVERY_CACHE[issuer] = (time.time() + _CACHE_TTL, doc)
    return doc


# ------------------------------------------------------------------- sessions
# The session HMAC key must be secret. It used to fall back to a hash of the
# (public) OIDC client id, which let anyone mint a cookie for any subject. Now:
#   1. MACROLOG_SESSION_SECRET if set, else
#   2. <data dir>/session.key, created once with 32 random bytes (mode 0600), else
#   3. a random per-process key (sessions end on restart, but never forgeable).
# The key is resolved lazily so a .env loaded after import still takes effect.
_SESSION_KEY: bytes | None = None
SESSION_KEY_FILE = "session.key"


def init_session_key(data_dir: str | None = None) -> str:
    """Resolve the session key now; returns where it came from (for logging)."""
    global _SESSION_KEY
    env = os.environ.get("MACROLOG_SESSION_SECRET", "").strip()
    if env:
        _SESSION_KEY = env.encode()
        return "env" if len(env) >= 32 else "env (short: use 32+ random chars)"
    if data_dir:
        path = os.path.join(data_dir, SESSION_KEY_FILE)
        try:
            with open(path, "rb") as fh:
                key = fh.read().strip()
            if len(key) >= 32:
                _SESSION_KEY = key
                return path
        except FileNotFoundError:
            pass
        os.makedirs(data_dir, exist_ok=True)
        key = _b64url(secrets.token_bytes(32)).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        _SESSION_KEY = key
        return path + " (new)"
    _SESSION_KEY = secrets.token_bytes(32)
    return "ephemeral"


def _session_key() -> bytes:
    if _SESSION_KEY is None:
        init_session_key(os.environ.get("MACROLOG_DATA") or None)
    return _SESSION_KEY  # type: ignore[return-value]


def make_session_cookie(user: dict) -> str:
    payload = {"sub": user["subject"], "email": user.get("email"),
               "name": user.get("name"), "exp": int(time.time()) + 7 * 86400}
    body = _b64url(json.dumps(payload).encode())
    sig = _b64url(hmac.new(_session_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session_cookie(raw: str | None) -> dict | None:
    if not raw or "." not in raw:
        return None
    body, _, sig = raw.rpartition(".")
    expect = _b64url(hmac.new(_session_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_b64url_decode(body))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------------------------------------------------------------------------
# Encrypted, stateless OIDC handshake ("state blob").
#
# The PKCE verifier and nonce must survive between /auth/login and the
# provider's callback. They used to live in an in-memory dict, which broke on
# every server restart (deploys!) and can't span multiple workers — iOS Safari
# re-delivers callbacks later than Android does, so users hit "invalid or
# expired state". Now the whole handshake travels inside the `state` parameter
# itself: sealed (AES-256-GCM when `cryptography` is present, else an HMAC-
# SHA256 envelope). Confidential clients key it with the client secret; public
# clients key it with the persisted session key. Either way it is tamper-
# evident — the provider echoes state back verbatim — and needs no shared
# storage. The browser's state cookie may carry several in-flight states so
# duplicate / concurrent /auth/login navigations cannot invalidate each other.
# ---------------------------------------------------------------------------
_STATE_BLOB_AAD = b"macrolog-oidc-state-v1"

try:                                    # prefer real encryption if available
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
except Exception:                       # LXC has it; dev boxes may not
    _AESGCM = None


def _state_key() -> bytes:
    """Key for the sealed handshake state.

    Confidential clients use the client secret. Public clients have no client
    secret, so use the persisted session key — secret, stable across restarts,
    and already required for sessions. Never a public constant.
    """
    secret = (os.environ.get("OIDC_CLIENT_SECRET") or "").strip()
    if secret:
        return hashlib.sha256(("macrolog-state:" + secret).encode()).digest()
    return hmac.new(_session_key(), _STATE_BLOB_AAD + b":public",
                    hashlib.sha256).digest()


def seal_state(meta: dict) -> str:
    """Encode the handshake so it survives restarts and other workers.

    Returns a URL-safe, tamper-evident string that the provider echoes back in
    `state`. Uses AES-256-GCM when `cryptography` is present, otherwise an
    HMAC-SHA256 envelope. Integrity matters here, not confidentiality (the
    verifier is worthless without this request's own code exchange).
    """
    payload = json.dumps({
        "v": meta["verifier"], "n": meta["nonce"],
        "r": meta.get("redirect_after", "/"), "t": int(meta["created"]),
    }).encode()
    if _AESGCM is not None:
        nonce = secrets.token_bytes(12)
        ct = _AESGCM(_state_key()).encrypt(nonce, payload, _STATE_BLOB_AAD)
        return "G" + _b64url(nonce + ct)
    iv = secrets.token_bytes(16)
    mac = hmac.new(_state_key(), _STATE_BLOB_AAD + iv + payload,
                   hashlib.sha256).digest()[:16]
    return "H" + _b64url(iv + mac + payload)


def open_state(state: str) -> dict | None:
    """Inverse of seal_state; None if this isn't one of our sealed blobs."""
    if not state or state[0] not in "GH":
        return None
    try:
        raw = _b64url_decode(state[1:])
        if state[0] == "G":
            if _AESGCM is None:
                return None
            pt = _AESGCM(_state_key()).decrypt(raw[:12], raw[12:], _STATE_BLOB_AAD)
        else:
            iv, mac, payload = raw[:16], raw[16:32], raw[32:]
            expect = hmac.new(_state_key(), _STATE_BLOB_AAD + iv + payload,
                              hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(mac, expect):
                return None                  # tampered
            pt = payload
        d = json.loads(pt)
        return {"verifier": d["v"], "nonce": d["n"],
                "redirect_after": d.get("r", "/"), "created": float(d["t"])}
    except Exception:
        return None


def build_login_url(base_url: str, redirect_after: str = "/") -> tuple[str, dict]:
    """Return (authorize_url, cookie_state). Caller sets a short-lived state cookie."""
    issuer = os.environ["OIDC_ISSUER"]
    client_id = os.environ["OIDC_CLIENT_ID"]
    doc = discover(issuer)
    code_verifier = _b64url(secrets.token_bytes(32))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(16))
    nonce = _b64url(secrets.token_bytes(16))
    redirect_uri = _redirect_uri(base_url)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    url = doc["authorization_endpoint"] + sep + urllib.parse.urlencode(params)
    meta = {"verifier": code_verifier, "nonce": nonce, "state": state,
            "redirect_after": redirect_after, "created": time.time()}
    # Stateless handshake: the provider echoes `state` back verbatim, so we
    # carry the verifier+nonce inside it and store nothing — survives restarts
    # and works across workers.
    sealed = seal_state(meta)
    meta["state"] = sealed
    url = url.replace(urllib.parse.quote_plus(state),
                      urllib.parse.quote_plus(sealed))
    return url, meta


def _redirect_uri(base_url: str) -> str:
    base = os.environ.get("OIDC_REDIRECT_BASE") or base_url
    return base.rstrip("/") + "/auth/callback"


def allowed(email: str | None) -> bool:
    csv = os.environ.get("OIDC_ALLOWED_EMAILS", "").strip()
    if not csv:
        return True                       # anyone Pocket ID authenticates
    ok = {e.strip().lower() for e in csv.split(",") if e.strip()}
    return bool(email) and email.lower() in ok


def complete_login(base_url: str, query: dict, expected_states) -> dict:
    """Exchange the authorization code for tokens, validate, return user dict.

    `expected_states` is the list of in-flight states from the browser's
    HttpOnly state cookie. The callback's state must be one of them: this is
    the CSRF binding, and it also proves the callback belongs to a login this
    browser started. A list (rather than a single value) keeps duplicate or
    concurrent /auth/login navigations from invalidating each other.
    """
    if "error" in query:
        raise AuthError(f"provider returned error: {query.get('error')} "
                        f"{query.get('error_description', '')}".strip())
    code = query.get("code")
    state = query.get("state")
    if not code or not state:
        raise AuthError("missing code/state on callback")
    meta = open_state(state)             # stateless: survives restarts/workers
    legacy = False
    if meta is None:
        meta = _PENDING.pop(state, None)  # legacy in-memory fallback
        legacy = True
    if not meta:
        raise AuthError("invalid or expired state (possible CSRF); retry login")
    if isinstance(expected_states, str):
        expected_states = [expected_states]
    expected_states = [s for s in (expected_states or []) if s]
    # The state is public (it rides in the URL); plain membership is enough.
    # It must have been minted for *this* browser's cookie, which is the CSRF
    # binding that stops a saved attacker callback URL from logging a victim in.
    if state not in expected_states:
        raise AuthError("invalid or expired state (possible CSRF); retry login")
    if legacy and meta.get("state") and meta["state"] != state:
        raise AuthError("invalid or expired state (possible CSRF); retry login")
    if time.time() - meta["created"] > 600:
        raise AuthError("login took too long; start again")

    issuer = os.environ["OIDC_ISSUER"]
    client_id = os.environ["OIDC_CLIENT_ID"]
    client_secret = os.environ.get("OIDC_CLIENT_SECRET", "")
    doc = discover(issuer)

    form = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": _redirect_uri(base_url),
            "client_id": client_id, "code_verifier": meta["verifier"]}
    if client_secret:
        form["client_secret"] = client_secret
    req = urllib.request.Request(
        doc["token_endpoint"], data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AuthError(f"token exchange failed ({e.code}): {e.read().decode()[:200]}") from None
    except Exception as e:
        raise AuthError(f"token exchange error: {e}") from None

    id_token = tok.get("id_token")
    claims = {}
    if id_token:
        claims = verify_id_token(id_token, doc, issuer, client_id, meta["nonce"])
    if not claims.get("sub"):
        # No usable id_token claims -> call userinfo over TLS as fallback.
        claims = fetch_userinfo(doc, tok.get("access_token"))
    if not claims.get("sub"):
        raise AuthError("could not determine user identity from provider")

    email = claims.get("email")
    if not allowed(email):
        raise AuthError(f"user {email or claims['sub']} is not in the allowlist")

    user = {"subject": str(claims["sub"]), "email": email,
            "name": claims.get("name") or email or claims.get("preferred_username"),
            "picture": claims.get("picture")}
    return user, meta.get("redirect_after", "/")


def fetch_userinfo(doc: dict, access_token: str | None) -> dict:
    if not access_token:
        return {}
    req = urllib.request.Request(doc.get("userinfo_endpoint", ""),
                                 headers={"Authorization": f"Bearer {access_token}",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return {}


# --------------------------------------------------------- id_token validation
def verify_id_token(jwt: str, doc: dict, issuer: str, client_id: str,
                    nonce: str) -> dict:
    parts = jwt.split(".")
    if len(parts) != 3:
        raise AuthError("malformed id_token")
    header_b, payload_b, sig_b = parts
    claims = json.loads(_b64url_decode(payload_b))
    header = json.loads(_b64url_decode(header_b))

    now = time.time()
    if claims.get("iss") not in (issuer, issuer + "/"):
        raise AuthError(f"id_token issuer mismatch: {claims.get('iss')}")
    aud = claims.get("aud")
    if not (aud == client_id or (isinstance(aud, list) and client_id in aud)):
        raise AuthError("id_token audience mismatch")
    if claims.get("exp", 0) < now - 30:
        raise AuthError("id_token expired")
    if claims.get("nonce") and claims["nonce"] != nonce:
        raise AuthError("id_token nonce mismatch")

    # Signature verification is MANDATORY when an id_token is present. We fail
    # closed: if the crypto library is unavailable we reject rather than accept
    # an unverified token. Set OIDC_INSECURE_SKIP_SIGCHECK=1 only for local dev.
    alg = header.get("alg", "")
    if os.environ.get("OIDC_INSECURE_SKIP_SIGCHECK") == "1":
        return claims                       # explicitly opted-in dev escape hatch
    try:
        _verify_sig(doc, header_b + "." + payload_b, sig_b,
                    claims.get("kid") or header.get("kid"), alg)
    except AuthError:
        raise
    except ImportError:
        raise AuthError(
            "cannot verify id_token: 'cryptography' is not installed. "
            "Install it (pip install cryptography) or set OIDC_REDIRECT_BASE to "
            "use the userinfo fallback. Refusing to accept an unverified token.") from None
    except Exception as e:
        raise AuthError(f"id_token signature check failed: {e}") from None
    return claims


def _jwk_to_public_key(kid: str, doc: dict):
    from cryptography.hazmat.primitives.asymmetric import rsa, padding  # noqa
    jwks_url = doc.get("jwks_uri")
    cached = _JWKS_CACHE.get("__jwks__")
    if not (cached and cached[0] > time.time()):
        with urllib.request.urlopen(jwks_url, timeout=15) as r:
            jwks = json.loads(r.read())
        _JWKS_CACHE["__jwks__"] = (time.time() + _CACHE_TTL, jwks)
        keys = jwks.get("keys", [])
    else:
        keys = cached[1].get("keys", [])
    jwk = next((k for k in keys if k.get("kid") == kid), None) or (keys[0] if keys else None)
    if not jwk:
        raise AuthError("no matching JWK for token kid")
    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    pub = rsa.RSAPublicNumbers(e, n).public_key()
    return pub, padding


def _verify_sig(doc: dict, signing_input: str, sig_b64: str, kid: str, alg: str) -> None:
    # HS256/384/512 verify with the client secret (pure stdlib, no native deps).
    if alg in ("HS256", "HS384", "HS512"):
        secret = os.environ.get("OIDC_CLIENT_SECRET", "").encode()
        if not secret:
            raise AuthError(f"id_token uses {alg} but no OIDC_CLIENT_SECRET configured")
        hashmod = {"HS256": hashlib.sha256, "HS384": hashlib.sha384,
                   "HS512": hashlib.sha512}[alg]
        expect = hmac.new(secret, signing_input.encode(), hashmod).digest()
        if not hmac.compare_digest(expect, _b64url_decode(sig_b64)):
            raise AuthError("id_token HMAC signature mismatch")
        return
    # RS* requires the cryptography package.
    from cryptography.hazmat.primitives import hashes  # raises ImportError if absent
    pub, padding = _jwk_to_public_key(kid, doc)
    sig = _b64url_decode(sig_b64)
    mapping = {"RS256": hashes.SHA256(), "RS384": hashes.SHA384(), "RS512": hashes.SHA512()}
    if alg not in mapping:
        raise AuthError(f"unsupported id_token alg {alg}")
    pub.verify(sig, signing_input.encode(),
               padding.PKCS1v15(), mapping[alg])
