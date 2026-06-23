#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo scripts/uninstall.sh"
  exit 1
fi

systemctl disable --now printcam 2>/dev/null || true
systemctl disable --now printcam-wifi-reconnect.timer 2>/dev/null || true

if [[ -f /etc/printcam/power-state.env ]]; then
  # shellcheck disable=SC1091
  source /etc/printcam/power-state.env || true
fi
for target in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
  var_name="PRINTCAM_PREVIOUS_${target//[-.]/_}"
  if [[ "${!var_name:-}" != "masked" ]]; then
    systemctl unmask "$target" 2>/dev/null || true
  fi
done

rm -f /etc/systemd/system/printcam.service
rm -f /etc/systemd/system/printcam-wifi-reconnect.service
rm -f /etc/systemd/system/printcam-wifi-reconnect.timer
rm -f /etc/sudoers.d/printcam-display
rm -f /etc/systemd/logind.conf.d/99-printcam-power.conf
rm -f /etc/dconf/db/local.d/99-printcam-power
dconf update 2>/dev/null || true
systemctl try-restart systemd-logind 2>/dev/null || true
systemctl daemon-reload
rm -rf /opt/printcam /etc/printcam /var/lib/printcam

if id -u printcam >/dev/null 2>&1; then
  userdel printcam
fi

echo "PrintCAM removed."
