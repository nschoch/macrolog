"""Regression: concurrent login tabs must not invalidate each other.

Symptom (live, 2026-11-02 22:18-22:19 UTC): the PWA fires /auth/login twice
within a second, then one /auth/callback returns 400 with
"invalid or expired state (possible CSRF)". Both logins wrote a single
macrolog_oidc_state cookie; the second overwrote the first, so the first
callback's state no longer matched the cookie.

Run:  python3 tests/auth_race_test.py
Exit 0 = both concurrent callbacks succeed; exit 1 = race present.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mock_oidc  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    return ok


class Jar:
    """Tiny browser cookie jar: last write wins, Max-Age=0 deletes."""

    def __init__(self):
        self.cookies = {}

    def header(self):
        return "; ".join(f"{k}={urllib.parse.quote(v)}" for k, v in self.cookies.items())

    def absorb(self, headers):
        for raw in headers.get_all("Set-Cookie") or []:
            name, _, rest = raw.partition("=")
            val = rest.split(";", 1)[0]
            if "Max-Age=0" in raw:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = urllib.parse.unquote(val)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(url, jar=None):
    """GET an absolute URL; absorb Set-Cookie; return (status, headers, text)."""
    h = {"Cookie": jar.header()} if jar and jar.cookies else {}
    try:
        with urllib.request.build_opener(NoRedirect).open(
                urllib.request.Request(url, headers=h), timeout=15) as r:
            status, headers, body = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, headers, body = e.code, e.headers, e.read()
    if jar is not None:
        jar.absorb(headers)
    return status, headers, body.decode(errors="replace")


def login(jar):
    """Hit /auth/login, follow the mock IdP, return this flow's callback path."""
    st, hd, bd = http(BASE + "/auth/login", jar)
    loc = hd.get("Location", "")
    if st != 302 or not loc:
        raise SystemExit(f"/auth/login failed: {st} {bd[:160]}")
    st2, hd2, bd2 = http(loc)                # mock IdP mints a code, redirects back
    cb = hd2.get("Location", "")
    if st2 != 302 or not cb:
        raise SystemExit(f"mock IdP failed: {st2} {bd2[:160]}")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(cb).query)
    return "/auth/callback?" + urllib.parse.urlencode(
        {"code": q.get("code", [""])[0], "state": q.get("state", [""])[0]})


# ---- start mock IdP -------------------------------------------------------
idp, idp_url = mock_oidc.start()
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
BASE = f"http://127.0.0.1:{port}"
work = tempfile.mkdtemp(prefix="macrolog-race-")
env = dict(os.environ,
           MACROLOG_DATA=work,
           OIDC_ISSUER=idp_url,
           OIDC_CLIENT_ID="macrolog",
           OIDC_CLIENT_SECRET=mock_oidc.HS_SECRET.decode(),
           OIDC_REDIRECT_BASE=BASE,
           QWENPLAN_API_KEY="")
proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"),
                         "--host", "127.0.0.1", "--port", str(port)],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

try:
    for _ in range(80):
        if proc.poll() is not None:
            sys.exit(f"server exited early ({proc.returncode})")
        try:
            if http(BASE + "/healthz")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        sys.exit("server did not start")

    print(f"\nConcurrent login race -> {BASE}\n")

    # The browser/PWA fires two logins before the first callback lands.
    jar = Jar()
    cb1 = login(jar)
    cb2 = login(jar)

    def state_of(cb):
        return urllib.parse.parse_qs(urllib.parse.urlparse(cb).query).get("state", [""])[0]

    check("second login is a separate authorization request",
          state_of(cb1) != state_of(cb2))

    # The first callback is the one the user actually lands on. It must pass
    # even though another login started while it was in flight.
    st, hd, bd = http(BASE + cb1, jar)
    check("first callback survives a concurrent second login", st == 302,
          f"{st} {bd[:160]}")
    check("session cookie issued by first callback",
          any("macrolog_session=" in c for c in (hd.get_all("Set-Cookie") or [])),
          str(hd.get_all("Set-Cookie"))[:120])

    # The other in-flight login must still be able to finish.
    st, hd, bd = http(BASE + cb2, jar)
    check("second in-flight login also completes", st == 302,
          f"{st} {bd[:160]}")

    # Cross-browser replay must still be rejected: A's callback in B's jar.
    jar_a, jar_b = Jar(), Jar()
    cb_a = login(jar_a)
    login(jar_b)
    st, hd, bd = http(BASE + cb_a, jar_b)
    check("callback replayed in a different browser is rejected", st == 400,
          f"{st} {bd[:160]}")
    check("rejection is the CSRF error", "CSRF" in bd, bd[:160])

    # A missing state cookie must not be accepted (audit H3).
    st, hd, bd = http(BASE + cb_a, Jar())
    check("callback with no state cookie is rejected", st == 400,
          f"{st} {bd[:160]}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    idp.shutdown()
