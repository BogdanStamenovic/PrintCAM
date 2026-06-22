import os
import socket
import subprocess
import threading
import time
from datetime import datetime

import cv2
import psutil
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash


CAMERA_DEVICE = os.environ.get("PRINTCAM_CAMERA_DEVICE", "/dev/video2")
FRAME_WIDTH = int(os.environ.get("PRINTCAM_FRAME_WIDTH", "1280"))
FRAME_HEIGHT = int(os.environ.get("PRINTCAM_FRAME_HEIGHT", "720"))
FRAME_FPS = int(os.environ.get("PRINTCAM_FRAME_FPS", "15"))
PASSWORD_HASH = os.environ.get("PRINTCAM_PASSWORD_HASH", "")
SERVICE_STARTED_AT = time.time()

app = Flask(__name__)
app.secret_key = os.environ.get("PRINTCAM_SECRET_KEY", "dev-only-change-me")

stats_lock = threading.Lock()
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
            return self.last_frame

    def status(self):
        with self.lock:
            opened = bool(self.capture and self.capture.isOpened())
            return {
                "device": self.device,
                "opened": opened,
                "last_error": self.last_error,
                "last_frame_at": self.last_frame_at,
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


@app.get("/healthz")
def healthz():
    cam = camera.status()
    return jsonify(
        {
            "ok": bool(PASSWORD_HASH),
            "password_configured": bool(PASSWORD_HASH),
            "camera_device": CAMERA_DEVICE,
            "camera_opened": cam["opened"],
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


if __name__ == "__main__":
    app.run(host=os.environ.get("PRINTCAM_HOST", "0.0.0.0"), port=int(os.environ.get("PRINTCAM_PORT", "8080")))
