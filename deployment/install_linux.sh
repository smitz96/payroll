#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/smartfill-attendance}"
APP_USER="${APP_USER:-smartfill}"
APP_PORT="${APP_PORT:-3000}"
REPO_URL="${REPO_URL:-git@github.com:smitz96/payroll.git}"
SERVICE_NAME="${SERVICE_NAME:-smartfill-attendance}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer with sudo or as root."
  exit 1
fi

apt-get update
apt-get install -y git python3 python3-venv python3-pip build-essential

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

if [ ! -d "$APP_DIR/.git" ]; then
  mkdir -p "$(dirname "$APP_DIR")"
  git clone "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" pull --ff-only
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
sudo -u "$APP_USER" mkdir -p "$APP_DIR/data" "$APP_DIR/uploads" "$APP_DIR/output" "$APP_DIR/tmp"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" "$APP_DIR/scripts/init_db.py"

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<SERVICE
[Unit]
Description=SMARTfill Attendance and Payroll Management
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment="PATH=$APP_DIR/venv/bin"
Environment="APP_PORT=$APP_PORT"
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 2 --bind 0.0.0.0:$APP_PORT app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "SMARTfill is installed and running on http://localhost:$APP_PORT"
