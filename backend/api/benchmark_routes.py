from flask import Blueprint, request, jsonify
from ..system_prompt import SYSTEM_PROMPT_GENERATION
from ..services import benchmark_service as bench

bp = Blueprint("benchmark", __name__, url_prefix="/api/benchmark")


@bp.route("/run", methods=["POST"])
def benchmark_run():
    """Benchmark oneshot: una domanda su N modelli, nessuna memoria condivisa."""
    d = request.json or {}
    question = (d.get("question") or "").strip()
    model_ids = d.get("model_ids", [])
    if not question:
        return jsonify({"error": "Domanda vuota"}), 400
    if not model_ids:
        return jsonify({"error": "Seleziona almeno un modello"}), 400
    result = bench.run_oneshot_benchmark(question, model_ids, SYSTEM_PROMPT_GENERATION)
    return jsonify(result)


@bp.route("/run-conversation", methods=["POST"])
def benchmark_run_conversation():
    """
    Benchmark conversazionale: simula una conversazione di più domande in
    sequenza ("scenario") su N modelli, ognuno con la propria memoria isolata.
    Payload: { "questions": [...], "model_ids": [...] }
    """
    d = request.json or {}
    questions = [q.strip() for q in d.get("questions", []) if q and q.strip()]
    model_ids = d.get("model_ids", [])
    if not questions:
        return jsonify({"error": "Lo scenario deve contenere almeno una domanda"}), 400
    if len(questions) > 15:
        return jsonify({"error": "Massimo 15 domande per scenario"}), 400
    if not model_ids:
        return jsonify({"error": "Seleziona almeno un modello"}), 400
    result = bench.run_conversational_benchmark(questions, model_ids, SYSTEM_PROMPT_GENERATION)
    return jsonify(result)
