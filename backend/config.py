"""
Configurazione centralizzata dell'applicazione LYNX.
Tutti i parametri sensibili/ambientali si leggono da variabili d'ambiente
(file .env in sviluppo locale). Nessuna mutazione runtime non validata:
la configurazione DB può essere aggiornata solo tramite API dedicate che
validano l'input (vedi api/db_routes.py).
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
BENCHMARK_DIR = LOGS_DIR / "benchmark"
MODELS_FILE = BASE_DIR / "data" / "models.json"
SECRET_KEY_FILE = BASE_DIR / "data" / "secret.key"

LOGS_DIR.mkdir(exist_ok=True, parents=True)
BENCHMARK_DIR.mkdir(exist_ok=True, parents=True)
MODELS_FILE.parent.mkdir(exist_ok=True, parents=True)


@dataclass
class DBConfig:
    host: str = field(default_factory=lambda: os.getenv("DB_HOST", "10.22.255.161"))
    port: int = field(default_factory=lambda: int(os.getenv("DB_PORT", "5432")))
    dbname: str = field(default_factory=lambda: os.getenv("DB_NAME", ""))
    user: str = field(default_factory=lambda: os.getenv("DB_USER", ""))
    password: str = field(default_factory=lambda: os.getenv("DB_PASSWORD", ""))
    connect_timeout: int = 8

    def as_psycopg_kwargs(self):
        return {
            "host": self.host, "port": self.port, "dbname": self.dbname,
            "user": self.user, "password": self.password,
            "connect_timeout": self.connect_timeout,
        }

    def update(self, host=None, port=None, dbname=None, user=None, password=None):
        """Aggiorna solo i campi forniti e validi. Ritorna (ok, errore)."""
        if host is not None:
            if not isinstance(host, str) or not host.strip():
                return False, "host non valido"
            self.host = host.strip()
        if port is not None:
            try:
                p = int(port)
                if not (1 <= p <= 65535):
                    return False, "porta fuori range"
                self.port = p
            except (TypeError, ValueError):
                return False, "porta non numerica"
        if dbname is not None:
            self.dbname = str(dbname).strip()
        if user is not None:
            self.user = str(user).strip()
        if password:
            self.password = password
        return True, None

    def public_dict(self):
        return {"db_host": self.host, "db_port": self.port,
                "db_name": self.dbname, "db_user": self.user}


class Settings:
    MAX_ROWS_HARD = int(os.getenv("MAX_ROWS_HARD", "5000"))
    STATEMENT_TIMEOUT_MS = int(os.getenv("STATEMENT_TIMEOUT_MS", "30000"))
    LLM_TIMEOUT_S = int(os.getenv("LLM_TIMEOUT_S", "60"))
    # --- Resilienza indipendente dal modello ---
    # Righe massime dei risultati inviate all'LLM in Fase 2: con 200 righe i
    # prompt superavano i limiti TPM dei tier gratuiti (errore 413) e il costo
    # in token variava enormemente tra modelli, falsando i confronti.
    MAX_ROWS_TO_LLM = int(os.getenv("MAX_ROWS_TO_LLM", "50"))
    # Tentativi di auto-correzione quando una query generata fallisce sul DB:
    # l'errore esatto di PostgreSQL viene reinviato al modello per la correzione.
    SQL_REPAIR_ATTEMPTS = int(os.getenv("SQL_REPAIR_ATTEMPTS", "1"))
    # Retry quando il modello restituisce una risposta senza alcuna query
    # (tipico dei modelli reasoning che esauriscono i token nel "pensiero").
    EMPTY_SQL_RETRIES = int(os.getenv("EMPTY_SQL_RETRIES", "1"))
    # Pausa (secondi) tra un modello e il successivo nei benchmark, per non
    # esaurire i limiti TPM condivisi del provider (errori 429 a catena).
    BENCHMARK_MODEL_DELAY_S = int(os.getenv("BENCHMARK_MODEL_DELAY_S", "12"))
    # Soglie di escalation per i fallimenti di persistenza su query_log: un
    # fallimento "indeterminato" (tipicamente in fase di connessione) diventa
    # un allarme grave quando supera una delle due, quella che arriva prima.
    PERSISTENZA_FALLIMENTI_PER_ALLARME = int(os.getenv("PERSISTENZA_FALLIMENTI_PER_ALLARME", "5"))
    PERSISTENZA_MINUTI_PER_ALLARME = int(os.getenv("PERSISTENZA_MINUTI_PER_ALLARME", "10"))
    CONVERSATION_MAX_TURNS = int(os.getenv("CONVERSATION_MAX_TURNS", "20"))
    CONVERSATION_TTL_MINUTES = int(os.getenv("CONVERSATION_TTL_MINUTES", "240"))
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    PORT = int(os.getenv("PORT", "5000"))


db_config = DBConfig()
settings = Settings()
