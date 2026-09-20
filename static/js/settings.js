import { api } from "./api.js";
import { state } from "./state.js";
import { esc, toast } from "./utils.js";

const PROVIDER_LABELS = { groq: "Groq", gemini: "Google Gemini" };
const PROVIDER_COLORS = { groq: "#f55036", gemini: "#4d8bf0" };
const PROVIDER_HINTS = { groq: "es. llama-3.3-70b-versatile", gemini: "es. gemini-2.5-flash" };

let onModelsChanged = () => {};

export function setOnModelsChanged(cb) { onModelsChanged = cb; }

export async function refreshModels() {
  state.models = await api.getModels();
  if (!state.currentModelId && state.models.length) {
    state.currentModelId = state.models[0].id;
  }
  onModelsChanged();
  renderSettingsModels();
}

export function providerColor(p) { return PROVIDER_COLORS[p] || "#888"; }
export function providerLabel(p) { return PROVIDER_LABELS[p] || p; }

export function renderSettingsModels() {
  const list = document.getElementById("models-list");
  if (!list) return;
  if (!state.models.length) {
    list.innerHTML = `<div class="empty-state">Nessun modello configurato. Aggiungine uno qui sotto.</div>`;
    return;
  }
  list.innerHTML = state.models.map((m) => `
    <div class="model-card" id="mc-${m.id}">
      <div class="mc-info">
        <span class="model-badge" style="background:${providerColor(m.provider)}">${esc(m.name.slice(0,2).toUpperCase())}</span>
        <div>
          <div style="font-size:13px;color:var(--text-primary)">${esc(m.name)}</div>
          <div style="font-size:11px;color:var(--text-muted)">${providerLabel(m.provider)} · ${esc(m.model_string)}</div>
        </div>
      </div>
      <div class="mc-actions">
        <button class="icon-btn" data-action="edit" data-id="${m.id}" title="Modifica">✎</button>
        <button class="icon-btn" data-action="delete" data-id="${m.id}" title="Elimina">✕</button>
      </div>
    </div>`).join("");

  list.querySelectorAll('[data-action="delete"]').forEach((btn) =>
    btn.addEventListener("click", () => deleteModel(btn.dataset.id)));
  list.querySelectorAll('[data-action="edit"]').forEach((btn) =>
    btn.addEventListener("click", () => startEdit(btn.dataset.id)));
}

let editingId = null;

function startEdit(id) {
  const m = state.models.find((x) => x.id === id);
  if (!m) return;
  editingId = id;
  document.getElementById("new-name").value = m.name;
  document.getElementById("new-prov").value = m.provider;
  document.getElementById("new-mstr").value = m.model_string;
  document.getElementById("new-key").value = "";
  document.getElementById("new-key").placeholder = "(lascia vuoto per non modificare)";
  document.getElementById("model-form-submit").textContent = "Salva modifiche";
  onProvChange();
}

export function cancelEdit() {
  editingId = null;
  document.getElementById("new-name").value = "";
  document.getElementById("new-mstr").value = "";
  document.getElementById("new-key").value = "";
  document.getElementById("new-key").placeholder = "API key";
  document.getElementById("model-form-submit").textContent = "Aggiungi modello";
}

export async function deleteModel(id) {
  const m = state.models.find((x) => x.id === id);
  if (!confirm(`Eliminare il modello "${m ? m.name : id}"? L'API key salvata verrà rimossa.`)) return;
  try {
    await api.deleteModel(id);
    if (state.currentModelId === id) state.currentModelId = null;
    await refreshModels();
    toast("Modello eliminato.", "success");
  } catch (e) {
    toast(`Eliminazione fallita: ${e.message}`, "error");
  }
}

export async function submitModelForm() {
  const name = document.getElementById("new-name").value.trim();
  const provider = document.getElementById("new-prov").value;
  const model_string = document.getElementById("new-mstr").value.trim();
  const api_key = document.getElementById("new-key").value.trim();
  if (!name || !model_string || (!editingId && !api_key)) {
    toast("Compila tutti i campi obbligatori.", "error");
    return;
  }
  try {
    if (editingId) {
      const payload = { name, provider, model_string };
      if (api_key) payload.api_key = api_key;
      await api.updateModel(editingId, payload);
      toast("Modello aggiornato.", "success");
    } else {
      await api.addModel({ name, provider, model_string, api_key });
      toast("Modello aggiunto.", "success");
    }
    cancelEdit();
    await refreshModels();
  } catch (e) {
    toast(`Salvataggio fallito: ${e.message}`, "error");
  }
}

export function onProvChange() {
  const p = document.getElementById("new-prov").value;
  document.getElementById("new-mstr").placeholder = PROVIDER_HINTS[p] || "";
}

// ---------------- DB config ----------------
export async function loadDbConfig() {
  try {
    const c = await api.getDbConfig();
    document.getElementById("db-host").value = c.db_host || "";
    document.getElementById("db-port").value = c.db_port || "";
    document.getElementById("db-name").value = c.db_name || "";
    document.getElementById("db-user").value = c.db_user || "";
  } catch (e) { /* silenzioso: pannello impostazioni non ancora aperto */ }
}

export async function saveDbConfig() {
  const payload = {
    db_host: document.getElementById("db-host").value.trim(),
    db_port: document.getElementById("db-port").value.trim(),
    db_name: document.getElementById("db-name").value.trim(),
    db_user: document.getElementById("db-user").value.trim(),
  };
  const pwd = document.getElementById("db-password").value;
  if (pwd) payload.db_password = pwd;
  try {
    await api.setDbConfig(payload);
    document.getElementById("db-password").value = "";
    toast("Configurazione DB salvata.", "success");
  } catch (e) {
    toast(`Salvataggio fallito: ${e.message}`, "error");
  }
}

export async function testDbConnection() {
  const pill = document.getElementById("db-pill");
  pill.textContent = "Verifica…";
  pill.className = "status-pill pending";
  try {
    const d = await api.testDb();
    pill.textContent = d.message;
    // Una connessione riuscita con lo schema incompleto NON e' "tutto a posto":
    // e' esattamente lo stato in cui l'applicazione e' rimasta per tre mesi
    // senza che nessuno se ne accorgesse. Il pill deve dirlo.
    let classe = d.status === "ok" ? "ok" : "error";
    if (d.schema_ok === false) {
      classe = "error";
      const mancanti = (d.colonne_mancanti || []).join(", ");
      pill.textContent = `Schema di query_log incompleto: mancano ${mancanti}`;
    }
    pill.className = `status-pill ${classe}`;
  } catch (e) {
    pill.textContent = e.message;
    pill.className = "status-pill error";
  }
}
