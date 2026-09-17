"""
SICUREZZA — Solo lettura, nessuna modifica al DB possibile (3 livelli):
  Livello 1: il system prompt istruisce l'LLM a generare solo SELECT/WITH.
  Livello 2: whitelist applicativa qui sotto, prima di toccare il DB.
  Livello 3: la connessione è aperta in transazione READ ONLY, quindi anche
             un comando di scrittura verrebbe rifiutato direttamente da Postgres.
"""
import re
import time

import psycopg2
import psycopg2.extras

from .connection import get_connection
from ..config import settings
from ..transfer import byte_payload_json

FORBIDDEN_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "REVOKE", "MERGE", "CALL",
    "EXECUTE", "VACUUM", "REINDEX", "COPY", "LOCK", "COMMENT",
    "SECURITY", "OWNER", "RENAME", "ATTACH", "DETACH",
]
# Nota: REPLACE non è nella blacklist perché in PostgreSQL è una funzione di
# stringa di sola lettura (REPLACE(col, 'a', 'b')), non un comando di scrittura
# come in altri DBMS. Bloccarla impedirebbe query SELECT legittime.

_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_literals_and_comments(sql: str) -> str:
    """Rimuove stringhe letterali e commenti prima dell'analisi.

    Senza questa pulizia il guard produce falsi positivi: un ';' o una parola
    come 'UPDATE'/'CREATE' DENTRO una stringa (es. WHERE note ILIKE '%update%'
    o un valore testuale contenente un punto e virgola) veniva scambiato per
    un secondo statement o per un comando di scrittura, bloccando query di
    sola lettura perfettamente legittime.
    """
    cleaned = _BLOCK_COMMENT_RE.sub(" ", sql)
    cleaned = _LINE_COMMENT_RE.sub(" ", cleaned)
    cleaned = _STRING_LITERAL_RE.sub("''", cleaned)
    return cleaned


def validate_readonly_sql(sql: str):
    """Ritorna (is_valid, error_message)."""
    if not sql or not sql.strip():
        return False, "Query SQL vuota."

    analyzable = _strip_literals_and_comments(sql).strip()
    body = analyzable.rstrip(";").rstrip()
    if not body:
        return False, "Query SQL vuota."
    if ";" in body:
        return False, "Sono ammesse solo query singole: rilevati più statement separati da ';'."

    first_word_match = re.match(r"^\s*(\w+)", body)
    first_word = first_word_match.group(1).upper() if first_word_match else ""
    if first_word not in ("SELECT", "WITH", "EXPLAIN"):
        return False, f"Solo query di lettura (SELECT) sono permesse. Rilevato: '{first_word}'."

    upper_body = body.upper()
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper_body):
            return False, f"Operazione non permessa: la query contiene '{kw}'. Il sistema consente solo lettura dei dati."

    return True, None


def _byte_query(cur):
    """Byte della query effettivamente spedita al server.

    `cursor.query` è ciò che psycopg2 ha inviato DOPO il binding dei parametri,
    quindi è preferibile a len(sql) per principio (qui coincidono, perché la
    query utente si esegue senza parametri). Non comprende il framing del
    protocollo — i messaggi Parse/Bind/Execute con i loro prefissi di lunghezza —
    che vale qualche decina di byte a query.
    """
    q = getattr(cur, "query", None)
    if q is None:
        return None
    if isinstance(q, str):
        q = q.encode("utf-8")
    return len(q)


def execute_readonly_query(sql: str, row_limit: int = None):
    """
    Esegue una query in sola lettura.
    Ritorna (columns, rows, error, truncated, latency_db_ms, transfer), dove
    `transfer` è {"query_bytes": int|None, "result_payload_bytes": int|None}.

    Sui byte in risposta: psycopg2 NON espone contatori di byte ricevuti (né il
    cursore né ConnectionInfo), e nemmeno libpq. `result_payload_bytes` è quindi
    un CALCOLO sul payload applicativo, non una misura del traffico di rete.
    """
    row_limit = row_limit or settings.MAX_ROWS_HARD
    trasferimento = {"query_bytes": None, "result_payload_bytes": None}
    is_valid, err = validate_readonly_sql(sql)
    if not is_valid:
        return [], [], f"Query bloccata per sicurezza — {err}", False, 0, trasferimento

    t0 = time.time()
    conn = None
    cur = None
    try:
        conn = get_connection()
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SET statement_timeout = {settings.STATEMENT_TIMEOUT_MS}")
        cur.execute(sql)
        trasferimento["query_bytes"] = _byte_query(cur)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows_all = cur.fetchmany(row_limit + 1)
        truncated = len(rows_all) > row_limit
        rows = rows_all[:row_limit]
        # La latenza DB si ferma QUI: la serializzazione che segue è costo della
        # misurazione, non del database, e includerla falserebbe latency_db_ms.
        latency_db_ms = int((time.time() - t0) * 1000)
        righe = [dict(r) for r in rows]
        trasferimento["result_payload_bytes"] = byte_payload_json(righe)
        return columns, righe, None, truncated, latency_db_ms, trasferimento
    except psycopg2.errors.QueryCanceled:
        latency_db_ms = int((time.time() - t0) * 1000)
        trasferimento["query_bytes"] = _byte_query(cur) if cur is not None else None
        return [], [], (
            "La query ha superato il tempo massimo di esecuzione "
            f"({settings.STATEMENT_TIMEOUT_MS // 1000}s). Provare a restringere "
            "la richiesta (es. aggiungere LIMIT o filtri)."
        ), False, latency_db_ms, trasferimento
    except Exception as e:
        latency_db_ms = int((time.time() - t0) * 1000)
        trasferimento["query_bytes"] = _byte_query(cur) if cur is not None else None
        return [], [], str(e), False, latency_db_ms, trasferimento
    finally:
        if conn is not None:
            conn.close()
