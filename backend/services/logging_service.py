"""
Logging persistente delle interrogazioni: file .txt leggibili in
logs/ (e logs/benchmark/ per i run multi-modello) + riga nella tabella
query_log su Postgres per analytics/history/suggestions.
"""
import logging
import os
import re
from datetime import datetime

from .. import persistence_state
from ..config import LOGS_DIR, BENCHMARK_DIR
from ..transfer import somma_byte
from ..db.query_log_schema import log_table_cursor

logger = logging.getLogger("lynx.logsvc")


def slugify(text, max_len=55):
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "_", text)
    return text[:max_len].rstrip("_") or "query"


def _cosa_guardare(classificazione):
    """Un rigo di indirizzo, non una diagnosi: dice dove andare a vedere."""
    if classificazione == persistence_state.STRUTTURALE:
        return (" Genere strutturale: non si risolve da solo. Controllare schema "
                "e permessi con GET /api/db/test.")
    if classificazione == persistence_state.INDETERMINATO:
        return " Fallimento in fase di connessione: verificare la VPN."
    return ""


def esito_persistenza(log_id):
    """Esito della scrittura appena tentata, nella forma attesa da
    `save_log_file`.

    Da chiamare subito dopo `save_to_db_log`, nella stessa richiesta: legge lo
    stato aggiornato un istante prima.
    """
    if log_id is not None:
        return {"ok": True}
    s = persistence_state.istantanea()
    return {"ok": False, "classificazione": s["classificazione"],
            "errore": s["ultimo_errore"]}


def save_to_db_log(session_id, model_name, provider, question, category, sql,
                    nl_response, rows_count, latency_sql, latency_db, latency_nl,
                    tokens_prompt=0, tokens_completion=0, tokens_total=0,
                    log_filename=None, error=None, conversation_turn=None,
                    is_benchmark=False, benchmark_run_id=None, transfer=None):
    t = transfer or {}
    try:
        with log_table_cursor() as (conn, cur):
            cur.execute("""
            INSERT INTO query_log
              (session_id, conversation_turn, model, provider, question, category,
               sql_generated, nl_response, rows_returned,
               latency_sql_ms, latency_db_ms, latency_nl_ms, latency_ms,
               tokens_prompt, tokens_completion, tokens_total,
               log_filename, error, is_benchmark, benchmark_run_id,
               llm_bytes_request, llm_bytes_response, llm_bytes_response_wire,
               db_bytes_query, db_bytes_result_payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s)
            RETURNING id
        """, (session_id, conversation_turn, model_name, provider, question, category,
              sql, nl_response, rows_count,
              latency_sql, latency_db, latency_nl, (latency_sql or 0) + (latency_db or 0) + (latency_nl or 0),
              tokens_prompt, tokens_completion, tokens_total,
              log_filename, error, is_benchmark, benchmark_run_id,
              t.get("llm_bytes_request"), t.get("llm_bytes_response"),
              t.get("llm_bytes_response_wire"), t.get("db_bytes_query"),
              t.get("db_bytes_result_payload")))
            log_id = cur.fetchone()[0]
            conn.commit()
        falliti = persistence_state.registra_successo()
        if falliti:
            # Il segnale di recupero conta quanto quello di guasto: senza, non
            # si sa mai se il problema e' ancora in corso.
            logger.info("Persistenza su query_log ripristinata dopo %d fallimenti consecutivi.",
                        falliti)
        return log_id
    except Exception as e:
        classificazione = persistence_state.registra_fallimento(e)
        if persistence_state.deve_segnalare():
            logger.error(
                "Scrittura su query_log FALLITA — genere: %s, eccezione: %s — %s. "
                "Il file .txt resta disponibile.%s",
                classificazione, type(e).__name__, e, _cosa_guardare(classificazione))
        elif persistence_state.deve_riepilogare():
            s = persistence_state.istantanea()
            logger.warning(
                "Persistenza su query_log ancora fallita: %d volte consecutive (genere: %s).",
                s["fallimenti_consecutivi"], s["classificazione"])
        return None


def update_log_filename(log_id, filename):
    if log_id is None:
        return
    try:
        with log_table_cursor() as (conn, cur):
            cur.execute("UPDATE query_log SET log_filename=%s WHERE id=%s", (filename, log_id))
            conn.commit()
    except Exception as e:
        # Era a DEBUG, cioe' invisibile con FLASK_DEBUG=false, che e' la
        # configurazione normale. Non viene registrato fra i fallimenti di
        # persistenza: la riga su query_log ESISTE, e' solo rimasta senza il
        # nome del file. Record degradato, non perso.
        logger.warning("Aggiornamento di log_filename sulla riga %s non riuscito (%s): %s",
                       log_id, type(e).__name__, e)


def _riga_log_id(log_id, persistenza):
    """Riga "Log ID" del file di sessione.

    `N/A` si legge come "non applicabile", non come "la scrittura e' fallita":
    e' la differenza che oggi, per le 165 esecuzioni di luglio, non permette di
    sapere quali manchino dal database. Quando l'esito e' noto lo si dice; dove
    non lo e' — nel ramo di errore DB il file viene scritto PRIMA della
    scrittura sul database — `N/A` resta la risposta onesta.
    """
    if persistenza and persistenza.get("ok") is False:
        return "Log ID       : NON SALVATO SU DATABASE"
    return f"Log ID       : {log_id or 'N/A'}"


def save_log_file(session_id, question, model_name, provider, category, sql, rows,
                   nl_response, latency_sql, latency_db, latency_nl, rows_count,
                   tokens_prompt=0, tokens_completion=0, tokens_total=0,
                   log_id=None, error=None, truncated=False, transfer=None,
                   persistenza=None):
    ts = datetime.now()
    ts_str = ts.strftime("%Y%m%d_%H%M%S")
    filename = f"{ts_str}_{slugify(question)}.txt"
    filepath = os.path.join(LOGS_DIR, filename)
    sep = "=" * 70
    lines = [
        sep, "LYNX — Industrial Network Intelligence · SESSION LOG", sep,
        _riga_log_id(log_id, persistenza),
        f"Session ID   : {session_id}",
        f"Timestamp    : {ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Modello      : {model_name}",
        f"Provider     : {provider}",
        f"Categoria    : {category}",
        "",
        "[ DOMANDA ]", question, "",
        "[ QUERY SQL GENERATA ]", sql or "(nessuna)",
        f"Latenza SQL  : {latency_sql} ms",
        "",
        "[ ESECUZIONE ]",
        f"Righe restituite : {rows_count}{' (TRONCATO al limite massimo)' if truncated else ''}",
        f"Latenza DB       : {latency_db} ms",
        f"Latenza NL       : {latency_nl} ms",
        f"Latenza totale   : {(latency_sql or 0) + (latency_db or 0) + (latency_nl or 0)} ms",
        f"Token prompt     : {tokens_prompt}",
        f"Token completion : {tokens_completion}",
        f"Token totali     : {tokens_total}",
    ]
    if persistenza and persistenza.get("ok") is False:
        lines += [
            "",
            "[ PERSISTENZA DB ]",
            "Esito           : FALLITA — questa esecuzione NON e' su query_log",
            f"Genere          : {persistenza.get('classificazione') or 'n/d'}",
            f"Errore          : {persistenza.get('errore') or 'n/d'}",
        ]
    if transfer:
        def _b(chiave):
            v = transfer.get(chiave)
            return "n/d" if v is None else f"{v} byte"
        lines += [
            "",
            "[ TRASFERIMENTI ]",
            "(DB risultati e LLM richiesta misurano popolazioni diverse: il DB",
            " restituisce fino a MAX_ROWS_HARD righe, al modello ne vanno solo",
            " MAX_ROWS_TO_LLM. Non metterli a rapporto.)",
            f"LLM richiesta       : {_b('llm_bytes_request')}",
            f"LLM risposta        : {_b('llm_bytes_response')}",
            f"LLM risposta (filo) : {_b('llm_bytes_response_wire')}",
            f"DB query            : {_b('db_bytes_query')}",
            f"DB risultati        : {_b('db_bytes_result_payload')}",
        ]
    if error:
        lines += ["", "[ ERRORE ]", error]
    else:
        lines += ["", "[ RISPOSTA IN LINGUAGGIO NATURALE ]", nl_response or "(nessuna)"]
    if rows:
        lines += ["", f"[ RISULTATI RAW — prime {min(50, len(rows))} di {rows_count} righe ]"]
        cols = list(rows[0].keys())
        lines.append(" | ".join(cols))
        lines.append("-" * 60)
        for row in rows[:50]:
            lines.append(" | ".join(str(row.get(c, "")) for c in cols))
        if len(rows) > 50:
            lines.append(f"... e altre {len(rows) - 50} righe (troncato nel log)")
    lines += ["", sep, "END OF LOG", sep]
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return filename


def list_log_files():
    files = []
    for fname in sorted(os.listdir(LOGS_DIR), reverse=True):
        if fname.endswith(".txt"):
            stat = os.stat(os.path.join(LOGS_DIR, fname))
            files.append({"filename": fname, "size_bytes": stat.st_size,
                          "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()})
    return files


def read_log_file(filename):
    fpath = os.path.join(LOGS_DIR, filename)
    if not os.path.exists(fpath):
        return None
    with open(fpath, encoding="utf-8") as f:
        return f.read()


def _riga_trasferimenti(d):
    """Riga compatta dei byte per i file di riepilogo dei benchmark.

    Ritorna None se non c'è nulla di misurato, così i file dei run vecchi non
    guadagnano una riga di "n/d" senza informazione.
    """
    chiavi = [("LLM req", "llm_bytes_request"), ("LLM resp", "llm_bytes_response"),
              ("filo", "llm_bytes_response_wire"), ("DB query", "db_bytes_query"),
              ("DB risult.", "db_bytes_result_payload")]
    if all(d.get(k) is None for _e, k in chiavi):
        return None
    pezzi = ["%s %s" % (etichetta, "n/d" if d.get(k) is None else d.get(k))
             for etichetta, k in chiavi]
    return "[ TRASFERIMENTI ] " + " · ".join(pezzi) + "  (byte)"


def save_benchmark_file(session_id, question, results, conversational=False, scenario=None):
    """
    Salva un file .txt riepilogativo per un run di benchmark.
    Se `conversational` è True, `scenario` è la lista di domande del turno
    multi-step e `results` è una lista per-modello, ognuno con la lista dei
    turni eseguiti (sql/nl/latenze/token per ogni domanda dello scenario).
    """
    ts = datetime.now()
    ts_str = ts.strftime("%Y%m%d_%H%M%S")
    filename = f"{ts_str}_benchmark_{slugify(question)}.txt"
    filepath = os.path.join(BENCHMARK_DIR, filename)
    sep = "=" * 78

    lines = [
        sep,
        "LYNX — BENCHMARK MULTI-MODELLO" + (" (CONVERSAZIONALE)" if conversational else ""),
        sep,
        f"Session ID   : {session_id}",
        f"Timestamp    : {ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Domanda/Scenario : {question}",
        f"Modelli testati ({len(results)}):",
    ]
    for r in results:
        ok = "OK" if not r.get("error") else "ERRORE"
        lines.append(f"  - {r.get('model_name', '?')} ({r.get('provider', '?')}) — {ok}")

    if conversational:
        lines += [
            "", sep,
            "DETTAGLIO PER TURNO (SQL + risposta completa, come nel benchmark one-shot)",
            sep,
        ]
        for r in results:
            lines += ["", sep, f"MODELLO: {r.get('model_name', '?')} ({r.get('provider', '?')})", sep]
            for i, turn in enumerate(r.get("turns", []), 1):
                lines += ["", "-" * 60, f"TURNO {i}", "-" * 60,
                          "[ DOMANDA ]", turn.get("question", "")]
                if turn.get("error"):
                    lines += ["", "[ ERRORE ]", turn["error"]]
                    continue
                lines += [
                    "", "[ QUERY SQL GENERATA ]", turn.get("sql", "(nessuna)"),
                ]
                if turn.get("sql_repaired"):
                    lines += [
                        "", "[ AUTO-RIPARAZIONE APPLICATA ]",
                        f"Query originale: {turn.get('sql_original', '(n/d)')}",
                        f"Errore originale: {turn.get('repair_original_error', '(n/d)')}",
                    ]
                lines += [
                    f"Latenza SQL: {turn.get('latency_sql', 0)} ms",
                    "",
                    "[ RISPOSTA IN LINGUAGGIO NATURALE ]", turn.get("nl_response", "(nessuna)"),
                    f"Latenza DB: {turn.get('latency_db', 0)} ms · Latenza NL: {turn.get('latency_nl', 0)} ms",
                    f"Token: prompt {turn.get('tokens_prompt', 0)} · completion {turn.get('tokens_completion', 0)} · totale {turn.get('tokens_total', 0)}",
                ]
                riga_byte = _riga_trasferimenti(turn)
                if riga_byte:
                    lines.append(riga_byte)
                lines += [
                    "",
                    f"[ RISULTATI — {turn.get('rows_count', 0)} righe totali{' (TRONCATO)' if turn.get('truncated') else ''} ]",
                ]
                rows = turn.get("rows", [])
                if rows:
                    cols = turn.get("columns") or list(rows[0].keys())
                    lines.append(" | ".join(cols))
                    lines.append("-" * 60)
                    for row in rows[:20]:
                        lines.append(" | ".join(str(row.get(c, "")) for c in cols))
                    if len(rows) > 20:
                        lines.append(f"... e altre {len(rows) - 20} righe (troncato nel log riepilogativo)")

        lines += ["", sep, "RIEPILOGO PER TURNO (vista compatta)", sep]
        for r in results:
            lines += ["", f"MODELLO: {r.get('model_name', '?')} ({r.get('provider', '?')})", "-" * 60]
            for i, turn in enumerate(r.get("turns", []), 1):
                lines.append(f"  Turno {i}: {turn.get('question', '')}")
                if turn.get("error"):
                    lines.append(f"    ERRORE: {turn['error']}")
                else:
                    riparata = " [auto-riparata]" if turn.get("sql_repaired") else ""
                    byte_llm = somma_byte(turn.get("llm_bytes_request"),
                                          turn.get("llm_bytes_response"))
                    lines.append(
                        f"    SQL {turn.get('latency_sql', 0)}ms · DB {turn.get('latency_db', 0)}ms · "
                        f"NL {turn.get('latency_nl', 0)}ms · Token tot {turn.get('tokens_total', 0)} · "
                        f"Byte LLM {'n/d' if byte_llm is None else byte_llm} · "
                        f"Righe {turn.get('rows_count', 0)}{riparata}"
                    )
    else:
        lines += ["", sep, "RIEPILOGO LATENZE E TOKEN", sep]
        lines.append(
            f"{'Modello':<26}{'Provider':<12}{'SQL(ms)':<10}{'DB(ms)':<10}{'NL(ms)':<10}{'Tot(ms)':<10}{'Token':<10}{'Righe':<8}{'Byte LLM':<12}"
        )
        lines.append("-" * 108)
        for r in results:
            byte_llm = somma_byte(r.get("llm_bytes_request"), r.get("llm_bytes_response"))
            byte_llm = "n/d" if byte_llm is None else byte_llm
            if r.get("error"):
                lines.append(f"{r.get('model_name', '?'):<26}{r.get('provider', '?'):<12}{'—':<10}{'—':<10}{'—':<10}{'—':<10}{'—':<10}{'ERR':<8}{byte_llm:<12}")
            else:
                lines.append(
                    f"{r.get('model_name', '?'):<26}{r.get('provider', '?'):<12}"
                    f"{r.get('latency_sql', 0):<10}{r.get('latency_db', 0):<10}{r.get('latency_nl', 0):<10}"
                    f"{r.get('latency_ms', 0):<10}{r.get('tokens_total', 0):<10}{r.get('rows_count', 0):<8}"
                    f"{byte_llm:<12}"
                )
        for r in results:
            lines += ["", sep, f"MODELLO: {r.get('model_name', '?')} ({r.get('provider', '?')})", sep]
            if r.get("error"):
                lines += ["[ ERRORE ]", r["error"]]
                continue
            lines += [
                "[ QUERY SQL GENERATA ]", r.get("sql", "(nessuna)"),
                f"Latenza SQL: {r.get('latency_sql', 0)} ms",
                "",
                "[ RISPOSTA IN LINGUAGGIO NATURALE ]", r.get("nl_response", "(nessuna)"),
                f"Latenza DB: {r.get('latency_db', 0)} ms · Latenza NL: {r.get('latency_nl', 0)} ms",
                f"Token: prompt {r.get('tokens_prompt', 0)} · completion {r.get('tokens_completion', 0)} · totale {r.get('tokens_total', 0)}",
            ]
            riga_byte = _riga_trasferimenti(r)
            if riga_byte:
                lines.append(riga_byte)
            lines += [
                "",
                f"[ RISULTATI — {r.get('rows_count', 0)} righe totali{' (TRONCATO)' if r.get('truncated') else ''} ]",
            ]
            rows = r.get("rows", [])
            if rows:
                cols = list(rows[0].keys())
                lines.append(" | ".join(cols))
                lines.append("-" * 60)
                for row in rows[:20]:
                    lines.append(" | ".join(str(row.get(c, "")) for c in cols))
                if len(rows) > 20:
                    lines.append(f"... e altre {len(rows) - 20} righe (troncato nel log riepilogativo)")

    lines += ["", sep, "END OF BENCHMARK LOG", sep]
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return filename


def list_benchmark_files():
    files = []
    for fname in sorted(os.listdir(BENCHMARK_DIR), reverse=True):
        if fname.endswith(".txt"):
            stat = os.stat(os.path.join(BENCHMARK_DIR, fname))
            files.append({"filename": fname, "size_bytes": stat.st_size,
                          "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()})
    return files


def read_benchmark_file(filename):
    fpath = os.path.join(BENCHMARK_DIR, filename)
    if not os.path.exists(fpath):
        return None
    with open(fpath, encoding="utf-8") as f:
        return f.read()
