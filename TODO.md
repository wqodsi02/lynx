# TODO — LYNX

## Bloccati

Nessuno al momento.

## Da fare

### Refactoring multi-provider

Oggi aggiungere un provider LLM richiede modifiche coordinate in **cinque
punti**, e dimenticarne uno lascia il sistema in uno stato a metà:

1. un nuovo modulo in `backend/llm/providers/`
2. `_DISPATCH` in `backend/llm/client.py`
3. la whitelist `("groq", "gemini")` in `backend/api/models_routes.py`
   (duplicata: sia nella POST sia nella PUT)
4. le `<option>` del `<select id="new-prov">` in `static/index.html`
5. `PROVIDER_LABELS` / `PROVIDER_COLORS` / `PROVIDER_HINTS` in
   `static/js/settings.js`

**Obiettivo.** Due interventi che si completano a vicenda:

- Un adattatore generico **`openai_compatible`**, parametrizzato per
  `base_url`. Un solo modulo coprirebbe Groq, DeepSeek, OpenRouter, Cerebras,
  xAI e Mistral, e soprattutto **vLLM in locale**, che è la strada per un
  eventuale deployment on-premise senza dipendere da API esterne.
- Un **registro dichiarativo dei provider**, letto sia dal backend sia dal
  frontend (esposto da un endpoint, così non esiste una seconda copia da
  tenere allineata a mano). Aggiungere un provider diventerebbe aggiungere
  una riga di configurazione invece di toccare cinque file in due linguaggi.

Gemini e in generale i provider la cui API non è compatibile con lo schema
chat-completions di OpenAI restano **adattatori dedicati**: forzarli nel
generico costerebbe più di quanto farebbe risparmiare.

## Risolti

### Misurazione dei transfer rate — risolto il 2026-09-10

Requisito esplicito del relatore (punto 4), mai strumentato prima. I byte
trasferiti sono ora misurati verso i provider LLM e verso il database,
propagati lungo la catena Fase 1 → Fase 2, persistiti su `query_log` e nei file
`.txt`, e riportati nei benchmark, in Analytics e nel frontend.

**Quattro tappe, quattro commit:**

- `0620636` — i test, scritti per primi e committati rossi
- `ffe7172` — misurazione nei provider e in `execute_readonly_query`
- `47d05d1` — propagazione lungo la catena Fase 1 → Fase 2
- `9450f04` — benchmark, Analytics e frontend

**Cosa è misurabile e cosa no.** È la parte che conta più dei numeri, perché
determina come vanno letti:

| Quantità | Stato |
|---|---|
| **Payload applicativo** — JSON spedito, JSON ricevuto decompresso | **Sì**, con precisione: `len(response.request.body)` e `len(response.content)`. |
| **Corpo sul filo** — il corpo HTTP come ha attraversato la rete, quindi compresso | **Misura abbandonata: in pratica non disponibile.** L'unica fonte ammessa era `Content-Length`, ma nessuno dei due provider lo manda (vedi sotto). `llm_bytes_response_wire` resta `NULL`. |
| **Traffico reale** — header HTTP, record TLS, framing TCP, ritrasmissioni | **No, e non stimato.** Non è osservabile dall'interno di `requests`. Servirebbero strumenti esterni (contatori socket, un proxy, `tcpdump`). Sommare gli header e chiamarlo traffico darebbe un numero plausibile e falso. |


**Il corpo sul filo non lo misuriamo, e non è un difetto del codice.** Verificato
il 2026-09-10 su tre chiamate reali: **né Groq né Gemini mandano
`Content-Length`**, quasi certamente perché rispondono con
`Transfer-Encoding: chunked`, che è la norma per le API LLM predisposte allo
streaming. `llm_bytes_response_wire` è quindi destinata a restare `NULL` con i
provider attuali.

Che l'assenza sia del provider e non nostra è stato verificato offline:
`requests`/urllib3 **conservano** l'header `Content-Length` anche dopo aver
decompresso il gzip (corpo compresso 34 byte, `len(response.content)` 220,
header ancora `34`). Se un provider lo mandasse, lo leggeremmo.

`response.raw.tell()` **non verrà implementato**: era l'unica strada rimasta per
un numero sul filo, ma non è validato contro una risposta reale e restituirebbe
un valore di cui non ci si può fidare. La colonna resta come documentazione del
tentativo e del suo esito — non è un buco da riempire.

**Lato database.** La richiesta è misurabile: `cursor.query` sono i byte
effettivamente spediti dopo il binding dei parametri. La risposta **no**:
psycopg2 non espone contatori di byte ricevuti, né sul cursore né su
`ConnectionInfo`, e nemmeno libpq lo fa. `db_bytes_result_payload` è quindi un
*calcolo* sul payload applicativo, non una misura di rete — e il nome della
colonna lo dichiara apertamente.

**Due avvertenze da non perdere di vista:**

- `db_bytes_result_payload` e `llm_bytes_request` **non vanno messi a rapporto**:
  misurano popolazioni diverse. Il DB restituisce fino a `MAX_ROWS_HARD` (5000)
  righe, al modello ne arrivano solo `MAX_ROWS_TO_LLM` (50).
- `NULL` significa "non misurato o non determinabile", zero significa "misurato,
  era zero". Tutte le righe scritte prima di questa misurazione hanno le colonne
  a `NULL`: per questo gli aggregati di Analytics usano `COALESCE` e il frontend
  mostra `—` invece di `0`.

Effetto collaterale utile: la riscrittura del retry da ricorsione a ciclo ha
portato alla luce le latenze storiche sottostimate, registrate fra i difetti
noti.

**Verifica end-to-end del 2026-09-10.** Una domanda in chat più un benchmark su
due modelli, dal browser:

- **tre righe su `query_log`** — una di chat (`is_benchmark=False`, Gpt Oss 120b)
  e due di benchmark (Gpt Oss 120b e Gemini 3.5 Flash Lite);
- **quattro colonne su cinque valorizzate** su tutte e tre; la quinta,
  `llm_bytes_response_wire`, è `NULL` per il motivo appena descritto;
- **`db_bytes_query` verificato contro le query reali**: 43, 44 e 29 byte,
  coincidenti carattere per carattere con la SQL registrata nei rispettivi log,
  punto e virgola finale compreso. Non è un numero prodotto altrove: è la query
  che è davvero partita;
- **aggregati di Analytics coerenti**: per Gpt Oss 120b media richiesta 6592 e
  risposta 1344 sulle due righe misurate (`AVG` ignora la terza, del 9 settembre,
  che è a `NULL`), somma 15871 = 7934 + 7937; per Gemini 8414, che coincide con
  la colonna `Byte LLM` del file di riepilogo del benchmark;
- **blocco `[ TRASFERIMENTI ]`** presente nei `.txt` di sessione e riga
  equivalente nei blocchi per modello del file di benchmark, con la colonna
  `Byte LLM` valorizzata per entrambi i modelli.

La verifica ha fatto emergere un difetto, corretto in `736588d`: per un modello
le cui righe sono **tutte** non misurate, `sum_llm_bytes_total` restituiva `0`
invece di `NULL` — la `COALESCE` proteggeva i modelli misti ma affermava una
misura mai fatta su quelli mai misurati.


### Scrittura su `query_log` — risolto il 2026-09-09

**Sintomo.** Tutti e 123 i file in `logs/` riportavano `Log ID : N/A`:
`save_to_db_log` restituiva `None` a ogni chiamata e nessuna riga veniva mai
scritta su `query_log`. `GET /api/history` rispondeva 500. Analytics
funzionava, perché la sua query non tocca la colonna incriminata.

**Causa.** La colonna `log_filename` era dichiarata **solo** dentro il
`CREATE TABLE IF NOT EXISTS` e non in `_COLUMNS_DDL`. Su un database dove
`query_log` esiste già — la tabella qui è stata creata il 15 giugno 2026 — il
`CREATE` è un no-op, e l'`ALTER` è l'unico meccanismo che può aggiungere una
colonna. Non esistendo un `ALTER` per `log_filename`, ogni `INSERT` falliva con
`column "log_filename" does not exist`.

**Correzione.** Più larga della singola colonna esplosa: altre sette
(`model`, `question`, `sql_generated`, `nl_response`, `rows_returned`,
`latency_ms`, `error`) erano nella stessa identica condizione e sopravvivevano
solo perché presenti fin dalla creazione originale della tabella. Aggiunte
tutte a `_COLUMNS_DDL`, più `timestamp`. Resta fuori solo `id`, che è
`SERIAL PRIMARY KEY` e non è aggiungibile via `ALTER` a una tabella che una
primary key ce l'ha già.

- `7c65834` — test di coerenza dello schema, committato **rosso**
- `63ac02d` — `_COLUMNS_DDL` completata, suite verde

Il test (`tests/test_query_log_schema_coerenza.py`) è analisi statica del
sorgente e gira senza database: verifica che ogni colonna nominata
dall'`INSERT` e dalle query di `/api/history` e `/api/analytics` sia creabile
da `_COLUMNS_DDL`. Chiude la classe di bug, non solo l'istanza.

**Verifica del 2026-09-09, 22:07** (domanda posta dal browser):

| Controllo | Esito |
|---|---|
| `Log ID` nel file `.txt` | `3` — un numero, non `N/A` |
| Riga su `query_log` | tutte le colonne valorizzate |
| `provider` / `category` | `groq` / `generico` |
| Latenze | `latency_sql_ms` 752, `latency_db_ms` 332, `latency_nl_ms` 628, `latency_ms` 1712 |
| Token | prompt 2025, completion 127, totale 2152 |
| `update_log_filename` | `log_filename` coincide con il nome del `.txt` |
| `GET /api/history?limit=3` | 200, la nuova riga compare in testa |

Le uniche colonne NULL sulla nuova riga sono `feedback` (nessun voto
espresso), `error` (nessun errore) e `benchmark_run_id` (non è un benchmark):
tutte legittime.

## Difetti noti

### Soppressione degli errori: un guasto totale rimasto invisibile

Un fallimento di persistenza completo è passato inosservato per **tre mesi e
165 esecuzioni**, e non per sfortuna: ci sono tre livelli di soppressione
sovrapposti.

1. `save_to_db_log` cattura ogni eccezione, emette un `logger.warning` e
   restituisce `None`.
2. I chiamanti — `query_routes.py` e `benchmark_service.py` — **ignorano quel
   `None`**: lo passano a `save_log_file`, dove diventa la stringa `"N/A"`
   nel file di log. Nessun controllo, nessun avviso all'utente.
3. In `query_log_schema.py` ognuno dei 14 `ALTER TABLE` ha un
   `except Exception: conn.rollback()` **senza alcun logging**. Una colonna
   che non viene aggiunta non produce nessun segnale: il guasto si manifesta
   molto più tardi, all'`INSERT`, con un errore che sembra non correlato.

`update_log_filename` logga a livello `DEBUG`, quindi invisibile con
`FLASK_DEBUG=false`.

**Da ripensare prima della produzione.** In un prodotto commerciale un
fallimento di persistenza deve essere visibile, non silenzioso: il `None` va
propagato o trasformato in un segnale che raggiunga l'utente, e gli `except`
sugli `ALTER` devono almeno loggare cosa hanno inghiottito.

### Le 165 esecuzioni di luglio 2026 non sono recuperabili

Non sono mai state scritte sul database: `query_log` contiene solo le due
righe del 15 giugno più quelle successive alla correzione. Per quelle
esecuzioni **i file `.txt` in `logs/` sono l'unica copia esistente**. Vanno
trattati come dato primario, non come artefatto rigenerabile — in particolare
non vanno cancellati per fare pulizia.

### Doppia scrittura dei log di benchmark

`run_oneshot_benchmark` chiama **sia** `save_to_db_log` **sia** `save_log_file`
per ogni modello testato. Il risultato è che **120 dei 123 file in `logs/`**
sono duplicati di esecuzioni già presenti in `logs/benchmark/`: solo 3
provengono da uso interattivo reale.

Che sia un effetto collaterale e non una scelta lo dicono quattro indizi:

- il percorso **conversazionale** non lo fa: `run_conversational_benchmark`
  chiama solo `save_to_db_log`;
- il frontend non usa mai il nome file restituito — `benchmark.js` non
  contiene alcun riferimento a `log_filename`: è output morto;
- `list_log_files()` **non filtra**, quindi la sidebar "Log sessione" mescola
  log di benchmark e di uso reale;
- sul canale database la distinzione esiste eccome (`is_benchmark`,
  `benchmark_run_id`, e `get_history` filtra `is_benchmark IS NOT TRUE`). È
  stata implementata da un lato solo.

Chi analizzasse le due cartelle senza deduplicare gonfierebbe il corpus di
1,6× e falserebbe ogni calcolo di consenso fra modelli.

### Deriva di formato dei log: siamo alla terza variante

I file `.txt` non hanno un formato stabile. Le varianti finora:

1. **originale** — benchmark conversazionali con il solo `RIEPILOGO PER TURNO`,
   senza SQL né risultati (3 file del 3 luglio 2026);
2. **con dettaglio** — aggiunta della sezione `DETTAGLIO PER TURNO` (dal
   4 luglio 2026);
3. **con trasferimenti** — aggiunta del blocco `[ TRASFERIMENTI ]` nei session
   log (settembre 2026, tappa 3 dei transfer rate).

Ogni variante costringe ad aggiornare qualunque parser scritto sul corpus, e i
file vecchi restano nel formato in cui sono nati: un'analisi che copra tutto il
corpus deve gestire tutte le varianti contemporaneamente. Da tenere presente
prima di aggiungere altri campi ai log — e un argomento in più a favore di un
formato macchina (JSON Lines) affiancato al `.txt` leggibile.

### Latenze storiche sottostimate sui retry

Fino al commit `ffe7172` il retry su 429 era ricorsivo e il `t0` ripartiva a
ogni tentativo: `latency_ms` escludeva sia i tentativi falliti sia le attese di
backoff. Misurato dal test `test_retry_429_latenza_include_tentativi_falliti_e_attese`:
**250 ms riportati su 2750 ms reali**.

**Conseguenza sui dati storici.** Ogni latenza registrata prima di quel commit
per una chiamata ritentata è sottostimata, sia in `query_log` sia nei file
`.txt`. L'analisi del corpus di luglio 2026 ha contato **24 errori di
rate-limit su 207 esecuzioni** di benchmark: i retry erano frequenti e
l'effetto non è marginale.

Peggio: quei 24 sono i retry **esauriti**, cioè le chiamate finite comunque in
errore. Le chiamate riuscite al secondo o terzo tentativo non lasciano alcuna
traccia di errore, ma hanno la stessa latenza sottostimata — quindi il numero
di misure affette è certamente più alto di 24, e non è determinabile.

**Non sono ricalcolabili.** I log non registrano quanti tentativi ha richiesto
ciascuna chiamata, quindi non c'è modo di correggere a posteriori i valori già
scritti. Le latenze del corpus di luglio — quelle su cui si basano i benchmark
dell'articolo — vanno lette con questa riserva.

## Note operative

### VPN universitaria

- **La prima richiesta dopo l'avvio dell'app può fallire con timeout.** Il
  messaggio è `connection to server at "10.22.255.161", port 5432 failed:
  timeout expired`: è il `connect_timeout=8` superato mentre il link si
  assesta, non un problema di schema né di credenziali. Basta riprovare.
- **Non serve riavviare l'app se la VPN cade.** `ensure_schema_once` rilancia
  l'eccezione senza impostare `_schema_ready`, quindi ritenta la migrazione a
  ogni richiesta finché non riesce. È il motivo per cui l'`ALTER` della
  correzione è passato da solo alla seconda chiamata, senza riavvio.
