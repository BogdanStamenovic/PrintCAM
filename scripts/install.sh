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
POWER_CONFIG_DIR="/etc/systemd/logind.conf.d"
POWER_CONFIG_FILE="$POWER_CONFIG_DIR/99-printcam-power.conf"
DCONF_PROFILE_FILE="/etc/dconf/profile/user"
DCONF_CONFIG_DIR="/etc/dconf/db/local.d"
DCONF_CONFIG_FILE="$DCONF_CONFIG_DIR/99-printcam-power"
POWER_STATE_FILE="$CONFIG_DIR/power-state.env"
DATA_DIR="/var/lib/printcam"
MOTION_DIR="$DATA_DIR/motion"
MOTION_STATE_FILE="$DATA_DIR/motion-enabled"
CAMERA_DEVICE="${PRINTCAM_CAMERA_DEVICE:-}"
PORT="${PRINTCAM_PORT:-8080}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo scripts/install.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> Installing OS packages"
apt-get update
apt-get install -y curl ca-certificates python3 python3-venv python3-pip v4l-utils ffmpeg rsync network-manager openssh-server dconf-cli

echo "==> Selecting camera"
if [[ -n "$CAMERA_DEVICE" ]]; then
  echo "Using camera from PRINTCAM_CAMERA_DEVICE: $CAMERA_DEVICE"
else
  CAMERA_DEVICES=()
  CAMERA_DEVICE_SEEN=" "
  while IFS= read -r line; do
    device="${line#"${line%%[![:space:]]*}"}"
    if [[ "$device" == /dev/video* && -e "$device" && "$CAMERA_DEVICE_SEEN" != *" $device "* ]]; then
      CAMERA_DEVICES+=("$device")
      CAMERA_DEVICE_SEEN+=" $device "
    fi
  done < <(v4l2-ctl --list-devices 2>/dev/null || true)

  if [[ "${#CAMERA_DEVICES[@]}" -eq 0 ]]; then
    while IFS= read -r device; do
      if [[ "$CAMERA_DEVICE_SEEN" != *" $device "* ]]; then
        CAMERA_DEVICES+=("$device")
        CAMERA_DEVICE_SEEN+=" $device "
      fi
    done < <(find /dev -maxdepth 1 -type c -name 'video*' 2>/dev/null | sort -V)
  fi

  if [[ "${#CAMERA_DEVICES[@]}" -gt 0 ]]; then
    echo "Detected cameras:"
    for index in "${!CAMERA_DEVICES[@]}"; do
      printf '  %d) %s\n' "$((index + 1))" "${CAMERA_DEVICES[$index]}"
    done

    while [[ -z "$CAMERA_DEVICE" ]]; do
      read -r -p "Camera to use [1] or device path: " CAMERA_CHOICE
      CAMERA_CHOICE="${CAMERA_CHOICE:-1}"
      if [[ "$CAMERA_CHOICE" =~ ^[0-9]+$ && "$CAMERA_CHOICE" -ge 1 && "$CAMERA_CHOICE" -le "${#CAMERA_DEVICES[@]}" ]]; then
        CAMERA_DEVICE="${CAMERA_DEVICES[$((CAMERA_CHOICE - 1))]}"
      elif [[ "$CAMERA_CHOICE" == /dev/video* ]]; then
        CAMERA_DEVICE="$CAMERA_CHOICE"
      else
        echo "Choose a number from the list, or enter a path like /dev/video2."
      fi
    done
  else
    read -r -p "No cameras were detected. Camera device path [/dev/video2]: " CAMERA_DEVICE
    CAMERA_DEVICE="${CAMERA_DEVICE:-/dev/video2}"
  fi
fi

echo "==> Enabling SSH"
if systemctl list-unit-files ssh.service 2>/dev/null | grep -q '^ssh\.service'; then
  systemctl enable --now ssh.service
elif systemctl list-unit-files sshd.service 2>/dev/null | grep -q '^sshd\.service'; then
  systemctl enable --now sshd.service
else
  systemctl enable --now ssh || systemctl enable --now sshd
fi

echo "==> Configuring display blanking and disabling sleep"
mkdir -p "$CONFIG_DIR"
mkdir -p "$POWER_CONFIG_DIR"
cat > "$POWER_CONFIG_FILE" <<EOF_POWER
[Login]
IdleAction=ignore
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF_POWER

POWER_TARGETS=(sleep.target suspend.target hibernate.target hybrid-sleep.target)
if [[ ! -f "$POWER_STATE_FILE" ]]; then
  {
    for target in "${POWER_TARGETS[@]}"; do
      var_name="PRINTCAM_PREVIOUS_${target//[-.]/_}"
      printf '%s=%q\n' "$var_name" "$(systemctl is-enabled "$target" 2>/dev/null || true)"
    done
  } > "$POWER_STATE_FILE"
  chmod 600 "$POWER_STATE_FILE"
  chown root:root "$POWER_STATE_FILE"
fi

systemctl mask "${POWER_TARGETS[@]}"
systemctl try-restart systemd-logind || true

mkdir -p "$(dirname "$DCONF_PROFILE_FILE")" "$DCONF_CONFIG_DIR"
if [[ ! -f "$DCONF_PROFILE_FILE" ]]; then
  cat > "$DCONF_PROFILE_FILE" <<EOF_DCONF_PROFILE
user-db:user
system-db:local
EOF_DCONF_PROFILE
elif ! grep -qx 'system-db:local' "$DCONF_PROFILE_FILE"; then
  printf '\nsystem-db:local\n' >> "$DCONF_PROFILE_FILE"
fi

cat > "$DCONF_CONFIG_FILE" <<EOF_DCONF
[org/cinnamon/desktop/session]
idle-delay=uint32 60

[org/cinnamon/settings-daemon/plugins/power]
sleep-display-ac=uint32 60
sleep-display-battery=uint32 60
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
button-lid-ac='nothing'
button-lid-battery='nothing'

[org/gnome/desktop/session]
idle-delay=uint32 60

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
sleep-inactive-ac-timeout=uint32 0
sleep-inactive-battery-timeout=uint32 0
EOF_DCONF
dconf update || true

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
PRINTCAM_MOTION_RECORDING_CODEC=MJPG
PRINTCAM_FFMPEG_BIN=ffmpeg
PRINTCAM_MOTION_OUTPUT_VIDEO_CODEC=libx264
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
systemctl enable printcam.service
systemctl start printcam.service
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
