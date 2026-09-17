import { api } from "./api.js";
import { state, newJobId } from "./state.js";
import { esc, fmtNL, highlightSQL, fmtMs, toast } from "./utils.js";
import { ensureSession, editTurnAndTruncate, registerSessionInIndex } from "./conversation.js";

let chatInnerEl, composerInputEl, sendBtnEl;

export function initChat({ chatInner, composerInput, sendBtn }) {
  chatInnerEl = chatInner;
  composerInputEl = composerInput;
  sendBtnEl = sendBtn;
  // Stato iniziale del pulsante (idle): collega l'invio. Da qui in poi la
  // gestione passa a setComposerBusy(), che alterna invio/annulla.
  setComposerBusy(false);
}

function hideWelcome() {
  const w = document.getElementById("welcome");
  if (w) w.remove();
}

function resizeTextarea(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}

export function handleComposerKey(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendQuestion();
  }
}

export function setQuestionText(q) {
  composerInputEl.value = q;
  resizeTextarea(composerInputEl);
  composerInputEl.focus();
}

function notifyLogsChanged() {
  // I file di log su disco sono cambiati: la sidebar destra si aggiorna da sola
  // (prima restava stantia finché non si cambiava tab manualmente).
  window.dispatchEvent(new CustomEvent("lynx:logs-refresh"));
}

// ---------- Rendering helpers ----------
function appendUserMsg(text, turnIndex) {
  const d = document.createElement("div");
  d.className = "msg msg-user";
  d.dataset.turnIndex = turnIndex;
  d.innerHTML = `
    <div class="bubble" data-role="bubble">
      ${esc(text)}
      <button class="edit-btn" title="Modifica domanda">✎</button>
    </div>`;
  d.querySelector(".edit-btn").addEventListener("click", () => startEditMessage(d, text, turnIndex));
  chatInnerEl.appendChild(d);
  scrollToBottom();
  return d;
}

function startEditMessage(msgEl, originalText, turnIndex) {
  if (state.currentJobId) { toast("Attendi la fine della richiesta in corso prima di modificare.", "info"); return; }
  const bubble = msgEl.querySelector('[data-role="bubble"]');
  bubble.outerHTML = `
    <div class="msg-edit-box" style="width:100%;max-width:80%">
      <textarea>${esc(originalText)}</textarea>
      <div class="msg-edit-actions">
        <button class="btn-action" data-act="cancel">Annulla</button>
        <button class="btn-action primary" data-act="save">Salva e rigenera</button>
      </div>
    </div>`;
  const box = msgEl.querySelector(".msg-edit-box");
  box.querySelector('[data-act="cancel"]').addEventListener("click", () => {
    // Ripristina la vista normale senza modifiche
    renderUserBubble(msgEl, originalText, turnIndex);
  });
  box.querySelector('[data-act="save"]').addEventListener("click", async () => {
    const newText = box.querySelector("textarea").value.trim();
    if (!newText) return;
    // Rimuove dalla UI tutti i messaggi successivi a questo turno (inclusi)
    let next = msgEl.nextElementSibling;
    while (next) { const toRemove = next; next = next.nextElementSibling; toRemove.remove(); }
    msgEl.remove();
    await editTurnAndTruncate(turnIndex);
    await appendQuestionAndRun(newText);
  });
}

function renderUserBubble(msgEl, text, turnIndex) {
  msgEl.innerHTML = `
    <div class="bubble" data-role="bubble">
      ${esc(text)}
      <button class="edit-btn" title="Modifica domanda">✎</button>
    </div>`;
  msgEl.querySelector(".edit-btn").addEventListener("click", () => startEditMessage(msgEl, text, turnIndex));
}

// Indice del turno per cui si sta attualmente costruendo un blocco assistente.
// Usato da getAssistantBlock per taggare il wrapper con il turno di appartenenza,
// così un rigenera successivo può individuare e rimuovere solo quel blocco.
let activeTurnIndex = null;

function getAssistantBlock() {
  let cur = document.getElementById("cur-assist");
  if (cur) return cur;
  const wrap = document.createElement("div");
  wrap.className = "msg";
  if (activeTurnIndex !== null) wrap.dataset.assistTurnIndex = activeTurnIndex;
  wrap.innerHTML = `<div class="assist-block" id="cur-assist"></div>`;
  chatInnerEl.appendChild(wrap);
  scrollToBottom();
  return wrap.querySelector("#cur-assist");
}

function removeAssistantBlockForTurn(turnIndex) {
  const el = chatInnerEl.querySelector(`.msg[data-assist-turn-index="${turnIndex}"]`);
  if (el) el.remove();
}

/** Chiude il blocco assistente corrente. Va chiamata su OGNI percorso
 *  terminale (successo, errore, annullamento, abort): prima veniva chiamata
 *  solo su successo/errore, e dopo un "Annulla" il div #cur-assist restava
 *  aperto — la risposta della domanda successiva finiva accodata dentro il
 *  blocco del turno precedente. */
function finalizeAssistantBlock() {
  const cur = document.getElementById("cur-assist");
  if (cur) cur.removeAttribute("id");
  activeTurnIndex = null;
}

function appendThinking(label) {
  const b = getAssistantBlock();
  const id = "think-" + Date.now();
  const d = document.createElement("div");
  d.className = "thinking";
  d.id = id;
  d.innerHTML = `<span class="spinner"></span> ${esc(label)}`;
  b.appendChild(d);
  scrollToBottom();
  return id;
}

function removeThinking(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

function appendSQLBlock(sql, { onConfirm, onCancel, warning }) {
  const b = getAssistantBlock();
  const sl = document.createElement("div");
  sl.className = "step-label";
  sl.textContent = "Query SQL generata";
  b.appendChild(sl);

  const lines = sql.split("\n");
  const gutter = lines.map((_, i) => `<div>${i + 1}</div>`).join("");
  const sb = document.createElement("div");
  sb.className = "sql-block";
  sb.innerHTML = `<div class="sql-gutter">${gutter}</div><div class="sql-code">${highlightSQL(sql)}</div>`;
  b.appendChild(sb);

  if (warning) {
    const w = document.createElement("div");
    w.className = "step-label";
    w.style.color = "var(--warning)";
    w.textContent = `Attenzione: ${warning}`;
    b.appendChild(w);
  }

  const cb = document.createElement("div");
  cb.className = "confirm confirm-bar-el";
  cb.innerHTML = `
    <button class="btn-action primary" data-act="confirm">▶ Esegui</button>
    <button class="btn-action danger" data-act="cancel">Annulla</button>`;
  cb.querySelector('[data-act="confirm"]').addEventListener("click", () => { cb.remove(); onConfirm(); });
  cb.querySelector('[data-act="cancel"]').addEventListener("click", () => { cb.remove(); onCancel(); });
  b.appendChild(cb);
  scrollToBottom();
}

function appendResolved(status) {
  const b = getAssistantBlock();
  const r = document.createElement("span");
  r.className = "resolved " + (status === "ok" ? "ok" : "no");
  r.textContent = status === "ok" ? "completato" : "annullato";
  b.appendChild(r);
}

function appendNLResponse(text) {
  const b = getAssistantBlock();
  const d = document.createElement("div");
  d.className = "answer";
  d.innerHTML = fmtNL(text);
  b.appendChild(d);
  scrollToBottom();
}

function appendLatency(latSql, latDb, latNl, tokensTotal, logFilename) {
  const b = getAssistantBlock();
  const d = document.createElement("div");
  d.className = "latency-row";
  const logBtn = logFilename
    ? `<button class="btn-view-log" data-log="${esc(logFilename)}">log</button>` : "";
  d.innerHTML = `
    <span>SQL ${fmtMs(latSql)}</span><span class="lat-sep">·</span>
    <span>DB ${fmtMs(latDb)}</span><span class="lat-sep">·</span>
    <span>NL ${fmtMs(latNl)}</span><span class="lat-sep">·</span>
    <span>Σ ${fmtMs((latSql || 0) + (latDb || 0) + (latNl || 0))}</span>
    <span class="token-badge">${tokensTotal ?? 0} token</span>
    ${logBtn}`;
  b.appendChild(d);
  if (logFilename) {
    d.querySelector("[data-log]").addEventListener("click", () => {
      window.dispatchEvent(new CustomEvent("lynx:open-log", { detail: { filename: logFilename } }));
    });
  }
  scrollToBottom();
}

function appendActionsRow({ onRegenerate }) {
  const b = getAssistantBlock();
  const d = document.createElement("div");
  d.className = "confirm-bar-el";
  d.innerHTML = `<button class="btn-action" data-act="regen">↻ Rigenera</button>`;
  d.querySelector('[data-act="regen"]').addEventListener("click", onRegenerate);
  b.appendChild(d);
}

function appendResult(rows, cols, total) {
  const b = getAssistantBlock();
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "step-label";
    empty.textContent = "Nessuna riga restituita.";
    b.appendChild(empty);
    return;
  }
  const d = document.createElement("div");
  d.className = "result";
  const trunc = total > rows.length ? ` <span class="lat-sep">·</span> mostrando ${rows.length}` : "";
  const head = `<tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>`;
  const body = rows.map((row) => `<tr>${cols.map((c) => {
    const v = row[c];
    const isNum = typeof v === "number";
    return `<td class="${isNum ? "num" : ""}">${esc(v)}</td>`;
  }).join("")}</tr>`).join("");
  d.innerHTML = `
    <div class="step-label">Risultati — ${total} righe${trunc}</div>
    <div class="table-scroll"><table>${head}${body}</table></div>
    <div class="result-actions">
      <button class="btn-action" data-act="csv">⬇ Esporta CSV</button>
    </div>`;
  d.querySelector('[data-act="csv"]').addEventListener("click", () => exportCsv(rows, cols));
  b.appendChild(d);
  scrollToBottom();
}

async function exportCsv(rows, cols) {
  try {
    const res = await fetch(api.exportCsvUrl(), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows, columns: cols, question: "export" }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "export.csv";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast(`Esportazione CSV fallita: ${e.message}`, "error");
  }
}

function scrollToBottom() {
  const scroll = document.getElementById("chat-scroll");
  if (scroll) scroll.scrollTop = scroll.scrollHeight;
}

// ---------- Ricostruzione di una conversazione riaperta ----------
/** Re-render completo di una conversazione passata (turni da query_log).
 *  Sola lettura visiva del passato + composer attivo per proseguire. */
export function renderRestoredConversation(turns) {
  clearChatUI();
  hideWelcome();
  turns.forEach((t, i) => {
    appendUserMsg(t.question, i);
    activeTurnIndex = i;
    const b = getAssistantBlock();
    if (t.sql) {
      const sl = document.createElement("div");
      sl.className = "step-label";
      sl.textContent = "Query SQL generata";
      b.appendChild(sl);
      const lines = t.sql.split("\n");
      const gutter = lines.map((_, n) => `<div>${n + 1}</div>`).join("");
      const sb = document.createElement("div");
      sb.className = "sql-block";
      sb.innerHTML = `<div class="sql-gutter">${gutter}</div><div class="sql-code">${highlightSQL(t.sql)}</div>`;
      b.appendChild(sb);
    }
    if (t.error) {
      const d = document.createElement("div");
      d.className = "step-label";
      d.style.color = "var(--danger)";
      d.textContent = `Errore: ${t.error}`;
      b.appendChild(d);
    } else if (t.nl_response) {
      appendNLResponse(t.nl_response);
      appendLatency(t.latency_sql_ms, t.latency_db_ms, t.latency_nl_ms, t.tokens_total, t.log_filename);
    }
    finalizeAssistantBlock();
  });
  scrollToBottom();
}

// ---------- Flusso principale ----------
export async function sendQuestion() {
  // Guardia anti-doppio-invio: se una richiesta è già in corso (job attivo o
  // controller non ancora rilasciato), ignora ulteriori trigger. Protegge da
  // Enter premuto ripetutamente o da eventuali doppi eventi, evitando chiamate
  // LLM duplicate parallele sullo stesso turno.
  if (state.currentJobId) return;
  const q = composerInputEl.value.trim();
  if (!q) return;
  composerInputEl.value = "";
  resizeTextarea(composerInputEl);
  await appendQuestionAndRun(q);
}

async function appendQuestionAndRun(question) {
  hideWelcome();
  const sessionId = ensureSession();
  const turnIndex = state.conversationTurns.length;
  appendUserMsg(question, turnIndex);
  registerSessionInIndex(sessionId, question);
  window.dispatchEvent(new CustomEvent("lynx:session-touched"));
  await runAssistantFlow(question, turnIndex);
}

async function runAssistantFlow(question, turnIndex) {
  if (!state.currentModelId) {
    toast("Seleziona un modello prima di inviare una domanda.", "error");
    return;
  }
  const sessionId = ensureSession();
  activeTurnIndex = turnIndex;

  setComposerBusy(true);
  const jobId = newJobId();
  state.currentJobId = jobId;
  const controller = new AbortController();
  state.currentAbortController = controller;

  const thinkingId = appendThinking("Traduzione in SQL sullo schema PROFINET…");
  let genData;
  try {
    genData = await api.generateSql({
      question, model_id: state.currentModelId, session_id: sessionId, job_id: jobId,
    }, controller.signal);
  } catch (e) {
    removeThinking(thinkingId);
    setComposerBusy(false);
    if (e.name === "AbortError") { appendResolved("cancelled"); finalizeAssistantBlock(); return; }
    appendErrorBlock(e.message);
    return;
  }
  removeThinking(thinkingId);
  setComposerBusy(false);

  if (genData.cancelled) { appendResolved("cancelled"); finalizeAssistantBlock(); return; }
  if (genData.error) { appendErrorBlock(genData.error); return; }

  appendSQLBlock(genData.sql, {
    warning: genData.warning,
    onConfirm: () => executeStep(question, genData, sessionId, turnIndex),
    onCancel: () => { appendResolved("cancelled"); finalizeAssistantBlock(); },
  });
}

function appendErrorBlock(message) {
  const b = getAssistantBlock();
  const d = document.createElement("div");
  d.className = "step-label";
  d.style.color = "var(--danger)";
  d.textContent = `Errore: ${message}`;
  b.appendChild(d);
  finalizeAssistantBlock();
}

async function executeStep(question, genData, sessionId, turnIndex) {
  setComposerBusy(true);
  const jobId = newJobId();
  state.currentJobId = jobId;
  const controller = new AbortController();
  state.currentAbortController = controller;

  const thinkingId = appendThinking("Esecuzione query e interpretazione risultati…");
  let data;
  try {
    data = await api.executeAndExplain({
      question, sql: genData.sql, model_id: state.currentModelId,
      latency_sql_ms: genData.latency_ms, tokens_prompt: genData.tokens_prompt,
      tokens_completion: genData.tokens_completion, session_id: sessionId, job_id: jobId,
      // Anello 2 della catena: i byte della Fase 1 vanno inoltrati alla Fase 2,
      // altrimenti il backend somma solo la propria meta' e i totali su
      // query_log risultano dimezzati, senza alcun errore visibile.
      llm_bytes_request: genData.llm_bytes_request,
      llm_bytes_response: genData.llm_bytes_response,
      llm_bytes_response_wire: genData.llm_bytes_response_wire,
    }, controller.signal);
  } catch (e) {
    removeThinking(thinkingId);
    setComposerBusy(false);
    if (e.name === "AbortError") { appendResolved("cancelled"); finalizeAssistantBlock(); return; }
    // Anche i turni falliti restano tracciati: il server li ha registrati in
    // memoria/log, quindi l'indice locale deve avanzare per restare allineato.
    state.conversationTurns[turnIndex] = { question, sql: genData.sql, nl_response: `[errore: ${e.message}]` };
    appendErrorBlock(e.message);
    notifyLogsChanged();
    return;
  }
  removeThinking(thinkingId);
  setComposerBusy(false);

  if (data.cancelled) { appendResolved("cancelled"); finalizeAssistantBlock(); return; }
  if (data.error) {
    state.conversationTurns[turnIndex] = { question, sql: genData.sql, nl_response: `[errore: ${data.error}]` };
    appendErrorBlock(data.error);
    notifyLogsChanged();
    return;
  }

  appendResolved("ok");
  // Se la query confermata è fallita sul DB ed è stata corretta in automatico
  // dal backend (auto-riparazione), mostra la versione effettivamente eseguita:
  // l'utente deve sempre vedere il SQL reale dietro i risultati.
  if (data.repaired && data.sql && data.sql !== genData.sql) {
    const b = getAssistantBlock();
    const note = document.createElement("div");
    note.className = "step-label";
    note.style.color = "var(--warning)";
    note.textContent = "La query iniziale ha prodotto un errore sul database ed è stata corretta automaticamente. Query eseguita:";
    b.appendChild(note);
    const lines = data.sql.split("\n");
    const gutter = lines.map((_, n) => `<div>${n + 1}</div>`).join("");
    const sb = document.createElement("div");
    sb.className = "sql-block";
    sb.innerHTML = `<div class="sql-gutter">${gutter}</div><div class="sql-code">${highlightSQL(data.sql)}</div>`;
    b.appendChild(sb);
    genData.sql = data.sql;
  }
  appendNLResponse(data.nl_response);
  if (data.rows && data.rows.length) appendResult(data.rows, data.columns, data.rows_count);
  appendLatency(data.latency_sql, data.latency_db, data.latency_nl, data.tokens_total, data.log_filename);
  appendActionsRow({ onRegenerate: () => regenerateTurn(question, sessionId, turnIndex) });
  finalizeAssistantBlock();
  notifyLogsChanged();

  state.conversationTurns[turnIndex] = { question, sql: genData.sql, nl_response: data.nl_response, log_id: data.log_id };
}

async function regenerateTurn(question, sessionId, turnIndex) {
  // Rigenera SOSTITUENDO il turno esistente: tronca la memoria server-side al
  // turno corrente (che era già stato committato al termine del giro precedente),
  // rimuove solo il blocco assistente associato a questo turno (mantenendo la
  // domanda utente invariata) e rilancia il flusso riusando lo stesso turnIndex,
  // così non si crea una domanda duplicata in chat.
  if (state.currentJobId) { toast("Attendi la fine della richiesta in corso.", "info"); return; }
  await editTurnAndTruncate(turnIndex);
  removeAssistantBlockForTurn(turnIndex);
  await runAssistantFlow(question, turnIndex);
}

export function cancelCurrentRequest() {
  if (state.currentAbortController) state.currentAbortController.abort();
  if (state.currentJobId) api.cancelJob(state.currentJobId).catch(() => {});
}

function setComposerBusy(isBusy) {
  sendBtnEl.disabled = false;
  if (isBusy) {
    sendBtnEl.classList.add("btn-stop");
    sendBtnEl.title = "Annulla";
    sendBtnEl.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`;
    sendBtnEl.onclick = () => { cancelCurrentRequest(); setComposerBusy(false); };
  } else {
    // Tornati in stato idle: rilascia lo stato della richiesta in corso, così
    // la guardia anti-doppio-invio in sendQuestion() si sblocca e un nuovo
    // invio è di nuovo possibile.
    state.currentJobId = null;
    state.currentAbortController = null;
    sendBtnEl.classList.remove("btn-stop");
    sendBtnEl.title = "Invia";
    sendBtnEl.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>`;
    sendBtnEl.onclick = () => sendQuestion();
  }
}

export function clearChatUI() {
  chatInnerEl.innerHTML = "";
}
