"""
Interfaccia comune per i provider LLM e normalizzazione di usage (token) e
byte trasferiti. Ogni provider ritorna un LLMResult uniforme, indipendentemente
dal formato nativo della sua API.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class LLMResult:
    text: str
    latency_ms: int
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_total: int = 0
    # Byte del corpo della richiesta e della risposta, sommati su tutti i
    # tentativi (un 429 ritentato ha consumato traffico anche nel tentativo
    # fallito). `bytes_response` è il corpo DECOMPRESSO.
    bytes_request: int = 0
    bytes_response: int = 0
    # Corpo della risposta come ha attraversato la rete, cioè compresso.
    # requests manda `Accept-Encoding: gzip, deflate` di default, quindi è
    # tipicamente molto minore di bytes_response. None quando il provider non
    # manda Content-Length: non si ripiega su stime.
    bytes_response_wire: Optional[int] = None


class LLMProviderError(Exception):
    """Errore "atteso" del provider (key non valida, modello inesistente,
    rate limit esaurito...).

    Porta con sé i byte già spesi: una chiamata fallita ha comunque generato
    traffico, e attribuirle zero sottostimerebbe il costo reale dei modelli
    che sbagliano di più — cioè proprio quelli che il benchmark deve smascherare.
    """

    def __init__(self, message, bytes_request=0, bytes_response=0,
                 bytes_response_wire=None):
        super().__init__(message)
        self.bytes_request = bytes_request
        self.bytes_response = bytes_response
        self.bytes_response_wire = bytes_response_wire


def byte_richiesta(response) -> int:
    """Byte del corpo effettivamente spedito, letti dalla PreparedRequest.

    Si legge da lì e non ri-serializzando il payload: è ciò che requests ha
    davvero messo sul socket, comprese le eventuali differenze di encoding.
    """
    richiesta = getattr(response, "request", None)
    corpo = getattr(richiesta, "body", None) if richiesta is not None else None
    if corpo is None:
        return 0
    if isinstance(corpo, str):
        corpo = corpo.encode("utf-8")
    return len(corpo)


def byte_wire(response) -> Optional[int]:
    """Byte del corpo sul filo, cioè compresso. None se non determinabile.

    Unica fonte ammessa: l'header Content-Length. Con
    `Transfer-Encoding: chunked` non c'è, e in quel caso si restituisce None:
    `response.raw.tell()` sarebbe un ripiego plausibile ma non è stato validato
    contro una risposta reale, e un numero di cui non ci si fida è peggio di
    un'assenza dichiarata.
    """
    valore = response.headers.get("Content-Length")
    if valore is None or not str(valore).isdigit():
        return None
    return int(valore)
