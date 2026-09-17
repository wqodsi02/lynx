"""
Route NL2SQL principali. Supportano:
  - sessioni/memoria conversazionale (session_id)
  - rigenerazione di un turno (stessa domanda, history invariata)
  - modifica di una domanda precedente (tronca i turni successivi nella memoria)
  - riapertura di una conversazione passata (turni ricostruiti da query_log)
  - annullamento "cooperativo": il frontend usa AbortController sul fetch;
    qui esponiamo anche /api/cancel-job per segnare un job come annullato,
    così una chiamata LLM lenta in corso non finisce comunque per essere
    salvata/loggata se l'utente ha annullato.
"""
import threading
import uuid
from collections import OrderedDict

from flask import Blueprint, jsonify, request

from ..db.query_log_schema import log_table_cursor
from ..models_repository import get_model
from ..services import conversation_service as conv
from ..services import logging_service as logsvc
from ..services import nl2sql_service as nl2sql
from ..system_prompt import SYSTEM_PROMPT_GENERATION
from ..transfer import somma_byte

bp = Blueprint("query", __name__, url_prefix="/api")

# Set (con ordine di inserimento) dei job annullati, limitato in dimensione:
# prima cresceva senza mai essere svuotato — memory leak lento ma certo su
# sessioni di lavoro lunghe.
_MAX_TRACKED_JOBS = 500
_cancelled_jobs = OrderedDict()
_jobs_lock = threading.Lock()


def _mark_cancelled(job_id):
    with _jobs_lock:
        _cancelled_jobs[job_id] = True
        while len(_cancelled_jobs) > _MAX_TRACKED_JOBS:
            _cancelled_jobs.popitem(last=False)


def _is_cancelled(job_id):
    with _jobs_lock:
        return job_id in _cancelled_jobs


@bp.route("/cancel-job", methods=["POST"])
def cancel_job():
    job_id = (request.json or {}).get("job_id")
    if job_id:
        _mark_cancelled(job_id)
    return jsonify({"status": "ok"})


@bp.route("/generate-sql", methods=["POST"])
def generate_sql():
    d = request.json or {}
    question = (d.get("question") or "").strip()
    model_id = d.get("model_id", "")
    session_id = d.get("session_id") or str(uuid.uuid4())
    job_id = d.get("job_id")
    use_history = d.get("use_history", True)

    if not question:
        return jsonify({"error": "Domanda vuota"}), 400
    model = get_model(model_id)
    if not model:
        return jsonify({"error": "Modello non trovato. Aggiungilo nelle Impostazioni."}), 400

    try:
        gen = nl2sql.generate_sql(question, model, SYSTEM_PROMPT_GENERATION, session_id=session_id, use_history=use_history)
        if job_id and _is_cancelled(job_id):
            return jsonify({"error": "Richiesta annullata dall'utente", "cancelled": True}), 200
        return jsonify({
            "sql": gen["sql"], "latency_ms": gen["latency_ms"], "warning": gen["warning"],
            "tokens_prompt": gen["tokens_prompt"], "tokens_completion": gen["tokens_completion"],
            "tokens_total": gen["tokens_total"], "session_id": session_id,
            # Anello 1 della catena: senza questi campi il frontend non ha
            # nulla da inoltrare alla Fase 2 e i byte spariscono in silenzio.
            "llm_bytes_request": gen["llm_bytes_request"],
            "llm_bytes_response": gen["llm_bytes_response"],
            "llm_bytes_response_wire": gen["llm_bytes_response_wire"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/execute-and-explain", methods=["POST"])
def execute_and_explain():
    d = request.json or {}
    question = (d.get("question") or "").strip()
    sql = (d.get("sql") or "").strip()
    model_id = d.get("model_id", "")
    lat_sql = d.get("latency_sql_ms", 0)
    tokens_prompt_sql = d.get("tokens_prompt", 0)
    tokens_completion_sql = d.get("tokens_completion", 0)
    # Anello 3: i byte della Fase 1 arrivano dal frontend e vanno SOMMATI a
    # quelli della Fase 2, non sostituiti.
    b_req_sql = d.get("llm_bytes_request") or 0
    b_resp_sql = d.get("llm_bytes_response") or 0
    b_wire_sql = d.get("llm_bytes_response_wire")
    session_id = d.get("session_id") or str(uuid.uuid4())
    job_id = d.get("job_id")
    use_history = d.get("use_history", True)
    conversation_turn = conv.turn_count(session_id) + 1

    if not sql:
        return jsonify({"error": "Query SQL vuota"}), 400
    model = get_model(model_id)
    if not model:
        return jsonify({"error": "Modello non trovato"}), 400

    category = nl2sql.classify_question(question)
    # Esecuzione con auto-riparazione: se la query fallisce sul DB, l'errore
    # esatto viene reinviato al modello per una correzione automatica prima di
    # arrendersi. final_sql può quindi differire dallo sql confermato in UI.
    final_sql, cols, rows, db_error, truncated, lat_db, repair, db_transfer = \
        nl2sql.execute_with_repair(question, sql, model, SYSTEM_PROMPT_GENERATION)
    if repair:
        # La riparazione appartiene alla fase SQL: latenza, token E byte.
        lat_sql = (lat_sql or 0) + repair["latency_ms"]
        tokens_prompt_sql += repair["tokens_prompt"]
        tokens_completion_sql += repair["tokens_completion"]
        b_req_sql = somma_byte(b_req_sql, repair["llm_bytes_request"])
        b_resp_sql = somma_byte(b_resp_sql, repair["llm_bytes_response"])
        b_wire_sql = somma_byte(b_wire_sql, repair["llm_bytes_response_wire"])
    sql = final_sql
    db_transfer = db_transfer or {}

    if db_error:
        trasferimento = {
            "llm_bytes_request": b_req_sql, "llm_bytes_response": b_resp_sql,
            "llm_bytes_response_wire": b_wire_sql,
            "db_bytes_query": db_transfer.get("query_bytes"),
            "db_bytes_result_payload": db_transfer.get("result_payload_bytes"),
        }
        fname = logsvc.save_log_file(session_id, question, model["name"], model["provider"], category,
                                      sql, [], None, lat_sql, lat_db, 0, 0, error=db_error,
                                      transfer=trasferimento)
        logsvc.save_to_db_log(session_id, model["name"], model["provider"], question, category,
                               sql, None, 0, lat_sql, lat_db, 0, tokens_prompt_sql, tokens_completion_sql,
                               tokens_prompt_sql + tokens_completion_sql, fname, db_error,
                               conversation_turn=conversation_turn, transfer=trasferimento)
        # Registra comunque il turno fallito in memoria (con marcatore di
        # errore): mantiene allineati gli indici turno tra UI e server e dà
        # al modello il contesto che il tentativo precedente non è andato a
        # buon fine (stesso comportamento del benchmark conversazionale).
        conv.append_turn(session_id, question, sql, f"[errore DB: {db_error}]")
        return jsonify({"error": db_error, "log_filename": fname}), 500

    if job_id and _is_cancelled(job_id):
        return jsonify({"error": "Richiesta annullata dall'utente", "cancelled": True}), 200

    try:
        exp = nl2sql.explain_results(question, sql, rows, truncated, model,
                                      session_id=session_id, use_history=use_history)
        if job_id and _is_cancelled(job_id):
            return jsonify({"error": "Richiesta annullata dall'utente", "cancelled": True}), 200

        tokens_prompt = tokens_prompt_sql + exp["tokens_prompt"]
        tokens_completion = tokens_completion_sql + exp["tokens_completion"]
        tokens_total = tokens_prompt + tokens_completion
        trasferimento = {
            "llm_bytes_request": somma_byte(b_req_sql, exp["llm_bytes_request"]),
            "llm_bytes_response": somma_byte(b_resp_sql, exp["llm_bytes_response"]),
            "llm_bytes_response_wire": somma_byte(b_wire_sql, exp["llm_bytes_response_wire"]),
            "db_bytes_query": db_transfer.get("query_bytes"),
            "db_bytes_result_payload": db_transfer.get("result_payload_bytes"),
        }

        log_id = logsvc.save_to_db_log(session_id, model["name"], model["provider"], question, category,
                                        sql, exp["nl_response"], len(rows), lat_sql, lat_db, exp["latency_ms"],
                                        tokens_prompt, tokens_completion, tokens_total,
                                        conversation_turn=conversation_turn, transfer=trasferimento)
        fname = logsvc.save_log_file(session_id, question, model["name"], model["provider"], category,
                                      sql, rows, exp["nl_response"], lat_sql, lat_db, exp["latency_ms"],
                                      len(rows), tokens_prompt, tokens_completion, tokens_total,
                                      log_id, truncated=truncated, transfer=trasferimento)
        logsvc.update_log_filename(log_id, fname)

        # Aggiorna la memoria conversazionale SOLO ora che il turno è completo.
        conv.append_turn(session_id, question, sql, exp["nl_response"])

        return jsonify({
            "repaired": bool(repair and repair.get("succeeded")),
            "repair_original_error": repair.get("original_error") if repair else None,
            "sql": sql,
            "nl_response": exp["nl_response"], "rows": rows, "columns": cols,
            "rows_count": len(rows), "truncated": truncated,
            "latency_sql": lat_sql, "latency_db": lat_db, "latency_nl": exp["latency_ms"],
            "latency_ms": lat_sql + lat_db + exp["latency_ms"],
            "tokens_prompt": tokens_prompt, "tokens_completion": tokens_completion, "tokens_total": tokens_total,
            "log_filename": fname, "log_id": log_id, "session_id": session_id,
            "category": category, "conversation_turn": conversation_turn,
            **trasferimento,
        })
    except Exception as e:
        fname = logsvc.save_log_file(session_id, question, model["name"], model["provider"], category,
                                      sql, rows, None, lat_sql, lat_db, 0, len(rows), error=str(e))
        logsvc.save_to_db_log(session_id, model["name"], model["provider"], question, category,
                               sql, None, len(rows), lat_sql, lat_db, 0, tokens_prompt_sql, tokens_completion_sql,
                               tokens_prompt_sql + tokens_completion_sql, fname, str(e),
                               conversation_turn=conversation_turn)
        conv.append_turn(session_id, question, sql, f"[errore: {e}]")
        return jsonify({"error": str(e), "log_filename": fname}), 500


@bp.route("/conversation/new", methods=["POST"])
def new_conversation():
    """Crea esplicitamente un nuovo session_id (= nuova chat = memoria vuota)."""
    return jsonify({"session_id": str(uuid.uuid4())})


@bp.route("/conversation/<session_id>/turns", methods=["GET"])
def get_conversation_turns(session_id):
    """Ritorna i turni persistiti di una sessione (da query_log) e ricostruisce
    la memoria conversazionale in-process, così una chat riaperta dalla sidebar
    riprende con il contesto corretto anche dopo un riavvio del backend."""
    try:
        with log_table_cursor(dict_rows=True) as (connection, cur):
            cur.execute("""
                SELECT id, timestamp, question, sql_generated AS sql, nl_response,
                       rows_returned, latency_sql_ms, latency_db_ms, latency_nl_ms,
                       tokens_total, log_filename, error, model
                FROM query_log
                WHERE session_id = %s AND is_benchmark IS NOT TRUE
                ORDER BY timestamp ASC, id ASC
            """, (session_id,))
            turns = [dict(r) for r in cur.fetchall()]
        conv.rebuild_session(session_id, [
            {"question": t["question"], "sql": t["sql"] or "",
             "nl_response": t["nl_response"] or (f"[errore: {t['error']}]" if t.get("error") else "")}
            for t in turns
        ])
        return jsonify({"session_id": session_id, "turns": turns})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/conversation/<session_id>/edit-turn", methods=["POST"])
def edit_turn(session_id):
    """
    Modifica una domanda precedente: tronca la memoria della sessione a partire
    da quel turno (escluso), così il prossimo invio ripartirà da history corretta.
    Il frontend si occupa di rimuovere visivamente i turni successivi.
    """
    d = request.json or {}
    turn_index = d.get("turn_index")
    if turn_index is None or not isinstance(turn_index, int) or turn_index < 0:
        return jsonify({"error": "turn_index non valido"}), 400
    conv.truncate_after(session_id, turn_index)
    return jsonify({"status": "ok"})


@bp.route("/conversation/<session_id>/reset", methods=["POST"])
def reset_conversation(session_id):
    conv.reset_session(session_id)
    return jsonify({"status": "ok"})
