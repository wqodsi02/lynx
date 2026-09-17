# -*- coding: utf-8 -*-
"""
Gruppo 8 - propagazione dei byte lungo la catena Fase 1 -> Fase 2.

La catena e' il punto in cui l'accounting si rompe in silenzio. Fase 1
(/api/generate-sql) e Fase 2 (/api/execute-and-explain) sono due richieste HTTP
distinte, con in mezzo la conferma dell'utente: e' il FRONTEND a trasportare i
valori della prima nella seconda, perche' il backend possa scrivere una sola
riga di query_log con i totali giusti. Se uno dei tre anelli si dimentica un
campo non succede niente di visibile - nessun errore, solo totali dimezzati.

I tre anelli:
  1. /api/generate-sql deve RESTITUIRE i campi
  2. executeStep() in chat.js deve INOLTRARLI
  3. /api/execute-and-explain deve ACCETTARLI e SOMMARLI

Tutto in-process: nessuna rete, nessun database, nessuna chiamata a un modello.
"""
import ast
import io
import json
import os
import re

import pytest

from backend.llm.base import LLMResult
from backend.services import logging_service as logsvc
from backend.services import nl2sql_service as nl2sql


_MODELLO = {"id": "m1", "name": "Gpt Oss 120b", "provider": "groq",
            "model_string": "openai/gpt-oss-120b", "api_key": "chiave"}


def _risultato_llm(testo="SELECT 1", b_req=100, b_resp=200, b_wire=50):
    return LLMResult(text=testo, latency_ms=10, tokens_prompt=5,
                     tokens_completion=3, tokens_total=8,
                     bytes_request=b_req, bytes_response=b_resp,
                     bytes_response_wire=b_wire)


# ---------------------------------------------------------------------------
# Servizio: i tre passaggi che producono byte (4)
# ---------------------------------------------------------------------------
def test_generate_sql_riporta_i_byte(monkeypatch):
    monkeypatch.setattr(nl2sql, "call_llm", lambda *a, **k: _risultato_llm())

    gen = nl2sql.generate_sql("quante catture?", _MODELLO, "prompt")

    assert gen["llm_bytes_request"] == 100
    assert gen["llm_bytes_response"] == 200
    assert gen["llm_bytes_response_wire"] == 50


def test_generate_sql_somma_i_byte_dei_tentativi_a_vuoto(monkeypatch):
    """Se il modello non produce SQL si ritenta (EMPTY_SQL_RETRIES). I byte del
    tentativo sprecato vanno sommati, come gia' si fa per latenza e token:
    quel traffico e' stato speso davvero."""
    risposte = iter([_risultato_llm(testo="", b_req=100, b_resp=200, b_wire=50),
                     _risultato_llm(testo="SELECT 1", b_req=7, b_resp=11, b_wire=3)])
    monkeypatch.setattr(nl2sql, "call_llm", lambda *a, **k: next(risposte))

    gen = nl2sql.generate_sql("quante catture?", _MODELLO, "prompt")

    assert gen["attempts"] == 2
    assert gen["llm_bytes_request"] == 107
    assert gen["llm_bytes_response"] == 211
    assert gen["llm_bytes_response_wire"] == 53


def test_explain_results_riporta_i_byte(monkeypatch):
    monkeypatch.setattr(nl2sql, "call_llm", lambda *a, **k: _risultato_llm(testo="Sono 206."))

    exp = nl2sql.explain_results("quante catture?", "SELECT 1", [{"n": 206}],
                                 False, _MODELLO, session_id=None, use_history=False)

    assert exp["llm_bytes_request"] == 100
    assert exp["llm_bytes_response"] == 200
    assert exp["llm_bytes_response_wire"] == 50


def test_repair_sql_riporta_i_byte(monkeypatch):
    monkeypatch.setattr(nl2sql, "call_llm", lambda *a, **k: _risultato_llm())

    rip = nl2sql.repair_sql("domanda", "SELECT sbagliata", "errore", _MODELLO, "prompt")

    assert rip["llm_bytes_request"] == 100
    assert rip["llm_bytes_response"] == 200
    assert rip["llm_bytes_response_wire"] == 50


# ---------------------------------------------------------------------------
# Auto-riparazione: byte attribuiti alla fase SQL, traffico DB cumulato (2)
# ---------------------------------------------------------------------------
def test_execute_with_repair_attribuisce_i_byte_alla_fase_sql(monkeypatch):
    """La riparazione e' una chiamata LLM in piu': i suoi byte appartengono
    alla fase SQL, esattamente come gia' avviene per latenza e token."""
    esiti = iter([
        ([], [], "colonna inesistente", False, 5, {"query_bytes": 30, "result_payload_bytes": None}),
        (["n"], [{"n": 1}], None, False, 7, {"query_bytes": 40, "result_payload_bytes": 12}),
    ])
    monkeypatch.setattr(nl2sql, "execute_readonly_query", lambda *a, **k: next(esiti))
    monkeypatch.setattr(nl2sql, "call_llm", lambda *a, **k: _risultato_llm())

    _sql, _cols, _rows, err, _tr, _lat, riparazione, trasferimento = \
        nl2sql.execute_with_repair("domanda", "SELECT bad", _MODELLO, "prompt")

    assert err is None
    assert riparazione["llm_bytes_request"] == 100
    assert riparazione["llm_bytes_response"] == 200
    # Il traffico verso il DB e' quello di ENTRAMBE le esecuzioni.
    assert trasferimento["query_bytes"] == 70
    assert trasferimento["result_payload_bytes"] == 12


def test_execute_with_repair_senza_riparazione(monkeypatch):
    monkeypatch.setattr(nl2sql, "execute_readonly_query",
                        lambda *a, **k: (["n"], [{"n": 1}], None, False, 7,
                                         {"query_bytes": 40, "result_payload_bytes": 12}))

    _sql, _cols, _rows, err, _tr, _lat, riparazione, trasferimento = \
        nl2sql.execute_with_repair("domanda", "SELECT 1", _MODELLO, "prompt")

    assert err is None
    assert riparazione is None
    assert trasferimento == {"query_bytes": 40, "result_payload_bytes": 12}


# ---------------------------------------------------------------------------
# Anello 1: /api/generate-sql restituisce i campi (1)
# ---------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    from backend.app import create_app
    from backend.api import query_routes
    monkeypatch.setattr(query_routes, "get_model", lambda mid: _MODELLO)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_route_generate_sql_espone_i_byte(client, monkeypatch):
    monkeypatch.setattr(nl2sql, "call_llm", lambda *a, **k: _risultato_llm())

    r = client.post("/api/generate-sql",
                    json={"question": "quante catture?", "model_id": "m1"})

    assert r.status_code == 200
    corpo = r.get_json()
    assert corpo["llm_bytes_request"] == 100
    assert corpo["llm_bytes_response"] == 200
    assert corpo["llm_bytes_response_wire"] == 50


# ---------------------------------------------------------------------------
# Anello 2: chat.js inoltra i campi (1)
# ---------------------------------------------------------------------------
def test_chat_js_inoltra_i_campi_byte():
    """Controllo statico sul sorgente del frontend: e' l'anello che si rompe
    senza fare rumore. Se qualcuno aggiunge un campo in Fase 1 e dimentica di
    inoltrarlo qui, i totali risultano dimezzati senza alcun errore."""
    percorso = os.path.join("static", "js", "chat.js")
    sorgente = io.open(percorso, encoding="utf-8").read()
    m = re.search(r"api\.executeAndExplain\(\{(.*?)\}", sorgente, re.S)
    assert m, "chiamata api.executeAndExplain non trovata in chat.js"
    payload = m.group(1)

    mancanti = [c for c in ("llm_bytes_request", "llm_bytes_response",
                            "llm_bytes_response_wire") if c not in payload]
    assert not mancanti, (
        "chat.js non inoltra alla Fase 2 i campi %s: i byte della Fase 1 "
        "andrebbero persi in silenzio" % mancanti
    )


# ---------------------------------------------------------------------------
# Anello 3: /api/execute-and-explain accetta e somma (2)
# ---------------------------------------------------------------------------
def _stub_fase2(monkeypatch, wire_fase2=50):
    from backend.api import query_routes
    monkeypatch.setattr(nl2sql, "execute_with_repair",
                        lambda *a, **k: ("SELECT 1", ["n"], [{"n": 206}], None, False, 7,
                                         None, {"query_bytes": 40, "result_payload_bytes": 12}))
    monkeypatch.setattr(nl2sql, "explain_results",
                        lambda *a, **k: {"nl_response": "Sono 206.", "latency_ms": 9,
                                         "tokens_prompt": 5, "tokens_completion": 3,
                                         "tokens_total": 8, "llm_bytes_request": 1000,
                                         "llm_bytes_response": 2000,
                                         "llm_bytes_response_wire": wire_fase2})
    monkeypatch.setattr(logsvc, "save_to_db_log", lambda *a, **k: 1)
    monkeypatch.setattr(logsvc, "save_log_file", lambda *a, **k: "finto.txt")
    monkeypatch.setattr(logsvc, "update_log_filename", lambda *a, **k: None)
    return query_routes


def test_route_execute_and_explain_somma_la_fase_1(client, monkeypatch):
    """IL test della catena: i byte arrivati dal frontend vanno sommati a
    quelli della Fase 2, non sostituiti."""
    _stub_fase2(monkeypatch)

    r = client.post("/api/execute-and-explain", json={
        "question": "quante catture?", "sql": "SELECT 1", "model_id": "m1",
        "latency_sql_ms": 100, "tokens_prompt": 10, "tokens_completion": 4,
        "llm_bytes_request": 100, "llm_bytes_response": 200,
        "llm_bytes_response_wire": 50,
    })

    assert r.status_code == 200
    corpo = r.get_json()
    assert corpo["llm_bytes_request"] == 1100
    assert corpo["llm_bytes_response"] == 2200
    assert corpo["llm_bytes_response_wire"] == 100
    assert corpo["db_bytes_query"] == 40
    assert corpo["db_bytes_result_payload"] == 12


def test_wire_none_in_fase_1_non_azzera_il_totale(client, monkeypatch):
    """Se la Fase 1 non ha potuto determinare il valore "wire" (nessun
    Content-Length) il contributo della Fase 2 deve sopravvivere: None e'
    neutro, non assorbente."""
    _stub_fase2(monkeypatch, wire_fase2=50)

    r = client.post("/api/execute-and-explain", json={
        "question": "quante catture?", "sql": "SELECT 1", "model_id": "m1",
        "llm_bytes_request": 100, "llm_bytes_response": 200,
        "llm_bytes_response_wire": None,
    })

    assert r.get_json()["llm_bytes_response_wire"] == 50


# ---------------------------------------------------------------------------
# Persistenza: blocco [ TRASFERIMENTI ] nei .txt (1)
# ---------------------------------------------------------------------------
def test_log_txt_contiene_il_blocco_trasferimenti(tmp_path, monkeypatch):
    monkeypatch.setattr(logsvc, "LOGS_DIR", str(tmp_path))

    nome = logsvc.save_log_file(
        "sess", "quante catture?", "Gpt Oss 120b", "groq", "generico",
        "SELECT 1", [{"n": 206}], "Sono 206.", 100, 7, 9, 1,
        tokens_prompt=15, tokens_completion=7, tokens_total=22,
        transfer={"llm_bytes_request": 1100, "llm_bytes_response": 2200,
                  "llm_bytes_response_wire": 100, "db_bytes_query": 40,
                  "db_bytes_result_payload": 12})

    contenuto = io.open(os.path.join(str(tmp_path), nome), encoding="utf-8").read()

    assert "[ TRASFERIMENTI ]" in contenuto
    assert "1100" in contenuto and "2200" in contenuto
    assert "40" in contenuto and "12" in contenuto


# ---------------------------------------------------------------------------
# Coerenza dell'arita' fra la funzione e i suoi chiamanti (1)
# ---------------------------------------------------------------------------
def test_tutti_i_chiamanti_di_execute_with_repair_spacchettano_otto_valori():
    """`execute_with_repair` restituisce 8 valori. Un chiamante che ne
    spacchetta 7 non fallisce all'import ne' durante i test: esplode a runtime
    con ValueError, e solo sul percorso che lo esercita.

    E' successo davvero in questa tappa: benchmark_service.py ne spacchettava
    ancora 7, e i test dei benchmark non esistevano ancora per accorgersene.

    Il conteggio passa dall'AST e non da una regex: una prima versione di questo
    test cercava i chiamanti con un'espressione regolare che non ne trovava
    nessuno, quindi passava senza verificare nulla. L'assert finale sul numero
    di chiamanti individuati serve proprio a impedire che torni a succedere.
    """
    attesi = 8
    chiamanti = []
    sospetti = []
    for cartella, _sub, file_list in os.walk("backend"):
        if "__pycache__" in cartella:
            continue
        for nome in file_list:
            if not nome.endswith(".py"):
                continue
            percorso = os.path.join(cartella, nome)
            albero = ast.parse(io.open(percorso, encoding="utf-8").read())
            for nodo in ast.walk(albero):
                if not isinstance(nodo, ast.Assign) or not isinstance(nodo.value, ast.Call):
                    continue
                funzione = nodo.value.func
                nome_funzione = (funzione.attr if isinstance(funzione, ast.Attribute)
                                 else getattr(funzione, "id", None))
                if nome_funzione != "execute_with_repair":
                    continue
                destinazione = nodo.targets[0]
                quanti = len(destinazione.elts) if isinstance(destinazione, ast.Tuple) else 1
                chiamanti.append((percorso, quanti))
                if quanti != attesi:
                    sospetti.append("%s: %d valori invece di %d" % (percorso, quanti, attesi))

    assert chiamanti, (
        "nessun chiamante di execute_with_repair individuato: il test non sta "
        "verificando nulla"
    )
    assert not sospetti, (
        "chiamanti con arita' sbagliata: %s" % sospetti
    )
