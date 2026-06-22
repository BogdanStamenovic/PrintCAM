#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo scripts/uninstall.sh"
  exit 1
fi

systemctl disable --now printcam 2>/dev/null || true
rm -f /etc/systemd/system/printcam.service
systemctl daemon-reload
rm -rf /opt/printcam /etc/printcam /var/lib/printcam

if id -u printcam >/dev/null 2>&1; then
  userdel printcam
fi

echo "PrintCAM removed."
