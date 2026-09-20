"""
Schema e accesso alla tabella query_log (log persistente delle interrogazioni,
usata da Analytics, History e Suggestions).
"""
import logging
import threading
from contextlib import contextmanager

from .connection import get_connection
from .. import persistence_state

logger = logging.getLogger("lynx.db")


class SchemaIncompleteError(Exception):
    """Lo schema di query_log non ha tutte le colonne che il codice usa.

    Sollevata dalla verifica che segue la migrazione, porta con sé l'elenco
    completo delle colonne mancanti: è il punto in cui un guasto che altrimenti
    si manifesterebbe molto dopo — al primo INSERT, con un errore che sembra
    non correlato — viene reso esplicito subito.
    """

    def __init__(self, messaggio, colonne_mancanti=()):
        super().__init__(messaggio)
        self.colonne_mancanti = list(colonne_mancanti)

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


# La CREATE TABLE sta in una costante e non inline, cosi' `_colonne_del_create`
# puo' ricavarne i nomi delle colonne: l'insieme atteso dalla verifica non e'
# una terza lista da tenere allineata a mano, si deriva dalle due che esistono.
_CREATE_TABLE_SQL = """
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
"""


def _colonne_del_create():
    """Nomi di colonna dichiarati nella CREATE TABLE."""
    corpo = _CREATE_TABLE_SQL[_CREATE_TABLE_SQL.index("(") + 1:_CREATE_TABLE_SQL.rindex(")")]
    nomi = []
    for riga in corpo.split("\n"):
        riga = riga.strip()
        if riga:
            nomi.append(riga.split()[0])
    return nomi


def colonne_richieste():
    """Tutte le colonne che il codice si aspetta di trovare su query_log.

    Unione di CREATE TABLE e _COLUMNS_DDL: la prima serve a chi parte da
    database vuoto, la seconda e' l'unico modo di aggiungere colonne a una
    tabella preesistente.
    """
    return set(_colonne_del_create()) | {nome for nome, _tipo in _COLUMNS_DDL}


def verify_schema(conn):
    """Colonne attese ma non presenti sul database, in ordine alfabetico.

    Lista vuota significa schema completo. Si interroga information_schema
    invece di fidarsi dell'esito degli ALTER, perche' un ALTER fallito viene
    inghiottito per non interrompere gli altri: l'unica verita' e' cosa c'e'
    davvero nella tabella.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'query_log'")
        reali = {riga[0] for riga in cur.fetchall()}
    finally:
        cur.close()
    return sorted(colonne_richieste() - reali)


def _ensure_log_table(conn):
    cur = conn.cursor()
    cur.execute(_CREATE_TABLE_SQL)
    conn.commit()
    for col, typedef in _COLUMNS_DDL:
        try:
            cur.execute(f"ALTER TABLE query_log ADD COLUMN IF NOT EXISTS {col} {typedef}")
            conn.commit()
        except Exception as e:
            # Il try/except per-ALTER resta: un fallimento non deve fermare gli
            # altri. Cambia solo che ora lascia una traccia — prima il segnale
            # era zero e il guasto si scopriva molto dopo, all'INSERT.
            logger.warning("ALTER TABLE query_log ADD COLUMN %s non riuscito (%s): %s",
                           col, type(e).__name__, e)
            # Rollback esplicito: senza, la transazione resta abortita e
            # TUTTI gli statement successivi fallirebbero.
            conn.rollback()
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_log_timestamp ON query_log (timestamp DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_log_model ON query_log (model)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_query_log_session ON query_log (session_id)")
        conn.commit()
    except Exception as e:
        logger.warning("Creazione degli indici di query_log non riuscita (%s): %s",
                       type(e).__name__, e)
        conn.rollback()
    cur.close()

    # Verifica: e' qui che un guasto altrimenti silenzioso diventa esplicito.
    mancanti = verify_schema(conn)
    if mancanti:
        raise SchemaIncompleteError(
            "Schema di query_log incompleto: mancano le colonne %s. "
            "Gli ALTER TABLE non sono riusciti ad aggiungerle (permessi "
            "insufficienti? transazione in sola lettura?). Finche' mancano, "
            "OGNI scrittura su query_log fallira'." % ", ".join(mancanti),
            colonne_mancanti=mancanti)


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
        except SchemaIncompleteError as e:
            # Non si imposta _schema_ready: la migrazione verra' ritentata alla
            # prossima richiesta. E' l'auto-recupero che ha fatto passare da
            # solo l'ALTER di log_filename quando la VPN e' tornata.
            persistence_state.registra_schema(False, e.colonne_mancanti)
            logger.error("%s", e)
            raise
        except Exception as e:
            logger.warning("Impossibile garantire lo schema di query_log: %s", e)
            raise
        persistence_state.registra_schema(True, [])
        _schema_ready = True


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
