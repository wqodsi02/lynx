# -*- coding: utf-8 -*-
"""
Classificazione dei fallimenti di persistenza e stato del loro andamento.

Nasce da un guasto reale: la scrittura su `query_log` è fallita per tre mesi e
165 esecuzioni senza che nessuno se ne accorgesse. Il segnale c'era — un
`logger.warning` in `save_to_db_log` — ma era identico ogni volta e non diceva
né di che genere fosse il problema né da quanto durasse. Questo modulo aggiunge
le due informazioni che mancavano.

Tre categorie, non due:

  - **strutturale**: schema disallineato, permessi mancanti, transazione in
    sola lettura. Non si risolve da solo, va segnalato subito e forte.
  - **transitorio**: connessione caduta a metà, timeout di statement, server in
    arresto. Rumore ricorrente, non merita un allarme per ogni occorrenza.
  - **indeterminato**: i fallimenti in fase di CONNESSIONE. È una zona grigia
    dichiarata, non un ripiego: `psycopg2.connect()` solleva `OperationalError`
    sia per la VPN caduta (transitorio) sia per la password sbagliata o il
    database inesistente (strutturale). Il testo del messaggio li
    distinguerebbe, ma dipende da versione e lingua di libpq e non ci si
    costruisce sopra una classificazione.

Per l'indeterminato vale una regola di **escalation**: se dura oltre le soglie
in `config.py` diventa grave comunque. È la regola che avrebbe intercettato il
guasto originale anche classificandolo male, perché una VPN caduta per tre mesi
è un problema strutturale a prescindere dalla causa.

La classificazione usa `isinstance`, non la stringa SQLSTATE: `pgcode` viene
popolato dal driver a partire dalla risposta del server e su un'eccezione
costruita a mano vale `None`, quindi una classificazione basata su di esso non
sarebbe verificabile offline — cioè proprio dove serve.

Stato in-process, come `conversation_service`: l'app gira su una sola macchina
e i campi sono a dimensione fissa, mai liste che crescono.
"""
import threading
import time

import psycopg2
import psycopg2.errors as PGE

from .config import settings

TRANSITORIO = "transitorio"
STRUTTURALE = "strutturale"
INDETERMINATO = "indeterminato"

# ATTENZIONE ALL'ORDINE dei due gruppi qui sotto: TUTTE le classi transitorie
# sono sottoclassi di OperationalError, quindi vanno controllate prima del
# ripiego su OperationalError nudo. E ReadOnlySqlTransaction discende da
# InternalError, non da ProgrammingError: va nominata esplicitamente.
_TRANSITORI = (
    PGE.QueryCanceled,
    PGE.AdminShutdown,
    PGE.CrashShutdown,
    PGE.CannotConnectNow,
    PGE.TooManyConnections,
    PGE.ConnectionException,
    PGE.ConnectionFailure,
    PGE.ConnectionDoesNotExist,
    PGE.SqlclientUnableToEstablishSqlconnection,
    PGE.LockNotAvailable,
    PGE.SerializationFailure,
    PGE.DeadlockDetected,
)
_STRUTTURALI = (
    PGE.UndefinedColumn,
    PGE.UndefinedTable,
    PGE.UndefinedObject,
    PGE.UndefinedFunction,
    PGE.DuplicateColumn,
    PGE.InsufficientPrivilege,
    PGE.ReadOnlySqlTransaction,
    PGE.InvalidTableDefinition,
    psycopg2.ProgrammingError,
)


def classifica(eccezione) -> str:
    """Genere del fallimento. L'ignoto viene trattato come STRUTTURALE.

    È una scelta deliberata: il default silenzioso è ciò che ha reso invisibile
    il guasto originale, quindi un'eccezione che non sappiamo interpretare deve
    farsi sentire, non essere assorbita.
    """
    if isinstance(eccezione, _TRANSITORI):
        return TRANSITORIO
    if isinstance(eccezione, _STRUTTURALI):
        return STRUTTURALE
    if isinstance(eccezione, psycopg2.OperationalError):
        return INDETERMINATO
    return STRUTTURALE


_lock = threading.Lock()


def _stato_iniziale():
    return {
        "ok": True,                      # nessun fallimento noto
        "classificazione": None,
        "ultimo_errore": None,
        "fallimenti_consecutivi": 0,
        "ultimo_successo": None,
        "primo_fallimento": None,        # riferimento quando non c'è mai stato un successo
        "segnalato": False,              # diritto di segnalazione già consumato
        # None = schema mai verificato. Affermare che va bene senza aver
        # guardato sarebbe la stessa bugia del NULL scambiato per zero.
        "schema_ok": None,
        "colonne_mancanti": [],
    }


_stato = _stato_iniziale()


def azzera():
    """Riporta lo stato a inizio processo. Serve ai test."""
    with _lock:
        _stato.clear()
        _stato.update(_stato_iniziale())


def registra_successo() -> int:
    """Registra una scrittura riuscita e ritorna quanti fallimenti consecutivi
    la precedevano.

    Il conteggio serve al segnale di ripristino: "persistenza ripristinata" da
    solo non distingue un singolo intoppo da tre mesi di guasto.
    """
    with _lock:
        falliti = _stato["fallimenti_consecutivi"]
        _stato.update({
            "ok": True,
            "classificazione": None,
            "ultimo_errore": None,
            "fallimenti_consecutivi": 0,
            "ultimo_successo": time.time(),
            "primo_fallimento": None,
            # Un guasto che tornasse dopo un ripristino sarebbe una notizia
            # nuova, non la ripetizione della precedente.
            "segnalato": False,
        })
    return falliti


def deve_riepilogare() -> bool:
    """True quando una serie di fallimenti raggiunge un multiplo della cadenza.

    Da chiamare una volta per fallimento. Non consuma nulla: si legge dal
    contatore, cosi' il comportamento e' verificabile sul contatore stesso e
    non sul framework di logging.
    """
    cadenza = settings.PERSISTENZA_RIEPILOGO_OGNI
    if cadenza <= 0:
        return False
    with _lock:
        n = _stato["fallimenti_consecutivi"]
    return n > 0 and n % cadenza == 0


def registra_fallimento(eccezione) -> str:
    """Registra un fallimento e ne ritorna la classificazione."""
    classificazione = classifica(eccezione)
    with _lock:
        if _stato["primo_fallimento"] is None:
            _stato["primo_fallimento"] = time.time()
        if classificazione != _stato["classificazione"]:
            # Cambio di genere: è un fatto nuovo e va poter essere segnalato.
            _stato["segnalato"] = False
        _stato["ok"] = False
        _stato["classificazione"] = classificazione
        _stato["ultimo_errore"] = str(eccezione)
        _stato["fallimenti_consecutivi"] += 1
    return classificazione


def registra_schema(ok: bool, colonne_mancanti=()):
    """Esito dell'ultima verifica dello schema di query_log.

    Permette all'indicatore di stato di dire non solo "rotto", ma "rotto
    perché mancano queste colonne" — che è la differenza fra un allarme e una
    diagnosi.
    """
    with _lock:
        _stato["schema_ok"] = bool(ok)
        _stato["colonne_mancanti"] = list(colonne_mancanti)


def deve_segnalare() -> bool:
    """Chiede — e CONSUMA — il diritto di segnalare il fallimento in corso.

    Ritorna True una sola volta per serie: al primo fallimento, e di nuovo
    quando cambia la classificazione o dopo un ripristino. Senza questo
    throttling si passerebbe da un warning ignorato a trecento error ignorati,
    che è la stessa cecità al contrario.
    """
    with _lock:
        if _stato["ok"] or _stato["segnalato"]:
            return False
        _stato["segnalato"] = True
        return True


def _e_grave():
    """Da chiamare con il lock già acquisito."""
    if _stato["classificazione"] == STRUTTURALE:
        return True
    if _stato["fallimenti_consecutivi"] == 0:
        return False
    if _stato["fallimenti_consecutivi"] >= settings.PERSISTENZA_FALLIMENTI_PER_ALLARME:
        return True
    riferimento = _stato["ultimo_successo"] or _stato["primo_fallimento"]
    if riferimento is None:
        return False
    return (time.time() - riferimento) > settings.PERSISTENZA_MINUTI_PER_ALLARME * 60


def per_api() -> dict:
    """Vista dello stato destinata alle risposte HTTP.

    Forma unica per /api/health, /api/history e /api/analytics: chi la consuma
    la impara una volta sola. `primo_fallimento` resta fuori — e' un dettaglio
    interno del calcolo dell'escalation.
    """
    s = istantanea()
    return {
        "ok": s["ok"],
        "grave": s["grave"],
        "classificazione": s["classificazione"],
        "errore": s["ultimo_errore"],
        "fallimenti_consecutivi": s["fallimenti_consecutivi"],
        "schema_ok": s["schema_ok"],
        "colonne_mancanti": s["colonne_mancanti"],
    }


def istantanea() -> dict:
    """Copia dello stato corrente, con `grave` calcolato al momento."""
    with _lock:
        s = dict(_stato)
        s["grave"] = _e_grave()
    s.pop("segnalato", None)  # dettaglio interno del throttling
    return s
