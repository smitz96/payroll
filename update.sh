#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/smartfill-attendance}"
APP_PORT="${APP_PORT:-3000}"
REPO_URL="${REPO_URL:-https://github.com/smitz96/payroll.git}"
APP_USER="${APP_USER:-smartfill}"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo APP_DIR="$APP_DIR" APP_PORT="$APP_PORT" REPO_URL="$REPO_URL" APP_USER="$APP_USER" bash "$0"
fi

mkdir -p "$APP_DIR/output"
exec > >(tee -a "$APP_DIR/output/server-update.log") 2>&1

if [ ! -d "$APP_DIR/.git" ]; then
  echo "Application repository not found at $APP_DIR"
  exit 1
fi

echo
echo "===== SMARTfill update started at $(date '+%d-%m-%Y %H:%M:%S %Z') ====="
echo "Updating SMARTfill Payroll in $APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only

echo "Re-running installer"
REPO_URL="$REPO_URL" APP_PORT="$APP_PORT" APP_USER="$APP_USER" bash "$APP_DIR/install.sh"

echo "SMARTfill Payroll update completed."
