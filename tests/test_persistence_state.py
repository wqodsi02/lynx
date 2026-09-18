# -*- coding: utf-8 -*-
"""
Gruppo 10 - classificazione dei fallimenti di persistenza e stato.

Nasce dal difetto che ha nascosto un guasto totale per tre mesi e 165
esecuzioni: `save_to_db_log` cattura ogni eccezione, emette un warning sempre
uguale e restituisce None. Il segnale c'era, ma era indistinguibile dal rumore
e nessuno sapeva da quanto durava.

Primo pezzo della correzione: saper dire CHE COSA e' andato storto e DA QUANTO,
prima ancora di decidere a chi dirlo.

La classificazione si basa su `isinstance`, non sulla stringa SQLSTATE. Non e'
un dettaglio implementativo: `pgcode` viene popolato dal driver a partire dalla
risposta del server, e su un'eccezione costruita a mano vale None —

    psycopg2.errors.UndefinedColumn('...').pgcode  ->  None

quindi una classificazione basata su pgcode sarebbe non testabile offline, che
e' esattamente quando serve. Attenzione all'ordine dei controlli: tutte le
classi transitorie sono sottoclassi di OperationalError, e
ReadOnlySqlTransaction discende da InternalError, non da ProgrammingError.

Nessuna connessione, nessuna rete.
"""
import psycopg2
import psycopg2.errors as PGE
import pytest

from backend import persistence_state as stato
from backend.config import settings


class _OrologioFinto:
    """Sostituisce il modulo `time` dentro persistence_state, per rendere
    deterministica la soglia temporale di escalation."""

    def __init__(self, adesso=1000.0):
        self.adesso = adesso

    def time(self):
        return self.adesso

    def avanza(self, secondi):
        self.adesso += secondi


# ---------------------------------------------------------------------------
# Classificazione (6)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("eccezione, atteso", [
    (PGE.UndefinedColumn("la colonna log_filename non esiste"), stato.STRUTTURALE),
    (PGE.InsufficientPrivilege("permesso negato per la tabella query_log"), stato.STRUTTURALE),
    (PGE.ReadOnlySqlTransaction("transazione in sola lettura"), stato.STRUTTURALE),
    (PGE.QueryCanceled("statement timeout"), stato.TRANSITORIO),
    (PGE.AdminShutdown("il server si sta arrestando"), stato.TRANSITORIO),
    (ValueError("qualcosa di inatteso"), stato.STRUTTURALE),
], ids=["colonna-inesistente", "permessi", "sola-lettura", "timeout",
        "shutdown", "eccezione-sconosciuta"])
def test_classificazione(eccezione, atteso):
    """I casi certi. Un'eccezione che non riconosciamo viene trattata come
    STRUTTURALE di proposito: il default silenzioso e' cio' che ci ha messi in
    questa situazione, quindi l'ignoto va segnalato, non assorbito."""
    assert stato.classifica(eccezione) == atteso


def test_operational_error_generico_e_indeterminato():
    """E' la zona grigia, e va dichiarata invece che inventata.

    Un fallimento in fase di connessione arriva come OperationalError nudo e
    comprende sia la VPN caduta (transitorio) sia la password sbagliata o il
    database inesistente (strutturale). Il testo del messaggio li
    distinguerebbe, ma dipende da versione e lingua di libpq: non ci si
    costruisce sopra una classificazione. Terza categoria, con escalation a
    tempo.
    """
    eccezione = psycopg2.OperationalError(
        'connection to server at "10.22.255.161", port 5432 failed: timeout expired')

    assert stato.classifica(eccezione) == stato.INDETERMINATO


# ---------------------------------------------------------------------------
# Stato (4)
# ---------------------------------------------------------------------------
def test_fallimento_aggiorna_stato_e_contatore():
    stato.registra_fallimento(PGE.QueryCanceled("timeout"))
    stato.registra_fallimento(PGE.QueryCanceled("timeout"))

    s = stato.istantanea()

    assert s["ok"] is False
    assert s["fallimenti_consecutivi"] == 2
    assert s["classificazione"] == stato.TRANSITORIO
    assert "timeout" in s["ultimo_errore"]


def test_successo_azzera_il_contatore():
    """Il segnale di ripristino conta quanto quello di guasto: senza, non si sa
    mai se il problema e' ancora in corso."""
    stato.registra_fallimento(PGE.QueryCanceled("timeout"))
    stato.registra_successo()

    s = stato.istantanea()

    assert s["ok"] is True
    assert s["fallimenti_consecutivi"] == 0
    assert s["grave"] is False
    assert s["ultimo_successo"] is not None


def test_strutturale_e_grave_al_primo_colpo():
    """Uno schema disallineato o dei permessi mancanti non si risolvono da
    soli: non ha senso aspettare una soglia prima di gridare."""
    stato.registra_fallimento(PGE.UndefinedColumn("la colonna x non esiste"))

    assert stato.istantanea()["grave"] is True


def test_transitorio_isolato_non_e_grave():
    stato.registra_fallimento(PGE.QueryCanceled("timeout"))

    assert stato.istantanea()["grave"] is False


# ---------------------------------------------------------------------------
# Escalation: soglie lette da config (3)
# ---------------------------------------------------------------------------
def test_escalation_per_numero_di_fallimenti(monkeypatch):
    monkeypatch.setattr(settings, "PERSISTENZA_FALLIMENTI_PER_ALLARME", 5)

    for _ in range(4):
        stato.registra_fallimento(psycopg2.OperationalError("timeout"))
    assert stato.istantanea()["grave"] is False, "al quarto non deve ancora allarmare"

    stato.registra_fallimento(psycopg2.OperationalError("timeout"))
    assert stato.istantanea()["grave"] is True, "al quinto si'"


def test_escalation_per_tempo_trascorso(monkeypatch):
    """Una VPN caduta per dieci minuti e' un problema, comunque la si
    classifichi. E' la regola che avrebbe intercettato il guasto originale
    ANCHE sbagliando la classificazione."""
    orologio = _OrologioFinto()
    monkeypatch.setattr(stato, "time", orologio)
    monkeypatch.setattr(settings, "PERSISTENZA_MINUTI_PER_ALLARME", 10)
    monkeypatch.setattr(settings, "PERSISTENZA_FALLIMENTI_PER_ALLARME", 999)

    stato.registra_successo()
    stato.registra_fallimento(psycopg2.OperationalError("timeout"))
    assert stato.istantanea()["grave"] is False

    orologio.avanza(11 * 60)
    stato.registra_fallimento(psycopg2.OperationalError("timeout"))

    assert stato.istantanea()["grave"] is True


def test_escalation_a_freddo_senza_alcun_successo(monkeypatch):
    """Se l'app parte con il database gia' irraggiungibile non esiste un
    "ultimo successo" da cui contare: il riferimento diventa il primo
    fallimento della serie."""
    orologio = _OrologioFinto()
    monkeypatch.setattr(stato, "time", orologio)
    monkeypatch.setattr(settings, "PERSISTENZA_MINUTI_PER_ALLARME", 10)
    monkeypatch.setattr(settings, "PERSISTENZA_FALLIMENTI_PER_ALLARME", 999)

    stato.registra_fallimento(psycopg2.OperationalError("timeout"))
    assert stato.istantanea()["grave"] is False

    orologio.avanza(11 * 60)
    stato.registra_fallimento(psycopg2.OperationalError("timeout"))

    assert stato.istantanea()["grave"] is True


# ---------------------------------------------------------------------------
# Throttling delle segnalazioni (2)
# ---------------------------------------------------------------------------
def test_segnala_solo_il_primo_di_una_serie():
    """Senza throttling si passerebbe da un warning ignorato a trecento error
    ignorati: la stessa cecita', al contrario.

    `deve_segnalare()` CONSUMA il diritto di segnalare: il chiamante registra
    il fallimento e poi chiede se tocca a lui parlare. Una prima versione di
    questi test la trattava come una domanda senza effetti, il che costringeva
    a interrogarla PRIMA di registrare — ordine innaturale e facile da
    sbagliare nel chiamante.
    """
    stato.registra_fallimento(PGE.QueryCanceled("timeout"))
    assert stato.deve_segnalare() is True, "il primo della serie va segnalato"
    assert stato.deve_segnalare() is False, "gia' segnalato, non si ripete"

    stato.registra_fallimento(PGE.QueryCanceled("timeout"))
    assert stato.deve_segnalare() is False


def test_un_cambio_di_classificazione_torna_a_segnalare():
    """Un transitorio che diventa strutturale e' una notizia nuova, non la
    ripetizione della precedente."""
    stato.registra_fallimento(PGE.QueryCanceled("timeout"))
    assert stato.deve_segnalare() is True
    assert stato.deve_segnalare() is False

    stato.registra_fallimento(PGE.UndefinedColumn("la colonna x non esiste"))

    assert stato.deve_segnalare() is True


def test_dopo_un_ripristino_una_nuova_serie_torna_a_segnalare():
    """Un guasto che torna dopo un ripristino e' una notizia nuova."""
    stato.registra_fallimento(PGE.QueryCanceled("timeout"))
    assert stato.deve_segnalare() is True
    stato.registra_successo()

    stato.registra_fallimento(PGE.QueryCanceled("timeout"))

    assert stato.deve_segnalare() is True
