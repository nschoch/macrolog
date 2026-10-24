#!/usr/bin/env bash
# Generate a self-signed TLS cert for MacroLog with the right SANs so iPhone
# Safari will accept it once trusted. Run on the LXC (or anywhere) as:
#
#   bash deploy/make-cert.sh 192.168.1.50 macrolog.local 100.64.0.12
#
# Args = every address your phone might use: LAN IP, mDNS name, Netbird IP.
# Outputs ./tls/macrolog.crt + ./tls/macrolog.key (chmod'd for the service).
set -euo pipefail

OUT_DIR="${TLS_DIR:-./tls}"
DAYS="${CERT_DAYS:-825}"          # iOS caps trusted certs; ~2yr is safe-ish
HOSTNAMES=("$@")

if [[ ${#HOSTNAMES[@]} -eq 0 ]]; then
  echo "usage: $0 <ip-or-host> [more ips/hosts...]" >&2
  echo "e.g.:  $0 192.168.1.50 macrolog.local 100.64.0.12" >&2
  exit 1
fi

command -v openssl >/dev/null || { echo "openssl not found"; exit 1; }
mkdir -p "$OUT_DIR"

# Build the subjectAltName list, classifying each arg as IP or DNS.
san=""
for h in "${HOSTNAMES[@]}"; do
  if [[ "$h" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    san+="IP:${h}, "
  else
    san+="DNS:${h}, "
  fi
done
san+="DNS:localhost, IP:127.0.0.1"

echo "==> generating key + self-signed cert"
echo "SANs: $san"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$OUT_DIR/macrolog.key" \
  -out    "$OUT_DIR/macrolog.crt" \
  -days "$DAYS" \
  -subj "/CN=${HOSTNAMES[0]}" \
  -addext "subjectAltName=${san}" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
  -addext "extendedKeyUsage=serverAuth"

chmod 600 "$OUT_DIR/macrolog.key"
chmod 644 "$OUT_DIR/macrolog.crt"

cat <<EOF

Done. Files:
  $OUT_DIR/macrolog.crt   (share/install this on the iPhone to trust it)
  $OUT_DIR/macrolog.key   (keep private)

Point the server at them:
  MACROLOG_TLS_CERT=$OUT_DIR/macrolog.crt MACROLOG_TLS_KEY=$***

To make iPhone stop warning / allow camera:
  1. AirDrop or host macrolog.crt and open it on the phone -> Settings installs profile
  2. Settings > General > About > Certificate Trust Settings -> ENABLE the profile
  See deploy/IPHONE_TRUST.md for the full walkthrough.
EOF
