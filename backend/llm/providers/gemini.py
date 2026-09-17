"""
Adapter per Google Gemini (generateContent REST API).
La API key viene passata via header (x-goog-api-key) invece che nella query
string dell'URL: se una chiamata fallisce, l'eccezione di rete/HTTP spesso
include l'URL richiesto nel messaggio di errore, e quell'errore può finire
salvato nei file di log o mostrato in UI — con la key nella query string
finirebbe esposta lì. Con l'header questo rischio non c'è.
"""
import time
import requests
from ..base import LLMResult, LLMProviderError, byte_richiesta, byte_wire

URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_MAX_TENTATIVI = 3  # 1 iniziale + 2 retry su 429


def call(model: str, api_key: str, messages: list, timeout: int = 60) -> LLMResult:
    # t0 fuori dal ciclo, come in groq.py: la latenza deve comprendere i
    # tentativi falliti e le attese di backoff.
    t0 = time.time()
    url = URL_TMPL.format(model=model)
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    gmsgs, sys_txt = [], None
    for m in messages:
        if m["role"] == "system":
            sys_txt = m["content"]
        else:
            gmsgs.append({"role": "user" if m["role"] == "user" else "model",
                          "parts": [{"text": m["content"]}]})
    payload = {"contents": gmsgs}
    if sys_txt:
        payload["systemInstruction"] = {"parts": [{"text": sys_txt}]}

    b_req = b_resp = 0
    b_wire = None
    tentativi_falliti = 0

    for tentativo in range(_MAX_TENTATIVI):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.exceptions.Timeout:
            raise TimeoutError(f"gemini: nessuna risposta entro {timeout}s")
        except requests.exceptions.ConnectionError:
            raise ConnectionError("gemini: impossibile contattare l'API (rete/DNS)")

        b_req += byte_richiesta(r)
        b_resp += len(r.content or b"")
        w = byte_wire(r)
        if w is not None:
            b_wire = (b_wire or 0) + w

        if r.status_code == 429 and tentativo < _MAX_TENTATIVI - 1:
            tentativi_falliti = tentativo + 1
            # Come per Groq: rispetta Retry-After se presente, ma con un tetto,
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
        if r.status_code in (401, 403):
            raise _errore(f"gemini: API key non valida o senza permessi. {detail}")
        if r.status_code == 404:
            raise _errore(f"gemini: modello '{model}' non trovato o non disponibile. {detail}")
        if r.status_code == 429:
            raise _errore(f"gemini: limite di richieste superato dopo {tentativi_falliti} tentativi. {detail}")
        raise _errore(f"gemini: errore HTTP {r.status_code}. {detail}")

    data = r.json()
    candidates = data.get("candidates")
    if not candidates:
        block_reason = data.get("promptFeedback", {}).get("blockReason")
        if block_reason:
            raise _errore(f"gemini ha bloccato la richiesta: {block_reason}")
        raise _errore(f"gemini: risposta vuota — {str(data)[:200]}")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = parts[0].get("text", "").strip() if parts else ""
    finish_reason = candidates[0].get("finishReason", "")
    if not text and finish_reason == "MAX_TOKENS":
        raise _errore(
            "gemini: risposta troncata per limite di token massimi raggiunto prima di produrre testo. "
            "Prova a semplificare la domanda o i risultati da interpretare."
        )
    usage = data.get("usageMetadata", {}) or {}
    return LLMResult(
        text=text,
        latency_ms=int((time.time() - t0) * 1000),
        tokens_prompt=usage.get("promptTokenCount", 0) or 0,
        tokens_completion=usage.get("candidatesTokenCount", 0) or 0,
        tokens_total=usage.get("totalTokenCount", 0) or 0,
        bytes_request=b_req,
        bytes_response=b_resp,
        bytes_response_wire=b_wire,
    )


def _extract_error_detail(response) -> str:
    try:
        body = response.json()
        msg = body.get("error", {}).get("message")
        return f"Dettaglio: {msg}" if msg else ""
    except Exception:
        return ""
