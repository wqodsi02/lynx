from flask import Blueprint, jsonify

from .. import persistence_state
from ..db.query_log_schema import log_table_cursor

bp = Blueprint("analytics", __name__, url_prefix="/api/analytics")


@bp.route("", methods=["GET"])
def get_analytics():
    # Nota sull'aggregato `sum_llm_bytes_total`: il CASE distingue "nessuna riga
    # misurata" da "misurato, era zero". Senza, un modello le cui righe sono
    # tutte precedenti alla misurazione dei transfer rate riporterebbe 0,
    # affermando una misura mai fatta. SUM ignora i NULL, quindi sui modelli
    # misti continua a sommare solo le righe effettivamente misurate.
    #
    # Ogni interrogazione ha il proprio try: se ne cade una, le altre arrivano
    # comunque. Meta' pannello e' meglio di niente, purche' sia dichiarato che
    # e' meta' — e qui si risponde 200 con il motivo, perche' un 500 non
    # distingue "rotto" da "vuoto", che e' l'ambiguita' dietro a tre mesi di
    # guasto invisibile.
    dati = {"by_model": [], "by_category": [], "top_questions": [], "daily": []}
    guasto = None
    try:
        with log_table_cursor(dict_rows=True) as (conn, cur):
            try:
                cur.execute("""
                SELECT
                    model, provider,
                    COUNT(*) AS total_queries,
                    ROUND(AVG(latency_sql_ms))::int AS avg_lat_sql,
                    ROUND(AVG(latency_db_ms))::int  AS avg_lat_db,
                    ROUND(AVG(latency_nl_ms))::int  AS avg_lat_nl,
                    ROUND(AVG(latency_ms))::int     AS avg_lat_total,
                    ROUND(AVG(tokens_prompt))::int     AS avg_tokens_prompt,
                    ROUND(AVG(tokens_completion))::int AS avg_tokens_completion,
                    ROUND(AVG(tokens_total))::int      AS avg_tokens_total,
                    SUM(tokens_total)                  AS sum_tokens_total,
                    ROUND(AVG(llm_bytes_request))::int  AS avg_llm_bytes_request,
                    ROUND(AVG(llm_bytes_response))::int AS avg_llm_bytes_response,
                    SUM(CASE WHEN llm_bytes_request IS NULL
                                  AND llm_bytes_response IS NULL
                             THEN NULL
                             ELSE COALESCE(llm_bytes_request, 0)
                                  + COALESCE(llm_bytes_response, 0)
                        END) AS sum_llm_bytes_total,
                    COUNT(*) FILTER (WHERE feedback='positive') AS feedback_pos,
                    COUNT(*) FILTER (WHERE feedback='negative') AS feedback_neg,
                    COUNT(*) FILTER (WHERE sql_correct='yes')   AS sql_ok,
                    COUNT(*) FILTER (WHERE sql_correct='no')    AS sql_ko,
                    COUNT(*) FILTER (WHERE sql_correct='partial') AS sql_partial,
                    COUNT(*) FILTER (WHERE error IS NOT NULL)   AS errors
                FROM query_log
                WHERE model IS NOT NULL
                GROUP BY model, provider
                ORDER BY total_queries DESC
            """)
                dati["by_model"] = [dict(r) for r in cur.fetchall()]
            except Exception as e:
                guasto = e
                # Senza rollback la transazione resta abortita e le
                # interrogazioni successive fallirebbero a cascata: il
                # recupero parziale sarebbe solo apparente.
                conn.rollback()
            try:
                cur.execute("""
                SELECT category, COUNT(*) AS count
                FROM query_log WHERE category IS NOT NULL
                GROUP BY category ORDER BY count DESC
            """)
                dati["by_category"] = [dict(r) for r in cur.fetchall()]
            except Exception as e:
                guasto = e
                # Senza rollback la transazione resta abortita e le
                # interrogazioni successive fallirebbero a cascata: il
                # recupero parziale sarebbe solo apparente.
                conn.rollback()
            try:
                cur.execute("""
                SELECT question, COUNT(*) AS count
                FROM query_log GROUP BY question ORDER BY count DESC LIMIT 10
            """)
                dati["top_questions"] = [dict(r) for r in cur.fetchall()]
            except Exception as e:
                guasto = e
                # Senza rollback la transazione resta abortita e le
                # interrogazioni successive fallirebbero a cascata: il
                # recupero parziale sarebbe solo apparente.
                conn.rollback()
            try:
                cur.execute("""
                SELECT DATE(timestamp) AS day, COUNT(*) AS queries
                FROM query_log WHERE timestamp > NOW() - INTERVAL '14 days'
                GROUP BY day ORDER BY day
            """)
                dati["daily"] = [dict(r) for r in cur.fetchall()]
            except Exception as e:
                guasto = e
                # Senza rollback la transazione resta abortita e le
                # interrogazioni successive fallirebbero a cascata: il
                # recupero parziale sarebbe solo apparente.
                conn.rollback()
    except Exception as e:
        guasto = e

    if guasto is not None:
        persistence_state.registra_fallimento(guasto)
    # Una lettura riuscita NON prova che le scritture funzionino, quindi non
    # si registra alcun successo: lo stato lo azzera chi scrive davvero.
    dati["persistenza"] = persistence_state.per_api()
    return jsonify(dati)
