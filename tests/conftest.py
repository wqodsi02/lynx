# -*- coding: utf-8 -*-
"""
Fixture condivise della suite.

Tutti i test di questa suite sono DETERMINISTICI: non aprono connessioni al
database, non contattano alcun provider LLM e non dipendono dalla rete. Se un
test qui dentro dovesse richiedere l'uno o l'altro, e' nel posto sbagliato.

Nota sugli effetti collaterali all'import: importare `backend.config` (cosa che
avviene indirettamente da quasi tutti i moduli) crea `logs/`, `logs/benchmark/`
e `data/` se non esistono. E' innocuo sul progetto reale, dove esistono gia',
ma va tenuto presente su una macchina pulita o in CI.
"""
import pytest

from backend import persistence_state
from backend.services import conversation_service as conv


@pytest.fixture(autouse=True)
def reset_conversation_state():
    """Azzera la memoria conversazionale prima e dopo ogni test.

    `conversation_service._sessions` e' un dizionario a livello di modulo,
    condiviso da tutto il processo. Senza questo azzeramento l'esito di un
    test dipenderebbe dall'ordine di esecuzione degli altri: una sessione
    lasciata sporca verrebbe ereditata dal test successivo, che passerebbe o
    fallirebbe a seconda di chi ha girato prima.
    """
    conv._sessions.clear()
    yield
    conv._sessions.clear()


@pytest.fixture(autouse=True)
def reset_persistence_state():
    """Azzera lo stato di persistenza prima e dopo ogni test.

    Stessa ragione della fixture qui sopra: `persistence_state` tiene contatori
    a livello di modulo, e senza azzeramento un test che registra un fallimento
    lascerebbe il contatore sporco per quelli successivi.
    """
    persistence_state.azzera()
    yield
    persistence_state.azzera()
