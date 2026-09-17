import { api } from "./api.js";
import { esc } from "./utils.js";

let activeTab = "session"; // 'session' | 'benchmark'
let currentLogFilename = null;

export function switchLogTab(tab) {
  activeTab = tab;
  document.querySelectorAll(".log-tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === tab));
  document.getElementById("log-list-session").classList.toggle("active", tab === "session");
  document.getElementById("log-list-benchmark").classList.toggle("active", tab === "benchmark");
  if (tab === "session") refreshLogs(); else refreshBenchmarkLogs();
}

export async function refreshLogs() {
  const list = document.getElementById("log-list-session");
  try {
    const logs = await api.getLogs();
    list.innerHTML = logs.length ? logs.map(renderLogEntry("le-", openLogModal)).join("")
      : `<div class="empty-state">Nessun log ancora.</div>`;
    attachEntryHandlers(list, "le-", openLogModal);
  } catch { list.innerHTML = `<div class="empty-state">Errore caricamento log.</div>`; }
}

export async function refreshBenchmarkLogs() {
  const list = document.getElementById("log-list-benchmark");
  try {
    const logs = await api.getBenchmarkLogs();
    list.innerHTML = logs.length ? logs.map(renderLogEntry("leb-", openBenchmarkLogModal)).join("")
      : `<div class="empty-state">Nessun benchmark ancora.</div>`;
    attachEntryHandlers(list, "leb-", openBenchmarkLogModal);
  } catch { list.innerHTML = `<div class="empty-state">Errore caricamento log.</div>`; }
}

function renderLogEntry(prefix) {
  return (log) => {
    const label = log.filename.replace(/^\d{8}_\d{6}_(benchmark_)?/, "").replace(/_/g, " ").replace(".txt", "");
    const date = log.modified.replace("T", " ").substring(0, 16);
    const kb = (log.size_bytes / 1024).toFixed(1);
    return `<div class="log-entry" id="${prefix}${esc(log.filename)}" data-filename="${esc(log.filename)}">
      <div class="log-name">${esc(label)}</div>
      <div class="log-meta">${date} · ${kb} KB</div>
    </div>`;
  };
}

function attachEntryHandlers(list, prefix, openFn) {
  list.querySelectorAll(".log-entry").forEach((el) =>
    el.addEventListener("click", () => openFn(el.dataset.filename)));
}

async function openLogModal(filename) {
  currentLogFilename = filename;
  document.querySelectorAll('[id^="le-"]').forEach((e) => e.classList.remove("active"));
  const entry = document.getElementById("le-" + filename);
  if (entry) entry.classList.add("active");
  showLogModalShell();
  try {
    const d = await api.getLogContent(filename);
    document.getElementById("log-modal-content").textContent = d.content;
  } catch { document.getElementById("log-modal-content").textContent = "Errore nel caricamento del log."; }
  document.getElementById("log-modal-download").onclick = () => window.open(api.logDownloadUrl(filename), "_blank");
}

async function openBenchmarkLogModal(filename) {
  currentLogFilename = filename;
  document.querySelectorAll('[id^="leb-"]').forEach((e) => e.classList.remove("active"));
  const entry = document.getElementById("leb-" + filename);
  if (entry) entry.classList.add("active");
  showLogModalShell();
  try {
    const d = await api.getBenchmarkLogContent(filename);
    document.getElementById("log-modal-content").textContent = d.content;
  } catch { document.getElementById("log-modal-content").textContent = "Errore nel caricamento del log."; }
  document.getElementById("log-modal-download").onclick = () => window.open(api.benchmarkLogDownloadUrl(filename), "_blank");
}

function showLogModalShell() {
  document.getElementById("log-modal-overlay").classList.remove("hidden");
}

export function closeLogModal() {
  document.getElementById("log-modal-overlay").classList.add("hidden");
}

export function toggleLogSidebar() {
  document.getElementById("sidebar-right").classList.toggle("collapsed");
}

// Apertura log direttamente da un turno di chat (evento da chat.js)
window.addEventListener("lynx:open-log", (e) => {
  openLogModal(e.detail.filename);
});

// Aggiornamento automatico della lista quando un turno o un benchmark
// producono nuovi file (evento da chat.js / benchmark.js): prima la sidebar
// restava stantia finché non si cambiava tab manualmente.
window.addEventListener("lynx:logs-refresh", () => {
  if (activeTab === "session") refreshLogs(); else refreshBenchmarkLogs();
});
