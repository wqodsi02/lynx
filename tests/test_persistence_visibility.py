# -*- coding: utf-8 -*-
"""
Gruppo 12 - visibilita' dei fallimenti di persistenza: console e file .txt.

Le tappe 1 e 2 hanno reso il sistema capace di sapere che cosa non va e da
quanto. Questa lo rende capace di DIRLO, alle due categorie di destinatari che
guardano dopo: chi ha la console davanti e chi rilegge i log.

Il difetto originale non era l'assenza di un messaggio — un `logger.warning`
c'era — ma la sua forma: identico 165 volte, senza genere ne' durata, e quindi
indistinguibile dal rumore. Qui si aggiungono le tre cose che mancavano: un
errore forte al primo colpo, il silenzio sulle ripetizioni, e un segnale di
RIPRISTINO senza il quale non si sa mai se il problema e' ancora in corso.

Dove possibile la verifica e' sul contatore e non sul framework di logging: i
contatori sono il comportamento, i livelli di log ne sono solo la
manifestazione.

Nessun database, nessuna rete.
"""
import io
import logging
import os

import psycopg2
import psycopg2.errors as PGE
import pytest

from backend import persistence_state as stato
from backend.config import settings
from backend.services import logging_service as logsvc


class _CursoreFinto:
    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return (42,)


class _ConnessioneFinta:
    def commit(self):
        pass


def _cursore_che_funziona(*a, **k):
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        yield _ConnessioneFinta(), _CursoreFinto()

    return _cm()


def _cursore_che_fallisce(eccezione):
    from contextlib import contextmanager

    def _fabbrica(*a, **k):
        @contextmanager
        def _cm():
            raise eccezione
            yield  # pragma: no cover

        return _cm()

    return _fabbrica


# ---------------------------------------------------------------------------
# Contatori: riepilogo periodico e conteggio della serie (4)
# ---------------------------------------------------------------------------
def test_riepilogo_scatta_alla_cadenza(monkeypatch):
    """Verifica sul contatore, non sui log: e' il contatore a decidere."""
    monkeypatch.setattr(settings, "PERSISTENZA_RIEPILOGO_OGNI", 5)

    for _ in range(4):
        stato.registra_fallimento(PGE.QueryCanceled("timeout"))
        assert stato.deve_riepilogare() is False

    stato.registra_fallimento(PGE.QueryCanceled("timeout"))
    assert stato.deve_riepilogare() is True


def test_riepilogo_si_ripete_a_ogni_multiplo(monkeypatch):
    monkeypatch.setattr(settings, "PERSISTENZA_RIEPILOGO_OGNI", 3)

    esiti = []
    for _ in range(6):
        stato.registra_fallimento(PGE.QueryCanceled("timeout"))
        esiti.append(stato.deve_riepilogare())

    assert esiti == [False, False, True, False, False, True]


def test_la_cadenza_arriva_da_config(monkeypatch):
    """Non cablata: chi gestisce l'installazione deve poterla cambiare."""
    monkeypatch.setattr(settings, "PERSISTENZA_RIEPILOGO_OGNI", 2)

    stato.registra_fallimento(PGE.QueryCanceled("timeout"))
    assert stato.deve_riepilogare() is False
    stato.registra_fallimento(PGE.QueryCanceled("timeout"))
    assert stato.deve_riepilogare() is True


def test_registra_successo_riporta_la_lunghezza_della_serie():
    """Il segnale di ripristino deve poter dire QUANTI fallimenti sono stati
    assorbiti: "ripristinata" da sola non distingue un singolo intoppo da tre
    mesi di guasto."""
    for _ in range(3):
        stato.registra_fallimento(PGE.QueryCanceled("timeout"))

    assert stato.registra_successo() == 3
    assert stato.registra_successo() == 0, "nessuna serie in corso"


# ---------------------------------------------------------------------------
# Console: save_to_db_log (4)
# ---------------------------------------------------------------------------
def test_primo_fallimento_logga_a_error_con_diagnosi(monkeypatch, caplog):
    """Il messaggio deve dire di che genere e' il problema e quale eccezione
    l'ha prodotto: senza, e' di nuovo una riga indistinguibile dalle altre."""
    monkeypatch.setattr(logsvc, "log_table_cursor",
                        _cursore_che_fallisce(PGE.UndefinedColumn("la colonna x non esiste")))

    with caplog.at_level(logging.WARNING):
        esito = logsvc.save_to_db_log("s", "m", "groq", "domanda", "cat",
                                      "SELECT 1", "risposta", 1, 1, 1, 1)

    assert esito is None, "il fallimento non deve propagarsi al chiamante"
    errori = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errori, "il primo fallimento di una serie va segnalato a ERROR"
    messaggio = errori[0].getMessage()
    assert stato.STRUTTURALE in messaggio
    assert "UndefinedColumn" in messaggio


def test_le_ripetizioni_non_riloggano_a_error(monkeypatch, caplog):
    """Senza throttling si passerebbe da un warning ignorato a trecento error
    ignorati: la stessa cecita', al contrario."""
    monkeypatch.setattr(logsvc, "log_table_cursor",
                        _cursore_che_fallisce(PGE.QueryCanceled("timeout")))

    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            logsvc.save_to_db_log("s", "m", "groq", "domanda", "cat",
                                  "SELECT 1", "risposta", 1, 1, 1, 1)

    errori = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errori) == 1, "un solo ERROR per serie, non uno per fallimento"


def test_il_ripristino_viene_segnalato_con_il_conteggio(monkeypatch, caplog):
    """Il segnale di recupero conta quanto quello di guasto: senza, non si sa
    mai se il problema e' ancora in corso."""
    monkeypatch.setattr(logsvc, "log_table_cursor",
                        _cursore_che_fallisce(PGE.QueryCanceled("timeout")))
    for _ in range(3):
        logsvc.save_to_db_log("s", "m", "groq", "d", "c", "SELECT 1", "r", 1, 1, 1, 1)

    monkeypatch.setattr(logsvc, "log_table_cursor", _cursore_che_funziona)
    with caplog.at_level(logging.INFO):
        esito = logsvc.save_to_db_log("s", "m", "groq", "d", "c", "SELECT 1", "r", 1, 1, 1, 1)

    assert esito == 42
    ripristini = [r.getMessage() for r in caplog.records
                  if r.levelno == logging.INFO and "ripristinat" in r.getMessage().lower()]
    assert ripristini, "il ripristino deve lasciare una traccia"
    assert "3" in ripristini[0], "deve dire quanti fallimenti sono stati assorbiti"


def test_successo_senza_fallimenti_non_logga_nulla(monkeypatch, caplog):
    """Non si annuncia un ripristino che non c'e' stato: sarebbe rumore."""
    monkeypatch.setattr(logsvc, "log_table_cursor", _cursore_che_funziona)

    with caplog.at_level(logging.INFO):
        logsvc.save_to_db_log("s", "m", "groq", "d", "c", "SELECT 1", "r", 1, 1, 1, 1)

    assert not [r for r in caplog.records if "ripristinat" in r.getMessage().lower()]


# ---------------------------------------------------------------------------
# update_log_filename (1)
# ---------------------------------------------------------------------------
def test_update_log_filename_fallito_logga_a_warning(monkeypatch, caplog):
    """Era a DEBUG, cioe' invisibile con FLASK_DEBUG=false — la configurazione
    normale."""
    monkeypatch.setattr(logsvc, "log_table_cursor",
                        _cursore_che_fallisce(psycopg2.OperationalError("timeout")))

    with caplog.at_level(logging.DEBUG):
        logsvc.update_log_filename(42, "file.txt")

    avvisi = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert avvisi, "un backfill fallito deve essere visibile senza FLASK_DEBUG"


# ---------------------------------------------------------------------------
# File .txt: blocco [ PERSISTENZA DB ] (3)
# ---------------------------------------------------------------------------
def _scrivi_log(tmp_path, monkeypatch, persistenza=None, log_id=None):
    monkeypatch.setattr(logsvc, "LOGS_DIR", str(tmp_path))
    nome = logsvc.save_log_file(
        "sess", "quante catture?", "Gpt Oss 120b", "groq", "generico",
        "SELECT 1", [{"n": 206}], "Sono 206.", 100, 7, 9, 1,
        log_id=log_id, persistenza=persistenza)
    return io.open(os.path.join(str(tmp_path), nome), encoding="utf-8").read()


def test_blocco_persistenza_presente_solo_se_fallita(tmp_path, monkeypatch):
    contenuto = _scrivi_log(tmp_path, monkeypatch, persistenza={
        "ok": False, "classificazione": stato.STRUTTURALE,
        "errore": 'la colonna "log_filename" non esiste'})

    assert "[ PERSISTENZA DB ]" in contenuto
    assert stato.STRUTTURALE in contenuto
    assert "log_filename" in contenuto


def test_blocco_persistenza_assente_quando_tutto_va_bene(tmp_path, monkeypatch):
    contenuto = _scrivi_log(tmp_path, monkeypatch, log_id=42,
                            persistenza={"ok": True})

    assert "[ PERSISTENZA DB ]" not in contenuto
    assert "Log ID       : 42" in contenuto


def test_log_id_esplicito_invece_di_na_quando_la_scrittura_e_fallita(tmp_path, monkeypatch):
    """`N/A` si legge come "non applicabile", non come "la scrittura e'
    fallita". E' la differenza che, per le 165 esecuzioni di luglio, non
    permette oggi di sapere quali manchino dal database.

    Resta `N/A` quando l'esito non e' noto — nel ramo di errore DB il file
    viene scritto PRIMA della scrittura sul database, e li' `N/A` significa
    onestamente "non ancora noto".
    """
    fallita = _scrivi_log(tmp_path, monkeypatch, persistenza={
        "ok": False, "classificazione": stato.TRANSITORIO, "errore": "timeout"})
    assert "NON SALVATO SU DATABASE" in fallita
    assert "Log ID       : N/A" not in fallita

    ignota = _scrivi_log(tmp_path, monkeypatch)
    assert "Log ID       : N/A" in ignota
    assert "[ PERSISTENZA DB ]" not in ignota
