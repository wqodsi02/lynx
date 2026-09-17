"""
Schema e accesso alla tabella query_log (log persistente delle interrogazioni,
usata da Analytics, History e Suggestions).
"""
import logging
import threading
from contextlib import contextmanager

from .connection import get_connection

logger = logging.getLogger("lynx.db")

# Ogni colonna che il codice applicativo nomina DEVE stare qui dentro, non solo
# nel CREATE TABLE: su un database dove query_log esiste gia' il CREATE e' un
# no-op, e l'ALTER e' l'unico modo per aggiungere una colonna. Una colonna
# presente solo nel CREATE TABLE e' raggiungibile unicamente da chi parte da
# database vuoto, e su tutti gli altri fa fallire ogni INSERT che la nomina.
# Unica esclusione volontaria: `id`, che e' SERIAL PRIMARY KEY e non ha senso
# aggiungere via ALTER a una tabella che una primary key ce l'ha gia'.
# I tipi devono corrispondere a quelli del CREATE TABLE qui sotto, altrimenti
# su installazione nuova e su installazione esistente si otterrebbero colonne
# di tipo diverso. Verificato contro information_schema del database reale.
_COLUMNS_DDL = [
    ("session_id", "TEXT"),
    ("conversation_turn", "INTEGER"),
    ("provider", "TEXT"),
    ("category", "TEXT"),
    ("sql_correct", "TEXT DEFAULT 'pending'"),
    ("latency_sql_ms", "INTEGER"),
    ("latency_db_ms", "INTEGER"),
    ("latency_nl_ms", "INTEGER"),
    ("tokens_prompt", "INTEGER"),
    ("tokens_completion", "INTEGER"),
    ("tokens_total", "INTEGER"),
    ("feedback", "TEXT"),
    ("is_benchmark", "BOOLEAN DEFAULT FALSE"),
    ("benchmark_run_id", "TEXT"),
    # Colonne originariamente dichiarate solo nel CREATE TABLE. Mancavano qui,
    # quindi su una tabella preesistente non potevano essere aggiunte:
    # `log_filename` e' quella che ha rotto ogni INSERT (aggiunta al CREATE
    # TABLE quando la tabella esisteva gia'), le altre sopravvivevano solo
    # perche' c'erano fin dalla creazione originale.
    ("timestamp", "TIMESTAMPTZ DEFAULT NOW()"),
    ("model", "TEXT"),
    ("question", "TEXT"),
    ("sql_generated", "TEXT"),
    ("nl_response", "TEXT"),
    ("rows_returned", "INTEGER"),
    ("latency_ms", "INTEGER"),
    ("log_filename", "TEXT"),
    ("error", "TEXT"),
    # Transfer rate. ATTENZIONE: db_bytes_result_payload e llm_bytes_request
    # misurano popolazioni DIVERSE e non vanno messe a rapporto — vedi il
    # commento in save_log_file e la nota in backend/transfer.py.
    ("llm_bytes_request", "INTEGER"),
    ("llm_bytes_response", "INTEGER"),
    ("llm_bytes_response_wire", "INTEGER"),
    ("db_bytes_query", "INTEGER"),
    ("db_bytes_result_payload", "INTEGER"),
]

# Lo schema va garantito UNA volta per processo, non a ogni richiesta:
# prima ogni endpoint rieseguiva CREATE TABLE + 14 ALTER TABLE per ogni
# connessione (DDL inutile su ogni chiamata), e un singolo statement fallito
# lasciava la transazione in stato "aborted" facendo fallire a cascata anche
# le query successive sulla stessa connessione.
_schema_ready = False
_schema_lock = threading.Lock()


def _ensure_log_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS query_log (
            id              SERIAL PRIMARY KEY,
            session_id      TEXT,
            timestamp       TIMESTAMPTZ DEFAULT NOW(),
            model           TEXT,
            provider        TEXT,
            question        TEXT,
            category        TEXT,
            sql_generated   TEXT,
            sql_correct     TEXT DEFAULT 'pending',
            nl_response     TEXT,
            rows_returned   INTEGER,
            latency_sql_ms  INTEGER,
            latency_db_ms   INTEGER,
            latency_nl_ms   INTEGER,
            latency_ms      INTEGER,
            feedback        TEXT,
            log_filename    TEXT,
            error           TEXT
        )
    """)
    conn.commit()
    for col, typedef in _COLUMNS_DDL:
        try:
            cur.execute(f"ALTER TABLE query_log ADD COLUMN IF NOT EXISTS {col} {typedef}")
            conn.commit()
        except Exception:
            # Rollback esplicito: senza, la transazione resta abortita e
            # TUTTI gli statement successivi fallirebbero.
            conn.rollback()
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_log_timestamp ON query_log (timestamp DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_log_model ON query_log (model)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_log_session ON query_log (session_id)")
        conn.commit()
    except Exception:
        conn.rollback()
    cur.close()


def ensure_schema_once(conn):
    """Garantisce lo schema una sola volta per processo (thread-safe)."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        try:
            _ensure_log_table(conn)
            _schema_ready = True
        except Exception as e:
            logger.warning("Impossibile garantire lo schema di query_log: %s", e)
            raise


@contextmanager
def log_table_cursor(dict_rows: bool = False):
    """Context manager: connessione + cursore con schema query_log garantito.
    Chiude SEMPRE la connessione, anche in caso di eccezione."""
    import psycopg2.extras
    conn = get_connection()
    try:
        ensure_schema_once(conn)
        factory = psycopg2.extras.RealDictCursor if dict_rows else None
        cur = conn.cursor(cursor_factory=factory)
        yield conn, cur
    finally:
        conn.close()


def with_log_table_cursor():
    """Compatibilità con il vecchio stile (conn, cur) senza auto-chiusura.
    Preferire log_table_cursor() nei nuovi utilizzi."""
    conn = get_connection()
    ensure_schema_once(conn)
    cur = conn.cursor()
    return conn, cur
