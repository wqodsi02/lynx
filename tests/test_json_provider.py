# -*- coding: utf-8 -*-
"""
Gruppo 4 - app.LynxJSONProvider

psycopg2 restituisce `Decimal` per ogni colonna NUMERIC (quindi per l'output di
ROUND/AVG/SUM), `date`/`time` per le colonne temporali e `UUID` per gli
identificativi: senza questo provider quei valori non arriverebbero al
frontend nella forma attesa.

I test girano attraverso `create_app().json.dumps(...)`, non chiamando la
classe a mano: cosi' verificano anche il wiring `app.json = LynxJSONProvider(app)`
in app.py, che e' la meta' del lavoro. Nessuna connessione, nessuna rete.

ATTENZIONE al confronto con il provider di serie di Flask 3.0.3, verificato
empiricamente e diverso da quanto dice il commento in app.py:
    Decimal('12')  -> '12'                              (STRINGA, non un numero)
    date(2026,7,4) -> 'Sat, 04 Jul 2026 00:00:00 GMT'   (formato HTTP, non ISO)
    time(13,27,14) -> TypeError
Solo il terzo caso sarebbe un errore 500. Gli altri due sono peggio, non
meglio: passerebbero silenziosamente al frontend una stringa dove ci si aspetta
un numero, o una data in un formato che il grafico non sa leggere.
"""
import json
import uuid
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal

import pytest
from flask.json.provider import DefaultJSONProvider


@pytest.fixture(scope="module")
def app():
    from backend.app import create_app
    return create_app()


def _round_trip(app, valore):
    """Serializza con il provider dell'app e rilegge, per poter controllare il
    TIPO JSON risultante e non solo il valore."""
    return json.loads(app.json.dumps({"v": valore}))["v"]


# ---------------------------------------------------------------------------
# Decimal (4)
# ---------------------------------------------------------------------------
def test_decimal_intero_diventa_int_json(app):
    """Un Decimal con parte decimale nulla deve uscire come intero: e' un
    conteggio, e `12.0` sarebbe fuorviante.

    L'ultima assert e' il vero motivo per cui LynxJSONProvider esiste: senza
    l'override lo stesso valore diventerebbe la STRINGA "12", che il frontend
    non puo' sommare ne' mettere in un grafico.
    """
    letto = _round_trip(app, Decimal("12"))
    assert letto == 12
    assert isinstance(letto, int)
    assert '"v": 12' in app.json.dumps({"v": Decimal("12")})
    assert DefaultJSONProvider.default(Decimal("12")) == "12"


def test_decimal_con_decimali_diventa_float_json(app):
    letto = _round_trip(app, Decimal("12.5"))
    assert letto == 12.5
    assert isinstance(letto, float)


def test_decimal_zero_diventa_int(app):
    """Caso limite di `o == o.to_integral_value()`: zero e' intero."""
    letto = _round_trip(app, Decimal("0"))
    assert letto == 0
    assert isinstance(letto, int)


def test_decimal_negativo_non_intero(app):
    """Il troncamento verso lo zero di to_integral_value() non deve far
    scambiare un valore negativo con decimali per un intero."""
    letto = _round_trip(app, Decimal("-3.25"))
    assert letto == -3.25
    assert isinstance(letto, float)


# ---------------------------------------------------------------------------
# Altri tipi restituiti da psycopg2 (4)
# ---------------------------------------------------------------------------
def test_date_e_orari_in_iso(app):
    """ISO 8601 per tutti e tre. `time` in particolare non e' gestito affatto
    dal provider di serie e sarebbe un TypeError."""
    assert _round_trip(app, datetime(2026, 7, 4, 13, 27, 14)) == "2026-07-04T13:27:14"
    assert _round_trip(app, date(2026, 7, 4)) == "2026-07-04"
    assert _round_trip(app, dtime(13, 27, 14)) == "13:27:14"


def test_timedelta_diventa_stringa(app):
    assert _round_trip(app, timedelta(seconds=90)) == "0:01:30"


def test_uuid_diventa_stringa(app):
    valore = uuid.UUID("4272b34b-59d5-41d2-8543-839fb3aab17d")
    assert _round_trip(app, valore) == "4272b34b-59d5-41d2-8543-839fb3aab17d"


def test_binario_diventa_segnaposto(app):
    """I dati binari non vengono trasferiti al frontend: si sostituiscono con un
    segnaposto invece di far fallire l'intera risposta."""
    assert _round_trip(app, b"\x00\x01") == "<binary>"
    assert _round_trip(app, memoryview(b"ab")) == "<binary>"


# ---------------------------------------------------------------------------
# Integrazione e limite (2)
# ---------------------------------------------------------------------------
def test_riga_psycopg2_realistica(app):
    """Una riga come quelle che tornano davvero da una query con aggregati:
    tutti i tipi problematici insieme, in un dizionario annidato."""
    riga = {
        "id_line": 314,
        "throughput_medio_mbps": Decimal("4.99"),
        "catture": Decimal("206"),
        "date": date(2014, 5, 25),
        "time": dtime(11, 1, 31),
        "durata": timedelta(seconds=30),
        "run_id": uuid.UUID("4272b34b-59d5-41d2-8543-839fb3aab17d"),
    }
    letto = json.loads(app.json.dumps({"rows": [riga]}))["rows"][0]
    assert letto["throughput_medio_mbps"] == 4.99
    assert isinstance(letto["catture"], int) and letto["catture"] == 206
    assert letto["date"] == "2014-05-25"
    assert letto["time"] == "11:01:31"
    assert letto["durata"] == "0:00:30"
    assert letto["run_id"].startswith("4272b34b")


def test_tipo_non_gestito_solleva_typeerror(app):
    """Il fallback a DefaultJSONProvider.default deve restare: un tipo davvero
    ignoto va segnalato, non mascherato con una stringa qualunque."""
    with pytest.raises(TypeError):
        app.json.dumps({"v": object()})
