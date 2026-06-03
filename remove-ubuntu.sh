#!/usr/bin/env bash
# Remove deployed app/service from Ubuntu.
# Usage:
#   ./remove-ubuntu.sh
#   ./remove-ubuntu.sh --purge-app
#   ./remove-ubuntu.sh --purge-app --purge-data --yes
set -euo pipefail

APP_DIR=/opt/asset_allocation
SERVICE_NAME=wealth-app
UNIT_FILE=/etc/systemd/system/${SERVICE_NAME}.service
PURGE_APP=false
PURGE_DATA=false
ASSUME_YES=false

for arg in "$@"; do
  case "$arg" in
    --purge-app)
      PURGE_APP=true
      ;;
    --purge-data)
      PURGE_DATA=true
      ;;
    --yes|-y)
      ASSUME_YES=true
      ;;
    -h|--help)
      sed -n '1,20p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg"
      echo "Use --help for usage."
      exit 1
      ;;
  esac
done

confirm() {
  local msg="$1"
  if [ "$ASSUME_YES" = true ]; then
    return 0
  fi
  read -r -p "$msg [y/N] " ans
  case "$ans" in
    y|Y|yes|YES)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

echo "=== Removing service: ${SERVICE_NAME} ==="
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}\.service"; then
  sudo systemctl stop "${SERVICE_NAME}" || true
  sudo systemctl disable "${SERVICE_NAME}" || true
fi

if [ -f "$UNIT_FILE" ]; then
  sudo rm -f "$UNIT_FILE"
fi

sudo systemctl daemon-reload
sudo systemctl reset-failed || true

echo "Service removed."

if [ "$PURGE_APP" = true ]; then
  if [ -d "$APP_DIR" ]; then
    if [ "$PURGE_DATA" = true ]; then
      if confirm "Delete entire ${APP_DIR} (including wealth.db/backups/.env)?"; then
        sudo rm -rf "$APP_DIR"
        echo "Deleted: ${APP_DIR}"
      else
        echo "Skip deleting ${APP_DIR}."
      fi
    else
      if confirm "Delete app code but keep wealth.db, backups, .env?"; then
        sudo find "$APP_DIR" -mindepth 1 \
          ! -name 'wealth.db' \
          ! -name '.env' \
          ! -name 'backups' \
          ! -path "$APP_DIR/backups/*" \
          -exec rm -rf {} +
        echo "App code removed, data kept."
      else
        echo "Skip app directory cleanup."
      fi
    fi
  else
    echo "App directory not found: ${APP_DIR}"
  fi
fi

echo "=== Done ==="
if [ "$PURGE_APP" = false ]; then
  echo "App files are kept at ${APP_DIR}."
  echo "Use --purge-app to remove app files."
fi
