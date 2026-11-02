# MacroLog

[![tests](https://github.com/nschoch/macrolog/actions/workflows/tests.yml/badge.svg)](https://github.com/nschoch/macrolog/actions/workflows/tests.yml)

**Self-hosted, homelab-first AI calorie & macro tracker. Snap a photo of your
meal, get an estimate, correct it, and watch your targets adapt to what your
body actually does.** No subscription. Your data is one SQLite file plus a
folder of photos.

> **Vibe-coded, homelab-first.** This project was built largely with AI
> assistance and has not been security-audited. It is intended for a trusted
> local network, VPN, or tailnet — **not for public internet exposure**. OIDC
> and HTTPS are available for multi-user and defense-in-depth setups, but they
> do not make public exposure supported. If you expose it anyway, you do so at
> your own risk. Treat it as a hobby project: review the code, keep backups,
> and expect rough edges.

It exists because the apps that do photo logging well (MacroFactor, Cal AI,
SnapCalorie) charge $5–12/month, and the open-source ones that exist only do
*logging* — none of them close the loop between your weight trend and your
calorie target, which is the actual reason MacroFactor is worth paying for.
MacroLog does.

---

## What makes it different from every other open-source option

| | Photo logging | Adaptive targets | Learns from your corrections | Self-hosted | BYO model |
|---|:---:|:---:|:---:|:---:|:---:|
| **MacroLog** | ✅ | ✅ | ✅ | ✅ | ✅ |
| caloriemate | ✅ | ❌ |  | ✅ | ✅ |
| Fud AI | ✅ | ❌ |  | ❌ (mobile app) | ✅ |
| lifeos / sate | ✅ | ❌ |  | ✅ | single provider |

The two feedback loops live in [`app/adaptive.py`](app/adaptive.py):

1. **Weight → target.** A robust (Theil-Sen) slope over up to 3 weeks of
   weigh-ins estimates your *real* rate of change without letting one water-weight
   spike dominate. The app solves for true maintenance directly
   (`maintenance = intake − observed energy imbalance`) rather than nudging last
   week's number, so repeated runs on unchanged data cannot drift or run away.
   It waits for **14 days, 8 weigh-ins and 10 logged-intake days**, then adjusts
   at most **once a week** and at most **150 kcal per move**; the target never
   goes below BMR, and it holds if intake logging looks incomplete. **locked**
   is a true freeze; **auto** resumes adaptation.

2. **Estimate → correction.** Every time you edit a logged meal, the predicted-
   vs-actual pair is stored. Per-food-group multipliers learned from that
   history (with James-Stein shrinkage so 2 edits don't override 200) are applied
   to future estimates. If the model consistently lowballs your pasta, it stops
   lowballing your pasta.

---

## Quick start

Requires Python 3.9+. No pip installs needed to run (Pillow is optional).

```bash
git clone https://github.com/nschoch/macrolog && cd macrolog
cp .env.example .env          # optional server-side key; you can also add one in Settings
python3 server.py             # → http://localhost:8765
```

Open it on your phone by running with `--host 0.0.0.0` and visiting the LAN/
Netbird address it prints. Without OIDC (below), everyone who can reach that
address is the same full-access user, so the server refuses a non-loopback bind
unless you either configure OIDC or explicitly accept a shared no-login instance
with `MACROLOG_ALLOW_NO_AUTH=1` on a network you trust. In mobile Safari, **Add to Home Screen** for a
full-screen app with camera access.

### Getting an AI provider

The photo→calories step needs a multimodal model. Provider settings are
**per user**: open **Settings → AI provider**, add a provider, paste your key,
then choose a model. Each account has its own chain, so a new family member does
not inherit (or spend) someone else's key. With no provider configured, the app
uses an offline placeholder so you can still evaluate the whole UX.

**Sharing a connection with everyone.** Edit one of your providers and tick
**Share this connection with everyone on this server**. It is appended after
each user's own providers and used as a fallback when their own connections are
missing or fail. Shared connections use the sharer's key and quota, are
read-only for everyone else, and only the sharer can edit or unshare them.
Recipients never receive the key. This is the intended way to let a family share
one paid key without putting it in `.env`.

The easiest path is **OpenRouter**:

1. Create a key at [openrouter.ai/keys](https://openrouter.ai/keys).
2. In **Settings → AI provider**, choose **OpenRouter**, paste the key, and press
   **Load**. The picker asks OpenRouter for its model list and keeps only models
   that accept image input (batch-only variants are hidden).
3. Pick a model and save. `qwen/qwen3.8-flash` is a cheap vision-capable choice;
   model IDs and prices are shown in the picker. You can also paste an exact ID
   if the list request fails.

You can also add a preset for Qwen Token Plan, Groq, OpenAI, Google Gemini,
Ollama, or any custom OpenAI-compatible endpoint. Server-wide environment keys
are still supported as fallbacks: a key in `.env` only backs a provider after
that provider is added in Settings.

- **OpenRouter** — `OPENROUTER_API_KEY`; optional `OPENROUTER_BASE_URL` and
  `OPENROUTER_MODEL`. Defaults to `https://openrouter.ai/api/v1`.
- **Qwen Token Plan** — `QWENPLAN_API_KEY` (an `sk-…` key from
  [dashscope.console.aliyun.com](https://dashscope.console.aliyun.com)) and, if
  your plan uses a custom host, `QWENPLAN_BASE_URL`. These Token Plans often
  expose *no* `qwen-vl-*` model — the multimodal text models (`qwen3.8-flash` /
  `qwen3.8-max`) read images fine.
- **Groq** *(free, no card)* — grab a key at
  [console.groq.com/keys](https://console.groq.com/keys), set `GROQ_API_KEY`.
  Serves Llama 4 Scout (vision).
- **Google Gemini** — set `GEMINI_API_KEY`. ⚠️ As of late 2026, new `AQ.`-prefixed
  AI Studio keys are being rejected by the Developer API
  ([open issue](https://discuss.ai.google.dev/t/gemini-api-returns-401-access-token-type-unsupported-with-valid-aq-key/188360));
  you need a legacy `AIza…` key.
- **OpenAI** — set `OPENAI_API_KEY`, uses `gpt-4o-mini` (~$0.005/meal).
- **DeepSeek direct** — add a **custom OpenAI-compatible** provider with base
  `https://api.deepseek.com`, model `deepseek-flash`, and your DeepSeek key. It
  accepts images. FoodLogger turns hidden reasoning off for DeepSeek
  automatically; otherwise a reasoning model can spend the whole token budget
  on `reasoning_content` and return empty JSON.
- **Ollama** *(fully offline)* — run `ollama pull llama3.2-vision` and set
  `OLLAMA_HOST`. No account, no network egress, weaker portion estimates.

Providers are tried top-to-bottom and the first one that answers wins. Keys are
stored per user in `data/macrolog.sqlite`, are never returned to the browser after
saving, and are excluded from JSON exports. Enter keys over HTTPS (Caddy in
front, or `--tls-cert` / `--tls-key`) so the browser-to-server hop is encrypted.

### Optional: sign-in with OIDC (Pocket ID / Authelia / Authentik)

By default MacroLog is single-user with no login. To gate it behind your identity
provider and give each family member their own private log, set in `.env`:

```bash
OIDC_ISSUER=https://auth.yourdomain.com      # provider root; discovery is auto
OIDC_CLIENT_ID=<from Pocket ID client>
OIDC_CLIENT_SECRET=<from Pocket ID client>  # optional for PKCE public clients
# pip install cryptography   # required for RS256 id_token verification
```

Then create an OIDC client in Pocket ID (**Administration → OIDC Clients**) with
redirect URI `https://your-macrolog-host/auth/callback` and scopes
`openid profile email`. Restart MacroLog — unauthenticated visits now redirect to
Pocket ID, and after passkey login each user's meals, weights, targets and
calibration history are stored separately (keyed by their OIDC `sub`).

Security notes: the flow is Authorization Code + PKCE with state + nonce; ID
tokens are signature-verified and fail **closed** if `cryptography` is missing.
Sessions are HMAC-signed cookies keyed by `MACROLOG_SESSION_SECRET` if set,
otherwise by a random key generated once into `data/session.key` (mode 0600).
Keep that file with your backups; deleting or changing it signs everyone out.
Set `OIDC_ALLOWED_EMAILS=a@x.com,b@y.com` to restrict who may enter (checked on
every request, so removing an address takes effect immediately); leave blank
to admit anyone your Pocket ID authenticates. Behind Caddy, `X-Forwarded-Proto`
is honored automatically; set `OIDC_REDIRECT_BASE` only if you see a
redirect_uri mismatch. Disable auth entirely by clearing `OIDC_ISSUER`.

---

## Deploying to a Proxmox LXC (or any Debian box)

One script, idempotent, safe to re-run after code updates:

```bash
sudo bash deploy/provision.sh
```

It creates a `macrolog` service user, copies the app to `/opt/macrolog`, writes a
hardened systemd unit, and starts it bound to **`127.0.0.1:8765`** over plain HTTP.

### Putting it behind Caddy (recommended — needed for iPhone camera)

iOS only allows camera access on HTTPS, so front the app with your existing
Caddy using [`deploy/Caddyfile.snippet`](deploy/Caddyfile.snippet):

```caddy
macrolog.example.com {
    request_body { max_size 25MB }
    reverse_proxy 127.0.0.1:8765
}
```

Then `caddy reload`. If Caddy runs on a different machine than the LXC, set
`MACROLOG_HOST=0.0.0.0` in `/opt/macrolog/.env` (it overrides the unit's
`Environment=` lines) and point `reverse_proxy` at the LXC's IP
(see Option B in the snippet). Full phone-trust walkthrough (ACME vs Caddy's
internal CA): [`deploy/IPHONE_TRUST.md`](deploy/IPHONE_TRUST.md).

The app uses only relative URLs, so it sits cleanly behind any reverse proxy.
Keep it off the public internet either way: enable OIDC for more than one
person, and restrict access with Netbird/Tailscale ACLs or a firewall.

### Or with Docker

```bash
docker build -t macrolog .
# With OIDC (recommended): pass your OIDC_* settings, e.g. --env-file .env
docker run -d --name macrolog -p 127.0.0.1:8765:8765 --env-file .env \
  -v $(pwd)/data:/data macrolog
# Single-user, no login, loopback only:
docker run -d --name macrolog -p 127.0.0.1:8765:8765 -e MACROLOG_ALLOW_NO_AUTH=1 \
  -v $(pwd)/data:/data macrolog
```

The image runs as a non-root user (uid 10001; `chown 10001 data` on the host if
needed), contains only `server.py`, `app/` and `static/`, and exposes an
unauthenticated `/healthz` for the health check.

Prefer no proxy? The server can also terminate TLS itself:
`--tls-cert tls/macrolog.crt --tls-key tls/macrolog.key`
(generate with `deploy/make-cert.sh <ip> <host> ...`).

---

## How a day goes

1. Tap the green add button → shoot your plate (or describe it).
2. The model returns items, grams, calories, macros, and a confidence score.
3. Fix anything wrong (this trains the calibrator) → **Add to log**.
4. Ring, macro bars, and remaining calories update instantly.
5. Log your weight each morning. After 14 days / 8 weigh-ins the target starts adapting.

Everything is editable after the fact, and every edit feeds loop #2.

### Apple Health

Direct HealthKit access is impossible from a PWA. For an iOS Shortcut that
writes daily food totals, weight, body fat, and exercise into Apple Health — and
automatically catches up missed days — use the token endpoints described in
[docs/APPLE_HEALTH.md](docs/APPLE_HEALTH.md). Generate the token in
**Settings → Apple Health**.

## Layout

```
server.py            stdlib HTTP server + JSON API (no framework)
app/
  database.py        SQLite schema + helpers (WAL, thread-safe writes)
  vision.py          provider layer: openrouter / custom / qwenplan / groq / gemini / openai / ollama / stub
  foods.py           folds verbose model labels to generic foods for quick logging
  adaptive.py        weight→target loop + estimate calibration + food groups
  auth.py            OIDC relying party + signed session cookies
  jev.py             optional Jev plausibility cross-check (off unless JEV_API_KEY)
static/              mobile-first PWA (vanilla JS, no build step)
tests/               unit, API, auth, and headless-browser suites (see CI)
deploy/              provision.sh, systemd unit, Caddy snippet, cert helper
Dockerfile           container image (code only; see .dockerignore)
data/                runtime: sqlite + photos (gitignored — this is your backup)
```

## Backups & privacy

Back up `data/`: the SQLite database, `photos/`, and `session.key`. The database
runs in WAL mode, so copy it with `sqlite3 data/macrolog.sqlite ".backup
backup.sqlite"` (or stop the service first) instead of copying the live file.
Treat backups as secrets: provider keys live in that same SQLite file (per
user). They are never sent back to the browser after saving and are excluded
from JSON exports. To restore, stop the service, put the files back, and
re-apply ownership (`chown -R macrolog:macrolog data && chmod 700 data`).

Photos stay on your machine; only the image bytes go to whichever provider you
configure. Operator `.env` keys are only ever sent to that provider's default
endpoint, never to a custom base URL. The one optional exception is the Jev
cross-check: if the operator sets `JEV_API_KEY`, the first photo of each photo
estimate is also sent to api.typesafe.ai for a plausibility check. Nothing else
leaves your network. See
[SECURITY.md](SECURITY.md) for private vulnerability reporting and deployment
hardening.

### Photo storage

Meal photos are files in `data/photos/`, not blobs in SQLite, so the database
stays small (tens of KB to a few MB) no matter how many pictures you take. New
photos are downscaled to a 1600px long edge on save — the model only reads
1024px, so rescan quality is unchanged. **Settings → Data → Keep meal photos**
sets a per-user retention window (default 12 months); expired photos are
removed by the six-hour maintenance sweep while the meal and its macros remain.
For a one-shot cleanup (expired temps, orphan files, existing oversized photos):

```bash
python3 server.py --maintain-photos --dry-run   # report only
python3 server.py --maintain-photos             # back up data/photos first
```

## Status

Working and tested end-to-end (API suite green, UI verified on a 420px mobile
viewport). Not yet a polished native app — PRs welcome, especially for barcode
scanning and native Health Connect integration.

## License

MIT — see [LICENSE](LICENSE).
