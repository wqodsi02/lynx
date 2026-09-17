# -*- coding: utf-8 -*-
"""
Gruppo 3 - nl2sql_service._strip_sql_from_nl_response

Rete di sicurezza della Fase 2. Il prompt di interpretazione vieta
esplicitamente di includere SQL nella risposta in linguaggio naturale (deve
solo descrivere a parole un eventuale approfondimento e chiedere conferma), ma
i modelli piu' "proattivi" lo fanno lo stesso. Questa funzione ripulisce il
testo a valle, cosi' il comportamento e' garantito anche quando il modello
ignora l'istruzione.

Il rischio principale non e' che rimuova troppo poco, ma che rimuova TROPPO,
mangiandosi pezzi di spiegazione legittima: per questo meta' dei test verifica
che il testo utile sopravviva intatto.

Funzione pura: nessuna connessione, nessuna rete.
"""
import pytest

from backend.services.nl2sql_service import _strip_sql_from_nl_response as strip_sql


# ---------------------------------------------------------------------------
# Rimozione (5)
# ---------------------------------------------------------------------------
def test_rimuove_blocco_fence():
    testo = "Ecco i risultati.\n\n```sql\nSELECT 1 FROM pcapinfo\n```\n\nFine."
    pulito = strip_sql(testo)
    assert "SELECT" not in pulito
    assert "```" not in pulito
    assert "Ecco i risultati." in pulito
    assert "Fine." in pulito


def test_rimuove_sql_in_prosa():
    """SQL sputata in mezzo al testo senza fence: e' il caso piu' frequente."""
    testo = "Il valore medio e' 4.99 Mbps. SELECT AVG(x) FROM pcapinfo. Fine."
    pulito = strip_sql(testo)
    assert "SELECT" not in pulito
    assert "Il valore medio e' 4.99 Mbps." in pulito
    assert "Fine." in pulito


def test_rimuove_cte_with():
    """Il secondo ramo della regex: una CTE `WITH x AS (...)` in prosa."""
    testo = ("Ti serve un approfondimento. "
             "WITH linee AS (SELECT id FROM linepn) SELECT * FROM linee. Fine.")
    pulito = strip_sql(testo)
    assert "SELECT" not in pulito
    assert "WITH linee" not in pulito
    assert "Ti serve un approfondimento." in pulito
    assert "Fine." in pulito


def test_leadin_rimosso_senza_mangiare_il_testo_che_lo_precede():
    """La frase di innesco va tolta dal punto di innesco a fine riga, NON
    l'intera riga: quello che la precede sulla stessa riga e' contenuto utile e
    deve sopravvivere. E' la regressione piu' insidiosa del gruppo, perche' un
    taglio troppo largo qui non fa fallire nulla, degrada solo la risposta.
    """
    testo = "I dati sono completi. Ecco la query di verifica che puoi lanciare\nAltra riga."
    pulito = strip_sql(testo)
    assert pulito.startswith("I dati sono completi.")
    assert "Ecco la query" not in pulito
    assert "Altra riga." in pulito


def test_leadin_piu_fence_insieme():
    """Combinazione realistica: frase introduttiva seguita dal blocco di codice.
    Devono sparire entrambi, e il risultato quantitativo deve restare."""
    testo = ("Risultato: 206 catture.\n\n"
             "Una possibile query di approfondimento:\n"
             "```sql\nSELECT 1 FROM pcapinfo\n```")
    pulito = strip_sql(testo)
    assert "SELECT" not in pulito
    assert "possibile query" not in pulito
    assert "206 catture" in pulito


# ---------------------------------------------------------------------------
# Non-rimozione: il testo utile deve sopravvivere (2)
# ---------------------------------------------------------------------------
def test_parola_select_senza_from_non_viene_toccata():
    """La regex richiede SELECT ... FROM proprio per non aggredire la prosa.
    Una frase che nomina «select» senza essere SQL resta intatta."""
    testo = "Il menu a tendina select non contiene tutti i modelli."
    assert strip_sql(testo) == testo


def test_frase_di_conferma_prevista_dal_prompt_sopravvive():
    """E' l'esempio di risposta corretta citato testualmente nel
    SYSTEM_PROMPT_INTERPRETATION: descrivere l'approfondimento a parole e
    chiedere conferma. Se la pulizia se lo mangiasse, la funzione starebbe
    combattendo il prompt invece di rinforzarlo.
    """
    testo = ("Non emergono anomalie a livello di sito: un problema su una singola linea "
             "puo' essere mascherato dalla media. Vuoi che analizzi il dato per singola linea?")
    assert strip_sql(testo) == testo


# ---------------------------------------------------------------------------
# Casi limite e idempotenza (2)
# ---------------------------------------------------------------------------
def test_input_vuoto_e_none_restituiti_invariati():
    """La guardia `if not text` deve precedere ogni regex: senza, un None
    farebbe esplodere la Fase 2 dopo che la query e' gia' stata eseguita."""
    assert strip_sql("") == ""
    assert strip_sql(None) is None


def test_idempotenza():
    """Applicare due volte la pulizia deve dare lo stesso risultato: se non
    fosse cosi', la funzione starebbe erodendo il testo a ogni passata."""
    testo = "Ecco i risultati.\n\n```sql\nSELECT 1 FROM pcapinfo\n```\n\nFine."
    una_volta = strip_sql(testo)
    assert strip_sql(una_volta) == una_volta
