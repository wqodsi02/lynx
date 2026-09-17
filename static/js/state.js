// Stato condiviso minimale dell'app (niente framework reattivo: l'app è
// abbastanza piccola da gestire lo stato con un oggetto + funzioni dirette
// di rendering, com'era nell'originale, ma centralizzato qui invece che
// sparso in variabili globali).
export const state = {
  models: [],
  currentModelId: null,
  sessionId: null,         // sessione/chat corrente (memoria conversazionale)
  conversationTurns: [],   // turni visibili in UI per la chat corrente: {question, sql, ...}
  currentJobId: null,      // job_id della richiesta LLM in corso (per cancel)
  currentAbortController: null,
};

export function newSessionId() {
  return (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
}

export function newJobId() {
  return `job-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
