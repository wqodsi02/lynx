from flask import Blueprint, jsonify, request
from ..config import db_config
from ..db.connection import test_connection

bp = Blueprint("db_config", __name__, url_prefix="/api")


@bp.route("/config/db", methods=["GET"])
def get_db_config():
    return jsonify(db_config.public_dict())


@bp.route("/config/db", methods=["POST"])
def set_db_config():
    d = request.json or {}
    ok, err = db_config.update(
        host=d.get("db_host"), port=d.get("db_port"),
        dbname=d.get("db_name"), user=d.get("db_user"),
        password=d.get("db_password"),
    )
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"status": "ok"})


@bp.route("/db/test", methods=["GET"])
def test_db():
    ok, message = test_connection()
    if ok:
        return jsonify({"status": "ok", "message": message})
    return jsonify({"status": "error", "message": message}), 500
