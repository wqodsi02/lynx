# -*- coding: utf-8 -*-
"""
Gruppo 9 - transfer rate nei benchmark.

E' il punto in cui la misura serve davvero: confrontare quanto traffico costa
la stessa domanda a modelli diversi e' lo scopo per cui e' stata chiesta. Fino
alla tappa 3 i byte arrivavano fino alla chat ma i benchmark li ignoravano,
catturando l'ottavo valore di execute_with_repair in `_db_transfer` senza usarlo.

Le somme seguono la stessa logica gia' usata nella catena: fase SQL (generazione
piu' eventuali riparazioni) piu' fase NL, con None neutro.

Tutto su doppi: nessuna rete, nessun database, nessuna chiamata a un modello.
"""
import inspect
import io
import os
import re
import sqlite3

import pytest

from backend.api import analytics_routes
from backend.services import benchmark_service as bench
from backend.services import logging_service as logsvc
from backend.services import nl2sql_service as nl2sql


_MODELLO = {"id": "m1", "name": "Gpt Oss 120b", "provider": "groq",
            "model_string": "openai/gpt-oss-120b", "api_key": "chiave"}


def _gen(b_req=100, b_resp=200, b_wire=50):
    return {"sql": "SELECT 1", "latency_ms": 10, "tokens_prompt": 5,
            "tokens_completion": 3, "tokens_total": 8, "warning": None,
            "attempts": 1, "llm_bytes_request": b_req,
            "llm_bytes_response": b_resp, "llm_bytes_response_wire": b_wire}


def _exp(b_req=1000, b_resp=2000, b_wire=500):
    return {"nl_response": "Sono 206.", "latency_ms": 9, "tokens_prompt": 7,
            "tokens_completion": 4, "tokens_total": 11,
            "llm_bytes_request": b_req, "llm_bytes_response": b_resp,
            "llm_bytes_response_wire": b_wire}


def _riparazione(b_req=7, b_resp=11, b_wire=3):
    return {"attempted": True, "succeeded": True, "original_sql": "SELECT bad",
            "original_error": "colonna inesistente", "latency_ms": 3,
            "tokens_prompt": 1, "tokens_completion": 1, "tokens_total": 2,
            "llm_bytes_request": b_req, "llm_bytes_response": b_resp,
            "llm_bytes_response_wire": b_wire, "attempts": 1}


_DB_TRANSFER = {"query_bytes": 40, "result_payload_bytes": 12}


@pytest.fixture
def scrittura_catturata(monkeypatch):
    """Intercetta le scritture su query_log per poterle ispezionare."""
    catturate = []

    def finta_save_to_db_log(*a, **k):
        catturate.append(k)
        return 1

    monkeypatch.setattr(bench.logsvc, "save_to_db_log", finta_save_to_db_log)
    monkeypatch.setattr(bench.logsvc, "save_log_file", lambda *a, **k: "finto.txt")
    monkeypatch.setattr(bench.logsvc, "update_log_filename", lambda *a, **k: None)
    monkeypatch.setattr(bench.logsvc, "save_benchmark_file", lambda *a, **k: "bench.txt")
    monkeypatch.setattr(bench, "get_model", lambda mid: _MODELLO)
    return catturate


def _stub_oneshot(monkeypatch, riparazione=None, db_error=None):
    monkeypatch.setattr(bench.nl2sql, "generate_sql", lambda *a, **k: _gen())
    monkeypatch.setattr(bench.nl2sql, "explain_results", lambda *a, **k: _exp())
    monkeypatch.setattr(
        bench.nl2sql, "execute_with_repair",
        lambda *a, **k: ("SELECT 1", ["n"], [{"n": 206}], db_error, False, 7,
                         riparazione, _DB_TRANSFER))


# ---------------------------------------------------------------------------
# One-shot (4)
# ---------------------------------------------------------------------------
def test_oneshot_riporta_i_byte(monkeypatch, scrittura_catturata):
    _stub_oneshot(monkeypatch)

    esito = bench.run_oneshot_benchmark("quante catture?", ["m1"], "prompt")
    r = esito["results"][0]

    assert r["llm_bytes_request"] == 1100
    assert r["llm_bytes_response"] == 2200
    assert r["llm_bytes_response_wire"] == 550
    assert r["db_bytes_query"] == 40
    assert r["db_bytes_result_payload"] == 12


def test_oneshot_attribuisce_la_riparazione_alla_fase_sql(monkeypatch, scrittura_catturata):
    """Stessa regola gia' applicata a latenza e token: i byte della query di
    correzione appartengono alla fase SQL, non a quella di interpretazione."""
    _stub_oneshot(monkeypatch, riparazione=_riparazione())

    r = bench.run_oneshot_benchmark("quante catture?", ["m1"], "prompt")["results"][0]

    assert r["llm_bytes_request"] == 1107
    assert r["llm_bytes_response"] == 2211
    assert r["llm_bytes_response_wire"] == 553


def test_oneshot_registra_i_byte_anche_su_errore_db(monkeypatch, scrittura_catturata):
    """Se la query fallisce sul DB il benchmark si ferma prima della Fase 2, ma
    il traffico della fase SQL e' stato speso: azzerarlo farebbe sembrare
    gratuiti proprio i modelli che sbagliano di piu'."""
    _stub_oneshot(monkeypatch, db_error="colonna inesistente")

    r = bench.run_oneshot_benchmark("quante catture?", ["m1"], "prompt")["results"][0]

    assert r["error"] == "colonna inesistente"
    assert r["llm_bytes_request"] == 100
    assert r["llm_bytes_response"] == 200
    assert r["db_bytes_query"] == 40


def test_oneshot_passa_i_transfer_a_save_to_db_log(monkeypatch, scrittura_catturata):
    """Punto 4: senza questo, le righe di benchmark su query_log avrebbero le
    colonne dei byte a NULL e Analytics non potrebbe confrontare i modelli."""
    _stub_oneshot(monkeypatch)

    bench.run_oneshot_benchmark("quante catture?", ["m1"], "prompt")

    assert scrittura_catturata, "save_to_db_log non e' stata chiamata"
    trasferimento = scrittura_catturata[0].get("transfer")
    assert trasferimento is not None, "save_to_db_log chiamata senza transfer"
    assert trasferimento["llm_bytes_request"] == 1100
    assert trasferimento["db_bytes_result_payload"] == 12


# ---------------------------------------------------------------------------
# Conversazionale (2)
# ---------------------------------------------------------------------------
def test_conversazionale_riporta_i_byte_per_turno(monkeypatch, scrittura_catturata):
    monkeypatch.setattr(bench.nl2sql, "generate_sql", lambda *a, **k: _gen())
    monkeypatch.setattr(bench.nl2sql, "explain_results", lambda *a, **k: _exp())
    monkeypatch.setattr(
        bench.nl2sql, "execute_with_repair",
        lambda *a, **k: ("SELECT 1", ["n"], [{"n": 206}], None, False, 7, None, _DB_TRANSFER))

    esito = bench.run_conversational_benchmark(["prima?", "seconda?"], ["m1"], "prompt")
    turni = esito["results"][0]["turns"]

    assert len(turni) == 2
    for t in turni:
        assert t["llm_bytes_request"] == 1100
        assert t["llm_bytes_response"] == 2200
        assert t["llm_bytes_response_wire"] == 550
        assert t["db_bytes_query"] == 40


def test_conversazionale_passa_i_transfer_a_save_to_db_log(monkeypatch, scrittura_catturata):
    monkeypatch.setattr(bench.nl2sql, "generate_sql", lambda *a, **k: _gen())
    monkeypatch.setattr(bench.nl2sql, "explain_results", lambda *a, **k: _exp())
    monkeypatch.setattr(
        bench.nl2sql, "execute_with_repair",
        lambda *a, **k: ("SELECT 1", ["n"], [{"n": 206}], None, False, 7, None, _DB_TRANSFER))

    bench.run_conversational_benchmark(["prima?"], ["m1"], "prompt")

    assert scrittura_catturata, "save_to_db_log non e' stata chiamata"
    trasferimento = scrittura_catturata[0].get("transfer")
    assert trasferimento is not None
    assert trasferimento["llm_bytes_request"] == 1100


# ---------------------------------------------------------------------------
# File di riepilogo del benchmark (2)
# ---------------------------------------------------------------------------
_RISULTATO_ONESHOT = [{
    "model_id": "m1", "model_name": "Gpt Oss 120b", "provider": "groq",
    "sql": "SELECT 1", "latency_sql": 13, "latency_db": 7, "latency_nl": 9,
    "latency_ms": 29, "tokens_prompt": 12, "tokens_completion": 7,
    "tokens_total": 19, "rows_count": 1, "columns": ["n"], "rows": [{"n": 206}],
    "nl_response": "Sono 206.", "truncated": False,
    "llm_bytes_request": 1100, "llm_bytes_response": 2200,
    "llm_bytes_response_wire": 550, "db_bytes_query": 8801,
    "db_bytes_result_payload": 9902,
}]


def test_file_benchmark_oneshot_contiene_i_byte(tmp_path, monkeypatch):
    monkeypatch.setattr(logsvc, "BENCHMARK_DIR", str(tmp_path))

    nome = logsvc.save_benchmark_file("run1", "quante catture?", _RISULTATO_ONESHOT)
    contenuto = io.open(os.path.join(str(tmp_path), nome), encoding="utf-8").read()

    # Numeri scelti perche' non possono comparire per caso fra latenze e token.
    assert "1100" in contenuto and "2200" in contenuto
    assert "8801" in contenuto and "9902" in contenuto
    assert "TRASFERIMENTI" in contenuto or "Byte" in contenuto


def test_file_benchmark_conversazionale_contiene_i_byte(tmp_path, monkeypatch):
    monkeypatch.setattr(logsvc, "BENCHMARK_DIR", str(tmp_path))
    risultati = [{
        "model_id": "m1", "model_name": "Gpt Oss 120b", "provider": "groq",
        "turns": [{
            "question": "prima?", "sql": "SELECT 1", "latency_sql": 13,
            "latency_db": 7, "latency_nl": 9, "tokens_prompt": 12,
            "tokens_completion": 7, "tokens_total": 19, "rows_count": 1,
            "columns": ["n"], "rows": [{"n": 206}], "nl_response": "Sono 206.",
            "llm_bytes_request": 1100, "llm_bytes_response": 2200,
            "llm_bytes_response_wire": 550, "db_bytes_query": 8801,
            "db_bytes_result_payload": 9902,
        }],
    }]

    nome = logsvc.save_benchmark_file("run1", "scenario", risultati,
                                      conversational=True, scenario=["prima?"])
    contenuto = io.open(os.path.join(str(tmp_path), nome), encoding="utf-8").read()

    assert "1100" in contenuto and "2200" in contenuto
    assert "8801" in contenuto and "9902" in contenuto
    assert "TRASFERIMENTI" in contenuto or "Byte" in contenuto


# ---------------------------------------------------------------------------
# Esposizione: analytics e frontend (2)
# ---------------------------------------------------------------------------
def test_analytics_aggrega_i_byte_per_modello():
    """Il confronto fra modelli e' lo scopo della misura: se i byte restano
    solo nelle righe grezze e non entrano negli aggregati per modello, nessuno
    li guardera' mai."""
    sorgente = inspect.getsource(analytics_routes.get_analytics)

    mancanti = [c for c in ("llm_bytes_request", "llm_bytes_response")
                if c not in sorgente]
    assert not mancanti, (
        "la query di /api/analytics non aggrega %s" % mancanti
    )


def test_benchmark_js_mostra_i_byte():
    """Controllo statico: i byte devono arrivare fino alla tabella di confronto
    del benchmark, che e' dove l'utente guarda."""
    sorgente = io.open(os.path.join("static", "js", "benchmark.js"),
                       encoding="utf-8").read()

    mancanti = [c for c in ("llm_bytes_request", "llm_bytes_response")
                if c not in sorgente]
    assert not mancanti, (
        "benchmark.js non mostra %s nella tabella di confronto" % mancanti
    )


# ---------------------------------------------------------------------------
# sum_llm_bytes_total: NULL non e' zero (3)
# ---------------------------------------------------------------------------
def _espressione_somma_byte():
    r"""Estrae dal sorgente l'espressione SQL di `sum_llm_bytes_total`.

    La ricerca va a ritroso dall'alias fino all'ultimo SUM( che lo precede: una
    prima versione usava r"SUM\(.*?\)\s*AS\s+sum_llm_bytes_total", che pero'
    agganciava il PRIMO SUM( del sorgente e si portava dietro mezza query,
    cast ::int compresi.
    """
    sorgente = inspect.getsource(analytics_routes.get_analytics)
    posizione = sorgente.find("AS sum_llm_bytes_total")
    assert posizione != -1, (
        "alias sum_llm_bytes_total non trovato nel sorgente: il test non sta "
        "verificando nulla"
    )
    prefisso = sorgente[:posizione]
    inizio = prefisso.rfind("SUM(")
    assert inizio != -1, "espressione SUM( non trovata prima dell'alias"
    espressione = prefisso[inizio:].rstrip()
    assert espressione.endswith(")"), (
        "espressione estratta malformata: %r" % espressione[-60:]
    )
    return espressione


def _valuta(espressione, righe):
    """Valuta l'espressione su righe sintetiche con sqlite (libreria standard).

    Non e' PostgreSQL, ma SUM, CASE, COALESCE e IS NULL sono SQL standard e si
    comportano allo stesso modo nei due motori: quello che serve qui e' la
    semantica dei NULL negli aggregati, che e' identica. Il vantaggio e' che il
    test resta deterministico e non richiede ne' VPN ne' database.
    """
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE q (llm_bytes_request INTEGER, llm_bytes_response INTEGER)")
    con.executemany("INSERT INTO q VALUES (?, ?)", righe)
    valore = con.execute("SELECT %s FROM q" % espressione).fetchone()[0]
    con.close()
    return valore


def test_somma_byte_e_null_se_nessuna_riga_e_misurata():
    """Un modello le cui righe sono tutte precedenti alla misurazione non ha
    trasferito zero byte: non e' stato misurato. Restituire 0 affermerebbe una
    misura mai fatta, ed e' esattamente la confusione NULL/zero che il resto
    del sistema evita con cura."""
    valore = _valuta(_espressione_somma_byte(), [(None, None), (None, None)])

    assert valore is None, (
        "modello interamente non misurato: atteso NULL, ottenuto %r" % valore
    )


def test_somma_byte_ignora_le_righe_non_misurate_ma_somma_le_altre():
    """Il caso misto e' il motivo per cui la COALESCE era stata messa: una riga
    non misurata non deve azzerare il totale del modello."""
    valore = _valuta(_espressione_somma_byte(),
                     [(100, 200), (None, None), (7, 11)])

    assert valore == 318


def test_analytics_js_non_stampa_null():
    """Con NULL al posto di 0 il frontend deve continuare a mostrare un trattino,
    non la stringa "null"."""
    sorgente = io.open(os.path.join("static", "js", "analytics.js"),
                       encoding="utf-8").read()
    righe = [r for r in sorgente.split("\n") if "sum_llm_bytes_total" in r]

    assert righe, "sum_llm_bytes_total non compare in analytics.js"
    assert any(u"\u2014" in r for r in righe), (
        "il valore non passa da una guardia che produce un trattino: con NULL "
        "il browser stamperebbe 'null'"
    )
