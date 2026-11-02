# Security policy

MacroLog stores personal food and weight logs, AI provider keys, and an Apple
Health token in a local SQLite file. It is **vibe-coded and homelab-first**:
public internet exposure is not supported or audited. Please report security
issues privately all the same.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:

https://github.com/nschoch/macrolog/security/advisories/new

Do **not** open a public issue containing a provider key, Apple Health token,
database export, photo, or any personal log data. Include the commit/version,
deployment mode (OIDC or single-user), and a minimal reproduction. I'll
acknowledge as soon as I can and coordinate a fix.

## Supported versions

The `main` branch is the supported version.

## Deployment hardening

- Put the app behind HTTPS (Caddy or equivalent) before exposing it.
- Enable OIDC and allowlist emails if the host is reachable beyond your devices.
  Otherwise bind to localhost or restrict access with a firewall/VPN.
- Keep `.env` and `data/` out of version control — both are gitignored; neither
  has ever been committed to this repo.
- Rotate provider keys and the Apple Health token if they may have been exposed.
- Back up `data/` (SQLite plus photos) and test restoring it.
- Photo retention and `--maintain-photos` are documented in the README; they
  affect photos only, never meal totals.

## Scope

Useful reports include authentication/session flaws, provider-key disclosure,
path traversal in photo/static handling, XSS in user-supplied food names or
descriptions, and export/privacy leaks. Issues that require an already-compromised
server or social engineering are out of scope.
