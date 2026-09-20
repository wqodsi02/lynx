# -*- coding: utf-8 -*-
"""
Gruppo 13 - indicatore di stato e comportamento degli endpoint.

Ultima tappa della correzione. Le prime tre hanno dato al sistema la capacita'
di sapere e di dire; questa la espone a chi guarda da fuori.

Il punto centrale e' /api/analytics. E' li' che il guasto si e' nascosto per
tre mesi: mostrava POCHI DATI invece di DATI MANCANTI, e a occhio le due cose
sono identiche. Un 500 non aiuta — non distingue "il sistema e' rotto" da "non
c'e' niente da mostrare" — quindi gli endpoint rispondono 200 dicendo che cosa
non funziona, piu' quel che riescono comunque a recuperare.

Vincolo non negoziabile su /api/health: non deve MAI aprire una connessione.
Con connect_timeout=8 su VPN instabile, un health check che si blocca otto
secondi e' peggio che inutile. Il test lo verifica sostituendo get_connection
con una funzione che esplode se chiamata.

Nessun database, nessuna rete.
"""
import io
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.errors as PGE
import pytest

from backend import persistence_state as stato


@pytest.fixture
def client():
    from backend.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _cursore_ko(eccezione):
    """log_table_cursor che fallisce prima di cedere il cursore."""
    def _fabbrica(*a, **k):
        @contextmanager
        def _cm():
            raise eccezione
            yield  # pragma: no cover
        return _cm()
    return _fabbrica


class _CursoreQuery:
    """Cursore che risponde in base al testo della query.

    `guasti` mappa un frammento di SQL all'eccezione da sollevare; `risposte`
    mappa un frammento alle righe da restituire.
    """

    def __init__(self, risposte=None, guasti=None):
        self.risposte = risposte or {}
        self.guasti = guasti or {}
        self._righe = []

    def execute(self, sql, params=None):
        for frammento, eccezione in self.guasti.items():
            if frammento in sql:
                raise eccezione
        self._righe = []
        for frammento, righe in self.risposte.items():
            if frammento in sql:
                self._righe = righe
        return None

    def fetchall(self):
        return self._righe


class _ConnessioneQuery:
    """La connessione finta deve offrire rollback(): in PostgreSQL una query
    fallita aborta la transazione, e senza rollback le successive
    fallirebbero a cascata — il recupero parziale sarebbe solo apparente."""

    def __init__(self):
        self.rollback_chiamati = 0

    def rollback(self):
        self.rollback_chiamati += 1


def _cursore_ok(risposte=None, guasti=None):
    def _fabbrica(*a, **k):
        @contextmanager
        def _cm():
            yield _ConnessioneQuery(), _CursoreQuery(risposte, guasti)
        return _cm()
    return _fabbrica


# ---------------------------------------------------------------------------
# /api/health (3)
# ---------------------------------------------------------------------------
def test_health_espone_lo_stato_di_persistenza(client):
    corpo = client.get("/api/health").get_json()

    assert corpo["status"] == "ok"
    assert "persistenza" in corpo
    assert corpo["persistenza"]["ok"] is True


def test_health_non_apre_connessioni(client, monkeypatch):
    """Vincolo, non preferenza: un health check che si blocca otto secondi
    sulla VPN e' peggio che inutile."""
    from backend.db import connection as conn_mod

    def _vietato(*a, **k):
        raise AssertionError("/api/health ha aperto una connessione al database")

    monkeypatch.setattr(conn_mod, "get_connection", _vietato)

    risposta = client.get("/api/health")

    assert risposta.status_code == 200


def test_health_riflette_un_guasto_in_corso(client):
    stato.registra_fallimento(PGE.UndefinedColumn("la colonna log_filename non esiste"))

    p = client.get("/api/health").get_json()["persistenza"]

    assert p["ok"] is False
    assert p["grave"] is True
    assert p["classificazione"] == stato.STRUTTURALE


# ---------------------------------------------------------------------------
# /api/db/test: verifica attiva, schema compreso (3)
# ---------------------------------------------------------------------------
def test_db_test_riporta_schema_completo(client, monkeypatch):
    from backend.api import db_routes
    monkeypatch.setattr(db_routes, "get_connection", lambda: _ConnessioneFinta())
    monkeypatch.setattr(db_routes, "verify_schema", lambda conn: [])

    corpo = client.get("/api/db/test").get_json()

    assert corpo["status"] == "ok"
    assert corpo["schema_ok"] is True
    assert corpo["colonne_mancanti"] == []


def test_db_test_nomina_le_colonne_mancanti(client, monkeypatch):
    from backend.api import db_routes
    monkeypatch.setattr(db_routes, "get_connection", lambda: _ConnessioneFinta())
    monkeypatch.setattr(db_routes, "verify_schema",
                        lambda conn: ["db_bytes_query", "log_filename"])

    corpo = client.get("/api/db/test").get_json()

    assert corpo["schema_ok"] is False
    assert corpo["colonne_mancanti"] == ["db_bytes_query", "log_filename"]
    assert "log_filename" in corpo["message"]


def test_db_test_connessione_fallita_resta_500(client, monkeypatch):
    """Comportamento invariato: se non si riesce nemmeno a collegarsi, e' un
    errore vero e va detto come tale."""
    from backend.api import db_routes

    def _ko():
        raise psycopg2.OperationalError("timeout expired")

    monkeypatch.setattr(db_routes, "get_connection", _ko)

    risposta = client.get("/api/db/test")

    assert risposta.status_code == 500
    assert risposta.get_json()["status"] == "error"


class _ConnessioneFinta:
    def close(self):
        pass


# ---------------------------------------------------------------------------
# /api/history: 200 anche quando la persistenza e' rotta (3)
# ---------------------------------------------------------------------------
def test_history_ok_riporta_righe_e_stato(client, monkeypatch):
    from backend.api import misc_routes
    monkeypatch.setattr(misc_routes, "log_table_cursor",
                        _cursore_ok({"FROM query_log": [{"id": 1, "question": "q"}]}))

    corpo = client.get("/api/history").get_json()

    assert corpo["rows"] == [{"id": 1, "question": "q"}]
    assert corpo["persistenza"]["ok"] is True


def test_history_non_risponde_piu_500_quando_il_db_e_rotto(client, monkeypatch):
    """Un 500 non distingue "il sistema e' rotto" da "non ci sono dati": e'
    l'ambiguita' che ha nascosto il guasto per tre mesi."""
    from backend.api import misc_routes
    monkeypatch.setattr(misc_routes, "log_table_cursor",
                        _cursore_ko(PGE.UndefinedColumn("la colonna log_filename non esiste")))

    risposta = client.get("/api/history")

    assert risposta.status_code == 200
    p = risposta.get_json()["persistenza"]
    assert p["ok"] is False
    assert p["classificazione"] == stato.STRUTTURALE
    assert "log_filename" in p["errore"]


def test_history_rotta_restituisce_rows_vuoto_non_assente(client, monkeypatch):
    """Il consumatore non deve indovinare la forma della risposta a seconda
    dell'esito."""
    from backend.api import misc_routes
    monkeypatch.setattr(misc_routes, "log_table_cursor",
                        _cursore_ko(psycopg2.OperationalError("timeout")))

    corpo = client.get("/api/history").get_json()

    assert corpo["rows"] == []


# ---------------------------------------------------------------------------
# /api/analytics: il punto dove il guasto si nascondeva (3)
# ---------------------------------------------------------------------------
def test_analytics_rotta_dice_perche(client, monkeypatch):
    from backend.api import analytics_routes
    monkeypatch.setattr(analytics_routes, "log_table_cursor",
                        _cursore_ko(PGE.InsufficientPrivilege("permesso negato")))

    risposta = client.get("/api/analytics")

    assert risposta.status_code == 200
    corpo = risposta.get_json()
    assert corpo["persistenza"]["ok"] is False
    assert corpo["persistenza"]["classificazione"] == stato.STRUTTURALE
    # Le chiavi dei dati ci sono comunque, vuote: il frontend non deve
    # distinguere "rotto" da "assente" guardando la forma.
    for chiave in ("by_model", "by_category", "top_questions", "daily"):
        assert corpo[chiave] == []


def test_analytics_recupera_il_recuperabile(client, monkeypatch):
    """Se cade una sola delle quattro interrogazioni, le altre tre devono
    arrivare comunque: meta' di un pannello e' meglio di niente, purche' sia
    dichiarato che e' meta'."""
    from backend.api import analytics_routes
    monkeypatch.setattr(analytics_routes, "log_table_cursor", _cursore_ok(
        risposte={"GROUP BY category": [{"category": "iat", "count": 3}]},
        guasti={"GROUP BY model": PGE.UndefinedColumn("la colonna llm_bytes_request non esiste")}))

    corpo = client.get("/api/analytics").get_json()

    assert corpo["by_model"] == []
    assert corpo["by_category"] == [{"category": "iat", "count": 3}]
    assert corpo["persistenza"]["ok"] is False


def test_analytics_sana_dichiara_lo_stato(client, monkeypatch):
    from backend.api import analytics_routes
    monkeypatch.setattr(analytics_routes, "log_table_cursor", _cursore_ok())

    corpo = client.get("/api/analytics").get_json()

    assert corpo["persistenza"]["ok"] is True


# ---------------------------------------------------------------------------
# Frontend: controlli statici (2)
# ---------------------------------------------------------------------------
def test_settings_js_mostra_lo_stato_dello_schema():
    sorgente = io.open(os.path.join("static", "js", "settings.js"),
                       encoding="utf-8").read()

    mancanti = [c for c in ("schema_ok", "colonne_mancanti") if c not in sorgente]
    assert not mancanti, (
        "la spia di stato DB non riporta %s: una connessione riuscita con lo "
        "schema rotto continuerebbe a sembrare tutto a posto" % mancanti
    )


def test_analytics_js_mostra_la_fascia_e_tollera_l_assenza():
    """Il frontend deve reggere una risposta senza il campo nuovo: un browser
    con il JS in cache riceverebbe proprio quella."""
    sorgente = io.open(os.path.join("static", "js", "analytics.js"),
                       encoding="utf-8").read()

    assert "persistenza" in sorgente, "Analytics non mostra lo stato di persistenza"
    righe = [r for r in sorgente.split("\n") if "persistenza" in r]
    assert any("?." in r or "&&" in r or "data.persistenza" in r for r in righe), (
        "il campo va letto con una guardia: se manca, il pannello non deve rompersi"
    )
