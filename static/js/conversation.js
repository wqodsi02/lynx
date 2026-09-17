import { api } from "./api.js";
import { state, newSessionId } from "./state.js";

const SESSIONS_KEY = "lynx-sessions"; // elenco sessioni note lato client, solo per la sidebar

function loadSessionsIndex() {
  try { return JSON.parse(localStorage.getItem(SESSIONS_KEY) || "[]"); }
  catch { return []; }
}

function saveSessionsIndex(list) {
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(list.slice(0, 50)));
}

export function registerSessionInIndex(sessionId, label) {
  const list = loadSessionsIndex().filter((s) => s.id !== sessionId);
  list.unshift({ id: sessionId, label: label.slice(0, 60), updated_at: Date.now() });
  saveSessionsIndex(list);
  return list;
}

export function getSessionsIndex() { return loadSessionsIndex(); }

export function removeSessionFromIndex(sessionId) {
  saveSessionsIndex(loadSessionsIndex().filter((s) => s.id !== sessionId));
}

/** Avvia una nuova chat: nuovo session_id locale, memoria server pulita di default
 *  (il server crea/azzera la memoria al primo uso di un session_id mai visto). */
export async function startNewChat() {
  let sessionId;
  try {
    const r = await api.newConversation();
    sessionId = r.session_id;
  } catch {
    sessionId = newSessionId();
  }
  state.sessionId = sessionId;
  state.conversationTurns = [];
  return sessionId;
}

export function ensureSession() {
  if (!state.sessionId) state.sessionId = newSessionId();
  return state.sessionId;
}

/** Riapre una conversazione passata: recupera i turni persistiti dal server
 *  (che nel farlo ricostruisce anche la propria memoria in-process) e aggiorna
 *  lo stato locale. Ritorna la lista dei turni per il re-render della chat. */
export async function loadSession(sessionId) {
  const data = await api.getConversationTurns(sessionId);
  state.sessionId = sessionId;
  state.conversationTurns = (data.turns || []).map((t) => ({
    question: t.question, sql: t.sql || "", nl_response: t.nl_response || "", log_id: t.id,
  }));
  return data.turns || [];
}

/** Modifica una domanda precedente: tronca history server-side dal turno indicato. */
export async function editTurnAndTruncate(turnIndex) {
  if (!state.sessionId) return;
  await api.editTurn(state.sessionId, turnIndex);
  state.conversationTurns = state.conversationTurns.slice(0, turnIndex);
}
