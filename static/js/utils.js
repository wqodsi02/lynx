export function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** Rendering "markdown-lite" delle risposte in linguaggio naturale del
 *  modello: grassetto, corsivo, codice inline ed elenchi puntati/numerati.
 *  Niente libreria esterna: il sottoinsieme usato dai modelli in questo
 *  dominio è piccolo e controllato, e l'input è sempre passato da esc(). */
export function fmtNL(t) {
  const lines = esc(t).split("\n");
  let html = "";
  let listType = null; // 'ul' | 'ol' | null

  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };
  const inline = (s) => s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>");

  for (const raw of lines) {
    const line = raw.trimEnd();
    const mUl = line.match(/^\s*[-•*]\s+(.+)$/);
    const mOl = line.match(/^\s*(\d+)[.)]\s+(.+)$/);
    if (mUl) {
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${inline(mUl[1])}</li>`;
    } else if (mOl) {
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += `<li>${inline(mOl[2])}</li>`;
    } else if (!line.trim()) {
      closeList();
      html += "<br>";
    } else {
      closeList();
      html += inline(line) + "<br>";
    }
  }
  closeList();
  // Compatta <br> multipli generati da righe vuote consecutive.
  return html.replace(/(<br>){3,}/g, "<br><br>").replace(/<br>$/, "");
}

const SQL_KW = new Set("select from where group by order limit join inner left right outer on as and or not in is null asc desc count avg sum min max round distinct having union with case when then else end between like ilike over partition filter".split(" "));
const SQL_FN = new Set("round avg count sum min max coalesce now stddev stddev_samp stddev_pop percentile_cont date_trunc extract".split(" "));

export function highlightSQL(sql) {
  const re = /(--[^\n]*)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?(?:e\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+)|([(),.;*=<>!/+\-]+)/g;
  let out = "";
  let m;
  while ((m = re.exec(sql)) !== null) {
    if (m[1]) out += `<span style="color:var(--text-muted)">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span style="color:var(--success)">${esc(m[2])}</span>`;
    else if (m[3]) out += `<span style="color:var(--warning)">${esc(m[3])}</span>`;
    else if (m[4]) {
      const l = m[4].toLowerCase();
      if (SQL_KW.has(l)) out += `<span class="tok-key">${esc(m[4])}</span>`;
      else if (SQL_FN.has(l)) out += `<span class="tok-fn">${esc(m[4])}</span>`;
      else out += esc(m[4]);
    } else out += esc(m[0]);
  }
  return out;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
}

export function debounce(fn, wait = 200) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), wait); };
}

/** Notifiche non bloccanti al posto di alert(): un alert() blocca l'intero
 *  event loop del browser (incluse le animazioni e le richieste in corso)
 *  ed è visivamente fuori luogo in un'app moderna. */
let toastContainer = null;

export function toast(message, type = "info", duration = 3200) {
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.className = "toast-container";
    document.body.appendChild(toastContainer);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.setAttribute("role", "status");
  el.textContent = message;
  toastContainer.appendChild(el);
  requestAnimationFrame(() => el.classList.add("visible"));
  setTimeout(() => {
    el.classList.remove("visible");
    el.addEventListener("transitionend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 500); // fallback se transitionend non arriva
  }, duration);
}
