#!/usr/bin/env bash
set -euo pipefail

APP_NAME="printcam"
APP_DIR="/opt/printcam"
CONFIG_DIR="/etc/printcam"
CONFIG_FILE="$CONFIG_DIR/printcam.env"
SERVICE_FILE="/etc/systemd/system/printcam.service"
CAMERA_DEVICE="${PRINTCAM_CAMERA_DEVICE:-/dev/video2}"
PORT="${PRINTCAM_PORT:-8080}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo scripts/install.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> Installing OS packages"
apt-get update
apt-get install -y curl ca-certificates python3 python3-venv python3-pip v4l-utils rsync

if ! command -v tailscale >/dev/null 2>&1; then
  echo "==> Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo "==> Enabling Tailscale"
systemctl enable --now tailscaled
if ! tailscale status >/dev/null 2>&1; then
  echo "==> Starting Tailscale login"
  tailscale up
fi

echo "==> Creating service user"
if ! id -u "$APP_NAME" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin --groups video "$APP_NAME"
fi
usermod -aG video "$APP_NAME" || true

echo "==> Copying app to $APP_DIR"
mkdir -p "$APP_DIR"
rsync_available=0
if command -v rsync >/dev/null 2>&1; then
  rsync_available=1
fi

if [[ "$rsync_available" -eq 1 ]]; then
  rsync -a --delete --exclude '.git' --exclude '.venv' "$REPO_DIR/" "$APP_DIR/"
else
  find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name '.venv' -exec rm -rf {} +
  cp -a "$REPO_DIR/." "$APP_DIR/"
  rm -rf "$APP_DIR/.git" "$APP_DIR/.venv"
fi

echo "==> Creating Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Configuring password"
mkdir -p "$CONFIG_DIR"
if [[ -z "${PRINTCAM_PASSWORD:-}" ]]; then
  read -r -s -p "PrintCAM web password: " PRINTCAM_PASSWORD
  echo
  read -r -s -p "Confirm password: " PRINTCAM_PASSWORD_CONFIRM
  echo
  if [[ "$PRINTCAM_PASSWORD" != "$PRINTCAM_PASSWORD_CONFIRM" ]]; then
    echo "Passwords did not match"
    exit 1
  fi
fi

PASSWORD_HASH="$(PRINTCAM_PASSWORD_INPUT="$PRINTCAM_PASSWORD" "$APP_DIR/.venv/bin/python" -c 'import os; from werkzeug.security import generate_password_hash; print(generate_password_hash(os.environ["PRINTCAM_PASSWORD_INPUT"]))')"
SECRET_KEY="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"

cat > "$CONFIG_FILE" <<EOF_CONFIG
PRINTCAM_HOST=0.0.0.0
PRINTCAM_PORT=$PORT
PRINTCAM_CAMERA_DEVICE=$CAMERA_DEVICE
PRINTCAM_FRAME_WIDTH=1280
PRINTCAM_FRAME_HEIGHT=720
PRINTCAM_FRAME_FPS=15
PRINTCAM_SECRET_KEY=$SECRET_KEY
PRINTCAM_PASSWORD_HASH=$PASSWORD_HASH
EOF_CONFIG
chmod 600 "$CONFIG_FILE"
chown root:root "$CONFIG_FILE"

echo "==> Installing systemd service"
cp "$APP_DIR/systemd/printcam.service" "$SERVICE_FILE"
chmod +x "$APP_DIR/scripts/"*.sh
chown -R "$APP_NAME:$APP_NAME" "$APP_DIR"
chown root:root "$APP_DIR/scripts/run_server.sh"

systemctl daemon-reload
systemctl enable --now printcam

TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
HOSTNAME="$(hostname)"

echo
echo "PrintCAM is installed."
echo "Local URL:     http://$HOSTNAME.local:$PORT"
if [[ -n "$TAILSCALE_IP" ]]; then
  echo "Tailscale URL: http://$TAILSCALE_IP:$PORT"
fi
echo
echo "Check logs with: sudo journalctl -u printcam -f"
