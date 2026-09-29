# Vinted Bag Finder

Monitora il catalogo Vinted configurato nello script. Per ogni inserzione nuova invia su Telegram la prima immagine, il nome, il prezzo dell'articolo (senza Protezione acquisti), la descrizione e il link diretto. Non scrive immagini, schede o annunci visti sul disco.

## Creare e configurare il bot Telegram

1. In Telegram cerca `@BotFather` e apri la chat verificata.
2. Invia `/newbot`, scegli il nome visibile e poi uno username unico che termini con `bot`.
3. BotFather ti inviera un token. Copialo e conservalo come una password: non pubblicarlo e non inviarlo in chat.
4. Apri il link del bot che BotFather ti ha fornito e premi **Avvia** (oppure invia `/start`).
5. Nella cartella del progetto crea un file chiamato `.env`, copiando `.env.example`.
6. Nel `.env`, inserisci il token dopo `TELEGRAM_BOT_TOKEN=`.
7. Per ricavare il tuo chat ID, apri PowerShell nella cartella del progetto e lancia:

```powershell
$token = (Get-Content .env | Where-Object { $_ -match '^TELEGRAM_BOT_TOKEN=' }) -replace '^TELEGRAM_BOT_TOKEN=', ''
Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates" | ConvertTo-Json -Depth 10
```

Nell'output cerca `message` e poi `chat` → `id`. Se `result` e vuoto, torna nella chat col tuo bot e invia `/start`, quindi ripeti il comando.

Esegui `getUpdates` solo per trovare il chat ID prima di avviare il monitor. Non eseguire contemporaneamente questo comando manuale e il bot, perche Telegram consente un solo polling `getUpdates` per token.

8. Inserisci quell'ID dopo `TELEGRAM_CHAT_ID=` nel `.env` e salva il file. Il file `.env` e escluso da Git.
9. Installa le dipendenze e avvia il monitor:

```powershell
python -m pip install -r requirements.txt
python vinted_monitor.py
```

Ogni nuova borsa arrivera come foto con nome, prezzo e descrizione. Il link Vinted pulito e mostrato in chiaro sotto la descrizione; se questa supera il limite della didascalia, il link compare nell'ultimo messaggio di continuazione.

## Cambiare il prezzo massimo da Telegram

Con il monitor in esecuzione, invia al bot `/prezzo`. Comparira un menu con alcuni importi rapidi e il pulsante **Altro importo** per digitare una cifra personalizzata. Puoi anche impostare direttamente il valore con, ad esempio, `/prezzo 17,50`. Dopo un cambio effettivo del prezzo massimo, il controllo successivo elabora al massimo 10 nuove inserzioni e poi torna al normale intervallo di aggiornamento. Il valore e mantenuto in RAM fino al riavvio del processo. Il comando e accettato solo dalla chat il cui ID e configurato in `.env` o nelle variabili d'ambiente Render.

## Avvio su Windows

Apri PowerShell nella cartella del progetto ed esegui:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python vinted_monitor.py
```

Al primo controllo dopo l'avvio invia al massimo le 5 inserzioni piu recenti. Nei controlli successivi aspetta 60 secondi, ricarica il catalogo e invia le nuove inserzioni fino alla prima gia presente nella memoria del processo. Puoi impostare un intervallo diverso (minimo 20 secondi) prima dell'avvio:

```powershell
$env:VINTED_POLL_SECONDS = "90"
python vinted_monitor.py
```

Per vedere il browser durante l'esecuzione, imposta `$env:VINTED_HEADLESS = "false"`.

## Deploy su Render

Il repository include `Dockerfile` e `render.yaml`. Pubblica il progetto su un repository Git e collegalo a Render con **New +** → **Blueprint**, oppure crea un servizio **Background Worker** usando Docker e il `Dockerfile` del repository. Seleziona un piano che supporti i Background Worker; il piano gratuito Render potrebbe non essere disponibile per questo tipo di servizio.

Nel pannello Render aggiungi le variabili segrete `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` in **Environment**. `VINTED_POLL_SECONDS` e gia impostato a 60 nel blueprint e puo essere modificato li. Non caricare il file `.env` nel repository. Avvia una sola istanza del worker: ogni processo ha la propria RAM e istanze multiple duplicherebbero polling e notifiche Telegram.

Il log `No open ports detected` e normale solo per un **Background Worker**, che non deve esporre porte HTTP. Se Render imposta `WEB_CONCURRENCY` e continua a cercare porte, il servizio attuale e probabilmente un **Web Service**: crea il servizio dal Blueprint `render.yaml` oppure crea un nuovo Background Worker Docker. Non avviare il monitor come web service.

Il monitor effettua richieste HTTP alle pagine pubbliche di Vinted e legge i dati prodotto dal JSON-LD: non installa ne avvia Chromium. Se nei log compare `Telegram getUpdates HTTP 409`, un'altra istanza sta interrogando lo stesso bot: arresta l'esecuzione locale e mantieni una sola istanza Render attiva.

Il monitor mantiene al massimo 5.000 ID visti in RAM e passa a Telegram l'URL remoto dell'immagine senza scaricarla. Al riavvio/deploy Render gli ID e il prezzo massimo ripartono vuoti/default: il monitor reinvia al massimo le 5 inserzioni iniziali e il prezzo torna a 10 EUR. Nessuna foto viene salvata sul disco del container.

I file `annunci_visti.json`, `monitor_config.json` e la cartella `borse_trovate` eventualmente rimasti dal test locale non vengono piu usati; non sono inclusi nell'immagine Docker.

La pagina e i selettori di Vinted possono cambiare. Il programma non effettua login e non tenta di superare CAPTCHA, blocchi o limitazioni del sito; se Vinted mostra una verifica o nega l'accesso, fermalo e riprova piu tardi.