#!/usr/bin/env bash
# Run this on the Ubuntu server to deploy latest code from GitHub.
# Usage: ./deploy.sh          (deploys main branch)
#        ./deploy.sh feat/xyz  (deploys a specific branch)
set -euo pipefail

APP_DIR=/opt/asset_allocation
BRANCH=${1:-main}

echo "=== Deploying branch: $BRANCH ==="
cd "$APP_DIR"

# Pull latest code
git fetch origin
git checkout "$BRANCH"
git pull origin "$BRANCH"

# Update dependencies if requirements changed
source .venv/bin/activate
pip install -r requirements.txt --quiet
deactivate

# Restart service (non-interactive friendly)
if [ "$(id -u)" -eq 0 ]; then
	run_systemctl() { systemctl "$@"; }
	run_systemctl restart wealth-app
else
	run_systemctl() { sudo -n systemctl "$@"; }
	if ! sudo -n true 2>/dev/null; then
		echo "ERROR: sudo requires password. Please configure NOPASSWD for systemctl/systemd-run before using web redeploy."
		exit 1
	fi
	run_systemctl restart wealth-app
fi

# Wait a moment and show status
sleep 2
echo ""
run_systemctl status wealth-app --no-pager -l

echo ""
echo "=== Deployed successfully at $(date) ==="
