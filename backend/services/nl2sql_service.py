"""
Logica applicativa NL2SQL a due fasi:
  Fase 1: domanda (+ history della sessione) -> LLM -> SQL
  Fase 2: SQL eseguito + risultati -> LLM -> risposta in linguaggio naturale
Entrambe le fasi sono "conversation-aware": se la sessione ha turni
precedenti, vengono ri-iniettati come contesto, permettendo domande di
follow-up ("e per la linea precedente?", "raddoppia quel valore", ecc.).
"""
import re
from ..config import settings
from ..llm.client import call_llm, clean_sql
from ..db.readonly_guard import validate_readonly_sql, execute_readonly_query
from ..transfer import byte_da_risultato, somma_byte
from . import conversation_service as conv

# Classificazione della categoria della domanda: euristica locale a keyword,
# non una chiamata LLM. Un tempo questa classificazione passava per una terza
# chiamata al modello ad ogni domanda (oltre a generazione SQL e interpretazione),
# aggiungendo un'intera roundtrip di rete e token di costo per un'informazione
# puramente etichettante (usata solo per Analytics). L'euristica è quasi
# istantanea e sufficientemente accurata per questo scopo.
_CATEGORY_KEYWORDS = [
    ("test_ridondanza", ("ridondan", "ring", "anello", "failover")),
    ("iat", ("iat", "inter-arrival", "inter arrival", "jitter")),
    ("throughput", ("throughput", "banda", "bitrate", "velocit")),
    ("anomalia", ("anomal", "anormal", "outlier", "deviazione standard", "z-score", "zscore")),
    ("confronto", ("confront", "rispetto a", "differenza tra", "vs ", "versus")),
    ("stato_rete", ("stato della rete", "stato rete", "normal", "open")),
    ("dispositivi", ("dispositiv", "device", "plc", "switch")),
    ("pacchetti", ("pacchett", "packet", "frame", "byte")),
    ("linee", ("linea", "linee", "impiant", "stabiliment", "reparto", "shop")),
]


def classify_question(question: str) -> str:
    q = (question or "").lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in q for kw in keywords):
            return category
    return "generico"


def generate_sql(question, model, system_prompt, session_id=None, use_history=True):
    """Fase 1. Ritorna dict con sql, latency_ms, tokens_*, warning, attempts.

    Robustezza indipendente dal modello: se la risposta non contiene alcuna
    query (tipico dei modelli reasoning che esauriscono i token nel blocco di
    "pensiero" e restituiscono contenuto vuoto o troncato), l'app riprova
    automaticamente con un'istruzione correttiva esplicita invece di
    propagare l'errore "Query SQL vuota" all'utente. Latenze e token dei
    tentativi si sommano, così le metriche riflettono il costo reale."""
    history = conv.get_history_as_messages(session_id) if (session_id and use_history) else []
    base_msgs = [{"role": "system", "content": system_prompt}] + history

    total_latency = total_tp = total_tc = 0
    # I byte si accumulano come latenza e token: un tentativo a vuoto ha
    # comunque speso traffico.
    total_breq = total_bresp = 0
    total_bwire = None
    sql = ""
    attempts = 0
    user_msg = f"FASE 1 — Genera solo la query SQL per rispondere a questa domanda:\n\n{question}"
    for attempt in range(1 + settings.EMPTY_SQL_RETRIES):
        attempts = attempt + 1
        msgs = base_msgs + [{"role": "user", "content": user_msg}]
        result = call_llm(model["provider"], model["model_string"], model["api_key"], msgs)
        total_latency += result.latency_ms
        total_tp += result.tokens_prompt
        total_tc += result.tokens_completion
        total_breq += result.bytes_request
        total_bresp += result.bytes_response
        total_bwire = somma_byte(total_bwire, result.bytes_response_wire)
        sql = clean_sql(result.text)
        if sql:
            break
        # Istruzione correttiva per il tentativo successivo: esplicita che la
        # risposta deve contenere SOLO la query, senza reasoning né preamboli.
        user_msg = (
            "FASE 1 (nuovo tentativo) — La risposta precedente non conteneva alcuna query SQL "
            "(era vuota o solo ragionamento). Rispondi ORA esclusivamente con la query SQL "
            f"PostgreSQL completa, senza alcun altro testo:\n\n{question}"
        )

    is_valid, err = validate_readonly_sql(sql)
    return {
        "sql": sql,
        "latency_ms": total_latency,
        "tokens_prompt": total_tp,
        "tokens_completion": total_tc,
        "tokens_total": total_tp + total_tc,
        "warning": None if is_valid else err,
        "attempts": attempts,
        "llm_bytes_request": total_breq,
        "llm_bytes_response": total_bresp,
        "llm_bytes_response_wire": total_bwire,
    }


def repair_sql(question, failed_sql, db_error, model, system_prompt):
    """Auto-correzione: reinvia al modello la query fallita con l'errore ESATTO
    di PostgreSQL e chiede la versione corretta. È il meccanismo che rende il
    sistema robusto rispetto agli errori di dialetto/tipo dei singoli modelli
    (FROM dual, ROUND su double precision senza ::numeric, AVG su colonne
    varchar, ecc.): l'errore reale del DB è un feedback molto più efficace di
    qualunque regola preventiva nel prompt."""
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "FASE 1-CORREZIONE — La query SQL generata per la domanda seguente è stata "
            "rifiutata da PostgreSQL. Correggi la query e rispondi SOLO con la query "
            "corretta (nessuna spiegazione, nessun markdown).\n\n"
            f"Domanda: {question}\n\n"
            f"Query fallita:\n{failed_sql or '(vuota)'}\n\n"
            f"Errore PostgreSQL:\n{db_error}"
        )},
    ]
    result = call_llm(model["provider"], model["model_string"], model["api_key"], msgs)
    sql = clean_sql(result.text)
    dati = {
        "sql": sql,
        "latency_ms": result.latency_ms,
        "tokens_prompt": result.tokens_prompt,
        "tokens_completion": result.tokens_completion,
        "tokens_total": result.tokens_total,
    }
    dati.update(byte_da_risultato(result))
    return dati


def _cumula_trasferimento(a, b):
    """Somma due misure di traffico verso il DB (esecuzione iniziale piu'
    eventuali riparazioni), con None neutro."""
    return {
        "query_bytes": somma_byte(a.get("query_bytes"), b.get("query_bytes")),
        "result_payload_bytes": somma_byte(a.get("result_payload_bytes"),
                                           b.get("result_payload_bytes")),
    }


def execute_with_repair(question, sql, model, system_prompt):
    """Esegue la query; su errore DB tenta fino a settings.SQL_REPAIR_ATTEMPTS
    auto-correzioni. Ritorna (final_sql, cols, rows, db_error, truncated,
    latency_db_totale, repair_info, transfer). repair_info è None se non è
    servita alcuna correzione, altrimenti un dict con il dettaglio del
    tentativo (query originale, errore originale, esito, latenza, token e
    byte extra) — utile sia per i log sia per l'analisi sperimentale del
    meccanismo. `transfer` cumula il traffico verso il DB di TUTTE le
    esecuzioni, inclusa quella fallita che ha innescato la riparazione."""
    cols, rows, db_error, truncated, lat_db, trasferimento = execute_readonly_query(sql)
    if not db_error:
        return sql, cols, rows, None, truncated, lat_db, None, trasferimento

    repair_info = {
        "attempted": True, "succeeded": False,
        "original_sql": sql, "original_error": db_error,
        "latency_ms": 0, "tokens_prompt": 0, "tokens_completion": 0, "tokens_total": 0,
        "llm_bytes_request": 0, "llm_bytes_response": 0, "llm_bytes_response_wire": None,
        "attempts": 0,
    }
    current_sql, current_error = sql, db_error
    total_lat_db = lat_db
    for _ in range(settings.SQL_REPAIR_ATTEMPTS):
        repair_info["attempts"] += 1
        try:
            rep = repair_sql(question, current_sql, current_error, model, system_prompt)
        except Exception as e:
            current_error = f"{current_error} (auto-correzione fallita: {e})"
            break
        repair_info["latency_ms"] += rep["latency_ms"]
        repair_info["tokens_prompt"] += rep["tokens_prompt"]
        repair_info["tokens_completion"] += rep["tokens_completion"]
        repair_info["tokens_total"] += rep["tokens_total"]
        repair_info["llm_bytes_request"] += rep["llm_bytes_request"]
        repair_info["llm_bytes_response"] += rep["llm_bytes_response"]
        repair_info["llm_bytes_response_wire"] = somma_byte(
            repair_info["llm_bytes_response_wire"], rep["llm_bytes_response_wire"])
        if not rep["sql"]:
            current_error = f"{current_error} (auto-correzione: il modello non ha prodotto una query)"
            continue
        current_sql = rep["sql"]
        cols, rows, current_error, truncated, lat_db2, tr2 = execute_readonly_query(current_sql)
        total_lat_db += lat_db2
        trasferimento = _cumula_trasferimento(trasferimento, tr2)
        if not current_error:
            repair_info["succeeded"] = True
            return current_sql, cols, rows, None, truncated, total_lat_db, repair_info, trasferimento

    return current_sql, [], [], current_error, False, total_lat_db, repair_info, trasferimento


# Rete di sicurezza per la Fase 2: il system prompt istruisce il modello a non
# scrivere mai SQL nella risposta in linguaggio naturale (deve solo chiedere
# conferma a parole prima di un eventuale approfondimento). Alcuni modelli,
# specialmente quelli più "proattivi" o meno inclini a seguire istruzioni
# rigide, tendono comunque a suggerire una query di verifica direttamente
# nella risposta. Questa funzione ripulisce il testo rimuovendo tali blocchi,
# così il comportamento è garantito anche se il modello ignora il prompt.
_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*\n?.*?```", re.DOTALL | re.IGNORECASE)
# Cattura SQL "in prosa" ovunque nel testo (non solo a inizio riga): richiede
# SELECT...FROM o un CTE (WITH ... AS () per ridurre falsi positivi su parole
# comuni come "with" in un contesto non tecnico.
_SQL_INLINE_RE = re.compile(
    r"(SELECT\b.{0,300}?\bFROM\b[^.;\n]*[.;]?|WITH\s+\w+\s+AS\s*\(.{0,500}?\)[^.;\n]*[.;]?)",
    re.DOTALL | re.IGNORECASE,
)
# Frasi tipiche di "lead-in" a una query che, una volta rimossa la query stessa,
# restano come frammento orfano privo di contenuto utile dopo (es. "Prova con:").
# Rimuove solo dal punto di innesco fino a fine riga, non l'intera riga, cosi'
# non cancella testo utile che precede sulla stessa riga.
_SQL_LEADIN_RE = re.compile(
    r"(?im)\b("
    r"una possibile query[^\n]*|query di (verifica|follow-?up|approfondimento)[^\n]*|"
    r"si potrebbe (usare|eseguire)[^\n]*|puoi (usare|eseguire)[^\n]*|prova (con|questa)[^\n]*|"
    r"ecco (una|la) query[^\n]*"
    r")"
)


def _strip_sql_from_nl_response(text: str) -> str:
    if not text:
        return text
    cleaned = _SQL_FENCE_RE.sub("", text)
    cleaned = _SQL_INLINE_RE.sub("", cleaned)
    cleaned = _SQL_LEADIN_RE.sub("", cleaned)
    # Rimuove righe/frasi rimaste vuote o ridotte a sola punteggiatura orfana.
    cleaned = re.sub(r"[ \t]*:\s*[.;]?\s*(?=\n|$)", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def explain_results(question, sql, rows, truncated, model, session_id=None, use_history=True):
    """Fase 2. Ritorna dict con nl_response, latency_ms, tokens_*.

    Nota: `system_prompt` (parametro storico, il prompt completo con lo schema)
    non viene piu' usato qui - la Fase 2 lavora su dati JSON gia' estratti e non
    ha bisogno dello schema delle tabelle, solo delle regole di interpretazione.
    Si usa quindi SYSTEM_PROMPT_INTERPRETATION, molto piu' corto, per ridurre
    sensibilmente i token per chiamata (soprattutto nei benchmark multi-modello,
    dove questo taglio si applica a ogni modello testato).
    """
    import json
    from ..system_prompt import SYSTEM_PROMPT_INTERPRETATION
    history = conv.get_history_as_messages(session_id) if (session_id and use_history) else []
    # Cap alle righe inviate al modello: oltre questo limite il prompt di
    # Fase 2 esplode in token (con 200 righe si superavano i limiti TPM dei
    # tier gratuiti — errore 413) senza migliorare la qualità dell'analisi.
    rows_preview = rows[:settings.MAX_ROWS_TO_LLM]
    results_text = json.dumps(rows_preview, ensure_ascii=False, default=str, indent=2)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT_INTERPRETATION}] + history + [
        {"role": "user", "content": (
            f"FASE 2 — Interpreta i risultati e rispondi in italiano in linguaggio naturale.\n\n"
            f"Domanda originale: {question}\n\nQuery SQL eseguita:\n{sql}\n\n"
            f"Risultati ({len(rows)} righe totali"
            f"{', mostrate le prime ' + str(len(rows_preview)) if len(rows) > len(rows_preview) else ''}"
            f"{', troncato al limite massimo' if truncated else ''}):\n{results_text}"
        )}
    ]
    result = call_llm(model["provider"], model["model_string"], model["api_key"], msgs)
    nl_response = _strip_sql_from_nl_response(result.text)
    dati = {
        "nl_response": nl_response,
        "latency_ms": result.latency_ms,
        "tokens_prompt": result.tokens_prompt,
        "tokens_completion": result.tokens_completion,
        "tokens_total": result.tokens_total,
    }
    dati.update(byte_da_risultato(result))
    return dati
