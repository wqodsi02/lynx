# -*- coding: utf-8 -*-
"""
Gruppo 1 - readonly_guard.validate_readonly_sql

E' il secondo dei tre livelli di sicurezza dell'applicazione (system prompt ->
whitelist applicativa -> transazione READ ONLY sul database). A differenza
degli altri due e' l'unico che gira interamente in-process, quindi l'unico
verificabile senza DB: per questo ha la priorita' nella suite.

Un falso negativo qui non e' un fastidio ma una vulnerabilita', e un falso
positivo blocca query di sola lettura perfettamente legittime. I test coprono
entrambe le direzioni.

`validate_readonly_sql` e' una funzione pura: nessuna connessione, nessuna rete.
"""
import pytest

from backend.db.readonly_guard import validate_readonly_sql


# ---------------------------------------------------------------------------
# Casi ammessi (6)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select id_line from linepn",
    "WITH ultime AS (SELECT id_line FROM pcapinfo) SELECT * FROM ultime",
    "EXPLAIN SELECT 1",
    "SELECT 1;",
    "\n   SELECT id_line FROM linepn\n",
], ids=[
    "select-semplice",
    "select-minuscolo",
    "cte-with",
    "explain",
    "punto-e-virgola-finale",
    "spazi-iniziali",
])
def test_query_di_lettura_ammesse(sql):
    """SELECT, WITH ed EXPLAIN passano, in qualsiasi combinazione di maiuscole,
    spaziatura e punto e virgola finale singolo."""
    is_valid, err = validate_readonly_sql(sql)
    assert is_valid is True, "query di lettura rifiutata: %s" % err
    assert err is None


# ---------------------------------------------------------------------------
# Casi rifiutati (6)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sql, frammento_atteso", [
    ("", "vuota"),
    ("   \n  ", "vuota"),
    ("INSERT INTO linepn (plant) VALUES ('X')", "Rilevato: 'INSERT'"),
    ("DROP TABLE linepn", "Rilevato: 'DROP'"),
    ("SELECT 1; DROP TABLE linepn", "solo query singole"),
    ("SELECT 1; -- pulizia\nDROP TABLE linepn", "solo query singole"),
], ids=[
    "stringa-vuota",
    "solo-spazi",
    "insert-prima-parola",
    "drop-prima-parola",
    "multi-statement",
    "multi-statement-con-commento",
])
def test_query_rifiutate(sql, frammento_atteso):
    """Query vuote, comandi di scrittura in prima posizione e piu' statement
    separati da ';' vengono bloccati, con un messaggio che dice perche'."""
    is_valid, err = validate_readonly_sql(sql)
    assert is_valid is False, "query non di lettura accettata: %r" % sql
    assert frammento_atteso in err


# ---------------------------------------------------------------------------
# Blacklist (4)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sql, keyword", [
    ("WITH d AS (DELETE FROM linepn RETURNING id_line) SELECT * FROM d",
     "DELETE"),
    ("WITH u AS (UPDATE linepn SET plant = 'X' RETURNING id_line) SELECT * FROM u",
     "UPDATE"),
    ("WITH i AS (INSERT INTO linepn (plant) VALUES ('X') RETURNING id_line) SELECT * FROM i",
     "INSERT"),
    ("EXPLAIN COPY linepn TO '/tmp/out.csv'",
     "COPY"),
], ids=["cte-delete", "cte-update", "cte-insert", "explain-copy"])
def test_blacklist_blocca_scritture_mascherate(sql, keyword):
    """Il controllo sulla prima parola da solo non basta: PostgreSQL ammette
    CTE che modificano i dati (`WITH x AS (DELETE ... RETURNING *) SELECT`),
    che iniziano con WITH e sarebbero quindi accettate dal primo controllo.
    E' esattamente il caso che la blacklist deve intercettare."""
    is_valid, err = validate_readonly_sql(sql)
    assert is_valid is False, "scrittura mascherata accettata: %r" % sql
    assert "non permessa" in err
    assert keyword in err


# ---------------------------------------------------------------------------
# REPLACE deve restare permesso (2)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "SELECT REPLACE(line, 'vecchio', 'nuovo') AS descrizione FROM linepn",
    "select replace(replace(line, 'a', ''), 'b', 'c') from linepn",
], ids=["replace-semplice", "replace-annidato-con-letterale-vuoto"])
def test_replace_resta_permesso(sql):
    """REPLACE non e' nella blacklist ed e' giusto cosi': in PostgreSQL e' una
    funzione di stringa di sola lettura, non un comando di scrittura come in
    altri DBMS. Bloccarla impedirebbe SELECT legittime.

    Il secondo caso esercita anche il letterale vuoto ('') dentro la regex dei
    letterali, che deve essere consumato correttamente e non mandare fuori
    sincrono il riconoscimento delle stringhe successive.
    """
    is_valid, err = validate_readonly_sql(sql)
    assert is_valid is True, "REPLACE bloccato per errore: %s" % err


# ---------------------------------------------------------------------------
# Letterali e commenti non devono ingannare il parser (4)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sql, motivo", [
    ("SELECT id_line FROM linepn WHERE line ILIKE '%update%'",
     "una keyword dentro un letterale stringa non e' un comando"),
    ("SELECT 'a;b' AS etichetta FROM linepn",
     "un ';' dentro un letterale stringa non separa due statement"),
    ("SELECT id_line FROM linepn -- DROP TABLE linepn",
     "una keyword in un commento di riga va ignorata"),
    ("SELECT /* CREATE TABLE tmp */ id_line FROM linepn",
     "una keyword in un commento a blocco va ignorata"),
], ids=[
    "keyword-in-letterale",
    "punto-e-virgola-in-letterale",
    "keyword-in-commento-di-riga",
    "keyword-in-commento-a-blocco",
])
def test_letterali_e_commenti_non_ingannano_il_parser(sql, motivo):
    """Sono i falsi positivi che `_strip_literals_and_comments` esiste per
    evitare: senza quella pulizia preventiva queste query di sola lettura
    verrebbero bloccate."""
    is_valid, err = validate_readonly_sql(sql)
    assert is_valid is True, "%s (errore restituito: %s)" % (motivo, err)


# ---------------------------------------------------------------------------
# Difetti noti, documentati come xfail stretti (2)
# ---------------------------------------------------------------------------
_MOTIVO_VIRGOLETTE = (
    "Difetto noto, non ancora corretto: _strip_literals_and_comments ripulisce "
    "solo i letterali fra apici singoli. Un identificatore delimitato da "
    "virgolette doppie che coincide con una keyword della blacklist viene "
    "scambiato per un comando di scrittura, e una SELECT legittima viene "
    "rifiutata. xfail STRETTO: se un giorno il guard verra' corretto questo "
    "test passera' e pytest lo segnalera' come XPASS/fallimento, obbligando ad "
    "aggiornarlo invece di lasciarlo silenziosamente obsoleto."
)


@pytest.mark.xfail(strict=True, reason=_MOTIVO_VIRGOLETTE)
def test_alias_fra_virgolette_doppie_che_e_una_keyword():
    is_valid, err = validate_readonly_sql('SELECT id_line AS "comment" FROM linepn')
    assert is_valid is True, err


@pytest.mark.xfail(strict=True, reason=_MOTIVO_VIRGOLETTE)
def test_colonna_fra_virgolette_doppie_che_e_una_keyword():
    is_valid, err = validate_readonly_sql('SELECT "create" FROM linepn')
    assert is_valid is True, err
