from flask import Blueprint, jsonify, request

from .. import persistence_state
from ..config import db_config
from ..db.connection import get_connection
from ..db.query_log_schema import verify_schema

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
    """Verifica ATTIVA: apre davvero una connessione e controlla lo schema.

    E' la controparte di /api/health, che invece non tocca il database. Qui il
    costo di una connessione lenta e' accettabile perche' l'utente l'ha chiesta
    esplicitamente.
    """
    try:
        conn = get_connection()
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Impossibile collegarsi al DB (host/VPN/credenziali?): {e}",
        }), 500

    try:
        mancanti = verify_schema(conn)
    except Exception as e:
        return jsonify({
            "status": "ok",
            "message": f"Connessione riuscita, ma lo schema non e' verificabile: {e}",
            "schema_ok": None, "colonne_mancanti": [],
        })
    finally:
        conn.close()

    persistence_state.registra_schema(not mancanti, mancanti)
    if mancanti:
        # Una connessione riuscita con lo schema incompleto NON e' "tutto a
        # posto": e' esattamente lo stato in cui l'applicazione e' rimasta per
        # tre mesi senza che nessuno se ne accorgesse.
        return jsonify({
            "status": "ok",
            "message": "Connessione riuscita, ma lo schema di query_log e' "
                       "incompleto: mancano %s. Le scritture falliranno."
                       % ", ".join(mancanti),
            "schema_ok": False, "colonne_mancanti": mancanti,
        })
    return jsonify({
        "status": "ok",
        "message": "Connessione al DB riuscita, schema completo",
        "schema_ok": True, "colonne_mancanti": [],
    })
