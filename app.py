import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import psutil
from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash


CAMERA_DEVICE = os.environ.get("PRINTCAM_CAMERA_DEVICE", "/dev/video2")
FRAME_WIDTH = int(os.environ.get("PRINTCAM_FRAME_WIDTH", "1280"))
FRAME_HEIGHT = int(os.environ.get("PRINTCAM_FRAME_HEIGHT", "720"))
FRAME_FPS = int(os.environ.get("PRINTCAM_FRAME_FPS", "15"))
PASSWORD_HASH = os.environ.get("PRINTCAM_PASSWORD_HASH", "")
SERVICE_STARTED_AT = time.time()
MOTION_ENABLED = os.environ.get("PRINTCAM_MOTION_ENABLED", "1") == "1"
MOTION_DIR = Path(os.environ.get("PRINTCAM_MOTION_DIR", "/var/lib/printcam/motion"))
MOTION_STATE_FILE = Path(os.environ.get("PRINTCAM_MOTION_STATE_FILE", "/var/lib/printcam/motion-enabled"))
MOTION_MIN_INTERVAL = float(os.environ.get("PRINTCAM_MOTION_MIN_INTERVAL", "8"))
MOTION_CHANGED_PERCENT = float(os.environ.get("PRINTCAM_MOTION_CHANGED_PERCENT", "1.8"))
MOTION_PIXEL_DELTA = int(os.environ.get("PRINTCAM_MOTION_PIXEL_DELTA", "28"))
MOTION_MAX_EVENTS = int(os.environ.get("PRINTCAM_MOTION_MAX_EVENTS", "200"))
MOTION_FILENAME_RE = re.compile(r"^motion-\d{8}-\d{6}-\d{3}-score-[0-9.]+\.jpg$")

app = Flask(__name__)
app.secret_key = os.environ.get("PRINTCAM_SECRET_KEY", "dev-only-change-me")

stats_lock = threading.Lock()
motion_lock = threading.Lock()
motion_enabled_state = MOTION_ENABLED
net_sample = {
    "time": time.time(),
    "bytes_sent": psutil.net_io_counters().bytes_sent,
    "bytes_recv": psutil.net_io_counters().bytes_recv,
    "up_bps": 0.0,
    "down_bps": 0.0,
}


class CameraStream:
    def __init__(self, device):
        self.device = device
        self.lock = threading.Lock()
        self.capture = None
        self.last_frame = None
        self.last_error = None
        self.last_frame_at = None
        self.last_motion_at = None
        self.last_motion_score = 0.0
        self.previous_motion_frame = None

    def open(self):
        with self.lock:
            if self.capture and self.capture.isOpened():
                return True

            cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, FRAME_FPS)

            if not cap.isOpened():
                self.last_error = f"Could not open {self.device}"
                self.capture = None
                return False

            self.capture = cap
            self.last_error = None
            return True

    def read_jpeg(self):
        if not self.open():
            return None

        with self.lock:
            ok, frame = self.capture.read()
            if not ok:
                self.last_error = f"Could not read frame from {self.device}"
                self._release_locked()
                return None

            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            if not ok:
                self.last_error = "Could not encode camera frame"
                return None

            self.last_frame = encoded.tobytes()
            self.last_frame_at = time.time()
            self.last_error = None
            self.detect_motion(frame, self.last_frame)
            return self.last_frame

    def detect_motion(self, frame, jpeg_bytes):
        if not motion_enabled():
            self.previous_motion_frame = None
            return

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 180))
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.previous_motion_frame is None:
            self.previous_motion_frame = gray
            return

        diff = cv2.absdiff(self.previous_motion_frame, gray)
        changed = cv2.threshold(diff, MOTION_PIXEL_DELTA, 255, cv2.THRESH_BINARY)[1]
        score = (cv2.countNonZero(changed) / changed.size) * 100
        self.previous_motion_frame = gray
        self.last_motion_score = score

        now = time.time()
        if score < MOTION_CHANGED_PERCENT:
            return
        if self.last_motion_at and now - self.last_motion_at < MOTION_MIN_INTERVAL:
            return

        save_motion_event(jpeg_bytes, score, now)
        self.last_motion_at = now

    def status(self):
        with self.lock:
            opened = bool(self.capture and self.capture.isOpened())
            return {
                "device": self.device,
                "opened": opened,
                "last_error": self.last_error,
                "last_frame_at": self.last_frame_at,
                "motion_enabled": motion_enabled(),
                "last_motion_at": self.last_motion_at,
                "last_motion_score": self.last_motion_score,
            }

    def _release_locked(self):
        if self.capture:
            self.capture.release()
        self.capture = None


camera = CameraStream(CAMERA_DEVICE)


def authenticated():
    return bool(session.get("authenticated"))


def require_auth():
    if not PASSWORD_HASH:
        return False
    return authenticated()


@app.before_request
def guard_routes():
    public_paths = {"/login", "/healthz"}
    if request.path.startswith("/static/") or request.path in public_paths:
        return None
    if not require_auth():
        return redirect(url_for("login"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    missing_password = not bool(PASSWORD_HASH)

    if request.method == "POST" and not missing_password:
        password = request.form.get("password", "")
        if check_password_hash(PASSWORD_HASH, password):
            session.clear()
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Wrong password"

    return render_template("login.html", error=error, missing_password=missing_password)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    return render_template("dashboard.html", camera_device=CAMERA_DEVICE)


@app.get("/video")
def video():
    return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/snapshot.jpg")
def snapshot():
    frame = camera.read_jpeg()
    if frame is None:
        return Response("camera unavailable\n", status=503, mimetype="text/plain")
    return Response(frame, mimetype="image/jpeg")


@app.get("/api/status")
def api_status():
    return jsonify(build_status())


@app.get("/api/motion")
def api_motion_events():
    return jsonify({"events": list_motion_events(), "enabled": motion_enabled()})


@app.patch("/api/motion/settings")
@app.post("/api/motion/settings")
def api_motion_settings():
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"ok": False, "error": "enabled must be true or false"}), 400
    set_motion_enabled(enabled)
    return jsonify({"ok": True, "enabled": motion_enabled()})


@app.delete("/api/motion/<filename>")
def api_delete_motion_event(filename):
    path = motion_event_path(filename)
    if path is None or not path.exists():
        return jsonify({"ok": False, "error": "event not found"}), 404
    path.unlink()
    return jsonify({"ok": True})


@app.delete("/api/motion")
def api_clear_motion_events():
    deleted = 0
    for event in list_motion_events():
        path = motion_event_path(event["filename"])
        if path and path.exists():
            path.unlink()
            deleted += 1
    return jsonify({"ok": True, "deleted": deleted})


@app.get("/motion/<filename>")
def motion_file(filename):
    path = motion_event_path(filename)
    if path is None or not path.exists():
        return Response("event not found\n", status=404, mimetype="text/plain")
    return send_from_directory(MOTION_DIR, filename, mimetype="image/jpeg")


@app.get("/healthz")
def healthz():
    cam = camera.status()
    return jsonify(
        {
            "ok": bool(PASSWORD_HASH),
            "password_configured": bool(PASSWORD_HASH),
            "camera_device": CAMERA_DEVICE,
            "camera_opened": cam["opened"],
            "motion_enabled": motion_enabled(),
            "motion_events": len(list_motion_events()),
            "service_uptime_seconds": int(time.time() - SERVICE_STARTED_AT),
        }
    )


def generate_frames():
    delay = 1 / max(FRAME_FPS, 1)
    while True:
        frame = camera.read_jpeg()
        if frame is not None:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        time.sleep(delay)


def build_status():
    return {
        "host": {
            "hostname": socket.gethostname(),
            "boot_time": iso_from_timestamp(psutil.boot_time()),
            "system_uptime_seconds": int(time.time() - psutil.boot_time()),
            "service_uptime_seconds": int(time.time() - SERVICE_STARTED_AT),
            "load_average": safe_load_average(),
        },
        "resources": {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory": memory_status(),
            "disk": disk_status("/"),
            "temperature_c": temperature_status(),
        },
        "network": network_status(),
        "tailscale": tailscale_status(),
        "camera": camera.status(),
        "motion": motion_status(),
        "now": iso_from_timestamp(time.time()),
    }


def safe_load_average():
    try:
        return os.getloadavg()
    except OSError:
        return None


def memory_status():
    mem = psutil.virtual_memory()
    return {
        "total": mem.total,
        "used": mem.used,
        "available": mem.available,
        "percent": mem.percent,
    }


def disk_status(path):
    disk = psutil.disk_usage(path)
    return {
        "path": path,
        "total": disk.total,
        "used": disk.used,
        "free": disk.free,
        "percent": disk.percent,
    }


def temperature_status():
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None

    readings = []
    for name, entries in temps.items():
        for entry in entries:
            if entry.current is not None:
                readings.append({"sensor": name, "label": entry.label or name, "current": entry.current})
    if not readings:
        return None
    return max(readings, key=lambda item: item["current"])


def network_status():
    counters = psutil.net_io_counters()
    now = time.time()

    with stats_lock:
        elapsed = max(now - net_sample["time"], 0.001)
        up_bps = (counters.bytes_sent - net_sample["bytes_sent"]) / elapsed
        down_bps = (counters.bytes_recv - net_sample["bytes_recv"]) / elapsed
        net_sample.update(
            {
                "time": now,
                "bytes_sent": counters.bytes_sent,
                "bytes_recv": counters.bytes_recv,
                "up_bps": max(up_bps, 0.0),
                "down_bps": max(down_bps, 0.0),
            }
        )
        return {
            "bytes_sent": counters.bytes_sent,
            "bytes_recv": counters.bytes_recv,
            "upload_bytes_per_second": net_sample["up_bps"],
            "download_bytes_per_second": net_sample["down_bps"],
        }


def tailscale_status():
    ip = run_command(["tailscale", "ip", "-4"])
    status = run_command(["tailscale", "status", "--peers=false"])
    return {
        "installed": ip["returncode"] != 127,
        "ip4": ip["stdout"].strip().splitlines()[0] if ip["returncode"] == 0 and ip["stdout"].strip() else None,
        "status": status["stdout"].strip() if status["returncode"] == 0 else status["stderr"].strip(),
        "ok": ip["returncode"] == 0,
    }


def ensure_motion_dir():
    MOTION_DIR.mkdir(parents=True, exist_ok=True)


def load_motion_enabled():
    try:
        value = MOTION_STATE_FILE.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        return MOTION_ENABLED
    except OSError:
        return MOTION_ENABLED
    return value in {"1", "true", "yes", "on", "enabled"}


def motion_enabled():
    with motion_lock:
        return motion_enabled_state


def set_motion_enabled(enabled):
    global motion_enabled_state
    with motion_lock:
        motion_enabled_state = bool(enabled)
    MOTION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MOTION_STATE_FILE.write_text("1\n" if enabled else "0\n", encoding="utf-8")


def save_motion_event(jpeg_bytes, score, created_at):
    ensure_motion_dir()
    stamp = datetime.fromtimestamp(created_at).strftime("%Y%m%d-%H%M%S")
    millis = int((created_at % 1) * 1000)
    filename = f"motion-{stamp}-{millis:03d}-score-{score:.2f}.jpg"
    path = MOTION_DIR / filename
    path.write_bytes(jpeg_bytes)
    prune_motion_events()


def motion_event_path(filename):
    if not MOTION_FILENAME_RE.match(filename):
        return None
    path = (MOTION_DIR / filename).resolve()
    try:
        path.relative_to(MOTION_DIR.resolve())
    except ValueError:
        return None
    return path


def list_motion_events():
    ensure_motion_dir()
    events = []
    for path in MOTION_DIR.glob("motion-*.jpg"):
        if not MOTION_FILENAME_RE.match(path.name):
            continue
        stat = path.stat()
        score = parse_motion_score(path.name)
        events.append(
            {
                "filename": path.name,
                "url": url_for("motion_file", filename=path.name),
                "created_at": iso_from_timestamp(stat.st_mtime),
                "size": stat.st_size,
                "score": score,
            }
        )
    events.sort(key=lambda item: item["created_at"], reverse=True)
    return events


def motion_status():
    events = list_motion_events()
    return {
        "enabled": motion_enabled(),
        "event_count": len(events),
        "latest_event": events[0] if events else None,
        "directory": str(MOTION_DIR),
        "changed_percent_threshold": MOTION_CHANGED_PERCENT,
        "min_interval_seconds": MOTION_MIN_INTERVAL,
    }


def parse_motion_score(filename):
    match = re.search(r"-score-([0-9.]+)\.jpg$", filename)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def prune_motion_events():
    events = sorted(MOTION_DIR.glob("motion-*.jpg"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in events[MOTION_MAX_EVENTS:]:
        if MOTION_FILENAME_RE.match(path.name):
            path.unlink(missing_ok=True)


def run_command(args):
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=2, check=False)
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": "not installed"}
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "stdout": "", "stderr": "timed out"}


def iso_from_timestamp(value):
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


motion_enabled_state = load_motion_enabled()

if __name__ == "__main__":
    app.run(host=os.environ.get("PRINTCAM_HOST", "0.0.0.0"), port=int(os.environ.get("PRINTCAM_PORT", "8080")))
