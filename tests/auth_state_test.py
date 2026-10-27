#!/usr/bin/env python3
"""Regression: OIDC state must survive a server restart and concurrent workers.

The in-memory _PENDING dict in app/auth.py means an authorization-code callback
that lands on a different process (or after any deploy/restart) than the login
that started it fails with 'invalid or expired state' — which is exactly what
iOS Safari users hit because iOS frequently discards PWA tabs (and their
in-flight navigations) and re-opens the callback URL later.

This test proves the failure by running the real handler logic against a fresh
module state (simulating a restart between /auth/login and /auth/callback).
Run:  python3 tests/auth_state_test.py
Exit 0 = flow survives; exit 1 = bug present.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OIDC_ISSUER", "https://issuer.invalid")
os.environ.setdefault("OIDC_CLIENT_ID", "test-client")
os.environ.setdefault("OIDC_CLIENT_SECRET", "x")

from app import auth  # noqa: E402

# Mock OIDC discovery so the test runs fully offline; build_login_url only
# needs an authorization_endpoint, complete_login fails before any network use.
FAKE_DOC = {
    "authorization_endpoint": "https://issuer.invalid/authorize",
    "token_endpoint": "https://issuer.invalid/token",
    "jwks_uri": "https://issuer.invalid/keys",
}
auth.discover = lambda issuer: FAKE_DOC


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def main():
    failures = 0

    # 1) Login stores pending state in memory only.
    url, meta = auth.build_login_url("https://app.example", redirect_after="/")
    state = meta["state"]
    stored = state in auth._PENDING
    if not check("login records pending state", stored):
        failures += 1

    # 2) Simulate ANY loss of that memory (restart, second worker, reload):
    #    the exact condition iOS Safari triggers by re-delivering the callback.
    auth._PENDING.clear()

    # With the fix, `state` is a self-contained sealed blob, so clearing
    # _PENDING must NOT break validation. complete_login will then fail at the
    # network token exchange (issuer.invalid) — reaching that step proves the
    # state check passed. A "invalid or expired state" error means the bug is
    # still present.
    query = {"code": "abc", "state": state}
    try:
        auth.complete_login("https://app.example", query, expected_state=state)
        reached_exchange = True
        err = ""
    except auth.AuthError as e:
        err = str(e)
        reached_exchange = "state" not in err.lower()

    if not check("callback survives server restart / lost memory", reached_exchange,
                 err[:90]):
        failures += 1

    # CSRF binding: provider echoing back a state that != our cookie must reject.
    auth._PENDING.clear()
    try:
        auth.complete_login("https://app.example",
                            {"code": "x", "state": state},
                            expected_state="attacker-supplied")
        csrf_ok = False
    except auth.AuthError as e:
        csrf_ok = "state" in str(e).lower()
    if not check("mismatched state cookie rejected (CSRF)", csrf_ok):
        failures += 1

    # 3) The handshake blob must be tamper-evident: flipping any character has
    #    to fail validation. (Confidentiality of the verifier is defense-in-depth
    #    via AES-GCM on prod; with the stdlib HMAC fallback the payload is
    #    readable but unforgeable, which is still safe — a leaked verifier is
    #    worthless without the code this same request exchanges.)
    forged = state[:-4] + ("AAAA" if not state.endswith("AAAA") else "BBBB")
    try:
        auth.complete_login("https://app.example", {"code": "x", "state": forged},
                            expected_state=forged)
        tamper_ok = False
    except auth.AuthError as e:
        tamper_ok = "state" in str(e).lower()
    if not check("tampered state blob rejected", tamper_ok):
        failures += 1

    print(f"\n{'PASS' if failures == 0 else 'FAIL'}: {4 - failures}/4 checks passed")
    if failures:
        print("=> OIDC login is fragile across restarts/workers; persist the "
              "pending handshake (server-side store keyed by state, or move "
              "verifier+nonce into an encrypted state blob).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
