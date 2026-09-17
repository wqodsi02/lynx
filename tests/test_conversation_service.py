# -*- coding: utf-8 -*-
"""
Gruppo 5 - services.conversation_service

E' il gruppo piu' delicato della suite perche' e' l'unico che lavora su STATO
GLOBALE: `_sessions` e' un dizionario a livello di modulo, condiviso da tutto
il processo. La fixture autouse in conftest.py lo azzera prima e dopo ogni
test; senza, l'esito dipenderebbe da chi ha girato prima.

Per la stessa ragione i test qui dentro non danno mai per scontato lo stato
iniziale: ognuno crea le proprie sessioni con un `session_id` proprio.

Nessuna connessione, nessuna rete: la memoria conversazionale e' interamente
in-process (scelta deliberata, l'app gira su una sola macchina).
"""
import time

import pytest

from backend.services import conversation_service as conv


def _invecchia(session_id, secondi_oltre_ttl=1):
    """Fa scadere una sessione manipolando il suo `last_access`.

    Preferito a un `sleep` o a un TTL di 0: e' deterministico e istantaneo, e
    non dipende dalla risoluzione dell'orologio.
    """
    ttl_s = conv.settings.CONVERSATION_TTL_MINUTES * 60
    conv._sessions[session_id]["last_access"] = time.time() - ttl_s - secondi_oltre_ttl


# ---------------------------------------------------------------------------
# TTL (3)
# ---------------------------------------------------------------------------
def test_sessione_non_scaduta_mantiene_i_turni():
    conv.append_turn("s1", "Quante catture?", "SELECT 1", "Sono 206.")
    assert conv.turn_count("s1") == 1


def test_sessione_scaduta_viene_ricreata_vuota():
    """Alla lettura successiva una sessione oltre TTL viene scartata e
    ricreata vuota (scarto pigro, non c'e' un job di pulizia)."""
    conv.append_turn("s1", "Quante catture?", "SELECT 1", "Sono 206.")
    _invecchia("s1")
    assert conv.turn_count("s1") == 0


def test_cleanup_expired_rimuove_solo_le_scadute():
    conv.append_turn("viva", "domanda", "SELECT 1", "risposta")
    conv.append_turn("morta", "domanda", "SELECT 1", "risposta")
    _invecchia("morta")

    rimosse = conv.cleanup_expired()

    assert rimosse == 1
    assert "morta" not in conv._sessions
    assert "viva" in conv._sessions


# ---------------------------------------------------------------------------
# Troncamento della history iniettata nel prompt (4)
# ---------------------------------------------------------------------------
def test_sql_troncata_a_500_caratteri_nella_history():
    sql_lunga = "S" * 600
    conv.append_turn("s1", "domanda", sql_lunga, "risposta breve")

    contenuto = conv.get_history_as_messages("s1")[1]["content"]

    assert "S" * 500 + "… [troncato]" in contenuto
    assert "S" * 501 not in contenuto


def test_risposta_nl_troncata_a_400_caratteri_nella_history():
    nl_lunga = "R" * 500
    conv.append_turn("s1", "domanda", "SELECT 1", nl_lunga)

    contenuto = conv.get_history_as_messages("s1")[1]["content"]

    assert "R" * 400 + "… [troncato]" in contenuto
    assert "R" * 401 not in contenuto


def test_testi_sotto_soglia_non_vengono_marcati():
    conv.append_turn("s1", "domanda", "SELECT 1", "Sono 206 catture.")

    contenuto = conv.get_history_as_messages("s1")[1]["content"]

    assert "[troncato]" not in contenuto
    assert "SELECT 1" in contenuto
    assert "Sono 206 catture." in contenuto


def test_il_turno_memorizzato_conserva_il_testo_integrale():
    """L'invariante su cui si regge tutta l'ottimizzazione dei token: si tronca
    SOLO la copia che finisce nel prompt. Il contenuto integrale resta nel
    turno, perche' serve alla UI e ai log. Se questa distinzione si perdesse,
    riaprire una conversazione mostrerebbe risposte mutilate.
    """
    sql_lunga = "S" * 600
    nl_lunga = "R" * 500
    conv.append_turn("s1", "domanda", sql_lunga, nl_lunga)

    turno = conv._sessions["s1"]["turns"][0]

    assert turno["sql"] == sql_lunga
    assert turno["nl_response"] == nl_lunga


# ---------------------------------------------------------------------------
# Formato dei messaggi passati al modello (2)
# ---------------------------------------------------------------------------
def test_n_turni_producono_2n_messaggi_alternati():
    conv.append_turn("s1", "prima domanda", "SELECT 1", "prima risposta")
    conv.append_turn("s1", "seconda domanda", "SELECT 2", "seconda risposta")

    messaggi = conv.get_history_as_messages("s1")

    assert len(messaggi) == 4
    assert [m["role"] for m in messaggi] == ["user", "assistant", "user", "assistant"]
    assert messaggi[0]["content"] == "prima domanda"
    assert messaggi[2]["content"] == "seconda domanda"


def test_formato_del_messaggio_assistant():
    """E' letteralmente cio' che il modello legge come contesto del turno
    precedente: la forma va fissata, non lasciata all'improvvisazione."""
    conv.append_turn("s1", "domanda", "SELECT 1", "Sono 206 catture.")

    assert conv.get_history_as_messages("s1")[1]["content"] == (
        "SQL generata: SELECT 1\nRisposta: Sono 206 catture."
    )


# ---------------------------------------------------------------------------
# Allineamento degli indici quando un turno fallisce (3)
# ---------------------------------------------------------------------------
def test_turno_in_errore_incrementa_comunque_il_contatore():
    """Un turno fallito viene registrato lo stesso: e' cio' che tiene allineati
    gli indici fra la UI e la memoria server-side."""
    conv.append_turn("s1", "domanda", "SELECT sbagliata",
                     "[errore DB: la colonna l.line non esiste]")
    assert conv.turn_count("s1") == 1


def test_indici_allineati_dopo_un_turno_fallito():
    conv.append_turn("s1", "prima domanda", "SELECT bad", "[errore DB: boom]")
    conv.append_turn("s1", "seconda domanda", "SELECT 1", "Sono 206.")

    turni = conv._sessions["s1"]["turns"]

    assert conv.turn_count("s1") == 2
    assert turni[0]["question"] == "prima domanda"
    assert turni[1]["question"] == "seconda domanda"


def test_marcatore_di_errore_visibile_nella_history():
    """Il modello deve sapere che il tentativo precedente e' fallito: e' il
    contesto che gli permette di non ripetere lo stesso errore."""
    conv.append_turn("s1", "domanda", "SELECT bad",
                     "[errore DB: la colonna l.line non esiste]")

    contenuto = conv.get_history_as_messages("s1")[1]["content"]

    assert "[errore DB:" in contenuto


# ---------------------------------------------------------------------------
# truncate_after, usato da "modifica domanda" (3)
# ---------------------------------------------------------------------------
def test_truncate_after_lascia_esattamente_k_turni():
    for i in range(3):
        conv.append_turn("s1", "domanda %d" % i, "SELECT %d" % i, "risposta %d" % i)

    conv.truncate_after("s1", 2)

    turni = conv._sessions["s1"]["turns"]
    assert len(turni) == 2
    assert [t["question"] for t in turni] == ["domanda 0", "domanda 1"]


def test_truncate_after_zero_svuota_la_memoria():
    conv.append_turn("s1", "domanda", "SELECT 1", "risposta")
    conv.truncate_after("s1", 0)
    assert conv.turn_count("s1") == 0


def test_truncate_after_oltre_la_lunghezza_non_solleva():
    """Indice fuori range: lo slicing Python non protesta, e va bene cosi'
    perche' l'endpoint edit-turn riceve l'indice dal client."""
    conv.append_turn("s1", "prima", "SELECT 1", "risposta")
    conv.append_turn("s1", "seconda", "SELECT 2", "risposta")

    conv.truncate_after("s1", 99)

    assert conv.turn_count("s1") == 2


# ---------------------------------------------------------------------------
# Cap dei turni e ricostruzione da query_log (1)
# ---------------------------------------------------------------------------
def test_cap_dei_turni_conserva_gli_ultimi_sia_live_sia_in_rebuild(monkeypatch):
    """Superato CONVERSATION_MAX_TURNS si tengono gli ULTIMI N, non i primi.
    `rebuild_session` (usata quando si riapre una chat dalla sidebar) deve
    applicare lo stesso identico cap, altrimenti una conversazione riaperta
    avrebbe piu' contesto di una viva.
    """
    monkeypatch.setattr(conv.settings, "CONVERSATION_MAX_TURNS", 3)

    for i in range(5):
        conv.append_turn("live", "domanda %d" % i, "SELECT %d" % i, "risposta %d" % i)

    turni_live = conv._sessions["live"]["turns"]
    assert len(turni_live) == 3
    assert [t["question"] for t in turni_live] == ["domanda 2", "domanda 3", "domanda 4"]

    conv.rebuild_session("riaperta", [
        {"question": "domanda %d" % i, "sql": "SELECT %d" % i, "nl_response": "risposta %d" % i}
        for i in range(5)
    ])

    turni_rebuild = conv._sessions["riaperta"]["turns"]
    assert len(turni_rebuild) == 3
    assert [t["question"] for t in turni_rebuild] == ["domanda 2", "domanda 3", "domanda 4"]
