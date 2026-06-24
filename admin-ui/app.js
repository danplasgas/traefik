const mountPrefix = window.location.pathname === "/admin" || window.location.pathname.startsWith("/admin/") ? "/admin" : "";
const helperBase = window.TRAEFIK_ADMIN_HELPER_BASE || `${mountPrefix}/api`;
const traefikApiBase = window.TRAEFIK_API_BASE || `${mountPrefix}/traefik-api`;

const el = {
  helperWarning: document.querySelector("#helper-warning"),
  attentionPanel: document.querySelector("#attention-panel"),
  hostname: document.querySelector("#hostname"),
  uptime: document.querySelector("#uptime"),
  routerCount: document.querySelector("#router-count"),
  helperState: document.querySelector("#helper-state"),
  cpuMeter: document.querySelector("#cpu-meter"),
  cpuText: document.querySelector("#cpu-text"),
  memoryMeter: document.querySelector("#memory-meter"),
  memoryText: document.querySelector("#memory-text"),
  diskList: document.querySelector("#disk-list"),
  failuresList: document.querySelector("#failures-list"),
  traefikRouteGroups: document.querySelector("#traefik-route-groups"),
  agentmemoryRouteGroups: document.querySelector("#agentmemory-route-groups"),
  agentmemorySummary: document.querySelector("#agentmemory-summary"),
  servicesTable: document.querySelector("#services-table"),
  logServiceSelect: document.querySelector("#log-service-select"),
  logsOutput: document.querySelector("#logs-output"),
  loadLogsButton: document.querySelector("#load-logs-button"),
  fileSelect: document.querySelector("#file-select"),
  fileMeta: document.querySelector("#file-meta"),
  fileEditor: document.querySelector("#file-editor"),
  loadFileButton: document.querySelector("#load-file-button"),
  saveFileButton: document.querySelector("#save-file-button"),
  refreshButton: document.querySelector("#refresh-button"),
};

let helperAvailable = false;
let currentFile = null;

const state = {
  overview: null,
  raw: null,
  routes: [],
  services: [],
  files: [],
};

const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const fetchJson = async (url, options) => {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
};

const formatBytes = (bytes) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes || 0;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(idx ? 1 : 0)} ${units[idx]}`;
};

const formatDuration = (seconds) => {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
};

const statusClass = (value) => {
  if (["enabled", "active", "ok"].includes(value)) return "good";
  if (["disabled", "inactive", "not-found"].includes(value)) return "muted";
  return "warn";
};

const showWarning = (message) => {
  el.helperWarning.textContent = message;
  el.helperWarning.classList.toggle("hidden", !message);
};

const currentModule = () => {
  const mountedPath = mountPrefix && window.location.pathname.startsWith(mountPrefix)
    ? window.location.pathname.slice(mountPrefix.length) || "/"
    : window.location.pathname;
  const path = mountedPath.replace(/\/$/, "") || "/";
  return ["/", "/traefik", "/agentmemory", "/services", "/logs", "/files"].includes(path) ? path : "/";
};

const renderModule = () => {
  const active = currentModule();
  document.querySelectorAll("[data-module]").forEach((node) => {
    node.classList.toggle("hidden", node.dataset.module !== active);
  });
  document.querySelectorAll("[data-nav]").forEach((node) => {
    node.classList.toggle("active", node.dataset.nav === active);
  });
};

const refreshHelperState = async () => {
  try {
    const health = await fetchJson(`${helperBase}/health`);
    helperAvailable = health.status === "ok";
    el.helperState.textContent = helperAvailable ? "Available" : "Unavailable";
    showWarning(helperAvailable ? "" : "The local write helper is not available.");
  } catch (error) {
    helperAvailable = false;
    el.helperState.textContent = "Unavailable";
    showWarning(`The local write helper is unavailable: ${error.message}`);
  }
};

const refreshTraefikState = async () => {
  state.raw = await fetchJson(`${traefikApiBase}/rawdata`);
  el.routerCount.textContent = String(Object.keys(state.raw.routers || {}).length);
};

const refreshOverview = async () => {
  state.overview = await fetchJson(`${helperBase}/system/overview`);
  renderOverview();
};

const refreshRoutes = async () => {
  const response = await fetchJson(`${helperBase}/routes`);
  state.routes = response.groups || [];
  renderRouteGroups(el.traefikRouteGroups);
  renderRouteGroups(el.agentmemoryRouteGroups, (group) => group.name === "agentmemory");
};

const refreshServices = async () => {
  const response = await fetchJson(`${helperBase}/system/services`);
  state.services = response.services || [];
  renderServices();
  renderLogSelect();
  renderAgentMemorySummary();
};

const refreshFiles = async () => {
  const response = await fetchJson(`${helperBase}/files`);
  state.files = response.files || [];
  renderFileSelect();
  renderAgentMemorySummary();
};

const renderOverview = () => {
  const overview = state.overview;
  if (!overview) return;

  el.hostname.textContent = overview.hostname || "-";
  el.uptime.textContent = formatDuration(overview.uptime_seconds || 0);

  const cpu = overview.cpu || {};
  el.cpuMeter.style.width = `${Math.min(cpu.usage_percent || 0, 100)}%`;
  el.cpuText.textContent = `${cpu.usage_percent || 0}% across ${cpu.count || 1} cores | load ${overview.load_average?.join(", ") || "-"}`;

  const memory = overview.memory || {};
  el.memoryMeter.style.width = `${Math.min(memory.used_percent || 0, 100)}%`;
  el.memoryText.textContent = `${memory.used_percent || 0}% used | ${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)}`;

  el.diskList.innerHTML = "";
  for (const disk of overview.disks || []) {
    const row = make("div", "disk-row");
    row.appendChild(make("strong", "", `${disk.label} ${disk.used_percent}%`));
    row.appendChild(make("span", "", `${disk.path} | ${formatBytes(disk.used_bytes)} / ${formatBytes(disk.total_bytes)}`));
    const meter = make("div", "meter small");
    const fill = make("span");
    fill.style.width = `${Math.min(disk.used_percent || 0, 100)}%`;
    meter.appendChild(fill);
    row.appendChild(meter);
    el.diskList.appendChild(row);
  }

  renderFailures();
  renderAttention();
};

const renderFailures = () => {
  const failures = state.overview?.failed_units || [];
  el.failuresList.innerHTML = "";
  if (!failures.length) {
    el.failuresList.appendChild(make("p", "muted-text", "No failed systemd units."));
    return;
  }
  for (const failure of failures) {
    const row = make("div", "failure-row");
    row.appendChild(make("strong", "", failure.unit));
    row.appendChild(make("span", "", failure.description || `${failure.active}/${failure.sub}`));
    el.failuresList.appendChild(row);
  }
};

const renderAttention = () => {
  const items = [];
  if (!helperAvailable) items.push("Write helper unavailable");
  for (const failure of state.overview?.failed_units || []) items.push(`Failed unit: ${failure.unit}`);
  for (const disk of state.overview?.disks || []) {
    if ((disk.used_percent || 0) >= 85) items.push(`High disk usage on ${disk.path}: ${disk.used_percent}%`);
  }

  el.attentionPanel.innerHTML = "";
  el.attentionPanel.classList.toggle("hidden", !items.length);
  for (const item of items) el.attentionPanel.appendChild(make("span", "attention-chip", item));
};

const groupRouteData = (filter = () => true) => {
  const grouped = new Map();
  for (const group of state.routes.filter(filter)) {
    const status = routeGroupStatus(group);
    for (const host of group.hosts || ["path-only"]) {
      if (!grouped.has(host)) grouped.set(host, new Map());
      const byStatus = grouped.get(host);
      if (!byStatus.has(status)) byStatus.set(status, new Map());
      const byApp = byStatus.get(status);
      const apps = group.upstream_apps?.length ? group.upstream_apps : ["internal"];
      for (const app of apps) {
        if (!byApp.has(app)) byApp.set(app, []);
        byApp.get(app).push(group);
      }
    }
  }
  return grouped;
};

const routeGroupStatus = (group) => {
  if (!group.enabled) return "disabled";
  const liveRouters = state.raw?.routers || {};
  const statuses = new Set((group.routers || []).map((router) => liveRouters[`${router.name}@file`]?.status).filter(Boolean));
  if (!statuses.size) return "enabled";
  if (statuses.size === 1) return [...statuses][0];
  return "mixed";
};

const renderRouteGroups = (target, filter = () => true) => {
  target.innerHTML = "";
  const grouped = groupRouteData(filter);
  if (!grouped.size) {
    target.appendChild(make("p", "muted-text", "No route groups for this module."));
    return;
  }
  for (const [host, byStatus] of [...grouped.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const hostBlock = make("section", "host-block");
    hostBlock.appendChild(make("h3", "", host));
    for (const [status, byApp] of [...byStatus.entries()].sort(([a], [b]) => a.localeCompare(b))) {
      const statusBlock = make("div", "status-block");
      const statusTitle = make("h4", "", status);
      statusTitle.appendChild(make("span", `badge ${statusClass(status)}`, String([...byApp.values()].flat().length)));
      statusBlock.appendChild(statusTitle);
      for (const [app, groups] of [...byApp.entries()].sort(([a], [b]) => a.localeCompare(b))) {
        const appBlock = make("div", "app-block");
        appBlock.appendChild(make("p", "app-title", app));
        for (const group of groups) appBlock.appendChild(routeCard(group));
        statusBlock.appendChild(appBlock);
      }
      hostBlock.appendChild(statusBlock);
    }
    target.appendChild(hostBlock);
  }
};

const routeCard = (group) => {
  const card = make("article", "route-card");
  const header = make("div", "route-card-header");
  const title = make("div");
  title.appendChild(make("strong", "", group.name));
  const liveStatus = routeGroupStatus(group);
  title.appendChild(make("span", `badge ${statusClass(liveStatus)}`, liveStatus));
  if (group.protected) title.appendChild(make("span", "badge warn", "protected"));
  header.appendChild(title);

  const button = make("button", "small-button", group.enabled ? "Disable" : "Enable");
  button.disabled = !helperAvailable || (group.enabled && group.protected);
  button.addEventListener("click", () => toggleRouteGroup(group));
  header.appendChild(button);
  card.appendChild(header);

  const routers = make("div", "router-chip-list");
  for (const router of group.routers) routers.appendChild(make("span", "router-chip", router.name));
  card.appendChild(routers);
  card.appendChild(make("p", "file-path", group.file));
  return card;
};

const toggleRouteGroup = async (group) => {
  const action = group.enabled ? "disable" : "enable";
  if (!confirm(`Confirm ${action} for route group "${group.name}"?`)) return;
  try {
    await fetchJson(`${helperBase}/routes/toggle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group: group.name, enabled: !group.enabled, confirmed: true }),
    });
    await refreshAll();
  } catch (error) {
    showWarning(error.message);
  }
};

const renderServices = () => {
  el.servicesTable.innerHTML = "";
  for (const service of state.services) el.servicesTable.appendChild(serviceCard(service));
};

const serviceCard = (service) => {
  const row = make("article", "service-row");
  const detail = make("div");
  detail.appendChild(make("strong", "", service.label));
  detail.appendChild(make("span", "unit-name", service.unit));
  detail.appendChild(make("span", `badge ${statusClass(service.active_state)}`, service.active_state));
  if (service.sub_state) detail.appendChild(make("span", "sub-state", service.sub_state));
  row.appendChild(detail);

  const actions = make("div", "row-actions");
  for (const action of service.actions || []) {
    const button = make("button", "small-button", action);
    button.disabled = !helperAvailable;
    button.addEventListener("click", () => serviceAction(service.unit, action));
    actions.appendChild(button);
  }
  row.appendChild(actions);
  return row;
};

const serviceAction = async (unit, action) => {
  if (!confirm(`Confirm systemctl ${action} ${unit}?`)) return;
  try {
    await fetchJson(`${helperBase}/system/services/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ unit, action }),
    });
    await refreshServices();
  } catch (error) {
    showWarning(error.message);
  }
};

const renderAgentMemorySummary = () => {
  el.agentmemorySummary.innerHTML = "";
  const service = state.services.find((item) => item.unit === "agent-memory-web.service");
  if (service) el.agentmemorySummary.appendChild(serviceCard(service));
  for (const file of state.files.filter((item) => item.id.includes("agent-memory") || item.id === "route:agentmemory")) {
    const card = make("article", "mini-card");
    card.appendChild(make("strong", "", file.label));
    card.appendChild(make("span", "file-path", file.path));
    el.agentmemorySummary.appendChild(card);
  }
};

const renderLogSelect = () => {
  const selected = el.logServiceSelect.value;
  el.logServiceSelect.innerHTML = "";
  for (const service of state.services) {
    const option = document.createElement("option");
    option.value = service.unit;
    option.textContent = service.label;
    el.logServiceSelect.appendChild(option);
  }
  if (selected) el.logServiceSelect.value = selected;
};

const loadLogs = async () => {
  const unit = el.logServiceSelect.value;
  if (!unit) return;
  try {
    const response = await fetchJson(`${helperBase}/system/logs?unit=${encodeURIComponent(unit)}&lines=160`);
    el.logsOutput.textContent = response.logs || "No log output.";
  } catch (error) {
    el.logsOutput.textContent = error.message;
  }
};

const renderFileSelect = () => {
  const selected = el.fileSelect.value;
  el.fileSelect.innerHTML = "";
  for (const file of state.files) {
    const option = document.createElement("option");
    option.value = file.id;
    option.textContent = `${file.label} (${file.kind})`;
    el.fileSelect.appendChild(option);
  }
  if (selected) el.fileSelect.value = selected;
};

const loadFile = async () => {
  const id = el.fileSelect.value;
  if (!id) return;
  try {
    currentFile = await fetchJson(`${helperBase}/files/${encodeURIComponent(id)}`);
    el.fileEditor.value = currentFile.content || "";
    el.fileEditor.readOnly = !currentFile.editable;
    el.saveFileButton.disabled = !currentFile.editable;
    el.fileMeta.textContent = `${currentFile.path} | ${currentFile.editable ? "editable" : "read-only"} | ${currentFile.checksum}`;
  } catch (error) {
    showWarning(error.message);
  }
};

const saveFile = async () => {
  if (!currentFile?.editable) return;
  if (!confirm(`Save ${currentFile.path}?`)) return;
  try {
    await fetchJson(`${helperBase}/files/${encodeURIComponent(currentFile.id)}/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmed: true, content: el.fileEditor.value }),
    });
    await Promise.all([refreshRoutes(), refreshFiles(), loadFile()]);
  } catch (error) {
    showWarning(error.message);
  }
};

const refreshAll = async () => {
  await Promise.allSettled([
    refreshHelperState(),
    refreshTraefikState(),
    refreshOverview(),
    refreshRoutes(),
    refreshServices(),
    refreshFiles(),
  ]);
  renderAttention();
};

window.addEventListener("popstate", () => {
  renderModule();
});

document.querySelectorAll("[data-nav]").forEach((node) => {
  const modulePath = node.dataset.nav;
  node.href = `${mountPrefix}${modulePath === "/" ? "/" : modulePath}`;
  node.addEventListener("click", (event) => {
    event.preventDefault();
    history.pushState({}, "", node.href);
    renderModule();
  });
});

el.refreshButton.addEventListener("click", refreshAll);
el.loadLogsButton.addEventListener("click", loadLogs);
el.loadFileButton.addEventListener("click", loadFile);
el.saveFileButton.addEventListener("click", saveFile);

renderModule();
refreshAll();
