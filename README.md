# PrintCAM

Temporary, scuffed, useful camera dashboard for a Linux Mint machine. PrintCAM streams `/dev/video2` through a password-protected web UI and shows basic machine health such as uptime, CPU, memory, disk, network throughput, Tailscale IP, and camera status.

It is intended to be hosted on your Tailnet with Tailscale, then kept alive by `systemd` after reboot.

## What You Get

- Password login page
- Live MJPEG camera stream from `/dev/video2`
- Motion detection that saves movement snapshots
- Motion event gallery with enable, disable, open, and delete controls
- Health dashboard with uptime, load, CPU, memory, disk, temperature, network speed, boot time, hostname, service time, Tailscale status, and camera status
- Linux Mint auto-install script
- Systemd service that starts on boot
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
2. Install Tailscale if it is not present.
3. Ask for the web password.
4. Copy the app to `/opt/printcam`.
5. Create `/etc/printcam/printcam.env`.
6. Create and start `printcam.service`.
7. Show the local and Tailscale URLs.

If Tailscale is not already logged in, the installer runs `tailscale up` and prints the login URL.

## Default Settings

| Setting | Default |
| --- | --- |
| Camera device | `/dev/video2` |
| Bind host | `0.0.0.0` |
| Port | `8080` |
| Install path | `/opt/printcam` |
| Config file | `/etc/printcam/printcam.env` |
| Motion event path | `/var/lib/printcam/motion` |
| Service | `printcam.service` |

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

Motion snapshots are saved when the camera image changes enough between frames. They show up in the Saved movement section on the dashboard.

Useful settings in `/etc/printcam/printcam.env`:

```text
PRINTCAM_MOTION_ENABLED=1
PRINTCAM_MOTION_DIR=/var/lib/printcam/motion
PRINTCAM_MOTION_STATE_FILE=/var/lib/printcam/motion-enabled
PRINTCAM_MOTION_MIN_INTERVAL=8
PRINTCAM_MOTION_CHANGED_PERCENT=1.8
PRINTCAM_MOTION_PIXEL_DELTA=28
PRINTCAM_MOTION_MAX_EVENTS=200
```

Lower `PRINTCAM_MOTION_CHANGED_PERCENT` if it misses small printer movement. Raise it if lighting flicker creates too many saves. `PRINTCAM_MOTION_MIN_INTERVAL` prevents one long move from saving hundreds of images.

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
scripts/set-password.sh Password reset helper
scripts/uninstall.sh   Remove installed service/app/config
templates/             HTML templates
static/                CSS and dashboard JavaScript
systemd/               Service template
```
