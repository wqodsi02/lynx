# -*- coding: utf-8 -*-
"""
Gruppo 7 - misurazione dei transfer rate (byte trasferiti).

Requisito esplicito del professore, mai strumentato. Questi test sono scritti
PRIMA dell'implementazione e fissano il contratto: girano su oggetti finti,
senza rete e senza database.

Tre quantita' distinte, che non vanno confuse:
  - byte del PAYLOAD applicativo (il JSON spedito / ricevuto decompresso):
    misurabili con precisione;
  - byte del CORPO SUL FILO (compresso): misurabili solo quando il provider
    manda Content-Length, altrimenti None;
  - byte del TRAFFICO REALE (header + TLS + TCP): NON misurabili dall'interno
    di requests, e infatti qui non si fingono.

`requests` invia `Accept-Encoding: gzip, deflate` di default, quindi
`len(response.content)` e' il decompresso e NON e' cio' che ha attraversato la
rete: i due valori vanno registrati separatamente.
"""
import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
import requests

from backend.db import readonly_guard
from backend.llm.base import LLMResult
from backend.llm.providers import gemini, groq
from backend.transfer import byte_payload_json


# ---------------------------------------------------------------------------
# Doppi
# ---------------------------------------------------------------------------
def _risposta_finta(corpo, status=200, headers=None, corpo_richiesta=b'{"m":"x"}'):
    """Costruisce una requests.Response vera, popolata a mano.

    `request` e' una PreparedRequest reale: e' da li' che l'adattatore deve
    leggere i byte effettivamente spediti, non ri-serializzando il payload.
    """
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(corpo).encode("utf-8")
    r.headers.update(headers or {})
    r.request = requests.Request("POST", "https://esempio.invalid/v1",
                                 data=corpo_richiesta).prepare()
    return r


_CORPO_GROQ_OK = {
    "choices": [{"message": {"content": "SELECT 1"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}
_CORPO_GEMINI_OK = {
    "candidates": [{"content": {"parts": [{"text": "SELECT 1"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
}

_MESSAGGI = [{"role": "system", "content": "sei un traduttore"},
             {"role": "user", "content": "quante catture?"}]


class _OrologioFinto:
    """Sostituisce il modulo `time` dentro l'adattatore: avanza solo quando
    qualcuno dorme o quando una richiesta finta dichiara di aver impiegato
    tempo. Rende la misura della latenza deterministica."""

    def __init__(self):
        self.adesso = 1000.0

    def time(self):
        return self.adesso

    def sleep(self, secondi):
        self.adesso += secondi


# ---------------------------------------------------------------------------
# Provider: byte richiesta e risposta (4)
# ---------------------------------------------------------------------------
def test_byte_richiesta_letti_dalla_prepared_request(monkeypatch):
    corpo_richiesta = b'{"model":"x","messages":[]}'
    risposta = _risposta_finta(_CORPO_GROQ_OK, corpo_richiesta=corpo_richiesta)
    monkeypatch.setattr(groq.requests, "post", lambda *a, **k: risposta)

    res = groq.call("llama-3.3-70b", "chiave", _MESSAGGI)

    assert res.bytes_request == len(corpo_richiesta)


def test_byte_risposta_decompressi(monkeypatch):
    risposta = _risposta_finta(_CORPO_GROQ_OK)
    monkeypatch.setattr(groq.requests, "post", lambda *a, **k: risposta)

    res = groq.call("llama-3.3-70b", "chiave", _MESSAGGI)

    assert res.bytes_response == len(risposta.content)


def test_content_length_registrato_a_parte_dal_decompresso(monkeypatch):
    """Con gzip il corpo sul filo e' molto piu' piccolo del decompresso: i due
    numeri devono restare distinti, non sovrascriversi."""
    risposta = _risposta_finta(_CORPO_GROQ_OK,
                               headers={"Content-Length": "42", "Content-Encoding": "gzip"})
    monkeypatch.setattr(groq.requests, "post", lambda *a, **k: risposta)

    res = groq.call("llama-3.3-70b", "chiave", _MESSAGGI)

    assert res.bytes_response_wire == 42
    assert res.bytes_response == len(risposta.content)
    assert res.bytes_response != res.bytes_response_wire


def test_content_length_assente_da_none_non_zero(monkeypatch):
    """None significa 'non determinabile', zero significherebbe 'misurato, era
    zero'. Confonderli falserebbe in silenzio le medie di Analytics.

    Deliberatamente NON si ripiega su response.raw.tell(): finche' non e'
    validato contro una risposta reale, meglio None di un numero di cui non ci
    si fida.
    """
    risposta = _risposta_finta(_CORPO_GROQ_OK)
    risposta.headers.pop("Content-Length", None)
    monkeypatch.setattr(groq.requests, "post", lambda *a, **k: risposta)

    res = groq.call("llama-3.3-70b", "chiave", _MESSAGGI)

    assert res.bytes_response_wire is None


# ---------------------------------------------------------------------------
# Provider: retry 429 (3)
# ---------------------------------------------------------------------------
def test_retry_429_somma_i_byte_dei_tentativi(monkeypatch):
    """IL test del gruppo. Oggi il retry e' ricorsivo ed e' la chiamata piu'
    interna a costruire LLMResult: i byte dei tentativi falliti andrebbero
    persi. Devono invece essere sommati, come gia' si fa per i token."""
    corpo_429 = b'{"tentativo":1}'
    corpo_ok = b'{"tentativo":2}'
    r429 = _risposta_finta({"error": {"message": "rate limit"}}, status=429,
                           corpo_richiesta=corpo_429)
    rok = _risposta_finta(_CORPO_GROQ_OK, corpo_richiesta=corpo_ok)
    risposte = iter([r429, rok])
    monkeypatch.setattr(groq.requests, "post", lambda *a, **k: next(risposte))
    monkeypatch.setattr(groq, "time", _OrologioFinto())

    res = groq.call("llama-3.3-70b", "chiave", _MESSAGGI)

    assert res.bytes_request == len(corpo_429) + len(corpo_ok)
    assert res.bytes_response == len(r429.content) + len(rok.content)


def test_retry_429_latenza_include_tentativi_falliti_e_attese(monkeypatch):
    """Bug PREESISTENTE, indipendente dai byte: nella versione ricorsiva il t0
    della chiamata interna parte DOPO il time.sleep(), quindi latency_ms esclude
    sia il tentativo fallito sia l'attesa. Con t0 fuori dal ciclo la latenza
    deve coprire tutto.

    Cronologia attesa: 0,5 s primo tentativo + 2 s di attesa + 0,25 s secondo
    tentativo = 2,75 s.

    Le durate sono scelte fra le frazioni binarie esatte (0,5 - 2 - 0,25) di
    proposito: `int((t1 - t0) * 1000)` TRONCA, e con uno 0,3 non rappresentabile
    esattamente 2,8 s diventerebbero 2799 ms invece di 2800, rendendo il test
    impossibile da soddisfare anche a correzione avvenuta.
    """
    orologio = _OrologioFinto()
    r429 = _risposta_finta({"error": {}}, status=429)
    rok = _risposta_finta(_CORPO_GROQ_OK)
    risposte = iter([(r429, 0.5), (rok, 0.25)])

    def post_finta(*a, **k):
        risposta, durata = next(risposte)
        orologio.adesso += durata
        return risposta

    monkeypatch.setattr(groq.requests, "post", post_finta)
    monkeypatch.setattr(groq, "time", orologio)

    res = groq.call("llama-3.3-70b", "chiave", _MESSAGGI)

    assert res.latency_ms == 2750


def test_errore_non_ritentabile_conta_comunque_i_byte(monkeypatch):
    """Un 401 non viene ritentato ma il traffico c'e' stato: i byte spesi vanno
    attribuiti anche quando la chiamata fallisce, altrimenti il costo reale dei
    modelli che sbagliano di piu' risulterebbe sottostimato."""
    corpo_richiesta = b'{"model":"x"}'
    r401 = _risposta_finta({"error": {"message": "bad key"}}, status=401,
                           corpo_richiesta=corpo_richiesta)
    monkeypatch.setattr(groq.requests, "post", lambda *a, **k: r401)

    from backend.llm.base import LLMProviderError
    with pytest.raises(LLMProviderError) as info:
        groq.call("llama-3.3-70b", "chiave", _MESSAGGI)

    assert getattr(info.value, "bytes_request", None) == len(corpo_richiesta)
    assert getattr(info.value, "bytes_response", None) == len(r401.content)


# ---------------------------------------------------------------------------
# Provider: parita' fra adattatori (1)
# ---------------------------------------------------------------------------
def test_anche_gemini_riporta_i_byte(monkeypatch):
    """La misurazione va aggiunta a ENTRAMBI gli adattatori: se ne coprisse uno
    solo, i confronti fra provider nel benchmark sarebbero falsati."""
    corpo_richiesta = b'{"contents":[]}'
    risposta = _risposta_finta(_CORPO_GEMINI_OK, headers={"Content-Length": "77"},
                               corpo_richiesta=corpo_richiesta)
    monkeypatch.setattr(gemini.requests, "post", lambda *a, **k: risposta)

    res = gemini.call("gemini-2.5-flash", "chiave", _MESSAGGI)

    assert res.bytes_request == len(corpo_richiesta)
    assert res.bytes_response == len(risposta.content)
    assert res.bytes_response_wire == 77


# ---------------------------------------------------------------------------
# Contratto di LLMResult (1)
# ---------------------------------------------------------------------------
def test_llmresult_ha_i_campi_byte_con_default_innocui():
    """I nuovi campi devono avere default, cosi' le costruzioni esistenti di
    LLMResult continuano a funzionare senza modifiche."""
    res = LLMResult(text="SELECT 1", latency_ms=100)

    assert res.bytes_request == 0
    assert res.bytes_response == 0
    assert res.bytes_response_wire is None


# ---------------------------------------------------------------------------
# Lato database (3)
# ---------------------------------------------------------------------------
class _CursoreFinto:
    def __init__(self, righe, query_inviata=b"SELECT 1"):
        self._righe = righe
        self.query = query_inviata
        self.description = [(c,) for c in (righe[0].keys() if righe else [])]

    def execute(self, sql, *a):
        pass

    def fetchmany(self, n):
        return self._righe[:n]


class _ConnessioneFinta:
    def __init__(self, cursore):
        self._cursore = cursore

    def set_session(self, **k):
        pass

    def cursor(self, **k):
        return self._cursore

    def close(self):
        pass


def _installa_db_finto(monkeypatch, righe, query_inviata=b"SELECT 1"):
    cur = _CursoreFinto(righe, query_inviata)
    monkeypatch.setattr(readonly_guard, "get_connection",
                        lambda: _ConnessioneFinta(cur))
    return cur


def test_byte_query_inviata(monkeypatch):
    """psycopg2 espone `cursor.query`: sono i byte della query effettivamente
    spedita dopo il binding dei parametri, quindi e' preferibile a len(sql)."""
    query = b"SELECT COUNT(*) FROM pcapinfo"
    _installa_db_finto(monkeypatch, [{"n": 1}], query_inviata=query)

    *_, trasferimento = readonly_guard.execute_readonly_query("SELECT COUNT(*) FROM pcapinfo")

    assert trasferimento["query_bytes"] == len(query)


def test_query_non_disponibile_da_none(monkeypatch):
    _installa_db_finto(monkeypatch, [{"n": 1}], query_inviata=None)

    *_, trasferimento = readonly_guard.execute_readonly_query("SELECT 1")

    assert trasferimento["query_bytes"] is None


def test_byte_payload_risultati(monkeypatch):
    """psycopg2 NON espone i byte ricevuti: ne' il cursore ne' ConnectionInfo
    hanno contatori, e nemmeno libpq. Questo valore e' quindi un CALCOLO sul
    payload applicativo, non una misura del traffico: il nome della colonna
    (`db_bytes_result_payload`) lo dice apertamente.
    """
    righe = [{"code": "SC1", "throughput": 4.99}, {"code": "SC2", "throughput": 8.1}]
    _installa_db_finto(monkeypatch, righe)

    *_, trasferimento = readonly_guard.execute_readonly_query("SELECT 1")

    assert trasferimento["result_payload_bytes"] == byte_payload_json(righe)


def test_payload_usa_lo_stesso_encoder_della_risposta_http(monkeypatch):
    """Fissa la convenzione di serializzazione, dopo averla quantificata.

    `default=str` sarebbe stata la via comoda per evitare la dipendenza
    circolare fra readonly_guard e app.py, ma serializza ogni Decimal come
    stringa quotata ("16.69" invece di 16.69): due byte in piu' per valore.
    Misurato su righe reali del progetto:

        riga a testo dominante (1 Decimal)      +1,67%
        aggregato per impianto (2 Decimal)      +3,96%
        statistiche IAT (4 Decimal)             +5,80%
        conteggio scalare (1 Decimal)           +8,00%
        200 righe di soli aggregati             +5,43%  (+1200 byte)

    Ben oltre l'1%: su una misura di transfer rate sarebbe un errore
    sistematico inaccettabile. Per questo la logica di LynxJSONProvider e'
    stata estratta in `backend/json_encoding.py` e importata da entrambi
    invece di duplicare una convenzione "quasi uguale". Questo test verifica
    che la misura usi davvero quell'encoder e non sia ricaduta su default=str.
    """
    righe = [{"plant": "MELFI", "avg_throughput_mbps": Decimal("16.69"),
              "date": date(2014, 5, 25),
              "run_id": uuid.UUID("4272b34b-59d5-41d2-8543-839fb3aab17d")}]
    _installa_db_finto(monkeypatch, righe)

    *_, trasferimento = readonly_guard.execute_readonly_query("SELECT 1")

    con_encoder = byte_payload_json(righe)
    con_str = len(json.dumps(righe, ensure_ascii=False, default=str).encode("utf-8"))

    assert trasferimento["result_payload_bytes"] == con_encoder
    assert con_str > con_encoder, (
        "le due convenzioni devono differire, altrimenti questo test non "
        "sta verificando nulla"
    )


# ---------------------------------------------------------------------------
# Aggregazione fra le fasi (1)
# ---------------------------------------------------------------------------
def test_somma_byte_tratta_none_come_neutro():
    """Serve a sommare i contributi di Fase 1, Fase 2 e auto-riparazione. Un
    contributo non misurato (None) non deve azzerare il totale ne' renderlo
    None; se pero' NIENTE e' stato misurato il totale e' None, non zero."""
    from backend.transfer import somma_byte

    assert somma_byte(100, 200, 50) == 350
    assert somma_byte(100, None, 50) == 150
    assert somma_byte(None, None) is None
    assert somma_byte() is None
