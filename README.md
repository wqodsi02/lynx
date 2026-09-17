# LYNX — Industrial Network Intelligence

Sistema di interrogazione in linguaggio naturale (NL2SQL) per dati di traffico
di rete industriale PROFINET, con benchmark multi-modello, memoria
conversazionale e analytics su latenze/token. Pensato per esecuzione
**locale**, non per essere esposto pubblicamente.

## Struttura del progetto

```
lynx/
  backend/
    config.py              # configurazione da .env
    crypto_utils.py         # cifratura API key su disco
    models_repository.py    # CRUD modelli LLM
    system_prompt.py        # SYSTEM_PROMPT_GENERATION (Fase 1, schema completo)
                             # + SYSTEM_PROMPT_INTERPRETATION (Fase 2, prompt ridotto)
    app.py                  # application factory Flask
    db/
      connection.py          # connessione Postgres
      readonly_guard.py       # sicurezza query (whitelist + readonly tx)
      query_log_schema.py      # schema tabella query_log
    llm/
      base.py                 # LLMResult, errori
      client.py                # dispatcher provider
      providers/groq.py        # adapter Groq
      providers/gemini.py      # adapter Gemini
    services/
      nl2sql_service.py        # fase1/fase2 NL2SQL, conversation-aware
      conversation_service.py  # memoria conversazionale per sessione
      benchmark_service.py     # benchmark oneshot e conversazionale
      logging_service.py       # log .txt + tabella query_log
    api/
      models_routes.py, db_routes.py, query_routes.py,
      benchmark_routes.py, analytics_routes.py, misc_routes.py
  static/
    index.html
    css/ (theme, layout, chat, modal)
    js/  (api, state, theme, utils, chat, conversation, settings,
          benchmark, sidebar_logs, analytics, main)
  data/
    models.json   (creato al primo avvio, API key cifrate)
    secret.key    (creato al primo avvio, NON condividere/versionare)
  logs/
    *.txt, benchmark/*.txt
  run.py
  requirements.txt
  .env.example
```

## Setup

1. Crea un ambiente virtuale e installa le dipendenze:
   ```
   python -m venv venv
   venv\Scripts\activate          (Windows)
   pip install -r requirements.txt
   ```
2. Copia `.env.example` in `.env` e inserisci le credenziali reali del
   database (host VPN, nome DB, utente, password).
3. Assicurati di essere connesso alla VPN universitaria (il DB è raggiungibile
   solo da rete interna).
4. Avvia il backend:
   ```
   python run.py
   ```
5. Apri http://localhost:5000 nel browser.

## Configurazione modelli LLM

Dal pannello **Impostazioni → Modelli**, aggiungi i modelli Groq e/o Gemini
con la relativa API key. Le chiavi vengono cifrate su disco
(`data/models.json` + `data/secret.key`) e non sono mai visibili nel
frontend dopo il salvataggio.

## Funzionalità principali

- **Tema chiaro/scuro**: toggle in sidebar, persistito in localStorage.
- **Memoria conversazionale**: ogni chat ha un `session_id` con history
  propria, ri-iniettata nelle chiamate LLM successive nella stessa chat.
- **Riapertura conversazioni**: le chat passate in sidebar sono cliccabili;
  i turni vengono ricostruiti dalla tabella `query_log`
  (`GET /api/conversation/<session_id>/turns`) e la memoria conversazionale
  server-side viene ripopolata, anche dopo un riavvio del backend.
- **Notifiche toast** non bloccanti al posto di `alert()`; chiusura modali
  con Esc o click sullo sfondo.
- **Serializzazione JSON estesa**: `Decimal`, `date/time`, `UUID` e simili
  (tipi restituiti da psycopg2 per NUMERIC/aggregati) sono serializzati da
  un JSON provider dedicato — senza, qualsiasi query con ROUND/AVG/SUM
  produceva un errore 500.
  "Nuova chat" azzera la memoria.
- **Annulla / Rigenera / Modifica domanda**: durante la generazione, il
  pulsante di invio diventa "stop" e annulla la richiesta in corso (sia
  lato client con `AbortController` sia lato server con un job id).
  Dopo una risposta è possibile rigenerarla o modificare il testo della
  domanda precedente (la history viene troncata e ricalcolata di conseguenza).
- **Benchmark domanda singola**: stessa domanda su più modelli, confronto
  di SQL generata, risposta, latenze (SQL/DB/NL) e token.
- **Benchmark conversazionale**: uno scenario di più domande in sequenza
  eseguito su più modelli, ognuno con una propria memoria isolata, per un
  confronto equo della capacità di mantenere il contesto.
- **Analytics**: tabella di confronto modelli (latenze, token, accuratezza
  SQL segnalata via feedback) e grafico del consumo token.
- **Export CSV**, **log per sessione e per benchmark** consultabili e
  scaricabili dalla sidebar destra.

## Diagnosi di lentezza

Ogni chiamata all'LLM viene ora cronometrata e loggata in console. All'avvio del
backend vedrai righe come:

```
13:41:02 [INFO] lynx.llm: Chiamata LLM: provider=groq model=llama-3.3-70b-versatile elapsed=1.34s tokens=856
```

Se una chiamata supera gli 8 secondi, viene emesso un warning esplicito:

```
13:41:15 [WARNING] lynx.llm: Chiamata LLM lenta: provider=groq model=... elapsed=42.0s (possibile rate-limit/retry)
```

Questo permette di distinguere immediatamente le due cause tipiche di lentezza:
- **Rate-limit del provider** (piano gratuito Groq/Gemini con limiti bassi di
  token/minuto): la chiamata riceve un HTTP 429 e attende prima di riprovare.
  I prompt di sistema sono stati resi molto compatti (~800 token per la
  generazione, ~380 per l'interpretazione) proprio per ridurre questo rischio.
- **Modello intrinsecamente lento** o rete/VPN lenta.

Se vedi warning di rate-limit ripetuti, le opzioni sono: usare un modello più
piccolo/veloce, ridurre il numero di modelli nel benchmark, oppure passare a un
piano a pagamento del provider con limiti più alti.

## Ottimizzazioni token e prestazioni

Il sistema usa **due prompt di sistema separati** invece di uno unico condiviso:

- **`SYSTEM_PROMPT_GENERATION`** (Fase 1, generazione SQL): include lo schema completo
  del database, le regole di traduzione della codifica anonima e le regole SQL. Serve
  davvero tutto questo contesto per generare query corrette.
- **`SYSTEM_PROMPT_INTERPRETATION`** (Fase 2, interpretazione risultati): prompt molto più
  corto (~3.400 caratteri contro i ~12.300 del primo, circa il 75% in meno), perché in
  questa fase il modello riceve già i risultati come JSON e non ha bisogno di rivedere
  lo schema delle tabelle — solo le regole di conversione unità di misura e la codifica
  anonima per rispondere in modo coerente.

Altri accorgimenti per ridurre token, costo e latenza:

- **Classificazione della categoria della domanda** (usata solo per Analytics) tramite
  euristica locale a keyword invece di una terza chiamata LLM per ogni domanda.
- **Cronologia conversazionale troncata**: quando la history di una chat viene
  ri-iniettata nei messaggi per le domande di follow-up, SQL e risposte dei turni
  passati sono troncati (rispettivamente 500 e 400 caratteri) — il contenuto integrale
  resta comunque salvato per la UI e i log, si tronca solo la copia usata come contesto,
  per evitare che conversazioni lunghe facciano crescere il costo di ogni nuova domanda
  in proporzione alla somma di tutte le risposte precedenti.

Su un benchmark multi-modello, questi accorgimenti riducono sensibilmente sia il volume
di token complessivo sia il rischio di rate-limiting lato provider (che si traduce in
tempi di esecuzione molto più lunghi per via dei retry automatici).

## Sicurezza query (invariata, a 3 livelli)

1. Il system prompt istruisce l'LLM a generare solo `SELECT`/`WITH`.
2. Whitelist applicativa (`readonly_guard.py`) blocca parole chiave di
   scrittura prima di toccare il DB.
3. La connessione Postgres è aperta in transazione `READ ONLY`: anche un
   comando di scrittura che superasse i primi due livelli verrebbe
   rifiutato dal database stesso.

## Note per i test

- L'app non implementa autenticazione: è pensata per girare solo in
  locale sul tuo PC. Non esporla su rete pubblica senza aggiungere
  autenticazione e HTTPS.
- `FLASK_DEBUG=true` nel `.env` attiva l'autoreload utile in sviluppo;
  lascialo `false` per simulare condizioni più vicine a un utilizzo reale.
- I log conversazionali (memoria) sono in-memory: si azzerano se riavvii
  il backend. I log su file e su DB invece restano.
