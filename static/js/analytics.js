import { api } from "./api.js";
import { esc } from "./utils.js";

let chartInstance = null;

export async function loadAnalytics() {
  const body = document.getElementById("analytics-body");
  body.innerHTML = `<div class="thinking"><span class="spinner"></span> Caricamento statistiche…</div>`;
  try {
    const data = await api.getAnalytics();
    body.innerHTML = renderAnalytics(data);
    renderTokenChart(data.by_model);
  } catch (e) {
    body.innerHTML = `<div class="step-label" style="color:var(--danger)">Errore: ${esc(e.message)}</div>`;
  }
}

function renderAnalytics(data) {
  const modelRows = (data.by_model || []).map((m) => `
    <tr>
      <td>${esc(m.model)}</td><td>${esc(m.provider)}</td><td>${m.total_queries}</td>
      <td>${m.avg_lat_sql ?? "—"}</td><td>${m.avg_lat_db ?? "—"}</td><td>${m.avg_lat_nl ?? "—"}</td>
      <td>${m.avg_tokens_total ?? "—"}</td><td>${m.sum_tokens_total ?? "—"}</td>
      <td>${m.sum_llm_bytes_total ? m.sum_llm_bytes_total.toLocaleString("it-IT") : "—"}</td>
      <td>${m.sql_ok ?? 0}/${m.sql_ko ?? 0}/${m.sql_partial ?? 0}</td>
      <td>${m.errors ?? 0}</td>
    </tr>`).join("");

  const catRows = (data.by_category || []).map((c) => `<tr><td>${esc(c.category)}</td><td>${c.count}</td></tr>`).join("");
  const topRows = (data.top_questions || []).map((q) => `<tr><td>${esc(q.question)}</td><td>${q.count}</td></tr>`).join("");

  return `
    <div class="analytics-card">
      <h4>Confronto modelli — latenze (ms), token, byte trasferiti, accuratezza SQL (ok/ko/parziale)</h4>
      <div style="overflow-x:auto">
        <table class="bench-table">
          <tr><th>Modello</th><th>Provider</th><th>Query</th><th>SQL</th><th>DB</th><th>NL</th>
              <th>Token medi</th><th>Token tot.</th><th>Byte LLM tot.</th>
              <th>SQL ok/ko/part</th><th>Errori</th></tr>
          ${modelRows || '<tr><td colspan="11" class="empty-state">Nessun dato ancora.</td></tr>'}
        </table>
      </div>
    </div>
    <div class="analytics-grid">
      <div class="analytics-card">
        <h4>Consumo token per modello</h4>
        <canvas id="token-chart" height="180"></canvas>
      </div>
      <div class="analytics-card">
        <h4>Domande per categoria</h4>
        <table class="bench-table"><tr><th>Categoria</th><th>Conteggio</th></tr>${catRows || '<tr><td colspan="2" class="empty-state">—</td></tr>'}</table>
      </div>
    </div>
    <div class="analytics-card" style="margin-top:16px">
      <h4>Domande più frequenti</h4>
      <table class="bench-table"><tr><th>Domanda</th><th>Conteggio</th></tr>${topRows || '<tr><td colspan="2" class="empty-state">—</td></tr>'}</table>
    </div>`;
}

function renderTokenChart(byModel) {
  const canvas = document.getElementById("token-chart");
  if (!canvas || typeof Chart === "undefined") return;
  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(canvas, {
    type: "bar",
    data: {
      labels: byModel.map((m) => m.model),
      datasets: [{ label: "Token totali", data: byModel.map((m) => m.sum_tokens_total || 0) }],
    },
    options: { responsive: true, plugins: { legend: { display: false } } },
  });
}
