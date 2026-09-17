import { initThemeToggle } from "./theme.js";
import { state } from "./state.js";
import { esc, toast } from "./utils.js";
import * as settings from "./settings.js";
import { initChat, handleComposerKey, setQuestionText, clearChatUI, cancelCurrentRequest, renderRestoredConversation } from "./chat.js";
import { startNewChat, getSessionsIndex, loadSession, removeSessionFromIndex } from "./conversation.js";
import * as sidebarLogs from "./sidebar_logs.js";
import * as benchmark from "./benchmark.js";
import { loadAnalytics } from "./analytics.js";
import { api } from "./api.js";

document.addEventListener("DOMContentLoaded", init);

async function init() {
  initThemeToggle(document.getElementById("theme-switch"));

  initChat({
    chatInner: document.getElementById("chat-inner"),
    composerInput: document.getElementById("q-input"),
    sendBtn: document.getElementById("send-btn"),
  });

  document.getElementById("q-input").addEventListener("keydown", handleComposerKey);
  document.getElementById("q-input").addEventListener("input", (e) => resizeTA(e.target));
  // Nota: il click sul pulsante di invio NON viene collegato qui. La gestione
  // del pulsante (invio vs. annulla, a seconda che una richiesta sia in corso)
  // è centralizzata in setComposerBusy() dentro chat.js, che imposta
  // sendBtn.onclick dinamicamente. Aggiungere qui un secondo listener causava
  // una doppia esecuzione di sendQuestion() ad ogni invio (due chiamate LLM
  // parallele sullo stesso turno).
  document.getElementById("btn-new-chat").addEventListener("click", onNewChat);

  wireModelPopover();
  wireSettingsModal();
  wireBenchmarkModal();
  wireAnalyticsModal();
  wireLogSidebar();
  wireModalOverlayClicks();

  settings.setOnModelsChanged(renderModelPill);
  await settings.refreshModels();
  await startNewChat();
  renderSessionsList();
  renderWelcome(true);
}

function resizeTA(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}

// ---------------- Welcome / suggerimenti ----------------
const DEFAULT_SUGGESTIONS = [
  "Quante righe ci sono nella tabella pcapinfo?",
  "Quali sono gli impianti che sono stati analizzati?",
  "Linee con più dispositivi PROFINET",
];

function renderWelcome(isFirst) {
  const inner = document.getElementById("chat-inner");
  inner.innerHTML = `
    <div class="welcome" id="welcome">
      <div class="welcome-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.6">
          <circle cx="12" cy="5" r="2.2"/><circle cx="5" cy="18" r="2.2"/><circle cx="19" cy="18" r="2.2"/>
          <path d="M10.8 6.9 6.2 16.1M13.2 6.9l4.6 9.2M7.2 18h9.6"/>
        </svg>
      </div>
      <h1>${isFirst ? "Ciao 👋" : "Nuova conversazione"}</h1>
      <p>${isFirst
        ? "Fai una domanda sui dati di rete PROFINET raccolti dagli impianti."
        : "La memoria di questa chat è indipendente da quelle precedenti."}</p>
      <div id="suggestions-box"></div>
    </div>`;
  renderSuggestionChips(DEFAULT_SUGGESTIONS);
  loadSuggestions();
}

function renderSuggestionChips(questions) {
  const box = document.getElementById("suggestions-box");
  if (!box) return;
  box.innerHTML = questions.map((q) =>
    `<span class="suggestion-chip" data-q="${esc(q)}">${esc(q)}</span>`).join("");
  box.querySelectorAll(".suggestion-chip").forEach((chip) =>
    chip.addEventListener("click", () => setQuestionText(chip.dataset.q)));
}

async function loadSuggestions() {
  try {
    const items = await api.getSuggestions();
    if (Array.isArray(items) && items.length) {
      renderSuggestionChips(items.map((it) => it.question).slice(0, 6));
    }
  } catch { /* pannello suggerimenti opzionale, errore silenzioso */ }
}

async function onNewChat() {
  cancelCurrentRequest();
  clearChatUI();
  await startNewChat();
  renderWelcome(false);
  renderSessionsList();
}

// ---------------- Sidebar conversazioni ----------------
function renderSessionsList() {
  const list = document.getElementById("sessions");
  const sessions = getSessionsIndex();
  list.innerHTML = sessions.length ? sessions.map((s) => `
    <div class="session ${s.id === state.sessionId ? "active" : ""}" data-id="${esc(s.id)}" title="${esc(s.label || "")}">
      <span class="session-label">${esc(s.label || "Conversazione")}</span>
      <button class="session-del" title="Rimuovi dall'elenco" data-id="${esc(s.id)}">✕</button>
    </div>
  `).join("") : `<div class="empty-state">Nessuna conversazione recente.</div>`;

  list.querySelectorAll(".session").forEach((el) =>
    el.addEventListener("click", () => openSession(el.dataset.id)));
  list.querySelectorAll(".session-del").forEach((btn) =>
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeSessionFromIndex(btn.dataset.id);
      renderSessionsList();
    }));
}
window.addEventListener("lynx:session-touched", renderSessionsList);

async function openSession(sessionId) {
  if (sessionId === state.sessionId) return;
  if (state.currentJobId) { toast("Attendi la fine della richiesta in corso.", "info"); return; }
  try {
    const turns = await loadSession(sessionId);
    if (!turns.length) {
      toast("Nessun turno salvato per questa conversazione (forse è stata solo iniziata).", "info");
    }
    renderRestoredConversation(turns);
    renderSessionsList();
  } catch (e) {
    toast(`Impossibile riaprire la conversazione: ${e.message}`, "error");
  }
}

// ---------------- Model pill / popover ----------------
function renderModelPill() {
  const pill = document.getElementById("model-pill");
  const m = state.models.find((x) => x.id === state.currentModelId);
  if (!m) { pill.innerHTML = `<span>Nessun modello</span>`; return; }
  pill.innerHTML = `
    <span class="model-badge" style="background:${settings.providerColor(m.provider)}">${esc(m.name.slice(0, 2).toUpperCase())}</span>
    <span>${esc(m.name)}</span>
    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" opacity="0.6"><polyline points="6 9 12 15 18 9"/></svg>`;
}

function wireModelPopover() {
  const pill = document.getElementById("model-pill");
  const pop = document.getElementById("model-pop");
  pill.addEventListener("click", (e) => {
    e.stopPropagation();
    pop.style.display = pop.style.display === "none" ? "block" : "none";
    if (pop.style.display === "block") renderModelPop();
  });
  document.addEventListener("click", () => { pop.style.display = "none"; });
}

function renderModelPop() {
  const pop = document.getElementById("model-pop");
  const groups = {};
  state.models.forEach((m) => { (groups[m.provider] = groups[m.provider] || []).push(m); });
  pop.innerHTML = Object.entries(groups).map(([prov, ms]) => `
    <div class="model-pop-group-label">${settings.providerLabel(prov)}</div>
    ${ms.map((m) => `
      <div class="model-pop-item ${m.id === state.currentModelId ? "selected" : ""}" data-id="${m.id}">
        <span class="model-badge" style="background:${settings.providerColor(m.provider)}">${esc(m.name.slice(0, 2).toUpperCase())}</span>
        ${esc(m.name)}
      </div>`).join("")}
  `).join("") || `<div class="empty-state">Nessun modello. Aprire Impostazioni.</div>`;
  pop.querySelectorAll(".model-pop-item").forEach((item) =>
    item.addEventListener("click", () => {
      state.currentModelId = item.dataset.id;
      renderModelPill();
      pop.style.display = "none";
    }));
}

// ---------------- Settings modal ----------------
function wireSettingsModal() {
  document.getElementById("btn-open-settings").addEventListener("click", async () => {
    document.getElementById("settings-modal-overlay").classList.remove("hidden");
    await settings.loadDbConfig();
  });
  document.getElementById("settings-modal-close").addEventListener("click", () =>
    document.getElementById("settings-modal-overlay").classList.add("hidden"));

  document.querySelectorAll("#settings-modal .modal-tab").forEach((tab) =>
    tab.addEventListener("click", () => switchModalTab("settings-modal", tab.dataset.tab)));

  document.getElementById("new-prov").addEventListener("change", settings.onProvChange);
  document.getElementById("model-form-submit").addEventListener("click", settings.submitModelForm);
  document.getElementById("model-form-cancel").addEventListener("click", settings.cancelEdit);

  document.getElementById("db-save").addEventListener("click", settings.saveDbConfig);
  document.getElementById("db-test").addEventListener("click", settings.testDbConnection);
}

function switchModalTab(modalId, tabName) {
  document.querySelectorAll(`#${modalId} .modal-tab`).forEach((t) => t.classList.toggle("active", t.dataset.tab === tabName));
  document.querySelectorAll(`#${modalId} .modal-tab-content`).forEach((c) => c.classList.toggle("active", c.dataset.tab === tabName));
}

// ---------------- Benchmark modal ----------------
function wireBenchmarkModal() {
  document.getElementById("btn-open-benchmark").addEventListener("click", () => {
    document.getElementById("benchmark-modal-overlay").classList.remove("hidden");
    benchmark.renderModelCheckboxes("bench-models");
    benchmark.renderModelCheckboxes("bench-conv-models");
  });
  document.getElementById("benchmark-modal-close").addEventListener("click", () =>
    document.getElementById("benchmark-modal-overlay").classList.add("hidden"));
  document.querySelectorAll("#benchmark-modal .modal-tab").forEach((tab) =>
    tab.addEventListener("click", () => switchModalTab("benchmark-modal", tab.dataset.tab)));

  document.getElementById("bench-run").addEventListener("click", benchmark.runOneshotBenchmark);
  document.getElementById("scenario-add-row").addEventListener("click", benchmark.addScenarioRow);
  document.getElementById("bench-conv-run").addEventListener("click", benchmark.runConversationalBenchmark);
  benchmark.addScenarioRow();
  benchmark.addScenarioRow();
}

// ---------------- Analytics modal ----------------
function wireAnalyticsModal() {
  document.getElementById("btn-open-analytics").addEventListener("click", () => {
    document.getElementById("analytics-modal-overlay").classList.remove("hidden");
    loadAnalytics();
  });
  document.getElementById("analytics-modal-close").addEventListener("click", () =>
    document.getElementById("analytics-modal-overlay").classList.add("hidden"));
}

// Chiusura modali cliccando sullo sfondo (comportamento atteso nelle app moderne).
function wireModalOverlayClicks() {
  document.querySelectorAll(".modal-overlay").forEach((overlay) => {
    overlay.addEventListener("mousedown", (e) => {
      if (e.target === overlay) overlay.classList.add("hidden");
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll(".modal-overlay:not(.hidden)").forEach((o) => o.classList.add("hidden"));
    }
  });
}

// ---------------- Log sidebar / layout collapse ----------------
function wireLogSidebar() {
  document.getElementById("btn-toggle-left").addEventListener("click", () => {
    document.querySelector(".app-shell").classList.toggle("left-collapsed");
  });
  document.getElementById("btn-toggle-right").addEventListener("click", () => {
    document.querySelector(".app-shell").classList.toggle("right-collapsed");
  });
  document.querySelectorAll(".log-tab").forEach((tab) =>
    tab.addEventListener("click", () => sidebarLogs.switchLogTab(tab.dataset.tab)));
  document.getElementById("log-modal-close").addEventListener("click", sidebarLogs.closeLogModal);
  sidebarLogs.refreshLogs();
}
