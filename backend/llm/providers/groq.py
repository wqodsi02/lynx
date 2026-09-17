"""
Adapter per Groq (API compatibile OpenAI chat-completions).
Gestisce: retry su 429 con backoff esponenziale, modelli "reasoning"
(qwen3, deepseek, gpt-oss) che vanno con reasoning_format=hidden per non
restituire i tag <think>...</think> nell'output.
"""
import time
import requests
from ..base import LLMResult, LLMProviderError, byte_richiesta, byte_wire

URL = "https://api.groq.com/openai/v1/chat/completions"
_REASONING_HINTS = ("qwen3", "deepseek", "gpt-oss")
_MAX_TENTATIVI = 3  # 1 iniziale + 2 retry su 429


def call(model: str, api_key: str, messages: list, timeout: int = 60) -> LLMResult:
    # t0 STA FUORI DAL CICLO. Nella versione ricorsiva precedente ogni tentativo
    # ripartiva con un t0 proprio, quindi la latenza riportata escludeva sia i
    # tentativi falliti sia le attese di backoff: su una chiamata ritentata si
    # riportavano 250 ms al posto di 2750 ms reali.
    t0 = time.time()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": 0}
    if any(h in model.lower() for h in _REASONING_HINTS):
        payload["reasoning_format"] = "hidden"

    # I byte si accumulano su TUTTI i tentativi: un 429 ritentato ha consumato
    # traffico anche nel tentativo fallito.
    b_req = b_resp = 0
    b_wire = None
    tentativi_falliti = 0

    for tentativo in range(_MAX_TENTATIVI):
        try:
            r = requests.post(URL, headers=headers, json=payload, timeout=timeout)
        except requests.exceptions.Timeout:
            raise TimeoutError(f"groq: nessuna risposta entro {timeout}s")
        except requests.exceptions.ConnectionError:
            raise ConnectionError("groq: impossibile contattare l'API (rete/DNS)")

        b_req += byte_richiesta(r)
        b_resp += len(r.content or b"")
        w = byte_wire(r)
        if w is not None:
            b_wire = (b_wire or 0) + w

        if r.status_code == 429 and tentativo < _MAX_TENTATIVI - 1:
            tentativi_falliti = tentativo + 1
            # Backoff contenuto: rispetta Retry-After se presente ma con un tetto,
            # per non trasformare un rate-limit temporaneo in un'attesa di minuti.
            retry_after = r.headers.get("Retry-After")
            wait = min(int(retry_after), 8) if (retry_after and retry_after.isdigit()) else (2 * (tentativo + 1))
            time.sleep(wait)
            continue
        break

    def _errore(messaggio):
        return LLMProviderError(messaggio, bytes_request=b_req,
                                bytes_response=b_resp, bytes_response_wire=b_wire)

    if not r.ok:
        detail = _extract_error_detail(r)
        if r.status_code == 401:
            raise _errore(f"groq: API key non valida o scaduta. {detail}")
        if r.status_code == 404:
            raise _errore(f"groq: modello '{model}' non trovato o non disponibile. {detail}")
        if r.status_code == 429:
            raise _errore(f"groq: limite di richieste superato dopo {tentativi_falliti} tentativi. {detail}")
        raise _errore(f"groq: errore HTTP {r.status_code}. {detail}")

    data = r.json()
    if not data.get("choices"):
        raise _errore(f"groq: risposta inattesa senza 'choices' — {str(data)[:200]}")

    # I modelli reasoning (qwen3, deepseek...) possono restituire content=None
    # se esauriscono il budget di token nella fase di "pensiero" prima di
    # produrre la risposta: senza questo guard il .strip() solleverebbe
    # AttributeError. La stringa vuota viene poi gestita dal retry a livello
    # di servizio (nl2sql_service.generate_sql).
    text = (data["choices"][0]["message"].get("content") or "").strip()
    usage = data.get("usage", {}) or {}
    return LLMResult(
        text=text,
        latency_ms=int((time.time() - t0) * 1000),
        tokens_prompt=usage.get("prompt_tokens", 0) or 0,
        tokens_completion=usage.get("completion_tokens", 0) or 0,
        tokens_total=usage.get("total_tokens", 0) or 0,
        bytes_request=b_req,
        bytes_response=b_resp,
        bytes_response_wire=b_wire,
    )


def _extract_error_detail(response) -> str:
    try:
        body = response.json()
        msg = body.get("error", {}).get("message") or body.get("message")
        return f"Dettaglio: {msg}" if msg else ""
    except Exception:
        return ""
