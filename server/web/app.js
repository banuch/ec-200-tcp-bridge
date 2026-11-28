"use strict";

// All text from the server (device names, meter values, errors) is put on
// the page with textContent, never as HTML.

const REFRESH_MS = 5000;
const HISTORY_LABELS = ["Voltage", "Phase current", "Active power", "Import active energy"];

let devices = [];
let modulePort = 4059;
let serverOffset = 0;          // server clock - browser clock, seconds
let selected = null;           // name of the device shown in the detail panel
let selectedJobRunning = false;
let editing = null;            // name being edited, null = adding

const $ = (id) => document.getElementById(id);

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (key === "className") node.className = value;
      else if (key === "onclick") node.addEventListener("click", value);
      else if (key === "title") node.title = value;
      else node.setAttribute(key, value);
    }
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

// ---------------------------------------------------------------- API

async function api(method, path, body) {
  const options = { method, credentials: "same-origin", headers: {} };
  // The server only accepts JSON POSTs (blocks cross-site form posts)
  if (method === "POST" && body === undefined) body = {};
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  // location.origin has no user:password, so this also works when the page
  // was opened as http://admin:password@host:8080/
  const response = await fetch(location.origin + path, options);
  let data = {};
  try { data = await response.json(); } catch (e) { /* empty body */ }
  if (!response.ok) {
    throw new Error(data.error || ("HTTP " + response.status));
  }
  return data;
}

function showMessage(text, kind) {
  const box = $("message");
  box.textContent = text;
  box.className = "message" + (kind ? " " + kind : "");
  box.hidden = false;
  clearTimeout(showMessage.timer);
  showMessage.timer = setTimeout(() => { box.hidden = true; }, 6000);
}

// ---------------------------------------------------------------- formatting

function nowServer() { return Date.now() / 1000 + serverOffset; }

function ago(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return seconds + " s ago";
  if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
  if (seconds < 86400) return (seconds / 3600).toFixed(1) + " h ago";
  return (seconds / 86400).toFixed(1) + " d ago";
}

function inTime(seconds) {
  seconds = Math.round(seconds);
  if (seconds <= 0) return "due now";
  if (seconds < 60) return "in " + seconds + " s";
  return "in " + Math.round(seconds / 60) + " min";
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return d + "d " + h + "h";
  if (h) return h + "h " + m + "m";
  return m + "m";
}

function localTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  return isNaN(date) ? iso : date.toLocaleString();
}

function signal(csq) {
  if (csq === null || csq === undefined || csq === 99) return "—";
  const dbm = -113 + 2 * csq;
  const word = csq >= 20 ? "good" : csq >= 15 ? "fair" : csq >= 10 ? "weak" : "poor";
  return csq + " (" + dbm + " dBm, " + word + ")";
}

function pill(text, kind) {
  return el("span", { className: "pill" + (kind ? " " + kind : "") }, text);
}

function valueText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    if (value.hex) return value.hex;
    return JSON.stringify(value);
  }
  return String(value);
}

// ---------------------------------------------------------------- devices

function statusCell(d) {
  const cell = el("td");
  const hb = d.heartbeat;
  if (!hb) cell.append(pill("No heartbeat"));
  else if (hb.online) cell.append(pill("Online", "ok"));
  else cell.append(pill("Offline", "bad"));
  if (hb) cell.append(el("span", { className: "sub-line" }, "beat " + ago(hb.age) + (hb.signed ? "" : " · unsigned")));
  if (d.job.running) cell.append(el("span", { className: "sub-line" }, pill(d.job.what + "…", "warn")));
  else if (d.pc) cell.append(el("span", { className: "sub-line" }, "PC piped"));
  else if (d.link !== null) cell.append(el("span", { className: "sub-line" }, "link #" + d.link + " open"));
  return cell;
}

function lastReadCell(d) {
  const cell = el("td");
  const last = d.last_reading;
  if (!last) { cell.append("never"); return cell; }
  cell.append(pill(last.ok ? "OK" : "Failed", last.ok ? "ok" : "bad"), " ", localTime(last.read_at));
  if (!last.ok && last.error) cell.append(el("span", { className: "sub-line" }, last.error));
  if (d.job.ok === false && d.job.error && !d.job.running) {
    cell.append(el("span", { className: "sub-line" }, "last try: " + d.job.error));
  }
  return cell;
}

function renderDevices() {
  const body = $("devices-body");
  body.replaceChildren();
  $("no-devices").hidden = devices.length > 0;
  for (const d of devices) {
    const busy = Boolean(d.busy);
    const row = el("tr", { className: "clickable" + (d.name === selected ? " selected" : "") },
      el("td", null, el("strong", null, d.name)),
      statusCell(d),
      el("td", { className: "mono" }, "[" + d.host + "]:" + d.port),
      el("td", null, signal(d.heartbeat ? d.heartbeat.csq : null)),
      el("td", null, d.heartbeat ? duration(d.heartbeat.up) : "—"),
      lastReadCell(d),
      el("td", null, d.next_read_at ? inTime(d.next_read_at - nowServer()) : "—"),
      el("td", { className: "actions-cell" },
        el("button", { className: "small", onclick: (e) => { e.stopPropagation(); readNow(d.name); },
                       title: busy ? d.busy : "Run a full meter read now" }, "Read now"),
        el("button", { className: "small secondary", onclick: (e) => { e.stopPropagation(); openForm(d); } }, "Edit"),
        el("button", { className: "small danger", onclick: (e) => { e.stopPropagation(); removeDevice(d.name); } }, "Delete")));
    if (busy) row.querySelector("button").disabled = true;
    row.addEventListener("click", () => openDetail(d.name));
    body.append(row);
  }
}

async function refresh() {
  try {
    const data = await api("GET", "/api/devices");
    serverOffset = data.now - Date.now() / 1000;
    modulePort = data.module_port;
    devices = data.devices;
    renderDevices();
    $("updated").textContent = "updated " + new Date().toLocaleTimeString();
    if (selected) updateDetailJob();
  } catch (error) {
    $("updated").textContent = "server not reachable: " + error.message;
  }
}

async function readNow(name) {
  try {
    await api("POST", "/api/devices/" + encodeURIComponent(name) + "/read");
    showMessage("Read of " + name + " started. It takes about 20–60 s.", "ok");
    if (name !== selected) openDetail(name);
  } catch (error) {
    showMessage("Cannot read " + name + ": " + error.message, "bad");
  }
  refresh();
}

async function removeDevice(name) {
  if (!confirm("Delete device " + name + "?\nIts stored readings stay in the database.")) return;
  try {
    await api("DELETE", "/api/devices/" + encodeURIComponent(name));
    showMessage(name + " deleted.", "ok");
    if (selected === name) closeDetail();
  } catch (error) {
    showMessage("Delete failed: " + error.message, "bad");
  }
  refresh();
}

// ---------------------------------------------------------------- add / edit form

function openForm(device) {
  editing = device ? device.name : null;
  $("form-title").textContent = device ? "Edit " + device.name : "Add device";
  $("f-name").value = device ? device.name : "";
  $("f-name").readOnly = Boolean(device);
  $("f-host").value = device ? device.host : "";
  $("f-port").value = device ? device.port : modulePort;
  $("device-form").hidden = false;
  (device ? $("f-host") : $("f-name")).focus();
}

function closeForm() {
  $("device-form").hidden = true;
  editing = null;
}

async function submitForm(event) {
  event.preventDefault();
  const body = {
    name: $("f-name").value.trim(),
    host: $("f-host").value.trim(),
    port: Number($("f-port").value),
  };
  try {
    await api("POST", "/api/devices", body);
    showMessage((editing ? "Saved " : "Added ") + body.name + ".", "ok");
    closeForm();
  } catch (error) {
    showMessage("Not saved: " + error.message, "bad");
  }
  refresh();
}

// ---------------------------------------------------------------- detail panel

function openDetail(name) {
  selected = name;
  $("detail").hidden = false;
  $("detail-title").textContent = name;
  $("rows-box").hidden = true;
  renderDevices();
  updateDetailJob();
  loadReading();
  loadProfiles();
  $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeDetail() {
  selected = null;
  $("detail").hidden = true;
  renderDevices();
}

function updateDetailJob() {
  const d = devices.find((x) => x.name === selected);
  if (!d) { closeDetail(); return; }
  const job = d.job;
  let text = "";
  if (job.running) {
    text = job.what + " running for " + Math.round(nowServer() - job.started) + " s…";
  } else if (job.finished) {
    text = "Last " + (job.what || "read") + " " + ago(nowServer() - job.finished) +
      (job.ok ? ": OK" : ": " + (job.error || "failed"));
  }
  $("detail-job").textContent = text;
  $("detail-read").disabled = Boolean(d.busy);
  $("detail-scan").disabled = Boolean(d.busy);
  // When a read or scan finishes, load the new data
  if (selectedJobRunning && !job.running) {
    loadReading();
    loadProfiles();
  }
  selectedJobRunning = job.running;
}

async function loadReading() {
  const name = selected;
  let data;
  try {
    data = await api("GET", "/api/devices/" + encodeURIComponent(name) + "/reading");
  } catch (error) {
    $("reading-time").textContent = "Could not load values: " + error.message;
    return;
  }
  if (name !== selected) return;
  renderLatest(data.latest);
  renderHistory(data.history);
}

function renderLatest(latest) {
  const box = $("reading-groups");
  box.replaceChildren();
  if (!latest) {
    $("reading-time").textContent = "No successful read stored yet. Press “Read now”.";
    return;
  }
  $("reading-time").textContent = "Read at " + localTime(latest.read_at);
  const groups = new Map();
  for (const [group, label, obis, text] of latest.values) {
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push([label, obis, text]);
  }
  for (const [group, items] of groups) {
    const dl = el("dl");
    for (const [label, obis, text] of items) {
      // "n/a (Access Error : ...)" -> "n/a", full meter error in the tooltip
      const na = String(text).startsWith("n/a");
      dl.append(el("dt", { title: obis }, label),
        el("dd", { className: na ? "na" : "", title: na ? obis + ": " + text : obis }, na ? "n/a" : text));
    }
    box.append(el("div", { className: "group" }, el("h4", null, group), dl));
  }
}

function renderHistory(history) {
  const table = $("history");
  table.replaceChildren();
  if (!history || !history.length) return;
  table.append(el("thead", null, el("tr", null,
    el("th", null, "Time"), el("th", null, "Result"),
    ...HISTORY_LABELS.map((label) => el("th", null, label)))));
  const body = el("tbody");
  for (const reading of history) {
    const byLabel = new Map(reading.values.map((v) => [v[1], v[3]]));
    body.append(el("tr", null,
      el("td", null, localTime(reading.read_at)),
      el("td", null, reading.ok ? pill("OK", "ok") : pill("Failed", "bad")),
      ...(reading.ok
        ? HISTORY_LABELS.map((label) => {
            const text = byLabel.get(label) ?? "—";
            const na = String(text).startsWith("n/a");
            return el("td", { className: na ? "muted" : "", title: na ? text : "" }, na ? "n/a" : text);
          })
        : [el("td", { className: "wrap", colspan: String(HISTORY_LABELS.length) }, reading.error || "")])));
  }
  table.append(body);
}

async function loadProfiles() {
  const name = selected;
  let data;
  try {
    data = await api("GET", "/api/devices/" + encodeURIComponent(name) + "/profiles");
  } catch (error) {
    showMessage("Could not load profiles: " + error.message, "bad");
    return;
  }
  if (name !== selected) return;
  const table = $("profiles");
  table.replaceChildren();
  $("no-profiles").hidden = data.profiles.length > 0;
  if (!data.profiles.length) return;
  table.append(el("thead", null, el("tr", null,
    ...["OBIS", "Type", "Enabled", "Status", "Entries", "Last capture", ""].map((h) => el("th", null, h)))));
  const body = el("tbody");
  for (const p of data.profiles) {
    const kind = p.status === "complete" ? "ok" : (p.status === "never" || !p.status) ? "" : "bad";
    const statusCell = el("td", { className: "wrap" }, pill(p.status || "never", kind));
    if (p.error) statusCell.append(el("span", { className: "sub-line" }, p.error));
    const toggle = el("input", { type: "checkbox", title: "Collect this profile on every read" });
    toggle.checked = Boolean(p.enabled);
    toggle.addEventListener("change", () => setProfile(p.obis, toggle.checked, toggle));
    body.append(el("tr", null,
      el("td", { className: "mono" }, p.obis),
      el("td", null, p.profile_type),
      el("td", null, toggle),
      statusCell,
      el("td", null, p.entries_in_use ?? "—"),
      el("td", null, localTime(p.last_capture_at)),
      el("td", { className: "actions-cell" },
        el("button", { className: "small secondary", onclick: () => loadRows(p.obis) }, "Rows"))));
  }
  table.append(body);
}

async function setProfile(obis, enabled, checkbox) {
  try {
    await api("POST", "/api/devices/" + encodeURIComponent(selected) + "/profiles/" + obis, { enabled });
    showMessage("Profile " + obis + (enabled ? " enabled" : " disabled") + ".", "ok");
  } catch (error) {
    checkbox.checked = !enabled;
    showMessage("Not changed: " + error.message, "bad");
  }
}

async function loadRows(obis) {
  let data;
  try {
    data = await api("GET", "/api/devices/" + encodeURIComponent(selected) +
      "/profiles/" + obis + "/rows?limit=50");
  } catch (error) {
    showMessage("Could not load rows: " + error.message, "bad");
    return;
  }
  $("rows-box").hidden = false;
  $("rows-title").textContent = "Profile " + obis + " — newest " + data.rows.length + " stored rows";
  const table = $("rows");
  table.replaceChildren();
  if (!data.rows.length) {
    table.append(el("tbody", null, el("tr", null, el("td", { className: "muted" },
      "No rows stored for this profile yet."))));
    return;
  }
  // Header from the stored columns, else from the first row's values
  const first = data.rows[0].values || [];
  const names = new Map(data.columns.map((c) => [c.position, c.description || c.obis]));
  const head = el("tr");
  for (const v of first) {
    const title = (names.get(v.position) || v.obis || "#" + v.position) + (v.unit ? " (" + v.unit + ")" : "");
    head.append(el("th", { title: v.obis || "" }, title));
  }
  table.append(el("thead", null, head));
  const body = el("tbody");
  for (const row of data.rows) {
    const tr = el("tr");
    for (const v of row.values || []) tr.append(el("td", null, valueText(v.value)));
    body.append(tr);
  }
  table.append(body);
  $("rows-box").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function scanProfiles() {
  try {
    await api("POST", "/api/devices/" + encodeURIComponent(selected) + "/scan");
    showMessage("Profile scan of " + selected + " started.", "ok");
  } catch (error) {
    showMessage("Cannot scan: " + error.message, "bad");
  }
  refresh();
}

// ---------------------------------------------------------------- start

$("add-btn").addEventListener("click", () => openForm(null));
$("form-cancel").addEventListener("click", closeForm);
$("device-form").addEventListener("submit", submitForm);
$("detail-close").addEventListener("click", closeDetail);
$("detail-read").addEventListener("click", () => readNow(selected));
$("detail-scan").addEventListener("click", scanProfiles);
$("rows-close").addEventListener("click", () => { $("rows-box").hidden = true; });

refresh();
setInterval(refresh, REFRESH_MS);
