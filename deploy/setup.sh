#!/usr/bin/env bash
# Flop Scout — one-shot VPS setup for Ubuntu 22.04 / 24.04 / Debian 12.
#
#   sudo bash deploy/setup.sh
#
# Idempotent: safe to re-run after git pull. Creates the service user, installs
# system packages, builds the venv, installs the systemd units and the nginx
# site. It does NOT write your secrets — you fill those into
# /etc/flop-scout.env afterwards (the script tells you where).

set -euo pipefail

APP_DIR=/opt/flop-scout
APP_USER=flop
ENV_FILE=/etc/flop-scout.env
REPO_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die 'run with sudo'

say 'Installing system packages'
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip build-essential nginx curl rsync

say "Creating service user '${APP_USER}'"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

say "Syncing code into ${APP_DIR}"
mkdir -p "$APP_DIR"
# Copy everything except local secrets, the venv and the data dir.
# .wallet.json is excluded so --delete cannot wipe the key renew_key.py wrote
# on this host (the file is created from WALLET_MNEMONIC on first run).
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'data' \
  --exclude '.wallet.json' --exclude '*.db' --exclude '__pycache__' \
  --exclude 'shots' --exclude '.env*' \
  "$REPO_SRC"/ "$APP_DIR"/

mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say 'Building the virtualenv'
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say 'Installing the secrets file'
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 -o root -g root "$APP_DIR/deploy/flop-scout.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE — you must fill in FLOP_API_KEY and WALLET_MNEMONIC."
else
  chmod 600 "$ENV_FILE"
  echo "$ENV_FILE already exists, left untouched."
fi
# Wrapped by systemd; the app reads DATA_DIR so the DB survives redeploys
grep -q '^DATA_DIR=' "$ENV_FILE" || echo 'DATA_DIR=/opt/flop-scout/data' >> "$ENV_FILE"

say 'Installing systemd units'
install -m 644 "$APP_DIR/deploy/flop-scout.service"        /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/flop-scout-renew.service"  /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/flop-scout-renew.timer"    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now flop-scout.service

# The renew timer is installed but NOT enabled. A devnet allowance is
# once-per-wallet and expires in under 24h (we measured 15h 4m), and the published
# contract says an expired allowance is never renewed by re-enrolling. So on a
# spent wallet this timer can only log 403 devnet_enrollment_expired forever.
# Enable it only for a wallet that has never enrolled — after that it is one-shot.
#   systemctl enable --now flop-scout-renew.timer
#   systemctl start  flop-scout-renew.service   # run once, now
# On testnet, where renewal is expected to be possible, re-evaluate this.

say 'Installing the nginx site'
mkdir -p /var/www/certbot
# If the Let's Encrypt certificate does not exist yet on a brand-new host,
# install only the port-80 block first so nginx can start and serve ACME;
# once the certificate is present, install the full 80+443 config.
if [[ -f /etc/letsencrypt/live/flopscout.com/fullchain.pem ]]; then
  install -m 644 "$APP_DIR/deploy/nginx-flop-scout.conf" /etc/nginx/sites-available/flop-scout
else
  awk '/^server \{/{n++} n<2' "$APP_DIR/deploy/nginx-flop-scout.conf" > /etc/nginx/sites-available/flop-scout
  chmod 644 /etc/nginx/sites-available/flop-scout
  echo "Certificate not found yet — installed HTTP-only nginx block until certbot runs."
fi
ln -sf /etc/nginx/sites-available/flop-scout /etc/nginx/sites-enabled/flop-scout
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl reload nginx || systemctl restart nginx

cat <<EOF

────────────────────────────────────────────────────────────
Flop Scout is installed.

  Next steps
  1. Fill in your secrets:
       sudo nano ${ENV_FILE}
     set FLOP_API_KEY and WALLET_MNEMONIC.

  2. Restart the app:
       sudo systemctl restart flop-scout

  3. Point DNS at this server, then set server_name in
     /etc/nginx/sites-available/flop-scout and get a certificate:
       sudo certbot --nginx -d your-domain

  Useful commands
    sudo systemctl status flop-scout
    sudo journalctl -u flop-scout -f
    sudo systemctl list-timers flop-scout-renew.timer   # disabled by default
    sudo systemctl start flop-scout-renew.service       # enroll a fresh wallet once

  The app listens on 127.0.0.1:8787 behind nginx.
────────────────────────────────────────────────────────────
EOF
