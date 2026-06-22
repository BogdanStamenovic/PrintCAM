#!/usr/bin/env bash
set -euo pipefail

HOST="${PRINTCAM_HOST:-0.0.0.0}"
PORT="${PRINTCAM_PORT:-8080}"

exec /opt/printcam/.venv/bin/waitress-serve \
  --host "$HOST" \
  --port "$PORT" \
  app:app
