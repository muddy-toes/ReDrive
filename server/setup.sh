#!/usr/bin/env bash
# setup.sh — provision a fresh Ubuntu 22.04 droplet for ReDrive
# Run as root: bash setup.sh
set -euo pipefail

DOMAIN="redrive.estimstation.com"
APP_DIR="/opt/redrive"
APP_USER="redrive"

echo "=== ReDrive server setup ==="

# --- System packages ---
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx certbot python3-certbot-nginx

# --- App user ---
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

# --- Clone / update repo ---
if [ ! -d "$APP_DIR/.git" ]; then
    git clone https://github.com/blucrew/ReDrive.git "$APP_DIR"
else
    git -C "$APP_DIR" pull --ff-only
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --- Python venv ---
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q aiohttp

# --- Touch asset directories ---
sudo -u "$APP_USER" mkdir -p "$APP_DIR/touch_assets/anatomy" "$APP_DIR/touch_assets/tools"

# --- nginx ---
cp "$APP_DIR/server/nginx.conf" /etc/nginx/sites-available/redrive
ln -sf /etc/nginx/sites-available/redrive /etc/nginx/sites-enabled/redrive
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx

# --- TLS via Let's Encrypt ---
echo ""
echo ">>> Obtaining TLS certificate for $DOMAIN"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m admin@$DOMAIN
systemctl reload nginx

# --- systemd service ---
cp "$APP_DIR/server/redrive.service" /etc/systemd/system/redrive.service
systemctl daemon-reload
systemctl enable --now redrive

echo ""
echo "=== Setup complete ==="
echo "Service status:"
systemctl status redrive --no-pager
echo ""
echo "ReDrive is live at https://$DOMAIN"
