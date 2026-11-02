"""Minimal in-process OpenID Connect provider for testing MacroLog's client.

Not secure, not spec-complete beyond what the RP needs: discovery doc, an
authorize endpoint that immediately redirects back with a code, a token endpoint
that mints an RS256 id_token signed by a throwaway key, and a JWKS endpoint.
Run standalone on a port, or import start() from a test.
"""
from __future__ import annotations
import base64, hashlib, json, secrets, threading, time, urllib.parse, http.server, socketserver

def _b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

# ---- one-time RSA key via cryptography (required for these tests) ----------
try:
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes, serialization
    HAVE_CRYPTO = True
except Exception as _e:                    # ImportError OR native-lib load error
    HAVE_CRYPTO = False
    CRYPTO_ERR = str(_e)[:120]

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048) if HAVE_CRYPTO else None
PUB = KEY.public_key().public_numbers() if HAVE_CRYPTO else None
KID = "testkey1"
HS_SECRET = b"shared-hs256-secret-for-mock-only"

CODES = {}          # code -> {sub, email, name, nonce, redirect}
STATE = {}

def int_to_b64(n):
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return _b64u(b)

JWKS = {"keys": ([{"kty": "RSA", "kid": KID, "alg": "RS256", "use": "sig",
                   "n": int_to_b64(PUB.n), "e": int_to_b64(PUB.e)}]
                 if HAVE_CRYPTO else [])}


def make_id_token(iss, aud, sub, nonce, extra=None):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "iat": now,
              "exp": now + 3600, "nonce": nonce, "email_verified": True}
    if extra:
        claims.update(extra)
    if HAVE_CRYPTO:
        header = {"alg": "RS256", "typ": "JWT", "kid": KID}
        signing_input = (_b64u(json.dumps(header).encode()) + "." +
                         _b64u(json.dumps(claims).encode()))
        sig = KEY.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return signing_input + "." + _b64u(sig)
    # HS256 fallback so the flow is testable without native crypto on this host.
    import hmac as _hmac
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = (_b64u(json.dumps(header).encode()) + "." +
                     _b64u(json.dumps(claims).encode()))
    sig = _b64u(_hmac.new(HS_SECRET, signing_input.encode(), hashlib.sha256).digest())
    return signing_input + "." + sig


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        iss = f"http://{self.headers.get('Host')}"
        if u.path == "/.well-known/openid-configuration":
            return self._json({
                "issuer": iss,
                "authorization_endpoint": iss + "/authorize",
                "token_endpoint": iss + "/token",
                "jwks_uri": iss + "/jwks",
                "userinfo_endpoint": iss + "/userinfo",
                "response_types_supported": ["code"],
                "scopes_supported": ["openid", "profile", "email"],
                "id_token_signing_alg_values_supported": ["RS256"],
            })
        if u.path == "/jwks":
            return self._json(JWKS)
        if u.path == "/authorize":
            code = "code-" + secrets.token_urlsafe(9)
            CODES[code] = {"redirect": q.get("redirect_uri"),
                           "nonce": q.get("nonce"), "state": q.get("state"),
                           "client_id": q.get("client_id")}
            loc = q["redirect_uri"] + "?code=" + code + "&state=" + q.get("state", "")
            self.send_response(302); self.send_header("Location", loc)
            self.send_header("Content-Length", "0"); self.end_headers()
            return
        if u.path == "/userinfo":
            return self._json({"sub": "user-abc", "email": "test@example.com"})
        self.send_error(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
        if u.path == "/token":
            code = form.get("code")
            meta = CODES.pop(code, None)
            if not meta:
                return self._json({"error": "invalid_grant"}, 400)
            email = "test@example.com"; sub = "user-abc"; name = "Test User"
            idt = make_id_token(f"http://{self.headers.get('Host')}",
                                form.get("client_id"), sub, meta["nonce"],
                                {"email": email, "name": name})
            return self._json({"access_token": "***", "token_type": "Bearer",
                               "id_token": idt})
        self.send_error(404)

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)


def start(port=0):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, url = start(9099)
    print("mock OIDC at", url)
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()
