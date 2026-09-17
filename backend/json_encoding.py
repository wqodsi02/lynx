# -*- coding: utf-8 -*-
"""
Convenzione UNICA di serializzazione JSON per i tipi restituiti da psycopg2.

Estratta da `LynxJSONProvider` (app.py) perché serve in due punti che non
possono importarsi a vicenda: il provider JSON di Flask, che confeziona la
risposta HTTP, e `readonly_guard`, che misura i byte del payload dei risultati.
Far importare `app.py` da `readonly_guard` creerebbe una dipendenza circolare
(app -> api -> services -> db -> app), quindi la logica sta qui, in un modulo
che dipende solo dalla libreria standard.

Perché non basta `default=str`: la differenza non è cosmetica. Con `default=str`
ogni Decimal diventa una stringa quotata (`"16.69"` invece di `16.69`), due byte
in più per valore. Misurato su righe reali del progetto: +1,67% su una riga a
testo dominante, +3,96% su un aggregato per impianto, +5,80% su una riga di
statistiche IAT, +8,00% su un conteggio scalare, e +5,43% (1200 byte) su un
risultato di 200 righe di soli aggregati. Su una misura di transfer rate un
errore sistematico di quell'ordine non è accettabile, ed è il motivo per cui
questo modulo esiste invece di una seconda convenzione "quasi uguale".
"""
import uuid
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal


def default(o):
    """Forma serializzabile dei tipi che psycopg2 restituisce.

    Solleva TypeError sui tipi che non conosce: chi la usa come `default=` di
    json.dumps ottiene l'errore standard, e `LynxJSONProvider` la incatena al
    provider di serie di Flask per i tipi che restano (dataclass, __html__).
    """
    if isinstance(o, Decimal):
        # int se il valore è intero, altrimenti float: evita "12.0" dove
        # il dato è concettualmente un conteggio.
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (datetime, date, dtime)):
        return o.isoformat()
    if isinstance(o, timedelta):
        return str(o)
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, (bytes, memoryview)):
        return "<binary>"
    raise TypeError("Object of type %s is not JSON serializable" % type(o).__name__)
