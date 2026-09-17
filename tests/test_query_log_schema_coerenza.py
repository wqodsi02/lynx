# -*- coding: utf-8 -*-
"""
Gruppo 6 - coerenza fra lo schema di query_log e le query che lo usano.

Nasce da un bug reale: `log_filename` era dichiarata SOLO dentro il
`CREATE TABLE IF NOT EXISTS`, e non in `_COLUMNS_DDL`. Su un database dove la
tabella esisteva gia' (creata da una versione precedente dello schema) il
CREATE e' un no-op, nessun ALTER aggiungeva quella colonna, e ogni INSERT
falliva con `column "log_filename" does not exist`. L'eccezione veniva
inghiottita da `save_to_db_log`, che restituiva None: 123 esecuzioni su 123
hanno scritto `Log ID : N/A` nei file di log senza che nulla lo segnalasse, e
/api/history rispondeva 500.

L'INVARIANTE che questi test difendono: su un'installazione dove la tabella
esiste gia', l'unico modo per aggiungere una colonna e' un ALTER, quindi ogni
colonna che il codice applicativo nomina deve stare in `_COLUMNS_DDL`. Le
colonne presenti solo nel CREATE TABLE sono raggiungibili unicamente da chi
parte da database vuoto.

ATTENZIONE a come leggere il fallimento: la regola non segnala solo
`log_filename`, ma OGNI colonna che una tabella preesistente non puo'
acquisire. Oggi sono otto (model, question, sql_generated, nl_response,
rows_returned, latency_ms, log_filename, error) piu' id e timestamp per le
query di lettura. Sette di quelle otto funzionano solo perche' erano gia' nel
CREATE TABLE originale quando la tabella fu creata: e' fortuna storica, non
una garanzia. `log_filename` e' semplicemente l'unica che e' stata aggiunta al
CREATE TABLE DOPO, quando la tabella esisteva gia', ed e' per questo l'unica
che e' esplosa. Le altre sette sono la stessa bomba, solo non ancora innescata.

Da notare che la regola piu' debole - colonne dell'INSERT contenute
nell'UNIONE fra CREATE TABLE e _COLUMNS_DDL - sarebbe VERDE anche prima della
correzione, perche' `log_filename` nel CREATE TABLE c'e'. Quella formulazione
non avrebbe intercettato il bug.

Analisi puramente STATICA del sorgente: si legge il codice, non si apre nessuna
connessione e non si esegue nessuna query. Va bene anche senza VPN.
"""
import ast
import inspect
import io
import re

import pytest

from backend.api import analytics_routes, misc_routes
from backend.db import query_log_schema
from backend.services import logging_service


# ---------------------------------------------------------------------------
# Estrazione dallo schema
# ---------------------------------------------------------------------------
def _colonne_del_create_table():
    """Nomi di colonna dichiarati nel CREATE TABLE di `_ensure_log_table`."""
    sorgente = inspect.getsource(query_log_schema._ensure_log_table)
    inizio = sorgente.index("CREATE TABLE IF NOT EXISTS query_log (")
    corpo = re.search(r"\((.*?)\n\s*\)", sorgente[inizio:], re.S).group(1)
    colonne = []
    for riga in corpo.split("\n"):
        riga = riga.strip()
        if not riga:
            continue
        m = re.match(r"([a-zA-Z_][a-zA-Z0-9_]*)", riga)
        if m:
            colonne.append(m.group(1))
    return colonne


# `id` e' l'unica colonna deliberatamente assente da _COLUMNS_DDL, e va quindi
# esclusa dai controlli: e' SERIAL PRIMARY KEY, e un ALTER TABLE ADD COLUMN che
# la aggiungesse a una tabella che una primary key ce l'ha gia' fallirebbe. Su
# qualunque tabella query_log preesistente `id` c'e' per forza, perche' senza
# di essa la tabella non sarebbe mai stata creata: non e' un rischio.
_COLONNE_STRUTTURALI = frozenset({"id"})


def _colonne_degli_alter():
    """Nomi di colonna che la migrazione sa aggiungere a una tabella esistente."""
    return [nome for nome, _tipo in query_log_schema._COLUMNS_DDL]


def _colonne_creabili():
    """Unione: tutto cio' che lo schema sa produrre, su installazione nuova o
    gia' esistente."""
    return set(_colonne_del_create_table()) | set(_colonne_degli_alter())


# ---------------------------------------------------------------------------
# Estrazione dalle query
# ---------------------------------------------------------------------------
def _insert_di_save_to_db_log():
    """Ritorna (colonne, numero_di_placeholder) dell'INSERT su query_log."""
    sorgente = inspect.getsource(logging_service.save_to_db_log)
    m = re.search(r"INSERT INTO query_log\s*\((.*?)\)\s*VALUES\s*\((.*?)\)", sorgente, re.S)
    assert m, "INSERT INTO query_log non trovato in save_to_db_log"
    colonne = [c.strip() for c in m.group(1).replace("\n", " ").split(",") if c.strip()]
    return colonne, m.group(2).count("%s")


def _sql_della_funzione(funzione):
    """Tutte le stringhe SQL passate a cur.execute() dentro una funzione."""
    sorgente = inspect.getsource(funzione)
    return re.findall(r'cur\.execute\(\s*"""(.*?)"""', sorgente, re.S)


# Parole che nelle query non sono nomi di colonna: keyword SQL, funzioni,
# nome della tabella. Se un giorno un test qui sotto fallisce su una parola
# nuova, la domanda da farsi e': ho aggiunto una funzione SQL (allora va qui)
# o una colonna (allora va in _COLUMNS_DDL)?
_PAROLE_SQL = frozenset("""
    select from where group by order asc desc limit offset as is not null and or
    true false distinct filter case when then else end
    count sum avg min max round date now interval extract coalesce
    int integer text boolean numeric
    query_log
""".split())


def _colonne_referenziate(sql):
    """Identificatori che, tolti letterali, cast, alias e parole SQL, possono
    solo essere nomi di colonna."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)  # commenti a blocco
    sql = re.sub(r"--[^\n]*", " ", sql)            # commenti di riga
    sql = re.sub(r"'(?:[^']|'')*'", " ", sql)      # letterali stringa
    sql = re.sub(r"::\s*\w+", " ", sql)            # cast ::int
    sql = sql.replace("%s", " ")                   # placeholder psycopg2
    alias = {a.lower() for a in re.findall(r"\bAS\s+([a-zA-Z_]\w*)", sql, re.I)}
    parole = {p.lower() for p in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", sql)}
    return parole - alias - _PAROLE_SQL


# ---------------------------------------------------------------------------
# I test
# ---------------------------------------------------------------------------
def test_insert_usa_solo_colonne_che_lo_schema_sa_creare():
    """IL test: ogni colonna nominata dall'INSERT deve essere creabile dalla
    migrazione anche su una tabella preesistente, quindi deve stare in
    `_COLUMNS_DDL`. Averla solo nel CREATE TABLE non basta."""
    colonne_insert, _ = _insert_di_save_to_db_log()
    creabili_da_alter = set(_colonne_degli_alter())
    mancanti = [c for c in colonne_insert if c not in creabili_da_alter]
    assert not mancanti, (
        "Colonne usate dall'INSERT ma assenti da _COLUMNS_DDL: %s.\n"
        "Su un database dove query_log esiste gia', il CREATE TABLE IF NOT EXISTS "
        "e' un no-op e nessun ALTER le aggiunge: ogni INSERT fallira' con "
        "'column ... does not exist', e save_to_db_log inghiottira' l'errore "
        "restituendo None." % mancanti
    )


def test_history_usa_solo_colonne_che_lo_schema_sa_creare():
    """/api/history proietta esplicitamente le colonne: se una non esiste, la
    rotta risponde 500 invece della cronologia."""
    creabili_da_alter = set(_colonne_degli_alter())
    referenziate = set()
    for sql in _sql_della_funzione(misc_routes.get_history):
        referenziate |= _colonne_referenziate(sql)
    mancanti = sorted(c for c in referenziate
                      if c not in creabili_da_alter and c not in _COLONNE_STRUTTURALI)
    assert not mancanti, (
        "Colonne lette da /api/history ma assenti da _COLUMNS_DDL: %s" % mancanti
    )


def test_analytics_usa_solo_colonne_che_lo_schema_sa_creare():
    creabili_da_alter = set(_colonne_degli_alter())
    referenziate = set()
    for sql in _sql_della_funzione(analytics_routes.get_analytics):
        referenziate |= _colonne_referenziate(sql)
    mancanti = sorted(c for c in referenziate
                      if c not in creabili_da_alter and c not in _COLONNE_STRUTTURALI)
    assert not mancanti, (
        "Colonne lette da /api/analytics ma assenti da _COLUMNS_DDL: %s" % mancanti
    )


def test_insert_colonne_e_placeholder_coincidono():
    """Disallineamento classico quando si aggiunge una colonna e si dimentica
    un %s (o viceversa): psycopg2 fallirebbe a runtime, e l'errore finirebbe
    nello stesso except che ha nascosto il bug di log_filename."""
    colonne, placeholder = _insert_di_save_to_db_log()
    assert len(colonne) == placeholder, (
        "L'INSERT dichiara %d colonne ma ha %d placeholder %%s"
        % (len(colonne), placeholder)
    )


def test_insert_placeholder_e_parametri_coincidono():
    """Terzo lato dello stesso triangolo: i valori passati a cur.execute()
    devono essere tanti quanti i placeholder. Contati sull'AST, cosi'
    un'espressione composta come `(a or 0) + (b or 0)` conta per uno solo."""
    _, placeholder = _insert_di_save_to_db_log()
    albero = ast.parse(io.open(logging_service.__file__.replace(".pyc", ".py"),
                               encoding="utf-8").read())
    funzione = next(n for n in ast.walk(albero)
                    if isinstance(n, ast.FunctionDef) and n.name == "save_to_db_log")
    tuple_passate = [n.args[1] for n in ast.walk(funzione)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"
                     and len(n.args) == 2 and isinstance(n.args[1], ast.Tuple)]
    assert len(tuple_passate) == 1, "atteso un solo cur.execute con parametri"
    assert len(tuple_passate[0].elts) == placeholder, (
        "L'INSERT ha %d placeholder ma vengono passati %d valori"
        % (placeholder, len(tuple_passate[0].elts))
    )
