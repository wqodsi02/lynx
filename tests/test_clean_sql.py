# -*- coding: utf-8 -*-
"""
Gruppo 2 - llm.client.clean_sql

Primo gradino della "scala di robustezza" contro l'output inaffidabile dei
modelli: estrae la query dalla risposta grezza, tollerando blocchi di
reasoning, fence markdown e prosa introduttiva. E' il punto in cui
l'indipendenza dal modello viene guadagnata o persa, perche' il benchmark
multi-modello non ha senso se i modelli piu' deboli falliscono per motivi
di formattazione invece che di competenza.

`clean_sql` e' una funzione pura: nessuna connessione, nessuna rete.
"""
import pytest

from backend.llm.client import clean_sql


# ---------------------------------------------------------------------------
# Blocchi di reasoning (4)
# ---------------------------------------------------------------------------
def test_blocco_reasoning_chiuso_viene_rimosso():
    """Un <think>...</think> completo va rimosso lasciando intatta la query."""
    grezzo = "<think>Devo contare le catture dell'impianto J.</think>\nSELECT COUNT(*) FROM pcapinfo"
    assert clean_sql(grezzo) == "SELECT COUNT(*) FROM pcapinfo"


def test_reasoning_troncato_restituisce_stringa_vuota():
    """Un <think> aperto e mai chiuso significa risposta troncata a meta' del
    ragionamento: tutto cio' che segue e' pensiero, non SQL.

    La stringa vuota NON e' un valore di ripiego, e' il segnale che innesca il
    retry in `nl2sql_service.generate_sql`. Se questo comportamento cambiasse,
    del testo di reasoning verrebbe passato a `validate_readonly_sql` come se
    fosse una query.
    """
    grezzo = "<think>Sto ragionando sulla struttura della query e la risposta si interrompe qui"
    assert clean_sql(grezzo) == ""


def test_reasoning_chiuso_seguito_da_fence():
    """I due meccanismi si compongono: prima si toglie il reasoning, poi si
    estrae il contenuto del fence."""
    grezzo = "<think>ok</think>\n```sql\nSELECT 1\n```"
    assert clean_sql(grezzo) == "SELECT 1"


def test_tag_think_maiuscolo_restituisce_stringa_vuota():
    """Asimmetria voluta o accidentale, ma comunque da fissare: la rimozione del
    blocco usa `re.sub` CASE-SENSITIVE, mentre il controllo successivo usa
    `"<think>" in text.lower()`. Un <THINK> maiuscolo non viene quindi rimosso,
    ma fa scattare il ritorno anticipato: la SQL che segue viene scartata e si
    paga un retry.

    Non e' un xfail perche' l'effetto e' nella direzione sicura (si perde una
    chiamata, non si esegue del testo arbitrario), ma il costo e' reale e va
    documentato.
    """
    grezzo = "<THINK>ragiono</THINK>\nSELECT 1"
    assert clean_sql(grezzo) == ""


# ---------------------------------------------------------------------------
# Fence markdown (3)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("grezzo, atteso", [
    ("```sql\nSELECT 1\n```", "SELECT 1"),
    ("```\nSELECT 1\n```", "SELECT 1"),
    ("Ecco la query:\n```sql\nSELECT 1\n```", "SELECT 1"),
], ids=["fence-sql", "fence-senza-linguaggio", "fence-preceduto-da-prosa"])
def test_fence_markdown(grezzo, atteso):
    """Il contenuto del fence vince su tutto il resto: la prosa attorno viene
    scartata senza doverla analizzare."""
    assert clean_sql(grezzo) == atteso


# ---------------------------------------------------------------------------
# Preamboli in prosa (3)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("grezzo, atteso", [
    ("Ecco la query:\n\nSELECT id_line FROM linepn",
     "SELECT id_line FROM linepn"),
    ("Certo, ecco:\n\nWITH x AS (SELECT 1) SELECT * FROM x",
     "WITH x AS (SELECT 1) SELECT * FROM x"),
    ("SELECT id_line FROM linepn",
     "SELECT id_line FROM linepn"),
], ids=["preambolo-prima-di-select", "preambolo-prima-di-with", "nessun-preambolo"])
def test_preambolo_in_prosa_viene_scartato(grezzo, atteso):
    """Senza fence, il taglio avviene alla prima keyword SELECT/WITH utile.
    Il terzo caso verifica il non-intervento: una risposta gia' pulita deve
    restare identica."""
    assert clean_sql(grezzo) == atteso


# ---------------------------------------------------------------------------
# Casi limite (2)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("grezzo, atteso", [
    ("", ""),
    ("Non sono in grado di rispondere a questa domanda.",
     "Non sono in grado di rispondere a questa domanda."),
], ids=["stringa-vuota", "nessuna-sql-testo-invariato"])
def test_casi_limite(grezzo, atteso):
    """Con input vuoto si esce con stringa vuota. Se invece non c'e' alcuna
    SELECT/WITH il testo viene restituito COM'E': tocchera' poi a
    `validate_readonly_sql` bloccarlo. E' comportamento attuale, fissato qui
    perche' un eventuale cambiamento sia una scelta e non una svista.
    """
    assert clean_sql(grezzo) == atteso
