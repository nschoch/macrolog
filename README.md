# MacroLog

**Self-hosted AI calorie & macro tracker. Snap a photo of your meal, get an
estimate, correct it, and watch your targets adapt to what your body actually
does.** No subscription. Your data is one SQLite file plus a folder of photos.

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

1. **Weight → target.** A least-squares slope over ~3 weeks of weigh-ins
   estimates your *real* rate of change, immune to daily water swings. The app
   solves for your true maintenance directly (`maintenance = intake − observed
   energy imbalance`) rather than nudging last week's number, so it's a fixed
   point — repeated runs on unchanged data cannot drift or run away. When you're
   losing slower than your goal implies, the target tightens; faster, it loosens.

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
cp .env.example .env          # add a vision key (see below)
python3 server.py             # → http://localhost:8765
```

Open it on your phone by running with `--host 0.0.0.0` and visiting the LAN/
Netbird address it prints. In mobile Safari, **Add to Home Screen** for a
full-screen app with camera access.

### Getting a vision key

The photo→calories step needs a multimodal model. Pick one:

- **Qwen Token Plan** *(default first provider)* — set `QWENPLAN_API_KEY` (an
  `sk-…` key from [dashscope.console.aliyun.com](https://dashscope.console.aliyun.com))
  and, if your plan uses a custom host, `QWENPLAN_BASE_URL`. Uses `qwen3.8-max`,
  which reads images directly. Note: these Token Plans often expose *no*
  `qwen-vl-*` model — the multimodal text models (qwen3.8-max / -flash) work fine.
- **Groq** *(free, no card)* — grab a key at
  [console.groq.com/keys](https://console.groq.com/keys), set `GROQ_API_KEY`.
  Serves Llama 4 Scout (vision).
- **Google Gemini** — set `GEMINI_API_KEY`. ⚠️ As of late 2026, new `AQ.`-prefixed
  AI Studio keys are being rejected by the Developer API
  ([open issue](https://discuss.ai.google.dev/t/gemini-api-returns-401-access-token-type-unsupported-with-valid-aq-key/188360));
  you need a legacy `AIza…` key.
- **OpenAI** — set `OPENAI_API_KEY`, uses `gpt-4o-mini` (~$0.005/meal).
- **Ollama** *(fully offline)* — run `ollama pull llama3.2-vision` and set
  `OLLAMA_HOST`. No account, no network egress, weaker portion estimates.

Providers are tried top-to-bottom; the first with a working key wins. With **no
key at all** the app still runs on an offline placeholder estimator, so you can
evaluate the whole UX before wiring anything up.

---

## Deploying to a Proxmox LXC (or any Debian box)

One script, idempotent, safe to re-run after code updates:

```bash
sudo bash deploy/provision.sh
```

It creates a `macrolog` service user, copies the app to `/opt/macrolog`, writes a
hardened systemd unit, and starts it bound to `0.0.0.0:8765`. Reach it from your
phone over Netbird at `http://<lxc-tailnet-ip>:8765`.

Or with Docker:

```bash
docker build -t macrolog .
docker run -d --name macrolog -p 8765:8765 -v $(pwd)/data:/data macrolog
```

Keep the API off the public internet — it has no auth by design (single-user,
LAN/tailnet only). Put Netbird, Tailscale, or an authenticated reverse proxy in
front if it must be reachable remotely.

---

## How a day goes

1. Tap the green camera button → shoot your plate.
2. The model returns items, grams, calories, macros, and a confidence score.
3. Fix anything wrong (this trains the calibrator) → **Add to log**.
4. Ring, macro bars, and remaining calories update instantly.
5. Log your weight each morning. After ~a week the target starts adapting.

Everything is editable after the fact, and every edit feeds loop #2.

## Layout

```
server.py            stdlib HTTP server + JSON API (no framework)
app/
  database.py        SQLite schema + helpers (WAL, thread-safe writes)
  vision.py          provider layer: qwenplan / groq / gemini / openai / ollama / stub
  adaptive.py        weight→target loop + estimate calibration + food groups
static/              mobile-first PWA (vanilla JS, no build step)
tests/api_test.py    21 end-to-end API checks
deploy/              provision.sh, systemd unit, Dockerfile
data/                runtime: sqlite + photos (gitignored — this is your backup)
```

## Backups & privacy

Back up by copying `data/`. Photos stay on your machine; only the image bytes go
to whichever vision provider you configure. Nothing else leaves your network.

## Status

Working and tested end-to-end (API suite green, UI verified on a 420px mobile
viewport). Not yet a polished native app — PRs welcome, especially for barcode
scanning and Health Connect / Apple Health export.

## License

MIT — see [LICENSE](LICENSE).
