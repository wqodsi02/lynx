"""
Application factory: registra blueprint, CORS, static, error handler globali
e il JSON provider personalizzato.
Tenere questo file minimale: niente logica qui, solo wiring.
"""
import logging

from flask import Flask, jsonify, send_from_directory
from flask.json.provider import DefaultJSONProvider
from flask_cors import CORS

from . import json_encoding, persistence_state
from .config import settings
from .llm.base import LLMProviderError
from .api import models_routes, db_routes, query_routes, benchmark_routes, analytics_routes, misc_routes


class LynxJSONProvider(DefaultJSONProvider):
    """JSON provider esteso per i tipi restituiti da psycopg2.

    Il provider di default di Flask 3 non fallisce sui tipi che psycopg2
    restituisce più spesso: li converte, ma nella forma sbagliata. Verificato
    empiricamente su Flask 3.0.3:
      - Decimal (con cui psycopg2 rappresenta ogni colonna NUMERIC, quindi
        l'output di ROUND/AVG/SUM) diventa una STRINGA: "12", "4.99".
      - date diventa una data in formato HTTP ("Sat, 04 Jul 2026 00:00:00 GMT"),
        non ISO.
      - time è l'unico di questi tipi che solleva davvero TypeError.

    Il guasto quindi non è un errore 500 rumoroso, ma una degradazione
    silenziosa lato frontend: un throughput medio arriva come "4.99" invece di
    4.99, e in JavaScript una stringa si ordina lessicograficamente ("10" prima
    di "9"), si concatena invece di sommarsi e non è plottabile in un grafico.
    Nessun errore visibile, solo numeri sbagliati sotto gli occhi dell'utente.

    Questo override converte Decimal in int/float, date e orari in ISO 8601, e
    copre anche i tipi che il provider di serie non gestisce affatto.
    """

    @staticmethod
    def default(o):
        try:
            return json_encoding.default(o)
        except TypeError:
            # Tipi che json_encoding non conosce: li gestisce il provider di
            # serie di Flask (dataclass, oggetti con __html__), che a sua volta
            # solleva TypeError su quelli davvero ignoti.
            return DefaultJSONProvider.default(o)


def _configure_logging():
    level = logging.DEBUG if settings.DEBUG else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Riduci il rumore delle librerie di terze parti, tieni i nostri log.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def create_app():
    _configure_logging()
    app = Flask(__name__, static_folder="../static", static_url_path="")
    app.json = LynxJSONProvider(app)
    CORS(app)

    app.register_blueprint(models_routes.bp)
    app.register_blueprint(db_routes.bp)
    app.register_blueprint(query_routes.bp)
    app.register_blueprint(benchmark_routes.bp)
    app.register_blueprint(analytics_routes.bp)
    app.register_blueprint(misc_routes.bp)

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/api/health", methods=["GET"])
    def health():
        # VINCOLO: questa rotta non apre connessioni al database. Con
        # connect_timeout=8 su VPN instabile, un health check che si blocca
        # otto secondi e' peggio che inutile. Lo stato di persistenza viene
        # letto dalla cache in-process, aggiornata da chi scrive davvero.
        return jsonify({"status": "ok", "service": "LYNX backend",
                        "persistenza": persistence_state.per_api()})

    @app.errorhandler(LLMProviderError)
    def llm_error(e):
        # Errore "atteso" del provider LLM (key non valida, modello inesistente,
        # rate limit esaurito...): messaggio leggibile, non stack trace.
        return jsonify({"error": str(e)}), 502

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Endpoint non trovato"}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"error": "Errore interno del server. Controlla i log del backend."}), 500

    return app
