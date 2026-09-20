# -*- coding: utf-8 -*-
"""
Gruppo 11 - verifica dello schema dopo la migrazione.

E' il pezzo di maggior valore della correzione. Oggi un ALTER fallito non
produce alcun segnale: il guasto si manifesta molto dopo, all'INSERT, con un
errore che sembra non correlato — ed e' esattamente cosi' che `log_filename`
e' rimasta assente per tre mesi.

La verifica converte quel ritardo in un fallimento immediato e parlante: dopo
il ciclo di ALTER si interroga `information_schema.columns` e si confrontano le
colonne reali con l'unione di CREATE TABLE e _COLUMNS_DDL. Se ne manca una, si
solleva UN SOLO errore che le nomina tutte.

Cio' che NON cambia, perche' oggi protegge correttamente:
  - il try/except attorno a ogni singolo ALTER (un fallimento non deve fermare
    gli altri tredici);
  - il conn.rollback() dentro quell'except (senza, la transazione resta
    abortita e tutti gli statement successivi falliscono a cascata);
  - `_schema_ready` non impostato in caso di errore, cosi' la migrazione viene
    ritentata a ogni richiesta: e' l'auto-recupero che ha fatto passare da solo
    l'ALTER di log_filename quando la VPN e' tornata.

Nessuna connessione: si usa un information_schema finto.
"""
import logging

import psycopg2.errors as PGE
import pytest

from backend import persistence_state as stato
from backend.db import query_log_schema as schema


class _CursoreFinto:
    """Cursore che conosce le colonne "realmente presenti" nel database finto.

    Un ALTER su una colonna elencata in `alter_falliti` solleva; gli altri
    aggiungono la colonna, come farebbe ADD COLUMN IF NOT EXISTS.
    """

    def __init__(self, colonne_reali, alter_falliti=()):
        self.colonne_reali = list(colonne_reali)
        self.alter_falliti = set(alter_falliti)
        self.eseguite = []
        self._risultato = []

    def execute(self, sql, params=None):
        self.eseguite.append(sql)
        if "information_schema.columns" in sql:
            self._risultato = [(c,) for c in self.colonne_reali]
            return
        if sql.strip().upper().startswith("ALTER TABLE"):
            colonna = sql.split("IF NOT EXISTS")[1].split()[0]
            if colonna in self.alter_falliti:
                raise PGE.InsufficientPrivilege(
                    "permesso negato per la tabella query_log")
            if colonna not in self.colonne_reali:
                self.colonne_reali.append(colonna)

    def fetchall(self):
        return self._risultato

    def close(self):
        pass


class _ConnessioneFinta:
    def __init__(self, cursore):
        self._cursore = cursore
        self.rollback_chiamati = 0
        self.commit_chiamati = 0

    def cursor(self, **k):
        return self._cursore

    def commit(self):
        self.commit_chiamati += 1

    def rollback(self):
        self.rollback_chiamati += 1

    def close(self):
        pass


def _colonne_complete():
    """Tutto cio' che il codice si aspetta: CREATE TABLE piu' _COLUMNS_DDL."""
    return sorted(schema.colonne_richieste())


# ---------------------------------------------------------------------------
# verify_schema (3)
# ---------------------------------------------------------------------------
def test_schema_completo_non_ha_colonne_mancanti():
    conn = _ConnessioneFinta(_CursoreFinto(_colonne_complete()))

    assert schema.verify_schema(conn) == []


def test_schema_incompleto_nomina_tutte_le_colonne_mancanti():
    """Tutte, non la prima: con una sola alla volta si scoprirebbe il problema
    a rate, un riavvio per colonna."""
    reali = [c for c in _colonne_complete()
             if c not in ("log_filename", "db_bytes_query")]
    conn = _ConnessioneFinta(_CursoreFinto(reali))

    mancanti = schema.verify_schema(conn)

    assert mancanti == ["db_bytes_query", "log_filename"]


def test_colonne_richieste_e_l_unione_di_create_table_e_alter():
    """L'insieme atteso non e' una terza lista da tenere allineata a mano: si
    ricava dalle due che gia' esistono."""
    richieste = schema.colonne_richieste()

    assert "id" in richieste, "presente solo nel CREATE TABLE"
    assert "llm_bytes_request" in richieste, "presente in _COLUMNS_DDL"
    assert {nome for nome, _tipo in schema._COLUMNS_DDL} <= richieste


# ---------------------------------------------------------------------------
# _ensure_log_table (4)
# ---------------------------------------------------------------------------
def test_migrazione_riuscita_non_solleva():
    conn = _ConnessioneFinta(_CursoreFinto(_colonne_complete()))

    schema._ensure_log_table(conn)  # non deve sollevare


def test_migrazione_incompleta_solleva_un_solo_errore_parlante():
    """Il valore della tappa: l'errore arriva ORA e dice quali colonne mancano,
    invece di arrivare al primo INSERT sotto forma di
    'column "log_filename" does not exist'."""
    reali = [c for c in _colonne_complete() if c != "log_filename"]
    cur = _CursoreFinto(reali, alter_falliti={"log_filename"})
    conn = _ConnessioneFinta(cur)

    with pytest.raises(schema.SchemaIncompleteError) as info:
        schema._ensure_log_table(conn)

    assert "log_filename" in str(info.value)


def test_alter_fallito_viene_loggato_con_colonna_e_classe(caplog):
    """Oggi il segnale e' zero: la colonna non viene aggiunta e non lo sa
    nessuno. Il guasto si scopre molto dopo e sembra non correlato."""
    reali = [c for c in _colonne_complete() if c != "log_filename"]
    cur = _CursoreFinto(reali, alter_falliti={"log_filename"})
    conn = _ConnessioneFinta(cur)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(schema.SchemaIncompleteError):
            schema._ensure_log_table(conn)

    messaggi = " ".join(r.getMessage() for r in caplog.records)
    assert "log_filename" in messaggi
    assert "InsufficientPrivilege" in messaggi


def test_alter_fallito_fa_comunque_rollback():
    """Protezione da non toccare: senza il rollback la transazione resta
    abortita e tutti gli statement successivi falliscono a cascata."""
    reali = [c for c in _colonne_complete() if c != "log_filename"]
    cur = _CursoreFinto(reali, alter_falliti={"log_filename"})
    conn = _ConnessioneFinta(cur)

    with pytest.raises(schema.SchemaIncompleteError):
        schema._ensure_log_table(conn)

    assert conn.rollback_chiamati >= 1


# ---------------------------------------------------------------------------
# ensure_schema_once e integrazione con lo stato (4)
# ---------------------------------------------------------------------------
def test_schema_rotto_finisce_nell_istantanea(monkeypatch):
    """L'indicatore di stato non deve dire solo "rotto", ma "rotto perche'
    mancano queste colonne"."""
    monkeypatch.setattr(schema, "_schema_ready", False)
    reali = [c for c in _colonne_complete() if c != "log_filename"]
    conn = _ConnessioneFinta(_CursoreFinto(reali, alter_falliti={"log_filename"}))

    with pytest.raises(schema.SchemaIncompleteError):
        schema.ensure_schema_once(conn)

    s = stato.istantanea()
    assert s["schema_ok"] is False
    assert s["colonne_mancanti"] == ["log_filename"]


def test_schema_rotto_non_marca_lo_schema_come_pronto(monkeypatch):
    """`_schema_ready` deve restare False: e' l'auto-recupero che ha fatto
    passare da solo l'ALTER di log_filename quando la VPN e' tornata, senza
    riavviare l'applicazione."""
    monkeypatch.setattr(schema, "_schema_ready", False)
    reali = [c for c in _colonne_complete() if c != "log_filename"]
    conn = _ConnessioneFinta(_CursoreFinto(reali, alter_falliti={"log_filename"}))

    with pytest.raises(schema.SchemaIncompleteError):
        schema.ensure_schema_once(conn)

    assert schema._schema_ready is False


def test_schema_sano_registra_lo_stato(monkeypatch):
    monkeypatch.setattr(schema, "_schema_ready", False)
    conn = _ConnessioneFinta(_CursoreFinto(_colonne_complete()))

    schema.ensure_schema_once(conn)

    s = stato.istantanea()
    assert s["schema_ok"] is True
    assert s["colonne_mancanti"] == []
    assert schema._schema_ready is True


def test_istantanea_ha_i_campi_schema_di_default():
    """Prima di qualunque verifica lo schema e' ignoto, non "sano": affermare
    che va bene senza aver guardato sarebbe la stessa bugia del NULL scambiato
    per zero."""
    s = stato.istantanea()

    assert s["schema_ok"] is None
    assert s["colonne_mancanti"] == []
