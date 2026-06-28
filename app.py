import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import psutil
from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, session, stream_with_context, url_for
from werkzeug.security import check_password_hash


CAMERA_NAME = os.environ.get("PRINTCAM_CAMERA_NAME", "").strip()
CAMERA_DEVICE_FALLBACK = os.environ.get("PRINTCAM_CAMERA_DEVICE", "/dev/video2")
FRAME_WIDTH = int(os.environ.get("PRINTCAM_FRAME_WIDTH", "1280"))
FRAME_HEIGHT = int(os.environ.get("PRINTCAM_FRAME_HEIGHT", "720"))
FRAME_FPS = int(os.environ.get("PRINTCAM_FRAME_FPS", "15"))
PASSWORD_HASH = os.environ.get("PRINTCAM_PASSWORD_HASH", "")
SERVICE_STARTED_AT = time.time()
MOTION_ENABLED = os.environ.get("PRINTCAM_MOTION_ENABLED", "1") == "1"
MOTION_DIR = Path(os.environ.get("PRINTCAM_MOTION_DIR", "/var/lib/printcam/motion"))
MOTION_STATE_FILE = Path(os.environ.get("PRINTCAM_MOTION_STATE_FILE", "/var/lib/printcam/motion-enabled"))
MOTION_CONFIRM_SECONDS = float(os.environ.get("PRINTCAM_MOTION_CONFIRM_SECONDS", "5"))
MOTION_CHANGED_PERCENT = float(os.environ.get("PRINTCAM_MOTION_CHANGED_PERCENT", "1.8"))
MOTION_PIXEL_DELTA = int(os.environ.get("PRINTCAM_MOTION_PIXEL_DELTA", "28"))
MOTION_MAX_EVENTS = int(os.environ.get("PRINTCAM_MOTION_MAX_EVENTS", "200"))
MOTION_RECORDING_CODEC = os.environ.get("PRINTCAM_MOTION_RECORDING_CODEC", "MJPG")
MOTION_FFMPEG_BIN = os.environ.get("PRINTCAM_FFMPEG_BIN", "ffmpeg")
MOTION_OUTPUT_VIDEO_CODEC = os.environ.get("PRINTCAM_MOTION_OUTPUT_VIDEO_CODEC", "libx264")
AUDIO_SOURCE = os.environ.get("PRINTCAM_AUDIO_SOURCE", "").strip()
AUDIO_RATE = int(os.environ.get("PRINTCAM_AUDIO_RATE", "44100"))
AUDIO_CHANNELS = int(os.environ.get("PRINTCAM_AUDIO_CHANNELS", "2"))
MOTION_TIMESTAMP_PATTERN = r"\d{8}-\d{6}-\d{3}"
MOTION_FILENAME_RE = re.compile(
    rf"^motion-(?P<start>{MOTION_TIMESTAMP_PATTERN})(?:-to-(?P<end>{MOTION_TIMESTAMP_PATTERN}))?-score-(?P<score>[0-9.]+)\.mp4$"
)

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


def parse_v4l2_devices(output):
    devices = []
    current_name = None
    current_paths = []

    def flush_current():
        if current_name and current_paths:
            devices.append({"name": current_name, "paths": list(current_paths)})

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_current()
            current_name = None
            current_paths = []
            continue

        if not raw_line.startswith((" ", "\t")) and stripped.endswith(":"):
            flush_current()
            current_name = stripped[:-1].strip()
            current_paths = []
            continue

        if stripped.startswith("/dev/video"):
            current_paths.append(stripped)

    flush_current()
    return devices


def even_video_path(paths):
    numbered_paths = []
    for path in paths:
        match = re.fullmatch(r"/dev/video(\d+)", path)
        if match:
            numbered_paths.append((int(match.group(1)), path))

    even_paths = [item for item in numbered_paths if item[0] % 2 == 0]
    if even_paths:
        return sorted(even_paths)[0][1]
    if numbered_paths:
        return sorted(numbered_paths)[0][1]
    return paths[0] if paths else None


def resolve_camera_device(camera_name, fallback_device):
    if not camera_name:
        return fallback_device, None

    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return fallback_device, f"Could not list cameras with v4l2-ctl: {exc}. Falling back to {fallback_device}."

    if result.returncode != 0:
        error = result.stderr.strip() or "unknown error"
        return fallback_device, f"v4l2-ctl --list-devices failed: {error}. Falling back to {fallback_device}."

    for device in parse_v4l2_devices(result.stdout):
        if device["name"] == camera_name:
            resolved = even_video_path(device["paths"])
            if resolved:
                return resolved, None

    return fallback_device, f"Camera named {camera_name!r} was not found. Falling back to {fallback_device}."


CAMERA_DEVICE, CAMERA_RESOLVE_ERROR = resolve_camera_device(CAMERA_NAME, CAMERA_DEVICE_FALLBACK)


class CameraStream:
    def __init__(self, device, name=""):
        self.device = device
        self.name = name
        self.lock = threading.Lock()
        self.capture = None
        self.last_frame = None
        self.last_error = None
        self.last_frame_at = None
        self.last_motion_at = None
        self.last_motion_score = 0.0
        self.previous_motion_frame = None
        self.motion_video_writer = None
        self.motion_recording_temp_path = None
        self.motion_recording_started_at = None
        self.motion_recording_last_change_at = None
        self.motion_recording_change_count = 0
        self.motion_recording_max_score = 0.0
        self.motion_recording_confirmed = False

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
            self.detect_motion(frame)
            return self.last_frame

    def detect_motion(self, frame):
        if not motion_enabled():
            self.previous_motion_frame = None
            self.discard_motion_recording()
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
            self.update_motion_recording(frame, False, score, now)
            return

        self.update_motion_recording(frame, True, score, now)
        self.last_motion_at = now

    def update_motion_recording(self, frame, changed, score, now):
        if self.motion_video_writer is None:
            if changed:
                self.start_motion_recording(frame, score, now)
            return

        if not self.write_motion_frame(frame):
            self.discard_motion_recording()
            return

        if changed:
            if not self.motion_recording_confirmed and now - self.motion_recording_started_at > MOTION_CONFIRM_SECONDS:
                self.discard_motion_recording()
                self.start_motion_recording(frame, score, now)
                return

            self.motion_recording_last_change_at = now
            self.motion_recording_change_count += 1
            self.motion_recording_max_score = max(self.motion_recording_max_score, score)
            if self.motion_recording_change_count >= 2:
                self.motion_recording_confirmed = True
            return

        if self.motion_recording_confirmed:
            if now - self.motion_recording_last_change_at >= MOTION_CONFIRM_SECONDS:
                self.finalize_motion_recording(now)
        elif now - self.motion_recording_started_at >= MOTION_CONFIRM_SECONDS:
            self.discard_motion_recording()

    def start_motion_recording(self, frame, score, now):
        ensure_motion_dir()
        temp_path = MOTION_DIR / f".motion-{motion_filename_stamp(now)}.recording.avi"
        height, width = frame.shape[:2]
        codec = (MOTION_RECORDING_CODEC or "MJPG")[:4].ljust(4)
        writer = cv2.VideoWriter(
            str(temp_path),
            cv2.VideoWriter_fourcc(*codec),
            max(FRAME_FPS, 1),
            (width, height),
        )
        if not writer.isOpened():
            self.last_error = "Could not start motion video recording"
            temp_path.unlink(missing_ok=True)
            return

        self.motion_video_writer = writer
        self.motion_recording_temp_path = temp_path
        self.motion_recording_started_at = now
        self.motion_recording_last_change_at = now
        self.motion_recording_change_count = 1
        self.motion_recording_max_score = score
        self.motion_recording_confirmed = False
        if not self.write_motion_frame(frame):
            self.discard_motion_recording()

    def write_motion_frame(self, frame):
        if self.motion_video_writer is None:
            return False
        self.motion_video_writer.write(frame)
        return True

    def finalize_motion_recording(self, finished_at):
        temp_path = self.motion_recording_temp_path
        started_at = self.motion_recording_started_at
        max_score = self.motion_recording_max_score
        self.release_motion_writer()

        if temp_path is None or started_at is None:
            return

        start_stamp = motion_filename_stamp(started_at)
        end_stamp = motion_filename_stamp(finished_at)
        final_path = MOTION_DIR / f"motion-{start_stamp}-to-{end_stamp}-score-{max_score:.2f}.mp4"
        if encode_motion_video(temp_path, final_path):
            temp_path.unlink(missing_ok=True)
            prune_motion_events()
            return

        self.last_error = "Could not encode motion video for browser playback"
        temp_path.unlink(missing_ok=True)

    def discard_motion_recording(self):
        temp_path = self.motion_recording_temp_path
        self.release_motion_writer()
        if temp_path:
            temp_path.unlink(missing_ok=True)

    def release_motion_writer(self):
        if self.motion_video_writer:
            self.motion_video_writer.release()
        self.motion_video_writer = None
        self.motion_recording_temp_path = None
        self.motion_recording_started_at = None
        self.motion_recording_last_change_at = None
        self.motion_recording_change_count = 0
        self.motion_recording_max_score = 0.0
        self.motion_recording_confirmed = False

    def status(self):
        with self.lock:
            opened = bool(self.capture and self.capture.isOpened())
            return {
                "device": self.device,
                "configured_name": self.name,
                "fallback_device": CAMERA_DEVICE_FALLBACK,
                "resolve_error": CAMERA_RESOLVE_ERROR,
                "opened": opened,
                "last_error": self.last_error,
                "last_frame_at": self.last_frame_at,
                "motion_enabled": motion_enabled(),
                "last_motion_at": self.last_motion_at,
                "last_motion_score": self.last_motion_score,
                "motion_recording": self.motion_video_writer is not None,
                "motion_recording_confirmed": self.motion_recording_confirmed,
            }

    def switch_device(self, device, name=""):
        with self.lock:
            self.discard_motion_recording()
            self._release_locked()
            self.device = device
            self.name = name
            self.last_frame = None
            self.last_frame_at = None
            self.last_motion_at = None
            self.last_motion_score = 0.0
            self.previous_motion_frame = None
            self.last_error = None
        return self.open()

    def _release_locked(self):
        if self.capture:
            self.capture.release()
        self.capture = None


class SpeakerInput:
    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = False
        self.last_error = None
        self.decoder_process = None
        self.playback_process = None

    def start(self):
        with self.lock:
            self._stop_locked()

            if shutil.which("ffmpeg") is None or shutil.which("paplay") is None:
                self.last_error = "ffmpeg/paplay are not installed"
                self.enabled = False
                return False

            set_default_volume_100()
            decoder_args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "webm",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(AUDIO_RATE),
                "-ac",
                str(AUDIO_CHANNELS),
                "pipe:1",
            ]
            playback_args = [
                "paplay",
                "--raw",
                "--format=s16le",
                f"--rate={AUDIO_RATE}",
                f"--channels={AUDIO_CHANNELS}",
            ]

            try:
                decoder = subprocess.Popen(
                    decoder_args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                playback = subprocess.Popen(
                    playback_args,
                    stdin=decoder.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if decoder.stdout:
                    decoder.stdout.close()
            except OSError as exc:
                self.last_error = f"Could not start speaker input: {exc}"
                self.enabled = False
                return False

            time.sleep(0.1)
            if decoder.poll() is not None or playback.poll() is not None:
                self._terminate_process(decoder)
                self._terminate_process(playback)
                self.last_error = "speaker input exited immediately"
                self.enabled = False
                return False

            self.decoder_process = decoder
            self.playback_process = playback
            self.enabled = True
            self.last_error = None
            return True

    def write(self, chunk):
        if not chunk:
            return True
        with self.lock:
            if not self._running_locked() or not self.decoder_process.stdin:
                self.enabled = False
                self.last_error = self.last_error or "speaker input is not running"
                return False
            try:
                self.decoder_process.stdin.write(chunk)
                self.decoder_process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self.enabled = False
                self.last_error = f"speaker input stopped: {exc}"
                return False
            return True

    def stop(self):
        with self.lock:
            self._stop_locked()

    def status(self):
        with self.lock:
            running = self._running_locked()
            if self.enabled and not running:
                self.enabled = False
                self.last_error = self.last_error or "speaker input stopped"
            return {
                "enabled": self.enabled and running,
                "last_error": self.last_error,
                "available": shutil.which("ffmpeg") is not None and shutil.which("paplay") is not None,
            }

    def _running_locked(self):
        return bool(
            self.decoder_process
            and self.playback_process
            and self.decoder_process.poll() is None
            and self.playback_process.poll() is None
        )

    def _stop_locked(self):
        self.enabled = False
        if self.decoder_process and self.decoder_process.stdin:
            try:
                self.decoder_process.stdin.close()
            except OSError:
                pass
        self._terminate_process(self.decoder_process)
        self._terminate_process(self.playback_process)
        self.decoder_process = None
        self.playback_process = None

    @staticmethod
    def _terminate_process(process):
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


camera = CameraStream(CAMERA_DEVICE, CAMERA_NAME)
speaker_input = SpeakerInput()


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


@app.get("/audio/device.wav")
def device_audio_stream():
    source = request.args.get("source", "").strip() or AUDIO_SOURCE
    if source:
        source_names = {item["name"] for item in list_audio_sources()}
        if source not in source_names:
            return Response("unknown audio source\n", status=400, mimetype="text/plain")
    if shutil.which("parec") is None:
        return Response("parec is not installed\n", status=503, mimetype="text/plain")
    return Response(
        stream_with_context(generate_audio_wav(source)),
        mimetype="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/snapshot.jpg")
def snapshot():
    frame = camera.read_jpeg()
    if frame is None:
        return Response("camera unavailable\n", status=503, mimetype="text/plain")
    return Response(frame, mimetype="image/jpeg")


@app.get("/api/status")
def api_status():
    return jsonify(build_status())


@app.get("/api/cameras")
def api_cameras():
    return jsonify({"devices": list_camera_devices(), "active_device": camera.device})


@app.patch("/api/camera/settings")
@app.post("/api/camera/settings")
def api_camera_settings():
    payload = request.get_json(silent=True) or {}
    device = str(payload.get("device", "")).strip()
    if not valid_video_device_path(device):
        return jsonify({"ok": False, "error": "device must be an existing /dev/video* path"}), 400

    name = ""
    for item in list_camera_devices():
        if device in item["paths"]:
            name = item["name"]
            break

    opened = camera.switch_device(device, name)
    return jsonify({"ok": opened, "camera": camera.status()})


@app.get("/api/audio")
def api_audio():
    return jsonify(
        {
            "sources": list_audio_sources(),
            "default_source": default_audio_source(),
            "listen": {"available": shutil.which("parec") is not None, "source": AUDIO_SOURCE},
            "speaker_input": speaker_input.status(),
        }
    )


@app.post("/api/audio/speaker/start")
def api_audio_speaker_start():
    ok = speaker_input.start()
    status = speaker_input.status()
    return jsonify({"ok": ok, "speaker_input": status}), (200 if ok else 503)


@app.post("/api/audio/speaker/chunk")
def api_audio_speaker_chunk():
    ok = speaker_input.write(request.get_data(cache=False))
    status = speaker_input.status()
    return jsonify({"ok": ok, "speaker_input": status}), (200 if ok else 409)


@app.post("/api/audio/speaker/stop")
def api_audio_speaker_stop():
    speaker_input.stop()
    return jsonify({"ok": True, "speaker_input": speaker_input.status()})


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
    return send_from_directory(MOTION_DIR, filename, mimetype="video/mp4")


@app.get("/healthz")
def healthz():
    cam = camera.status()
    return jsonify(
        {
            "ok": bool(PASSWORD_HASH),
            "password_configured": bool(PASSWORD_HASH),
            "camera_name": CAMERA_NAME,
            "camera_device": CAMERA_DEVICE,
            "camera_device_fallback": CAMERA_DEVICE_FALLBACK,
            "camera_resolve_error": CAMERA_RESOLVE_ERROR,
            "camera_opened": cam["opened"],
            "speaker_input_enabled": speaker_input.status()["enabled"],
            "motion_enabled": motion_enabled(),
            "motion_events": len(list_motion_events()),
            "service_uptime_seconds": int(time.time() - SERVICE_STARTED_AT),
        }
    )


@app.post("/api/display/sleep")
def api_display_sleep():
    """Attempt to turn the display off while leaving the system running.

    Tries a helper script at scripts/screen_off.sh first, then falls back to
    common commands like vcgencmd or xset.
    """
    script_path = Path(__file__).parent / "scripts" / "screen_off.sh"
    env = os.environ.copy()
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"

    script_commands = []
    last_error = ""
    if script_path.exists():
        if os.access(script_path, os.X_OK):
            script_commands.append([str(script_path)])
        script_commands.append(["/bin/bash", str(script_path)])
        sudo_path = shutil.which("sudo")
        if sudo_path:
            script_commands.insert(0, [sudo_path, "-n", str(script_path)])

    for command in script_commands:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)
            if completed.returncode == 0:
                return jsonify({"ok": True})
            if command[0] == sudo_path or "a password is required" in completed.stderr.lower():
                continue
            last_error = completed.stderr.strip() or completed.stdout.strip()
        except Exception as exc:
            last_error = str(exc)
    if script_path.exists():
        return jsonify({"ok": False, "error": last_error or "screen helper failed"}), 500

    # Fallback attempts
    fallbacks = [(["vcgencmd", "display_power", "0"], "vcgencmd"), (["xset", "dpms", "force", "off"], "xset")]
    for cmd, name in fallbacks:
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=5)
            if completed.returncode == 0:
                return jsonify({"ok": True, "method": name})
        except FileNotFoundError:
            continue
        except Exception as exc:
            continue

    return jsonify({"ok": False, "error": "no method succeeded"}), 500


def generate_frames():
    delay = 1 / max(FRAME_FPS, 1)
    while True:
        frame = camera.read_jpeg()
        if frame is not None:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        time.sleep(delay)


def generate_audio_wav(source=""):
    args = [
        "parec",
        "--format=s16le",
        f"--rate={AUDIO_RATE}",
        f"--channels={AUDIO_CHANNELS}",
    ]
    if source:
        args.append(f"--device={source}")

    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    yield wav_stream_header(AUDIO_RATE, AUDIO_CHANNELS, 16)
    try:
        while process.poll() is None:
            chunk = process.stdout.read(4096) if process.stdout else b""
            if not chunk:
                break
            yield chunk
    finally:
        SpeakerInput._terminate_process(process)


def wav_stream_header(rate, channels, bits_per_sample):
    byte_rate = rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = 0xFFFFFFFF
    riff_size = 0xFFFFFFFF
    return (
        b"RIFF"
        + riff_size.to_bytes(4, "little", signed=False)
        + b"WAVEfmt "
        + (16).to_bytes(4, "little", signed=False)
        + (1).to_bytes(2, "little", signed=False)
        + channels.to_bytes(2, "little", signed=False)
        + rate.to_bytes(4, "little", signed=False)
        + byte_rate.to_bytes(4, "little", signed=False)
        + block_align.to_bytes(2, "little", signed=False)
        + bits_per_sample.to_bytes(2, "little", signed=False)
        + b"data"
        + data_size.to_bytes(4, "little", signed=False)
    )


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
        "audio": {
            "listen_available": shutil.which("parec") is not None,
            "speaker_input": speaker_input.status(),
        },
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
            "wifi_ssid": active_wifi_ssid(),
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


def active_wifi_ssid():
    wifi = run_command(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    if wifi["returncode"] != 0:
        return None
    for line in wifi["stdout"].splitlines():
        fields = line.split(":", 1)
        if len(fields) == 2 and fields[0] == "yes":
            return fields[1] or None
    return None


def list_camera_devices():
    result = run_command(["v4l2-ctl", "--list-devices"], timeout=5)
    devices = []
    seen_paths = set()

    if result["returncode"] == 0:
        for item in parse_v4l2_devices(result["stdout"]):
            paths = [path for path in item["paths"] if valid_video_device_path(path)]
            if not paths:
                continue
            for path in paths:
                seen_paths.add(path)
            devices.append(
                {
                    "name": item["name"],
                    "paths": paths,
                    "selected_path": even_video_path(paths),
                }
            )

    for path in sorted(Path("/dev").glob("video*"), key=lambda value: natural_video_sort_key(str(value))):
        device = str(path)
        if device in seen_paths or not valid_video_device_path(device):
            continue
        devices.append({"name": device, "paths": [device], "selected_path": device})

    return devices


def valid_video_device_path(device):
    return bool(re.fullmatch(r"/dev/video\d+", device)) and Path(device).exists()


def natural_video_sort_key(device):
    match = re.search(r"(\d+)$", device)
    return int(match.group(1)) if match else 999999


def list_audio_sources():
    result = run_command(["pactl", "list", "sources"], timeout=5)
    if result["returncode"] != 0:
        return []

    sources = []
    current = {}
    for raw_line in result["stdout"].splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("Source #"):
            if current:
                append_audio_source(sources, current)
            current = {"index": stripped.removeprefix("Source #")}
        elif stripped.startswith("Name:"):
            current["name"] = stripped.removeprefix("Name:").strip()
        elif stripped.startswith("Description:"):
            current["description"] = stripped.removeprefix("Description:").strip()
        elif stripped.startswith("State:"):
            current["state"] = stripped.removeprefix("State:").strip()
    if current:
        append_audio_source(sources, current)
    return sources


def append_audio_source(sources, source):
    name = source.get("name", "")
    if not name or name.endswith(".monitor"):
        return
    sources.append(
        {
            "index": source.get("index"),
            "name": name,
            "description": source.get("description") or name,
            "state": source.get("state"),
        }
    )


def default_audio_source():
    result = run_command(["pactl", "get-default-source"], timeout=2)
    if result["returncode"] != 0:
        return None
    return result["stdout"].strip() or None


def set_default_volume_100():
    if shutil.which("pactl") is None:
        return False
    unmute = run_command(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], timeout=2)
    volume = run_command(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"], timeout=2)
    return unmute["returncode"] == 0 and volume["returncode"] == 0


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
    for path in MOTION_DIR.glob("motion-*.mp4"):
        details = parse_motion_filename(path.name)
        if details is None:
            continue
        stat = path.stat()
        started_at = details["started_at"] or stat.st_mtime
        ended_at = details["ended_at"] or stat.st_mtime
        events.append(
            {
                "filename": path.name,
                "url": url_for("motion_file", filename=path.name),
                "created_at": iso_from_timestamp(started_at),
                "started_at": iso_from_timestamp(started_at),
                "ended_at": iso_from_timestamp(ended_at),
                "duration_seconds": max(0, round(ended_at - started_at, 1)),
                "size": stat.st_size,
                "score": details["score"],
            }
        )
    events.sort(key=lambda item: item["started_at"], reverse=True)
    return events


def motion_status():
    events = list_motion_events()
    return {
        "enabled": motion_enabled(),
        "event_count": len(events),
        "latest_event": events[0] if events else None,
        "directory": str(MOTION_DIR),
        "changed_percent_threshold": MOTION_CHANGED_PERCENT,
        "confirm_seconds": MOTION_CONFIRM_SECONDS,
    }


def motion_filename_stamp(timestamp):
    stamp = datetime.fromtimestamp(timestamp).strftime("%Y%m%d-%H%M%S")
    millis = int((timestamp % 1) * 1000)
    return f"{stamp}-{millis:03d}"


def parse_motion_filename(filename):
    match = MOTION_FILENAME_RE.match(filename)
    if not match:
        return None
    start = parse_motion_filename_stamp(match.group("start"))
    end = parse_motion_filename_stamp(match.group("end")) if match.group("end") else None
    try:
        score = float(match.group("score"))
    except ValueError:
        score = None
    return {"started_at": start, "ended_at": end, "score": score}


def parse_motion_filename_stamp(value):
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%d-%H%M%S-%f")
    except ValueError:
        return None
    return parsed.timestamp()


def encode_motion_video(input_path, output_path):
    ffmpeg = shutil.which(MOTION_FFMPEG_BIN)
    if ffmpeg is None:
        return False

    temp_output = output_path.with_suffix(".encoding.mp4")
    temp_output.unlink(missing_ok=True)
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-an",
            "-c:v",
            MOTION_OUTPUT_VIDEO_CODEC,
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temp_output),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 or not temp_output.exists() or temp_output.stat().st_size == 0:
        temp_output.unlink(missing_ok=True)
        return False

    temp_output.replace(output_path)
    return True


def prune_motion_events():
    events = sorted(MOTION_DIR.glob("motion-*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in events[MOTION_MAX_EVENTS:]:
        if MOTION_FILENAME_RE.match(path.name):
            path.unlink(missing_ok=True)


def run_command(args, timeout=2):
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
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
