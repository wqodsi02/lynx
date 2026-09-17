"""
Persistenza dei modelli LLM configurati dall'utente (provider, nome, api key).
Le API key sono salvate cifrate su disco e decifrate solo in memoria quando
servono per una chiamata HTTP verso il provider.
"""
import json
import time
from datetime import datetime
from .config import MODELS_FILE
from .crypto_utils import encrypt, decrypt


def _read_raw():
    if not MODELS_FILE.exists():
        return []
    with open(MODELS_FILE, encoding="utf-8") as f:
        return json.load(f).get("models", [])


def _write_raw(models):
    with open(MODELS_FILE, "w", encoding="utf-8") as f:
        json.dump({"models": models}, f, indent=2)


def list_models(reveal_key: bool = False):
    """Ritorna i modelli. Per default la api_key NON viene esposta al frontend."""
    out = []
    for m in _read_raw():
        item = dict(m)
        if reveal_key:
            item["api_key"] = decrypt(item.get("api_key", ""))
        else:
            item.pop("api_key", None)
            item["has_key"] = bool(m.get("api_key"))
        out.append(item)
    return out


def get_model(model_id: str):
    """Ritorna il modello con api_key in chiaro (per uso interno, chiamate LLM)."""
    for m in _read_raw():
        if m["id"] == model_id:
            item = dict(m)
            item["api_key"] = decrypt(item.get("api_key", ""))
            return item
    return None


def add_model(name, provider, model_string, api_key):
    models = _read_raw()
    m = {
        "id": str(int(time.time() * 1000)),
        "name": name,
        "provider": provider.lower(),
        "model_string": model_string,
        "api_key": encrypt(api_key),
        "created_at": datetime.now().isoformat(),
    }
    models.append(m)
    _write_raw(models)
    out = dict(m)
    out.pop("api_key", None)
    return out


def update_model(model_id, name=None, provider=None, model_string=None, api_key=None):
    models = _read_raw()
    found = False
    for m in models:
        if m["id"] == model_id:
            found = True
            if name is not None:
                m["name"] = name
            if provider is not None:
                m["provider"] = provider.lower()
            if model_string is not None:
                m["model_string"] = model_string
            if api_key:
                m["api_key"] = encrypt(api_key)
    if found:
        _write_raw(models)
    return found


def delete_model(model_id):
    models = [m for m in _read_raw() if m["id"] != model_id]
    _write_raw(models)
