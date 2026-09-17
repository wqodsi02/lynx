"""
Memoria conversazionale in-process, isolata per session_id.
Ogni sessione mantiene una lista di turni (domanda + SQL + risposta NL).
Quando si chiama l'LLM nella stessa chat, la history viene ri-iniettata nei
messaggi: questo è ciò che permette "domande sulle domande precedenti".
"Nuova chat" lato frontend = nuovo session_id = contesto vuoto.

In locale (single-process) un dizionario in memoria con TTL è sufficiente;
non serve un vero store esterno (Redis) per un'app che gira solo sul PC
dell'utente. Le sessioni scadute vengono scartate pigramente alla lettura.
"""
import threading
import time
from ..config import settings

_lock = threading.Lock()
_sessions = {}  # session_id -> {"turns": [...], "last_access": ts}

# Lunghezza massima (caratteri) con cui una risposta NL o una query SQL di un
# turno PASSATO viene ri-iniettata come contesto nei turni successivi. Il
# contenuto integrale resta comunque salvato nel turno (per la UI, i log e la
# tabella query_log) - qui si tronca SOLO la copia che finisce nel prompt,
# perché una conversazione lunga altrimenti fa crescere il costo in token di
# ogni nuova chiamata in modo proporzionale alla somma di TUTTE le risposte
# precedenti, non solo al turno corrente.
_HISTORY_NL_MAX_CHARS = 400
_HISTORY_SQL_MAX_CHARS = 500


def _is_expired(entry):
    ttl_s = settings.CONVERSATION_TTL_MINUTES * 60
    return (time.time() - entry["last_access"]) > ttl_s


def _get_or_create(session_id):
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None or _is_expired(entry):
            entry = {"turns": [], "last_access": time.time()}
            _sessions[session_id] = entry
        entry["last_access"] = time.time()
        return entry


def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "… [troncato]"


def get_history_as_messages(session_id: str) -> list:
    """Ritorna la history della sessione come lista di messaggi role/content
    pronta da prependere al nuovo turno (domanda utente -> SQL generato come
    'assistant', risultato interpretato come testo aggiuntivo dell'assistant).
    SQL e risposta NL dei turni passati sono troncati (vedi _HISTORY_*_MAX_CHARS)
    per tenere sotto controllo la crescita dei token su conversazioni lunghe."""
    entry = _get_or_create(session_id)
    messages = []
    for turn in entry["turns"]:
        messages.append({"role": "user", "content": turn["question"]})
        sql_short = _truncate(turn.get("sql", ""), _HISTORY_SQL_MAX_CHARS)
        nl_short = _truncate(turn.get("nl_response", ""), _HISTORY_NL_MAX_CHARS)
        assistant_content = f"SQL generata: {sql_short}\nRisposta: {nl_short}"
        messages.append({"role": "assistant", "content": assistant_content})
    return messages


def append_turn(session_id: str, question: str, sql: str = "", nl_response: str = ""):
    entry = _get_or_create(session_id)
    max_turns = settings.CONVERSATION_MAX_TURNS
    with _lock:
        entry["turns"].append({"question": question, "sql": sql, "nl_response": nl_response})
        if len(entry["turns"]) > max_turns:
            entry["turns"] = entry["turns"][-max_turns:]


def rebuild_session(session_id: str, turns: list):
    """Ricostruisce la memoria di una sessione a partire da turni persistiti
    (usato quando l'utente riapre una conversazione passata dalla sidebar:
    la memoria in-process è andata persa al riavvio, ma i turni sono
    recuperabili dalla tabella query_log). Applica lo stesso limite di
    CONVERSATION_MAX_TURNS dei turni live."""
    max_turns = settings.CONVERSATION_MAX_TURNS
    with _lock:
        _sessions[session_id] = {
            "turns": [
                {"question": t.get("question", ""), "sql": t.get("sql", ""),
                 "nl_response": t.get("nl_response", "")}
                for t in turns
            ][-max_turns:],
            "last_access": time.time(),
        }


def truncate_after(session_id: str, turn_index: int):
    """Usato da 'modifica domanda': elimina tutti i turni successivi a quello modificato."""
    entry = _get_or_create(session_id)
    with _lock:
        entry["turns"] = entry["turns"][:turn_index]


def turn_count(session_id: str) -> int:
    entry = _get_or_create(session_id)
    return len(entry["turns"])


def reset_session(session_id: str):
    with _lock:
        _sessions.pop(session_id, None)


def cleanup_expired():
    with _lock:
        expired = [sid for sid, e in _sessions.items() if _is_expired(e)]
        for sid in expired:
            _sessions.pop(sid, None)
        return len(expired)
