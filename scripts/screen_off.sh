#!/usr/bin/env bash
# Attempt several methods to turn the display off while leaving the system running.
# Exits 0 on success, non-zero on failure.
set -euo pipefail

# Try Raspberry Pi helper
if command -v vcgencmd >/dev/null 2>&1; then
  vcgencmd display_power 0 && exit 0 || true
fi

# Try X11 DPMS via xset
if command -v xset >/dev/null 2>&1; then
  : "${DISPLAY:=:0}"
  export DISPLAY
  xset dpms force off && exit 0 || true
fi

# Try turning off each connected output via xrandr
if command -v xrandr >/dev/null 2>&1; then
  : "${DISPLAY:=:0}"
  export DISPLAY
  outputs=$(xrandr --query | awk '/ connected/{print $1}')
  for out in $outputs; do
    xrandr --output "$out" --off || true
  done
  exit 0
fi

# Nothing worked
exit 1
