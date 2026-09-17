"""
Gestione della connessione al database PostgreSQL del progetto.
Connessioni aperte on-demand (niente pool persistente: l'app gira in locale,
basso volume di richieste, e la VPN universitaria può cadere — meglio
fallire rapidamente su una nuova connessione che tenere un pool stantio).
"""
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from ..config import db_config


def get_connection():
    return psycopg2.connect(**db_config.as_psycopg_kwargs())


@contextmanager
def db_cursor(dict_rows: bool = False):
    """Context manager: apre connessione+cursore e garantisce SEMPRE la
    chiusura, anche in caso di eccezione (prima le connessioni restavano
    aperte se una query falliva a metà — leak silenzioso verso il DB
    universitario). Non fa commit automatico: va chiamato esplicitamente
    dal chiamante per le scritture su query_log."""
    conn = get_connection()
    try:
        factory = psycopg2.extras.RealDictCursor if dict_rows else None
        cur = conn.cursor(cursor_factory=factory)
        yield conn, cur
    finally:
        conn.close()


def test_connection():
    try:
        conn = get_connection()
        conn.close()
        return True, "Connessione al DB riuscita"
    except psycopg2.OperationalError as e:
        return False, f"Impossibile collegarsi al DB (host/VPN/credenziali?): {e}"
    except Exception as e:
        return False, str(e)
