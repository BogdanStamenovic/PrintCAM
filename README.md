# PrintCAM

Temporary, scuffed, useful camera dashboard for a Linux Mint machine. PrintCAM streams the camera selected during install through a password-protected web UI and shows basic machine health such as uptime, CPU, memory, disk, network throughput, Tailscale IP, and camera status.

It is intended to be hosted on your Tailnet with Tailscale, then kept alive by `systemd` after reboot.

## What You Get

- Password login page
- Live MJPEG camera stream from the selected camera device
- Motion detection that saves confirmed movement videos
- Motion event gallery with enable, disable, open, and delete controls
- Health dashboard with uptime, load, CPU, memory, disk, temperature, network speed, boot time, hostname, service time, Tailscale status, and camera status
- Linux Mint auto-install script
- Systemd service that starts on boot
- Wi-Fi setup with automatic reconnect checks
- SSH server enabled for remote access
- Laptop display blanks after 1 minute while the computer keeps running
- Password reset helper
- Tailscale install/login helper

## Quick Install On Linux Mint

From this repo:

```bash
chmod +x scripts/install.sh
sudo scripts/install.sh
```

The installer will:

1. Install OS packages.
2. Detect installed cameras and ask which camera to use.
3. Enable SSH.
4. Set the laptop display to blank after 1 minute and disable system sleep.
5. Ask for Wi-Fi name and password.
6. Connect the laptop to that Wi-Fi network.
7. Install Tailscale if it is not present.
8. Ask for the web password.
9. Copy the app to `/opt/printcam`.
10. Create `/etc/printcam/printcam.env`.
11. Enable `printcam.service` at system startup and start it now.
12. Create a Wi-Fi reconnect timer.
13. Show the local and Tailscale URLs.

If Tailscale is not already logged in, the installer runs `tailscale up` and prints the login URL.

## Default Settings

| Setting | Default |
| --- | --- |
| Camera device | Chosen during install |
| Bind host | `0.0.0.0` |
| Port | `8080` |
| Install path | `/opt/printcam` |
| Config file | `/etc/printcam/printcam.env` |
| Wi-Fi config file | `/etc/printcam/wifi.env` |
| Motion event path | `/var/lib/printcam/motion` |
| Service | `printcam.service` |
| Wi-Fi reconnect timer | `printcam-wifi-reconnect.timer` |
| Display blanking | 1 minute |
| System sleep | Disabled |

Change these by editing `/etc/printcam/printcam.env`, then restart:

```bash
sudo systemctl restart printcam
```

## Open The Site

After install, open one of:

```text
http://<tailscale-ip>:8080
http://<hostname>.tailnet-name.ts.net:8080
http://<lan-ip>:8080
```

To find the Tailscale IP:

```bash
tailscale ip -4
```

## Password Reset

```bash
sudo scripts/set-password.sh
sudo systemctl restart printcam
```

If the repo is not available anymore, the installed copy also has the helper:

```bash
sudo /opt/printcam/scripts/set-password.sh
sudo systemctl restart printcam
```

## Service Commands

```bash
sudo systemctl status printcam
sudo systemctl restart printcam
sudo journalctl -u printcam -f
```

## Wi-Fi Reconnect

The installer asks for the Wi-Fi network name and password, connects with NetworkManager, and stores the credentials in:

```text
/etc/printcam/wifi.env
```

That file is root-owned and only readable by root. A systemd timer runs every minute and tries to reconnect if the active Wi-Fi network is not the configured one.

Useful commands:

```bash
sudo systemctl status printcam-wifi-reconnect.timer
sudo systemctl start printcam-wifi-reconnect.service
sudo journalctl -u printcam-wifi-reconnect -f
```

To change Wi-Fi later, edit `/etc/printcam/wifi.env`, then run:

```bash
sudo systemctl start printcam-wifi-reconnect.service
```

## Camera Checks

List video devices:

```bash
v4l2-ctl --list-devices
```

Check `/dev/video2` exists:

```bash
ls -l /dev/video2
```

If the camera is at a different path, edit:

```bash
sudo nano /etc/printcam/printcam.env
```

Set:

```text
PRINTCAM_CAMERA_DEVICE=/dev/videoX
```

Then restart:

```bash
sudo systemctl restart printcam
```

## Motion Detection

Motion videos start recording when the camera image changes enough between frames. If there is no second change within 5 seconds, the temporary clip is discarded. If there is another change, one continuous clip is saved and recording continues until there have been no more changes for 5 seconds. Saved clips are displayed as recording start time - end time in the dashboard, with normal video controls for playback and scrubbing.

Useful settings in `/etc/printcam/printcam.env`:

```text
PRINTCAM_MOTION_ENABLED=1
PRINTCAM_MOTION_DIR=/var/lib/printcam/motion
PRINTCAM_MOTION_STATE_FILE=/var/lib/printcam/motion-enabled
PRINTCAM_MOTION_CONFIRM_SECONDS=5
PRINTCAM_MOTION_CHANGED_PERCENT=1.8
PRINTCAM_MOTION_PIXEL_DELTA=28
PRINTCAM_MOTION_MAX_EVENTS=200
PRINTCAM_MOTION_VIDEO_CODEC=mp4v
```

Lower `PRINTCAM_MOTION_CHANGED_PERCENT` if it misses small printer movement. Raise it if lighting flicker creates too many saves. `PRINTCAM_MOTION_CONFIRM_SECONDS` controls both the confirmation window and how long recording continues after the most recent change.

You can turn motion detection on and off from the dashboard. That toggle is saved in `PRINTCAM_MOTION_STATE_FILE`, so it survives app restarts.

## Uninstall

```bash
sudo scripts/uninstall.sh
```

This removes the service, `/opt/printcam`, `/etc/printcam`, and `/var/lib/printcam`. It does not uninstall Tailscale or system packages.

## Security Notes

This is intentionally simple. It is good enough for a temporary Tailnet-only tool, but it is not hardened for public internet exposure.

- Keep it behind Tailscale.
- Use a real password.
- Do not port-forward it to the open internet.
- Rotate the password if you share Tailnet access.

## Repo Layout

```text
app.py                  Flask app and camera streamer
requirements.txt       Python dependencies
scripts/install.sh     Linux Mint installer
scripts/run_server.sh  Systemd entrypoint
scripts/wifi-reconnect.sh Wi-Fi reconnect helper
scripts/set-password.sh Password reset helper
scripts/uninstall.sh   Remove installed service/app/config
templates/             HTML templates
static/                CSS and dashboard JavaScript
systemd/               Service template
```
