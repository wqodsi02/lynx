"""
Dispatcher unico per le chiamate ai provider LLM configurati (Groq, Gemini).
Punto unico da cui passano tutte le chiamate: qui si aggiungono nuovi
provider in futuro senza toccare i servizi a monte. Registra anche un log
di diagnostica sui tempi, utile per capire dove si perde il tempo (chiamata
API vera vs. attese di rate-limit).
"""
import time
import logging
from .base import LLMResult, LLMProviderError
from .providers import groq, gemini

logger = logging.getLogger("lynx.llm")

_DISPATCH = {
    "groq": groq.call,
    "gemini": gemini.call,
}


def call_llm(provider: str, model: str, api_key: str, messages: list, timeout: int = 60) -> LLMResult:
    fn = _DISPATCH.get(provider)
    if not fn:
        raise LLMProviderError(f"Provider non supportato: {provider}")
    t0 = time.time()
    result = fn(model, api_key, messages, timeout)
    elapsed = time.time() - t0
    # Segnala in console se una singola chiamata è stata anomalmente lenta:
    # aiuta a distinguere "il modello è lento" da "sto aspettando un retry".
    if elapsed > 8:
        logger.warning("Chiamata LLM lenta: provider=%s model=%s elapsed=%.1fs (possibile rate-limit/retry)",
                       provider, model, elapsed)
    else:
        logger.info("Chiamata LLM: provider=%s model=%s elapsed=%.2fs tokens=%s",
                    provider, model, elapsed, result.tokens_total)
    return result


def clean_sql(text: str) -> str:
    """Estrae la query SQL dalla risposta del modello, tollerando: blocchi di
    reasoning (<think>...</think>), fence markdown, e testo introduttivo/
    conclusivo che alcuni modelli aggiungono nonostante l'istruzione di
    rispondere solo con SQL (es. "Ecco la query:\\n\\nSELECT ...")."""
    import re
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # <think> aperto ma mai chiuso = risposta troncata a metà del reasoning:
    # tutto ciò che segue è "pensiero", non SQL. Restituire stringa vuota è il
    # segnale corretto per innescare il retry in generate_sql.
    if "<think>" in text.lower():
        return ""

    # Se c'è un fence markdown ```sql ... ``` o ``` ... ```, estrai solo il contenuto.
    fence_match = re.search(r"```(?:sql)?\s*\n?(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()

    # Nessun fence: se il testo non inizia già con una keyword SQL valida,
    # cerca dove inizia effettivamente la query (SELECT/WITH/EXPLAIN) e
    # scarta eventuale prosa introduttiva prima di essa.
    first_word_match = re.match(r"^\s*(\w+)", text)
    first_word = first_word_match.group(1).upper() if first_word_match else ""
    if first_word not in ("SELECT", "WITH", "EXPLAIN"):
        kw_match = re.search(r"\b(SELECT|WITH)\b", text, flags=re.IGNORECASE)
        if kw_match:
            text = text[kw_match.start():]

    return text.strip()
