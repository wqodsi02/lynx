"""
Cifratura delle API key dei modelli su disco.
In locale la chiave viene generata automaticamente al primo avvio e salvata
in data/secret.key (escluso da eventuale versionamento). Non è un sostituto
di un vero secret manager, ma evita di avere le chiavi in chiaro su disco.
"""
from cryptography.fernet import Fernet
from .config import SECRET_KEY_FILE


def _load_or_create_key() -> bytes:
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    SECRET_KEY_FILE.write_bytes(key)
    try:
        SECRET_KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


_fernet = Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        # Token salvato prima dell'introduzione della cifratura: trattalo come plaintext.
        return token
