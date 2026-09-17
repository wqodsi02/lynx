# -*- coding: utf-8 -*-
"""
Misurazione dei byte trasferiti (transfer rate).

Tre quantità distinte, che questo modulo tiene deliberatamente separate:

  - **payload applicativo**: il JSON spedito o ricevuto una volta decompresso.
    Misurabile con precisione.
  - **corpo sul filo**: il corpo HTTP come ha attraversato la rete, quindi
    compresso. Misurabile solo quando il provider manda `Content-Length`;
    altrimenti None, mai zero.
  - **traffico reale** (header + TLS + TCP): NON misurabile dall'interno di
    `requests`. Non viene stimato qui: servirebbero strumenti esterni
    (contatori socket, un proxy, tcpdump), e una somma di header spacciata per
    traffico sarebbe un numero plausibile e falso.

Convenzione sui None: None significa "non misurato o non determinabile", zero
significa "misurato, era zero". Confonderli falserebbe in silenzio le medie di
Analytics.
"""
import json

from . import json_encoding


def somma_byte(*valori):
    """Somma contributi di byte trattando None come neutro.

    Serve ad aggregare Fase 1, Fase 2 ed eventuale auto-riparazione in un solo
    totale. Un contributo non misurato non deve azzerare il totale né renderlo
    None; se però NIENTE è stato misurato il totale è None, non zero, perché
    zero affermerebbe una misura che non è stata fatta.
    """
    misurati = [v for v in valori if v is not None]
    if not misurati:
        return None
    return sum(misurati)


def byte_da_risultato(risultato):
    """I tre valori di byte di un LLMResult, con le chiavi usate lungo tutta la
    catena (rotte, log, colonne di query_log): un nome solo dal provider al
    database riduce le occasioni di perdere un campo per strada."""
    return {
        "llm_bytes_request": risultato.bytes_request,
        "llm_bytes_response": risultato.bytes_response,
        "llm_bytes_response_wire": risultato.bytes_response_wire,
    }


def byte_payload_json(oggetto):
    """Byte del payload JSON di `oggetto`, con la stessa convenzione di
    serializzazione usata per la risposta HTTP (vedi `json_encoding`).

    È un CALCOLO sul payload applicativo, non una misura del traffico di rete:
    psycopg2 non espone contatori di byte ricevuti (né il cursore né
    ConnectionInfo), e nemmeno libpq. Il nome della colonna che lo ospita —
    `db_bytes_result_payload` — porta la stessa avvertenza.

    NON METTERE A RAPPORTO con `llm_bytes_request`: misurano popolazioni
    diverse. Il database può restituire fino a MAX_ROWS_HARD righe (5000), ma
    alla Fase 2 ne vengono inviate al modello solo MAX_ROWS_TO_LLM (50). Un
    rapporto fra i due numeri non descrive alcuna grandezza reale.
    """
    return len(json.dumps(oggetto, ensure_ascii=False,
                          default=json_encoding.default).encode("utf-8"))
