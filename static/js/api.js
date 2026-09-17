// Wrapper unico per tutte le chiamate al backend. Centralizza base URL,
// JSON parsing, gestione errori e supporto AbortController per l'annullamento
// cooperativo delle richieste lunghe (generazione SQL / interpretazione NL).
const API = "";

async function request(path, { method = "GET", body, signal } = {}) {
  const opts = { method, signal };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`${API}${path}`, opts);
  let data;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok && !(data && data.cancelled)) {
    const msg = (data && data.error) || `Errore HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

export const api = {
  // Modelli
  getModels: () => request("/api/models"),
  addModel: (payload) => request("/api/models", { method: "POST", body: payload }),
  updateModel: (id, payload) => request(`/api/models/${id}`, { method: "PUT", body: payload }),
  deleteModel: (id) => request(`/api/models/${id}`, { method: "DELETE" }),

  // DB config
  getDbConfig: () => request("/api/config/db"),
  setDbConfig: (payload) => request("/api/config/db", { method: "POST", body: payload }),
  testDb: () => request("/api/db/test"),

  // NL2SQL
  generateSql: (payload, signal) => request("/api/generate-sql", { method: "POST", body: payload, signal }),
  executeAndExplain: (payload, signal) => request("/api/execute-and-explain", { method: "POST", body: payload, signal }),
  cancelJob: (jobId) => request("/api/cancel-job", { method: "POST", body: { job_id: jobId } }),

  // Conversazione
  newConversation: () => request("/api/conversation/new", { method: "POST" }),
  getConversationTurns: (sessionId) => request(`/api/conversation/${sessionId}/turns`),
  editTurn: (sessionId, turnIndex) =>
    request(`/api/conversation/${sessionId}/edit-turn`, { method: "POST", body: { turn_index: turnIndex } }),
  resetConversation: (sessionId) => request(`/api/conversation/${sessionId}/reset`, { method: "POST" }),

  // Benchmark
  runBenchmark: (payload) => request("/api/benchmark/run", { method: "POST", body: payload }),
  runBenchmarkConversation: (payload) => request("/api/benchmark/run-conversation", { method: "POST", body: payload }),

  // Logs
  getLogs: () => request("/api/logs"),
  getLogContent: (filename) => request(`/api/logs/${encodeURIComponent(filename)}`),
  getBenchmarkLogs: () => request("/api/benchmark/logs"),
  getBenchmarkLogContent: (filename) => request(`/api/benchmark/logs/${encodeURIComponent(filename)}`),

  // History / Analytics / Suggestions / Feedback
  getHistory: (limit = 20) => request(`/api/history?limit=${limit}`),
  getAnalytics: () => request("/api/analytics"),
  getSuggestions: () => request("/api/suggestions"),
  sendFeedback: (payload) => request("/api/feedback", { method: "POST", body: payload }),

  exportCsvUrl: () => `${API}/api/export/csv`,
  logDownloadUrl: (filename) => `${API}/api/logs/${encodeURIComponent(filename)}/download`,
  benchmarkLogDownloadUrl: (filename) => `${API}/api/benchmark/logs/${encodeURIComponent(filename)}/download`,
};
