"""
Benchmark multi-modello:
  - oneshot: una domanda, eseguita in parallelo (in sequenza per semplicità
    e per non saturare i rate-limit dei provider) su N modelli.
  - conversazionale: uno "scenario" di M domande in sequenza, eseguito per
    ognuno degli N modelli mantenendo una history INDIPENDENTE per modello
    (le history non si mescolano tra modelli: il confronto deve essere equo).
"""
import time
import uuid

from ..config import settings
from . import nl2sql_service as nl2sql
from . import conversation_service as conv
from . import logging_service as logsvc
from ..models_repository import get_model
from ..transfer import somma_byte


def run_oneshot_benchmark(question, model_ids, system_prompt):
    session_id = str(uuid.uuid4())
    results = []
    for idx, mid in enumerate(model_ids):
        # Pausa tra un modello e il successivo: i limiti TPM del provider sono
        # condivisi a livello di organizzazione, e i modelli eseguiti in rapida
        # sequenza si "rubavano" il budget a vicenda (errori 429 a catena che
        # falsavano latenze ed esiti del confronto).
        if idx > 0 and settings.BENCHMARK_MODEL_DELAY_S > 0:
            time.sleep(settings.BENCHMARK_MODEL_DELAY_S)
        model = get_model(mid)
        if not model:
            results.append({"model_id": mid, "error": "Modello non trovato"})
            continue
        result = {"model_id": mid, "model_name": model["name"], "provider": model["provider"]}
        try:
            gen = nl2sql.generate_sql(question, model, system_prompt, session_id=None, use_history=False)
            result.update(sql=gen["sql"], latency_sql=gen["latency_ms"],
                          tokens_prompt_sql=gen["tokens_prompt"], tokens_completion_sql=gen["tokens_completion"])
            # Byte della fase SQL, che le eventuali riparazioni faranno crescere.
            b_req = gen["llm_bytes_request"]
            b_resp = gen["llm_bytes_response"]
            b_wire = gen["llm_bytes_response_wire"]

            final_sql, cols, rows, db_error, truncated, latency_db, repair, db_transfer = \
                nl2sql.execute_with_repair(question, gen["sql"], model, system_prompt)
            db_transfer = db_transfer or {}
            result["latency_db"] = latency_db
            if repair:
                # La riparazione fa parte della fase SQL: latenza, token E byte
                # extra vengono attribuiti lì, così i totali restano confrontabili.
                result["latency_sql"] = gen["latency_ms"] + repair["latency_ms"]
                gen["tokens_prompt"] += repair["tokens_prompt"]
                gen["tokens_completion"] += repair["tokens_completion"]
                b_req = somma_byte(b_req, repair["llm_bytes_request"])
                b_resp = somma_byte(b_resp, repair["llm_bytes_response"])
                b_wire = somma_byte(b_wire, repair["llm_bytes_response_wire"])
                result.update(sql=final_sql, sql_repaired=repair["succeeded"],
                              sql_original=repair["original_sql"],
                              repair_original_error=repair["original_error"])
            if db_error:
                result["error"] = db_error
                result["latency_nl"] = 0
                # Il traffico della fase SQL è stato speso comunque: azzerarlo
                # farebbe sembrare gratuiti i modelli che sbagliano di più.
                result.update(
                    llm_bytes_request=b_req, llm_bytes_response=b_resp,
                    llm_bytes_response_wire=b_wire,
                    db_bytes_query=db_transfer.get("query_bytes"),
                    db_bytes_result_payload=db_transfer.get("result_payload_bytes"))
                results.append(result)
                continue

            result.update(rows_count=len(rows), columns=cols, rows=rows[:50], truncated=truncated)

            exp = nl2sql.explain_results(question, final_sql, rows, truncated, model,
                                          session_id=None, use_history=False)
            tokens_prompt = gen["tokens_prompt"] + exp["tokens_prompt"]
            tokens_completion = gen["tokens_completion"] + exp["tokens_completion"]
            trasferimento = {
                "llm_bytes_request": somma_byte(b_req, exp["llm_bytes_request"]),
                "llm_bytes_response": somma_byte(b_resp, exp["llm_bytes_response"]),
                "llm_bytes_response_wire": somma_byte(b_wire, exp["llm_bytes_response_wire"]),
                "db_bytes_query": db_transfer.get("query_bytes"),
                "db_bytes_result_payload": db_transfer.get("result_payload_bytes"),
            }
            result.update(
                nl_response=exp["nl_response"], latency_nl=exp["latency_ms"],
                latency_ms=result["latency_sql"] + latency_db + exp["latency_ms"],
                tokens_prompt=tokens_prompt, tokens_completion=tokens_completion,
                tokens_total=tokens_prompt + tokens_completion,
                **trasferimento,
            )

            category = nl2sql.classify_question(question)
            result["category"] = category

            log_id = logsvc.save_to_db_log(
                session_id, model["name"], model["provider"], question, f"benchmark:{category}",
                final_sql, exp["nl_response"], len(rows), result["latency_sql"], latency_db, exp["latency_ms"],
                tokens_prompt, tokens_completion, tokens_prompt + tokens_completion,
                is_benchmark=True, benchmark_run_id=session_id, transfer=trasferimento,
            )
            fname = logsvc.save_log_file(
                session_id, question, model["name"], model["provider"], f"benchmark:{category}",
                final_sql, rows, exp["nl_response"], result["latency_sql"], latency_db, exp["latency_ms"],
                len(rows), tokens_prompt, tokens_completion, tokens_prompt + tokens_completion,
                log_id, truncated=truncated, transfer=trasferimento,
            )
            result["log_id"], result["log_filename"] = log_id, fname
            logsvc.update_log_filename(log_id, fname)

        except Exception as e:
            result["error"] = str(e)

        results.append(result)

    benchmark_filename = logsvc.save_benchmark_file(session_id, question, results)
    return {"session_id": session_id, "question": question, "results": results,
            "benchmark_filename": benchmark_filename}


def run_conversational_benchmark(scenario_questions, model_ids, system_prompt):
    """
    scenario_questions: lista ordinata di domande che simulano una conversazione.
    Per ogni modello viene creata una sessione di memoria dedicata e isolata
    (session_id univoco per modello), così ogni modello costruisce la propria
    history senza interferenze dagli altri.
    """
    run_id = str(uuid.uuid4())
    results = []
    for idx, mid in enumerate(model_ids):
        if idx > 0 and settings.BENCHMARK_MODEL_DELAY_S > 0:
            time.sleep(settings.BENCHMARK_MODEL_DELAY_S)
        model = get_model(mid)
        if not model:
            results.append({"model_id": mid, "error": "Modello non trovato"})
            continue

        model_session_id = f"{run_id}:{mid}"
        result = {"model_id": mid, "model_name": model["name"], "provider": model["provider"], "turns": []}

        for question in scenario_questions:
            turn = {"question": question}
            try:
                gen = nl2sql.generate_sql(question, model, system_prompt, session_id=model_session_id, use_history=True)
                turn.update(sql=gen["sql"], latency_sql=gen["latency_ms"])
                b_req = gen["llm_bytes_request"]
                b_resp = gen["llm_bytes_response"]
                b_wire = gen["llm_bytes_response_wire"]

                final_sql, cols, rows, db_error, truncated, latency_db, repair, db_transfer = \
                    nl2sql.execute_with_repair(question, gen["sql"], model, system_prompt)
                db_transfer = db_transfer or {}
                turn["latency_db"] = latency_db
                if repair:
                    turn["latency_sql"] = gen["latency_ms"] + repair["latency_ms"]
                    gen["tokens_prompt"] += repair["tokens_prompt"]
                    gen["tokens_completion"] += repair["tokens_completion"]
                    b_req = somma_byte(b_req, repair["llm_bytes_request"])
                    b_resp = somma_byte(b_resp, repair["llm_bytes_response"])
                    b_wire = somma_byte(b_wire, repair["llm_bytes_response_wire"])
                    turn.update(sql=final_sql, sql_repaired=repair["succeeded"],
                                sql_original=repair["original_sql"],
                                repair_original_error=repair["original_error"])
                if db_error:
                    turn["error"] = db_error
                    turn.update(
                        llm_bytes_request=b_req, llm_bytes_response=b_resp,
                        llm_bytes_response_wire=b_wire,
                        db_bytes_query=db_transfer.get("query_bytes"),
                        db_bytes_result_payload=db_transfer.get("result_payload_bytes"))
                    conv.append_turn(model_session_id, question, final_sql, f"[errore: {db_error}]")
                    result["turns"].append(turn)
                    continue

                turn.update(rows_count=len(rows), columns=cols, rows=rows[:30], truncated=truncated)

                exp = nl2sql.explain_results(question, final_sql, rows, truncated, model,
                                              session_id=model_session_id, use_history=True)
                tokens_prompt = gen["tokens_prompt"] + exp["tokens_prompt"]
                tokens_completion = gen["tokens_completion"] + exp["tokens_completion"]
                trasferimento = {
                    "llm_bytes_request": somma_byte(b_req, exp["llm_bytes_request"]),
                    "llm_bytes_response": somma_byte(b_resp, exp["llm_bytes_response"]),
                    "llm_bytes_response_wire": somma_byte(b_wire, exp["llm_bytes_response_wire"]),
                    "db_bytes_query": db_transfer.get("query_bytes"),
                    "db_bytes_result_payload": db_transfer.get("result_payload_bytes"),
                }
                turn.update(
                    nl_response=exp["nl_response"], latency_nl=exp["latency_ms"],
                    tokens_prompt=tokens_prompt, tokens_completion=tokens_completion,
                    tokens_total=tokens_prompt + tokens_completion,
                    **trasferimento,
                )
                conv.append_turn(model_session_id, question, final_sql, exp["nl_response"])

                logsvc.save_to_db_log(
                    model_session_id, model["name"], model["provider"], question, "benchmark_conv",
                    final_sql, exp["nl_response"], len(rows), turn["latency_sql"], latency_db, exp["latency_ms"],
                    tokens_prompt, tokens_completion, tokens_prompt + tokens_completion,
                    conversation_turn=len(result["turns"]) + 1, is_benchmark=True,
                    benchmark_run_id=run_id, transfer=trasferimento,
                )

            except Exception as e:
                turn["error"] = str(e)

            result["turns"].append(turn)

        results.append(result)

    scenario_label = " → ".join(scenario_questions[:3]) + (" → ..." if len(scenario_questions) > 3 else "")
    benchmark_filename = logsvc.save_benchmark_file(run_id, scenario_label, results, conversational=True,
                                                      scenario=scenario_questions)
    return {"run_id": run_id, "scenario": scenario_questions, "results": results,
            "benchmark_filename": benchmark_filename}
