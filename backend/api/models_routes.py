from flask import Blueprint, request, jsonify
from .. import models_repository as repo

bp = Blueprint("models", __name__, url_prefix="/api/models")


@bp.route("", methods=["GET"])
def get_models():
    return jsonify(repo.list_models())


@bp.route("", methods=["POST"])
def add_model():
    d = request.json or {}
    for f in ["name", "provider", "model_string", "api_key"]:
        if not d.get(f):
            return jsonify({"error": f"Campo mancante: {f}"}), 400
    if d["provider"].lower() not in ("groq", "gemini"):
        return jsonify({"error": "Provider non supportato. Usare 'groq' o 'gemini'."}), 400
    m = repo.add_model(d["name"], d["provider"], d["model_string"], d["api_key"])
    return jsonify(m)


@bp.route("/<mid>", methods=["DELETE"])
def delete_model(mid):
    repo.delete_model(mid)
    return jsonify({"status": "ok"})


@bp.route("/<mid>", methods=["PUT"])
def update_model(mid):
    d = request.json or {}
    if "provider" in d and d["provider"].lower() not in ("groq", "gemini"):
        return jsonify({"error": "Provider non supportato. Usare 'groq' o 'gemini'."}), 400
    found = repo.update_model(mid, name=d.get("name"), provider=d.get("provider"),
                               model_string=d.get("model_string"), api_key=d.get("api_key"))
    if not found:
        return jsonify({"error": "Modello non trovato"}), 404
    return jsonify({"status": "ok"})
