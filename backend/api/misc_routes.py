import csv
import io

from flask import Blueprint, Response, jsonify, request, send_from_directory

from .. import persistence_state
from ..config import BENCHMARK_DIR, LOGS_DIR
from ..db.query_log_schema import log_table_cursor
from ..services import logging_service as logsvc

bp = Blueprint("misc", __name__, url_prefix="/api")


def _safe_txt_filename(filename):
    return filename.endswith(".txt") and "/" not in filename and "\\" not in filename


@bp.route("/logs", methods=["GET"])
def get_logs():
    return jsonify(logsvc.list_log_files())


@bp.route("/logs/<filename>", methods=["GET"])
def get_log_content(filename):
    if not _safe_txt_filename(filename):
        return jsonify({"error": "File non valido"}), 400
    content = logsvc.read_log_file(filename)
    if content is None:
        return jsonify({"error": "File non trovato"}), 404
    return jsonify({"filename": filename, "content": content})


@bp.route("/logs/<filename>/download", methods=["GET"])
def download_log(filename):
    if not _safe_txt_filename(filename):
        return jsonify({"error": "File non valido"}), 400
    return send_from_directory(LOGS_DIR, filename, as_attachment=True)


@bp.route("/benchmark/logs", methods=["GET"])
def get_benchmark_logs():
    return jsonify(logsvc.list_benchmark_files())


@bp.route("/benchmark/logs/<filename>", methods=["GET"])
def get_benchmark_log_content(filename):
    if not _safe_txt_filename(filename):
        return jsonify({"error": "File non valido"}), 400
    content = logsvc.read_benchmark_file(filename)
    if content is None:
        return jsonify({"error": "File non trovato"}), 404
    return jsonify({"filename": filename, "content": content})


@bp.route("/benchmark/logs/<filename>/download", methods=["GET"])
def download_benchmark_log(filename):
    if not _safe_txt_filename(filename):
        return jsonify({"error": "File non valido"}), 400
    return send_from_directory(BENCHMARK_DIR, filename, as_attachment=True)


@bp.route("/history", methods=["GET"])
def get_history():
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        limit = 50
    # 200 anche quando la persistenza e' rotta, con il motivo: un 500 non
    # distingue "il sistema e' rotto" da "non ci sono dati", ed e' l'ambiguita'
    # che ha tenuto nascosto il guasto per tre mesi.
    righe = []
    try:
        with log_table_cursor(dict_rows=True) as (conn, cur):
            cur.execute("""
                SELECT id, session_id, conversation_turn, timestamp, model, provider, question, category,
                       sql_generated, sql_correct, nl_response, rows_returned,
                       latency_sql_ms, latency_db_ms, latency_nl_ms, latency_ms,
                       tokens_prompt, tokens_completion, tokens_total,
                       feedback, log_filename, error
                FROM query_log WHERE is_benchmark IS NOT TRUE
                ORDER BY timestamp DESC LIMIT %s
            """, (limit,))
            righe = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        persistence_state.registra_fallimento(e)
    return jsonify({"rows": righe, "persistenza": persistence_state.per_api()})


@bp.route("/suggestions", methods=["GET"])
def get_suggestions():
    try:
        with log_table_cursor(dict_rows=True) as (conn, cur):
            cur.execute("""
                SELECT question, COUNT(*) AS count, category
                FROM query_log
                WHERE feedback='positive' OR feedback IS NULL
                GROUP BY question, category
                ORDER BY count DESC LIMIT 8
            """)
            rows = [dict(r) for r in cur.fetchall()]
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/feedback", methods=["POST"])
def save_feedback():
    d = request.json or {}
    log_id = d.get("log_id")
    feedback = d.get("feedback")
    sql_correct = d.get("sql_correct")
    if not log_id:
        return jsonify({"error": "log_id mancante"}), 400
    if feedback and feedback not in ("positive", "negative"):
        return jsonify({"error": "feedback non valido"}), 400
    if sql_correct and sql_correct not in ("yes", "no", "partial", "pending"):
        return jsonify({"error": "sql_correct non valido"}), 400
    try:
        with log_table_cursor() as (conn, cur):
            if feedback:
                cur.execute("UPDATE query_log SET feedback=%s WHERE id=%s", (feedback, log_id))
            if sql_correct:
                cur.execute("UPDATE query_log SET sql_correct=%s WHERE id=%s", (sql_correct, log_id))
            conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/export/csv", methods=["POST"])
def export_csv():
    d = request.json or {}
    rows = d.get("rows", [])
    columns = d.get("columns", [])
    question = d.get("question", "export")
    if not rows or not columns:
        return jsonify({"error": "Nessun dato da esportare"}), 400

    output = io.StringIO()
    output.write("\ufeff")  # BOM per Excel su Windows
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in columns})

    filename = logsvc.slugify(question)[:40] + ".csv"
    return Response(
        output.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
