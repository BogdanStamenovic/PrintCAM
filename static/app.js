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
  tailscale: document.querySelector("#tailscale"),
  camera: document.querySelector("#camera"),
  load: document.querySelector("#load"),
};

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
    setText(ids.tailscale, status.tailscale.ip4 || (status.tailscale.ok ? "online" : "offline"));
    setText(ids.camera, status.camera.opened ? "streaming" : status.camera.last_error || "offline");
    setText(ids.load, status.host.load_average ? status.host.load_average.map((v) => v.toFixed(2)).join(" ") : "n/a");

    ids.dot.classList.toggle("ok", Boolean(status.camera.opened || status.camera.last_frame_at));
  } catch (error) {
    ids.dot.classList.remove("ok");
    setText(ids.camera, "status offline");
  }
}

refreshStatus();
setInterval(refreshStatus, 2000);
