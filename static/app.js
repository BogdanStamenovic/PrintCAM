const ids = {
  dot: document.querySelector("#health-dot"),
  host: document.querySelector("#host"),
  systemUptime: document.querySelector("#system-uptime"),
  serviceUptime: document.querySelector("#service-uptime"),
  cpu: document.querySelector("#cpu"),
  memory: document.querySelector("#memory"),
  disk: document.querySelector("#disk"),
  temperature: document.querySelector("#temperature"),
  netDown: document.querySelector("#net-down"),
  netUp: document.querySelector("#net-up"),
  wifi: document.querySelector("#wifi"),
  tailscale: document.querySelector("#tailscale"),
  camera: document.querySelector("#camera"),
  audio: document.querySelector("#audio"),
  load: document.querySelector("#load"),
  motionCount: document.querySelector("#motion-count"),
  motionEnabled: document.querySelector("#motion-enabled"),
  lastMotion: document.querySelector("#last-motion"),
  motionEvents: document.querySelector("#motion-events"),
  clearMotion: document.querySelector("#clear-motion"),
  toggleMotion: document.querySelector("#toggle-motion"),
  screenSleep: document.querySelector("#screen-sleep"),
  cameraStream: document.querySelector("#camera-stream"),
  cameraDevice: document.querySelector("#camera-device"),
  applyCamera: document.querySelector("#apply-camera"),
  audioSource: document.querySelector("#audio-source"),
  toggleAudio: document.querySelector("#toggle-audio"),
};

let motionEnabled = false;
let audioEnabled = false;

function formatDuration(seconds) {
  const value = Math.max(Number(seconds) || 0, 0);
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes) || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatRate(bytesPerSecond) {
  return `${formatBytes(bytesPerSecond)}/s`;
}

function setText(element, value) {
  element.textContent = value ?? "n/a";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatDate(value) {
  if (!value) return "never";
  return new Date(value).toLocaleString();
}

function formatTime(value) {
  if (!value) return "unknown";
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatTimeRange(event) {
  return `${formatTime(event.started_at || event.created_at)} - ${formatTime(event.ended_at || event.created_at)}`;
}

function formatSeconds(seconds) {
  const value = Math.max(Number(seconds) || 0, 0);
  const minutes = Math.floor(value / 60);
  const remainingSeconds = Math.round(value % 60);
  if (minutes > 0) return `${minutes}m ${remainingSeconds}s`;
  return `${remainingSeconds}s`;
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const status = await response.json();

    setText(ids.host, status.host.hostname);
    setText(ids.systemUptime, formatDuration(status.host.system_uptime_seconds));
    setText(ids.serviceUptime, formatDuration(status.host.service_uptime_seconds));
    setText(ids.cpu, `${Math.round(status.resources.cpu_percent)}%`);
    setText(ids.memory, `${Math.round(status.resources.memory.percent)}% of ${formatBytes(status.resources.memory.total)}`);
    setText(ids.disk, `${Math.round(status.resources.disk.percent)}% of ${formatBytes(status.resources.disk.total)}`);
    setText(
      ids.temperature,
      status.resources.temperature_c ? `${Math.round(status.resources.temperature_c.current)} C` : "n/a",
    );
    setText(ids.netDown, formatRate(status.network.download_bytes_per_second));
    setText(ids.netUp, formatRate(status.network.upload_bytes_per_second));
    setText(ids.wifi, status.network.wifi_ssid || "offline");
    setText(ids.tailscale, status.tailscale.ip4 || (status.tailscale.ok ? "online" : "offline"));
    setText(ids.camera, status.camera.opened ? "streaming" : status.camera.last_error || "offline");
    setAudioToggle(Boolean(status.audio.enabled), status.audio.last_error);
    setText(ids.load, status.host.load_average ? status.host.load_average.map((v) => v.toFixed(2)).join(" ") : "n/a");
    setText(ids.motionCount, status.motion.event_count);
    setMotionToggle(status.motion.enabled);
    setText(ids.lastMotion, status.motion.latest_event ? formatDate(status.motion.latest_event.created_at) : "never");

    ids.dot.classList.toggle("ok", Boolean(status.camera.opened || status.camera.last_frame_at));
  } catch (error) {
    ids.dot.classList.remove("ok");
    setText(ids.camera, "status offline");
  }
}

async function refreshMotionEvents() {
  try {
    const response = await fetch("/api/motion", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    setMotionToggle(Boolean(payload.enabled));
    renderMotionEvents(payload.events || []);
  } catch (error) {
    ids.motionEvents.innerHTML = `<div class="empty-state">Could not load motion events.</div>`;
  }
}

function setMotionToggle(enabled) {
  motionEnabled = Boolean(enabled);
  setText(ids.motionEnabled, motionEnabled ? "enabled" : "disabled");
  ids.toggleMotion.textContent = motionEnabled ? "Disable motion" : "Enable motion";
  ids.toggleMotion.classList.toggle("is-on", motionEnabled);
}

function setAudioToggle(enabled, error) {
  audioEnabled = Boolean(enabled);
  setText(ids.audio, audioEnabled ? "playing" : error || "off");
  ids.toggleAudio.textContent = audioEnabled ? "Stop audio" : "Start audio";
  ids.toggleAudio.classList.toggle("is-on", audioEnabled);
}

function optionLabelForCamera(device, path) {
  const name = device.name === path ? path : `${device.name} (${path})`;
  return path === device.selected_path ? `${name} · stream` : name;
}

async function refreshCameraDevices() {
  try {
    const response = await fetch("/api/cameras", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const options = [];
    for (const device of payload.devices || []) {
      for (const path of device.paths || []) {
        options.push(`<option value="${escapeHtml(path)}">${escapeHtml(optionLabelForCamera(device, path))}</option>`);
      }
    }
    ids.cameraDevice.innerHTML = options.join("") || `<option value="">No cameras found</option>`;
    ids.cameraDevice.value = payload.active_device || "";
  } catch (error) {
    ids.cameraDevice.innerHTML = `<option value="">Could not load cameras</option>`;
  }
}

async function refreshAudioSources() {
  try {
    const response = await fetch("/api/audio", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    ids.audioSource.innerHTML = (payload.sources || [])
      .map((source) => `<option value="${escapeHtml(source.name)}">${escapeHtml(source.description || source.name)}</option>`)
      .join("") || `<option value="">No audio inputs found</option>`;
    ids.audioSource.value = payload.relay.source || payload.default_source || "";
    setAudioToggle(Boolean(payload.relay.enabled), payload.relay.last_error);
  } catch (error) {
    ids.audioSource.innerHTML = `<option value="">Could not load audio</option>`;
  }
}

function renderMotionEvents(events) {
  if (events.length === 0) {
    ids.motionEvents.innerHTML = `<div class="empty-state">No motion saved yet.</div>`;
    return;
  }

  ids.motionEvents.innerHTML = events
    .map(
      (event) => `
        <article class="motion-card">
          <video src="${event.url}" preload="metadata" muted controls aria-label="Motion event ${formatTimeRange(event)}"></video>
          <div class="motion-meta">
            <strong>${formatTimeRange(event)}</strong>
            <span>${formatDate(event.started_at || event.created_at)} · ${formatSeconds(event.duration_seconds)} · ${formatBytes(event.size)}</span>
          </div>
          <button class="delete-motion" type="button" data-filename="${event.filename}">Delete</button>
        </article>
      `,
    )
    .join("");
}

async function deleteMotionEvent(filename) {
  const response = await fetch(`/api/motion/${encodeURIComponent(filename)}`, { method: "DELETE" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  await refreshMotionEvents();
  await refreshStatus();
}

async function clearMotionEvents() {
  const response = await fetch("/api/motion", { method: "DELETE" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  await refreshMotionEvents();
  await refreshStatus();
}

async function setMotionEnabled(enabled) {
  const response = await fetch("/api/motion/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json();
  setMotionToggle(Boolean(payload.enabled));
}

async function setCameraDevice(device) {
  const response = await fetch("/api/camera/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  ids.cameraStream.src = `/video?ts=${Date.now()}`;
  await refreshCameraDevices();
}

async function setAudioEnabled(enabled) {
  const response = await fetch("/api/audio/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, source: ids.audioSource.value }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json();
  setAudioToggle(Boolean(payload.relay.enabled), payload.relay.last_error);
}

ids.motionEvents.addEventListener("click", async (event) => {
  const button = event.target.closest(".delete-motion");
  if (!button) return;
  button.disabled = true;
  try {
    await deleteMotionEvent(button.dataset.filename);
  } catch (error) {
    button.disabled = false;
  }
});

ids.clearMotion.addEventListener("click", async () => {
  ids.clearMotion.disabled = true;
  try {
    await clearMotionEvents();
  } finally {
    ids.clearMotion.disabled = false;
  }
});

ids.toggleMotion.addEventListener("click", async () => {
  ids.toggleMotion.disabled = true;
  try {
    await setMotionEnabled(!motionEnabled);
    await refreshStatus();
  } finally {
    ids.toggleMotion.disabled = false;
  }
});

ids.applyCamera.addEventListener("click", async () => {
  ids.applyCamera.disabled = true;
  try {
    await setCameraDevice(ids.cameraDevice.value);
    await refreshStatus();
  } finally {
    ids.applyCamera.disabled = false;
  }
});

ids.toggleAudio.addEventListener("click", async () => {
  ids.toggleAudio.disabled = true;
  try {
    await setAudioEnabled(!audioEnabled);
    await refreshStatus();
  } finally {
    ids.toggleAudio.disabled = false;
  }
});

if (ids.screenSleep) {
  ids.screenSleep.addEventListener("click", async () => {
    ids.screenSleep.disabled = true;
    try {
      const res = await fetch("/api/display/sleep", { method: "POST" });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(text || `HTTP ${res.status}`);
      }
      // screen will turn off; nothing further needed here
    } catch (err) {
      console.error(err);
      alert("Failed to turn screen off: " + (err && err.message));
    } finally {
      ids.screenSleep.disabled = false;
    }
  });
}

refreshStatus();
refreshCameraDevices();
refreshAudioSources();
refreshMotionEvents();
setInterval(refreshStatus, 2000);
setInterval(refreshAudioSources, 10000);
setInterval(refreshMotionEvents, 5000);
