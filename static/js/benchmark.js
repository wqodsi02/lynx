import { api } from "./api.js";
import { state } from "./state.js";
import { esc, fmtMs, fmtNL, highlightSQL, toast } from "./utils.js";

export function renderModelCheckboxes(containerId) {
  const c = document.getElementById(containerId);
  c.innerHTML = state.models.map((m) => `
    <label class="bench-model-check">
      <input type="checkbox" value="${m.id}" checked> ${esc(m.name)} <span style="color:var(--text-muted)">(${m.provider})</span>
    </label>`).join("") || `<div class="empty-state">Nessun modello configurato.</div>`;
}

function selectedModelIds(containerId) {
  return Array.from(document.querySelectorAll(`#${containerId} input:checked`)).map((i) => i.value);
}

function notifyBenchmarkLogsChanged() {
  window.dispatchEvent(new CustomEvent("lynx:logs-refresh"));
}

export async function runOneshotBenchmark() {
  const question = document.getElementById("bench-question").value.trim();
  const modelIds = selectedModelIds("bench-models");
  const out = document.getElementById("bench-result");
  const btn = document.getElementById("bench-run");
  if (!question || !modelIds.length) { toast("Inserisci una domanda e seleziona almeno un modello.", "error"); return; }
  btn.disabled = true;
  out.innerHTML = `<div class="thinking"><span class="spinner"></span> Esecuzione benchmark su ${modelIds.length} modelli… (i modelli vengono eseguiti in sequenza, può richiedere qualche minuto)</div>`;
  try {
    const data = await api.runBenchmark({ question, model_ids: modelIds });
    out.innerHTML = renderOneshotTable(data.results) + renderOneshotDetails(data.results);
    wireDetailToggles(out);
    notifyBenchmarkLogsChanged();
  } catch (e) {
    out.innerHTML = `<div class="step-label" style="color:var(--danger)">Errore: ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

/** Byte LLM (richiesta + risposta) di una riga di risultato. Restituisce "—"
 *  quando nessuno dei due e' stato misurato: i run precedenti alla misurazione
 *  dei transfer rate non hanno questi campi, e uno zero mentirebbe. */
function byteLlm(x) {
  if (x.llm_bytes_request == null && x.llm_bytes_response == null) return "—";
  return ((x.llm_bytes_request ?? 0) + (x.llm_bytes_response ?? 0)).toLocaleString("it-IT");
}

function renderOneshotTable(results) {
  const rows = results.map((r) => `
    <tr>
      <td>${esc(r.model_name || r.model_id)}</td>
      <td>${esc(r.provider || "-")}</td>
      <td>${r.error ? `<span style="color:var(--danger)">errore</span>` : fmtMs(r.latency_sql)}</td>
      <td>${r.error ? "—" : fmtMs(r.latency_db)}</td>
      <td>${r.error ? "—" : fmtMs(r.latency_nl)}</td>
      <td>${r.error ? "—" : fmtMs(r.latency_ms)}</td>
      <td>${r.error ? "—" : (r.tokens_total ?? "—")}</td>
      <td>${r.error ? "—" : (r.rows_count ?? "—")}</td>
      <td>${byteLlm(r)}</td>
    </tr>`).join("");
  return `
    <table class="bench-table">
      <tr><th>Modello</th><th>Provider</th><th>SQL</th><th>DB</th><th>NL</th><th>Tot</th><th>Token</th><th>Righe</th><th>Byte LLM</th></tr>
      ${rows}
    </table>`;
}

/** Dettaglio espandibile per modello: query SQL generata e risposta in
 *  linguaggio naturale, senza dover aprire il file di log per confrontare
 *  la QUALITÀ delle risposte oltre ai tempi. */
function renderOneshotDetails(results) {
  return results.map((r, i) => `
    <div class="bench-detail">
      <button class="bench-detail-toggle" data-target="bd-${i}">
        <span class="chev">▸</span> ${esc(r.model_name || r.model_id)} — dettaglio risposta
      </button>
      <div class="bench-detail-body hidden" id="bd-${i}">
        ${r.error
          ? `<div class="step-label" style="color:var(--danger)">${esc(r.error)}</div>`
          : `
            <div class="step-label">Query SQL generata</div>
            <div class="sql-block"><div class="sql-code">${highlightSQL(r.sql || "")}</div></div>
            <div class="step-label">Risposta in linguaggio naturale</div>
            <div class="answer">${fmtNL(r.nl_response || "(nessuna)")}</div>`}
      </div>
    </div>`).join("");
}

function wireDetailToggles(root) {
  root.querySelectorAll(".bench-detail-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      const body = document.getElementById(btn.dataset.target);
      const isHidden = body.classList.toggle("hidden");
      btn.querySelector(".chev").textContent = isHidden ? "▸" : "▾";
    }));
}

// ---------------- Benchmark conversazionale ----------------
export function addScenarioRow() {
  const c = document.getElementById("scenario-rows");
  const row = document.createElement("div");
  row.className = "scenario-row";
  row.innerHTML = `<textarea placeholder="Domanda del turno…"></textarea><button class="icon-btn" data-act="remove" title="Rimuovi turno">✕</button>`;
  row.querySelector('[data-act="remove"]').addEventListener("click", () => row.remove());
  c.appendChild(row);
}

export async function runConversationalBenchmark() {
  const questions = Array.from(document.querySelectorAll("#scenario-rows textarea"))
    .map((t) => t.value.trim()).filter(Boolean);
  const modelIds = selectedModelIds("bench-conv-models");
  const out = document.getElementById("bench-conv-result");
  const btn = document.getElementById("bench-conv-run");
  if (!questions.length || !modelIds.length) {
    toast("Aggiungi almeno una domanda allo scenario e seleziona almeno un modello.", "error");
    return;
  }
  btn.disabled = true;
  out.innerHTML = `<div class="thinking"><span class="spinner"></span> Esecuzione scenario (${questions.length} turni) su ${modelIds.length} modelli…</div>`;
  try {
    const data = await api.runBenchmarkConversation({ questions, model_ids: modelIds });
    out.innerHTML = renderConversationalResults(data.results);
    notifyBenchmarkLogsChanged();
  } catch (e) {
    out.innerHTML = `<div class="step-label" style="color:var(--danger)">Errore: ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

function renderConversationalResults(results) {
  return results.map((r) => {
    const rows = (r.turns || []).map((t, i) => `
      <tr>
        <td>#${i + 1}</td>
        <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis">${esc(t.question)}</td>
        <td>${t.error ? `<span style="color:var(--danger)">${esc(t.error)}</span>` :
        `${fmtMs(t.latency_sql)} / ${fmtMs(t.latency_db)} / ${fmtMs(t.latency_nl)}`}</td>
        <td>${t.error ? "—" : (t.tokens_total ?? "—")}</td>
        <td>${t.error ? "—" : (t.rows_count ?? "—")}</td>
        <td>${byteLlm(t)}</td>
      </tr>`).join("");
    return `
      <div class="analytics-card" style="margin-bottom:14px">
        <h4>${esc(r.model_name || r.model_id)} <span style="color:var(--text-muted)">(${esc(r.provider || "")})</span></h4>
        ${r.error ? `<div style="color:var(--danger)">${esc(r.error)}</div>` : `
        <table class="bench-table">
          <tr><th>Turno</th><th>Domanda</th><th>SQL/DB/NL</th><th>Token</th><th>Righe</th><th>Byte LLM</th></tr>
          ${rows}
        </table>`}
      </div>`;
  }).join("");
}
