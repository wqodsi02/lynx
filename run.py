"""
Entrypoint per l'esecuzione locale.
Uso:
    python run.py
Variabili d'ambiente (vedi .env.example): DB_HOST, DB_PORT, DB_NAME, DB_USER,
DB_PASSWORD, FLASK_DEBUG, PORT.

Usa waitress (server WSGI multi-thread, stabile) se disponibile — è quello
consigliato anche per esecuzione solo-locale, perché il server di sviluppo
integrato in Flask non è pensato per gestire più richieste concorrenti (es.
un benchmark su più modelli) in modo affidabile. Se waitress non è
installato, ripiega sul server di sviluppo Flask con un avviso.
"""
from backend.app import create_app
from backend.config import settings, LOGS_DIR

app = create_app()

if __name__ == "__main__":
    print("=" * 60)
    print("LYNX — Industrial Network Intelligence")
    print(f"Backend in ascolto su: http://localhost:{settings.PORT}")
    print(f"Logs: {LOGS_DIR}")
    print("Modalita: SOLA LETTURA (nessuna scrittura sul DB possibile)")
    print("=" * 60)

    try:
        from waitress import serve
        print("Server: waitress (multi-thread, adatto anche a benchmark concorrenti)")
        serve(app, host="0.0.0.0", port=settings.PORT, threads=8)
    except ImportError:
        print("Server: Flask dev server (waitress non installato — `pip install waitress`")
        print("per un server più stabile in caso di richieste concorrenti, es. benchmark multi-modello)")
        app.run(debug=settings.DEBUG, port=settings.PORT, threaded=True)
