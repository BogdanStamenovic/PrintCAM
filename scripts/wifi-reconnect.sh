#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE="${PRINTCAM_WIFI_CONFIG:-/etc/printcam/wifi.env}"
CONNECTION_NAME="${PRINTCAM_WIFI_CONNECTION_NAME:-PrintCAM WiFi}"

if [[ ! -r "$CONFIG_FILE" ]]; then
  exit 0
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

SSID="${PRINTCAM_WIFI_SSID:-}"
PASSWORD="${PRINTCAM_WIFI_PASSWORD:-}"

if [[ -z "$SSID" ]]; then
  exit 0
fi

if ! command -v nmcli >/dev/null 2>&1; then
  echo "nmcli is not installed"
  exit 1
fi

nmcli radio wifi on >/dev/null 2>&1 || true

ACTIVE_SSID="$(nmcli -t -f active,ssid dev wifi 2>/dev/null | awk -F: '$1 == "yes" {print $2; exit}')"
if [[ "$ACTIVE_SSID" == "$SSID" ]]; then
  exit 0
fi

if nmcli -t -f name connection show | grep -Fxq "$CONNECTION_NAME"; then
  nmcli connection up "$CONNECTION_NAME"
  exit 0
fi

if [[ -n "$PASSWORD" ]]; then
  nmcli dev wifi connect "$SSID" password "$PASSWORD" name "$CONNECTION_NAME"
else
  nmcli dev wifi connect "$SSID" name "$CONNECTION_NAME"
fi

nmcli connection modify "$CONNECTION_NAME" connection.autoconnect yes
