#!/usr/bin/env bash
# Attempt several methods to turn the display off while leaving the system running.
# Exits 0 on success, non-zero on failure.
set -euo pipefail

try_display_commands() {
  if command -v vcgencmd >/dev/null 2>&1; then
    vcgencmd display_power 0 && exit 0 || true
  fi

  if command -v xset >/dev/null 2>&1; then
    : "${DISPLAY:=:0}"
    export DISPLAY
    xset dpms force off && exit 0 || true
  fi

  if command -v xrandr >/dev/null 2>&1; then
    : "${DISPLAY:=:0}"
    export DISPLAY
    outputs=$(xrandr --query 2>/dev/null | awk '/ connected/{print $1}')
    if [[ -n "$outputs" ]]; then
      for out in $outputs; do
        xrandr --output "$out" --off || true
      done
      exit 0
    fi
  fi
}

try_as_desktop_user() {
  local uid="$1"
  local user="$2"
  local display="$3"
  local xauthority=""

  if [[ -f "/run/user/$uid/gdm/Xauthority" ]]; then
    xauthority="/run/user/$uid/gdm/Xauthority"
  elif [[ -f "/home/$user/.Xauthority" ]]; then
    xauthority="/home/$user/.Xauthority"
  fi

  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$user" -- env \
      DISPLAY="$display" \
      XAUTHORITY="$xauthority" \
      XDG_RUNTIME_DIR="/run/user/$uid" \
      "$0" --direct && exit 0 || true
  fi
}

if [[ "${1:-}" == "--direct" ]]; then
  try_display_commands
  exit 1
fi

# When called by the printcam service, DISPLAY=:0 alone is not enough for X11.
# Find the active local graphical login and run the display commands as that user
# so xset/xrandr receive a valid Xauthority context.
if command -v loginctl >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
  while read -r session; do
    [[ -n "$session" ]] || continue

    uid="$(loginctl show-session "$session" -p User --value 2>/dev/null || true)"
    user="$(loginctl show-session "$session" -p Name --value 2>/dev/null || true)"
    active="$(loginctl show-session "$session" -p Active --value 2>/dev/null || true)"
    remote="$(loginctl show-session "$session" -p Remote --value 2>/dev/null || true)"
    class="$(loginctl show-session "$session" -p Class --value 2>/dev/null || true)"
    type="$(loginctl show-session "$session" -p Type --value 2>/dev/null || true)"
    display="$(loginctl show-session "$session" -p Display --value 2>/dev/null || true)"

    [[ "$active" == "yes" ]] || continue
    [[ "$remote" == "no" ]] || continue
    [[ "$class" == "user" ]] || continue
    [[ "$type" == "x11" || "$type" == "wayland" ]] || continue
    [[ -n "$uid" && -n "$user" ]] || continue

    try_as_desktop_user "$uid" "$user" "${display:-:0}"
  done < <(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}')
fi

try_display_commands
exit 1
