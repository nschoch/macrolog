#!/usr/bin/env bash
# Provision MacroLog on a Debian/Ubuntu host (tested target: Proxmox LXC).
# Idempotent: safe to re-run. Run as root or with sudo.
#
#   sudo bash deploy/provision.sh
#
# What it does:
#   1. Installs python3 (+ venv) and optionally Pillow via pip.
#   2. Creates a dedicated `macrolog` user rooted at /opt/macrolog.
#   3. Copies the app, writes a systemd unit, enables + starts it.
#   4. Opens nothing by itself — bind stays 0.0.0.0 so Netbird/LAN can reach it;
#      put a firewall in front if the host is internet-facing.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/macrolog}"
SERVICE_USER="${SERVICE_USER:-macrolog}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${MACROLOG_PORT:-8765}"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  warn "run with sudo"; exit 1
fi

log "Installing prerequisites"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates >/dev/null

log "Creating service user $SERVICE_USER"
id -u "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --home "$APP_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"

log "Syncing app from $SRC_DIR -> $APP_DIR"
mkdir -p "$APP_DIR/data/photos"
# rsync if present, otherwise tar-copy; exclude local dev junk and secrets.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.git' --exclude 'data' --exclude '.env' \
    --exclude '__pycache__' --exclude '*.pyc' \
    "$SRC_DIR/" "$APP_DIR/"
else
  ( cd "$SRC_DIR" && tar --exclude='./.git' --exclude='./data' \
      --exclude='./.env' --exclude='__pycache__' --exclude='*.pyc' -cf - . ) \
    | ( cd "$APP_DIR" && tar -xf - )
fi

log "Optional: installing Pillow for photo downscaling"
python3 -m venv "$APP_DIR/.venv" 2>/dev/null || true
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip 2>/dev/null || true
"$APP_DIR/.venv/bin/pip" install --quiet Pillow 2>/dev/null \
  && log "Pillow installed (venv)" \
  || warn "Pillow install skipped; app still works without it"

# cryptography is required only when OIDC login is enabled (RS256 id_token
# verification). Install best-effort; the app fails closed if it's absent.
if [[ -n "${OIDC_ISSUER:-}" ]] || grep -q '^OIDC_ISSUER=..*' "$APP_DIR/.env" 2>/dev/null; then
  log "OIDC appears configured — installing cryptography for token verification"
  "$APP_DIR/.venv/bin/pip" install --quiet cryptography 2>/dev/null \
    && log "cryptography installed" \
    || warn "cryptography install failed; OIDC login will refuse tokens until fixed"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 700 "$APP_DIR/data"

# Use the venv interpreter if it exists, else system python3.
PYBIN="$APP_DIR/.venv/bin/python3"
[[ -x "$PYBIN" ]] || PYBIN="$(command -v python3)"

log "Writing systemd unit"
sed -e "s#^ExecStart=.*#ExecStart=$PYBIN $APP_DIR/server.py#" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=$APP_DIR#" \
    "$SRC_DIR/deploy/macrolog.service" > /etc/systemd/system/macrolog.service

if [[ ! -f "$APP_DIR/.env" ]]; then
  log "Creating $APP_DIR/.env from template (ADD YOUR API KEY)"
  cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  warn "No provider key yet. Edit $APP_DIR/.env, set GROQ_API_KEY (free), then:"
  warn "  systemctl restart macrolog"
fi

log "Enabling + starting macrolog"
systemctl daemon-reload
systemctl enable --now macrolog
sleep 1
systemctl --no-pager --lines=0 status macrolog || true

HOSTIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

  MacroLog is running (bound to 127.0.0.1:$PORT, plain HTTP).

    Local test:  curl http://127.0.0.1:$PORT/api/state
    This host IP ($HOSTIP) is only relevant if you set MACROLOG_HOST=0.0.0.0
    so a remote Caddy can reach it. By default put Caddy in front for HTTPS:

      deploy/Caddyfile.snippet   -> reverse_proxy 127.0.0.1:$PORT
      then caddy reload

    iPhone reaches it at your Caddy hostname over HTTPS (camera works there).

  Vision key: $APP_DIR/.env should already define QWENPLAN_API_KEY / _BASE_URL.
    If empty, add one (Qwen/Groq/Gemini/OpenAI/Ollama), then:
      systemctl restart macrolog

  Logs:          journalctl -u macrolog -f
  Backup:        copy $APP_DIR/data  (one sqlite file + photos/)
EOF
