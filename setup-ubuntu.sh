#!/usr/bin/env bash
# One-time server setup for Ubuntu 22.04/24.04
# Run as a regular user with sudo privileges (NOT as root)
set -euo pipefail

APP_DIR=/opt/asset_allocation
REPO_URL=https://github.com/MarkSuTW/asset_allocation.git
APP_USER=$(whoami)

echo "=== [1/6] Installing system packages ==="
sudo apt-get update -qq
sudo apt-get install -y python3.11 python3.11-venv python3-pip git curl

echo "=== [2/6] Installing Tailscale ==="
curl -fsSL https://tailscale.com/install.sh | sh
echo ""
echo "  *** Run this next to connect to your Tailscale network:"
echo "      sudo tailscale up"
echo "  *** Then note your Tailscale IP: tailscale ip -4"
echo ""

echo "=== [3/6] Cloning repository ==="
sudo mkdir -p "$APP_DIR"
sudo chown "$APP_USER":"$APP_USER" "$APP_DIR"
git clone "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"

echo "=== [4/6] Creating Python virtual environment ==="
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
deactivate

echo "=== [5/6] Setting up .env ==="
cp .env.example .env
# Allow all Tailscale devices (they're already secured by Tailscale ACLs)
sed -i 's|^#.*ALLOWED_ORIGINS.*|ALLOWED_ORIGINS=*|' .env
echo "" >> .env
echo "ALLOWED_ORIGINS=*" >> .env
echo ""
echo "  *** Edit /opt/asset_allocation/.env and fill in your API keys"
echo "  *** Then copy your wealth.db to $APP_DIR/wealth.db"
echo "  ***   OR run: python init_db.py   to create a fresh database"
echo ""

echo "=== [6/6] Installing and starting systemd service ==="
# Patch the service file with the actual username
sed "s/User=ubuntu/User=$APP_USER/g; s/Group=ubuntu/Group=$APP_USER/g" \
    "$APP_DIR/wealth-app.service" | sudo tee /etc/systemd/system/wealth-app.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable wealth-app

echo ""
echo "============================================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. sudo tailscale up"
echo "  2. Copy wealth.db to $APP_DIR/wealth.db"
echo "  3. Edit $APP_DIR/.env (API keys, etc.)"
echo "  4. sudo systemctl start wealth-app"
echo "  5. sudo systemctl status wealth-app"
echo ""
echo "  Once running, access from any Tailscale device:"
echo "    http://$(hostname -I | awk '{print $1}'):8001"
echo "  Or via Tailscale IP:"
echo "    http://<tailscale-ip>:8001"
echo "============================================================"
