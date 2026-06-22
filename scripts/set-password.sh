#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/printcam"
CONFIG_FILE="/etc/printcam/printcam.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo scripts/set-password.sh"
  exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "Missing installed Python environment at $APP_DIR/.venv"
  exit 1
fi

read -r -s -p "New PrintCAM web password: " PASSWORD
echo
read -r -s -p "Confirm password: " PASSWORD_CONFIRM
echo

if [[ "$PASSWORD" != "$PASSWORD_CONFIRM" ]]; then
  echo "Passwords did not match"
  exit 1
fi

PASSWORD_HASH="$(PRINTCAM_PASSWORD_INPUT="$PASSWORD" "$APP_DIR/.venv/bin/python" -c 'import os; from werkzeug.security import generate_password_hash; print(generate_password_hash(os.environ["PRINTCAM_PASSWORD_INPUT"]))')"

if grep -q '^PRINTCAM_PASSWORD_HASH=' "$CONFIG_FILE"; then
  sed -i "s|^PRINTCAM_PASSWORD_HASH=.*|PRINTCAM_PASSWORD_HASH=$PASSWORD_HASH|" "$CONFIG_FILE"
else
  printf '\nPRINTCAM_PASSWORD_HASH=%s\n' "$PASSWORD_HASH" >> "$CONFIG_FILE"
fi

chmod 600 "$CONFIG_FILE"
echo "Password updated. Restart with: sudo systemctl restart printcam"
