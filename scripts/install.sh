#!/usr/bin/env bash
set -euo pipefail

APP_NAME="printcam"
APP_DIR="/opt/printcam"
CONFIG_DIR="/etc/printcam"
CONFIG_FILE="$CONFIG_DIR/printcam.env"
WIFI_CONFIG_FILE="$CONFIG_DIR/wifi.env"
SERVICE_FILE="/etc/systemd/system/printcam.service"
WIFI_SERVICE_FILE="/etc/systemd/system/printcam-wifi-reconnect.service"
WIFI_TIMER_FILE="/etc/systemd/system/printcam-wifi-reconnect.timer"
DATA_DIR="/var/lib/printcam"
MOTION_DIR="$DATA_DIR/motion"
MOTION_STATE_FILE="$DATA_DIR/motion-enabled"
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
apt-get install -y curl ca-certificates python3 python3-venv python3-pip v4l-utils rsync network-manager

echo "==> Configuring Wi-Fi"
systemctl enable --now NetworkManager
if [[ -z "${PRINTCAM_WIFI_SSID:-}" ]]; then
  CURRENT_WIFI="$(nmcli -t -f active,ssid dev wifi 2>/dev/null | awk -F: '$1 == "yes" {print $2; exit}' || true)"
  if [[ -n "$CURRENT_WIFI" ]]; then
    read -r -p "Wi-Fi network name [$CURRENT_WIFI]: " PRINTCAM_WIFI_SSID
    PRINTCAM_WIFI_SSID="${PRINTCAM_WIFI_SSID:-$CURRENT_WIFI}"
  else
    read -r -p "Wi-Fi network name (leave blank to skip): " PRINTCAM_WIFI_SSID
  fi
fi

if [[ -n "${PRINTCAM_WIFI_SSID:-}" && -z "${PRINTCAM_WIFI_PASSWORD+x}" ]]; then
  read -r -s -p "Wi-Fi password for $PRINTCAM_WIFI_SSID (leave blank for open network): " PRINTCAM_WIFI_PASSWORD
  echo
fi

mkdir -p "$CONFIG_DIR"
if [[ -n "${PRINTCAM_WIFI_SSID:-}" ]]; then
  {
    printf 'PRINTCAM_WIFI_SSID=%q\n' "$PRINTCAM_WIFI_SSID"
    printf 'PRINTCAM_WIFI_PASSWORD=%q\n' "${PRINTCAM_WIFI_PASSWORD:-}"
  } > "$WIFI_CONFIG_FILE"
  chmod 600 "$WIFI_CONFIG_FILE"
  chown root:root "$WIFI_CONFIG_FILE"

  nmcli radio wifi on || true
  if [[ -n "${PRINTCAM_WIFI_PASSWORD:-}" ]]; then
    nmcli dev wifi connect "$PRINTCAM_WIFI_SSID" password "$PRINTCAM_WIFI_PASSWORD" name "PrintCAM WiFi" || true
  else
    nmcli dev wifi connect "$PRINTCAM_WIFI_SSID" name "PrintCAM WiFi" || true
  fi
  nmcli connection modify "PrintCAM WiFi" connection.autoconnect yes || true
fi

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
mkdir -p "$MOTION_DIR"
if [[ ! -f "$MOTION_STATE_FILE" ]]; then
  printf '1\n' > "$MOTION_STATE_FILE"
fi
chown -R "$APP_NAME:$APP_NAME" "$DATA_DIR"

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
PRINTCAM_MOTION_ENABLED=1
PRINTCAM_MOTION_DIR=$MOTION_DIR
PRINTCAM_MOTION_STATE_FILE=$MOTION_STATE_FILE
PRINTCAM_MOTION_CONFIRM_SECONDS=5
PRINTCAM_MOTION_CHANGED_PERCENT=1.8
PRINTCAM_MOTION_PIXEL_DELTA=28
PRINTCAM_MOTION_MAX_EVENTS=200
PRINTCAM_MOTION_VIDEO_CODEC=mp4v
PRINTCAM_SECRET_KEY=$SECRET_KEY
PRINTCAM_PASSWORD_HASH=$PASSWORD_HASH
EOF_CONFIG
chmod 600 "$CONFIG_FILE"
chown root:root "$CONFIG_FILE"

echo "==> Installing systemd service"
cp "$APP_DIR/systemd/printcam.service" "$SERVICE_FILE"
cp "$APP_DIR/systemd/printcam-wifi-reconnect.service" "$WIFI_SERVICE_FILE"
cp "$APP_DIR/systemd/printcam-wifi-reconnect.timer" "$WIFI_TIMER_FILE"
chmod +x "$APP_DIR/scripts/"*.sh
chown -R "$APP_NAME:$APP_NAME" "$APP_DIR"
chown root:root "$APP_DIR/scripts/run_server.sh"
chown root:root "$APP_DIR/scripts/wifi-reconnect.sh"

systemctl daemon-reload
systemctl enable --now printcam
if [[ -n "${PRINTCAM_WIFI_SSID:-}" ]]; then
  systemctl enable --now printcam-wifi-reconnect.timer
fi

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
if [[ -n "${PRINTCAM_WIFI_SSID:-}" ]]; then
  echo "Wi-Fi reconnect logs: sudo journalctl -u printcam-wifi-reconnect -f"
fi
