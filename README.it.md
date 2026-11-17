<p align="center"><img src="app/frontend/static/img/logo.svg" width="96" alt=""></p>

# Photo Organizer

**Riorganizzazione sicura e multiutente di librerie di foto e video, in un unico container protetto e offline.**

[English](README.md) · [Come contribuire](CONTRIBUTING.md) · [Deploy Docker](docker/README.md) · [Dettagli dell'applicazione](app/README.md) · Licenza: AGPL-3.0

Photo Organizer indicizza foto e video, trova i duplicati esatti, completa i metadati mancanti
**solo da fonti affidabili**, ti fa rivedere i file dubbi e sposta tutto in una struttura
`Anno/Mese/Giorno`. I file vengono **spostati, mai copiati né cancellati**, nulla viene mai
sovrascritto, ogni scrittura ha un'anteprima e richiede il tuo clic, e tutto si può annullare.

## Funzionalità

* **Solo interfaccia web**: setup, utenti, database, scansioni, revisione, esportazioni sono tutti
  nel browser (funziona offline, nessun CDN). La console serve solo per generare la chiave e
  reimpostare una password.
* **Multiutente** con tre ruoli (admin, administrator, utente base), gruppi e cartelle sorgente per
  utente mai sovrapposte; isolamento rigoroso di file, anteprime, log e manifest.
* **Duplicati esatti** (stessa dimensione + SHA-256 + confronto byte per byte): si tiene il file più
  vecchio, gli altri vanno in `Duplicati/`; export CSV.
* **Metadati senza inventare** (EXIF, IPTC, XMP, QuickTime con ExifTool): i campi mancanti si copiano
  solo da tag equivalenti o file collegati (`.xmp`, `.json` di Google Takeout); le deduzioni sono
  proposte da rivedere; i valori presenti non vengono mai sovrascritti; l'ambiguità UTC dei video
  QuickTime viene segnalata.
* **Schermata di revisione**: foto o video riproducibile (HEVC/MOV con anteprima H.264 temporanea),
  tutti i campi, le fonti, le proposte, la validazione, "applica a tutta la cartella/gruppo".
* **Organizzazione** in `AAAA/MM/GG`, `Senza_data` senza data affidabile, file collegati (`.xmp`,
  `.aae`, Live Photo) spostati insieme, omonimi con suffisso numerico, date di modifica conservate.
* **Job riprendibili** con avanzamento in tempo reale, checkpoint, spostamenti registrati in un
  giornale, ripristino automatico dopo un crash o un `docker kill`.
* **Due database**: SQLite integrato o MariaDB esterno; cambio database, esportazione/importazione e
  migrazione diretta (SQLite ↔ MariaDB, anche tra versioni) con verifica.
* **Sicurezza**: archivio cifrato per tutti i dati di accesso (AES-256-GCM), hash scrypt delle
  password, container non-root in sola lettura senza capability, reti consentite, limite ai
  tentativi di login, protezione CSRF.
* **Intestazione con dati di sistema in tempo reale**: host, sistema operativo, CPU, memoria (con
  limiti cgroup), dischi, volumi di ogni sorgente e destinazione, uptime, database, job.
* **10 lingue** con supporto RTL; se ne aggiunge una con un file JSON.

## Requisiti

* Docker Engine con Docker Compose **v2.20+** (host Linux, x86-64 o ARM64).
* Le librerie in una o più cartelle montabili sotto `/data`.
* Consigliato: sorgente e destinazione di ogni utente sullo **stesso filesystem** (gli spostamenti
  diventano rinomine atomiche; altrimenti copia, verifica e poi rimozione dell'originale).

## Avvio rapido

```sh
git clone <URL del tuo fork> photo-organizer && cd photo-organizer
cp .env.example .env && chmod 600 .env
docker compose run --rm app generate-key          # copia la riga APP_MASTER_KEY=… stampata nel .env
mkdir -p library state                            # metti (o monta) le foto in ./library
sudo chown 1000:1000 library state                # = PUID:PGID nel .env
docker compose up -d
docker compose logs app | grep SETUP_TOKEN        # token di setup monouso
```

Apri **http://<host>:7777** e segui il wizard.

## Uso passo per passo

1. **Generazione della chiave**: `docker compose run --rm app generate-key`, incolla la riga nel
   `.env` (`chmod 600 .env`) e conserva la chiave in un password manager.
2. **Setup**: apri l'interfaccia web, inserisci il token di setup preso dal log, scegli il database
   (SQLite, oppure MariaDB con *Testa connessione*: la struttura viene creata automaticamente),
   imposta la password dell'account fisso `admin`, conferma. Il wizard poi si blocca per sempre.
3. **Utenti e ruoli**: *Amministrazione → Utenti*: crea gli utenti base con le loro cartelle
   sorgente sotto `/data` (e la destinazione, se diversa), raggruppali, crea gli administrator (solo
   admin). I nuovi utenti cambiano la password temporanea al primo accesso.
4. **Scansione**: scegli una sorgente; i file vengono indicizzati e ne viene calcolato lo SHA-256 in
   background; puoi chiudere il browser, il job riprende dopo un riavvio.
5. **Duplicati**: *Analizza*, controlla i gruppi (puoi cambiare quale file tenere), *Anteprima
   spostamento*, conferma. I duplicati vanno in `<destinazione>/Duplicati/<percorso relativo originale>`.
6. **Metadati**: *Analizza e anteprima*: vedi cosa verrebbe completato e da quale fonte; conferma per
   scrivere (prima vengono salvati i valori originali). I file dubbi vanno in revisione.
7. **Revisione**: per ogni file, il media da un lato e tutti i campi dall'altro; salva, salta, lascia
   com'è o applica a tutta la cartella/gruppo. Le decisioni si salvano subito; *Anteprima scrittura*
   scrive i valori inseriti (solo nei campi mancanti).
8. **Organizzazione**: anteprima della struttura `Anno/Mese/Giorno`, poi conferma dello spostamento.
9. **Annullamento**: *Report e annullamento*: anteprima ed esecuzione del ripristino di spostamenti
   e/o metadati letti dal manifest; *Verifica* controlla file e hash rispetto a indice e manifest;
   download del manifest CSV.
10. **Cambio database**: *Amministrazione → Database*: test, cambio (job in pausa, vecchio database
    intatto, riconciliazione col disco, rollback in caso di errore) o ritorno a un database precedente.
11. **Esportazione e migrazione**: *Amministrazione → Esportazione e migrazione* (admin): archivio
    cifrato, importazione in un database vuoto o migrazione diretta; anteprima, checkpoint, verifica
    finale, poi eventualmente passaggio al nuovo database.
12. **Reset password admin**: `docker compose exec app reset-admin-password` (oppure `--generate`,
    `--user <nome>`). Funziona con l'app in esecuzione; tutte le sessioni di quell'utente vengono chiuse.

## Regole di sicurezza

* **Spostare, mai copiare** (tranne tra volumi diversi: copia → verifica SHA-256 → rimozione
  dell'originale, con avviso). **Mai sovrascrivere**: gli omonimi ricevono `_1`, `_2`…; una
  destinazione occupata blocca l'elemento. **Mai cancellare** senza conferma esplicita nella UI; i
  duplicati vengono spostati, non eliminati.
* **Anteprima e conferma** prima di ogni scrittura: i piani sono salvati nel database e ricontrollati
  elemento per elemento all'esecuzione; ciò che è cambiato nel frattempo viene saltato e messo in revisione.
* **Giornale e manifest** vengono scritti *prima* di ogni modifica; dopo un crash ogni spostamento a
  metà viene completato o annullato guardando il disco, mai lasciato ambiguo.
* **Cifratura**: account, ruoli, gruppi, sorgenti, hash delle password, impostazioni, credenziali
  MariaDB e audit log amministrativo stanno solo in `/state/vault.enc` (AES-256-GCM, nonce nuovo a
  ogni scrittura, manomissione rilevata, scritture atomiche, backup cifrato). Non esiste nessuna
  copia in chiaro, nemmeno temporanea. La chiave non sta mai in `/state`, nell'immagine o nel repo.
  **Una chiave persa non si può recuperare.**
* **Rete**: solo HTTP (vedi sotto), `BIND_ADDRESS`, `ALLOWED_NETWORKS`, limite ai tentativi di
  login, sessioni brevi, protezione CSRF, CSP restrittiva, nessuna chiamata verso Internet.
* **Backup**: fai il backup delle foto prima del primo utilizzo; fai regolarmente il backup di
  `/state` (e di MariaDB); conserva la chiave separatamente.

### Solo HTTP: da leggere

L'applicazione serve volutamente **solo HTTP** sulla porta 80 interna (pubblicata sulla 7777).
Password e cookie di sessione viaggiano **in chiaro**: usala solo su reti fidate. Puoi:

* limitarla all'host: `BIND_ADDRESS=127.0.0.1` nel `.env`;
* limitare i client: `ALLOWED_NETWORKS=192.168.1.0/24` (controllato su ogni richiesta, compresi
  login e wizard);
* aggiungere tu HTTPS con un reverse proxy (Caddy, Traefik, nginx…) davanti alla porta 7777, e
  impostare `TRUSTED_PROXIES` sul suo indirizzo. L'applicazione non richiede modifiche.

## Architettura

```
browser ──HTTP──▶ :7777 ─▶ container (non-root, sola lettura, nessuna capability)
                           uvicorn + app ASGI minimale (Python)
                           ├─ archivio cifrato  /state/vault.enc   (account, impostazioni, audit)
                           ├─ DB operativo      SQLite /state/*.sqlite3  oppure  MariaDB esterno
                           ├─ scheduler job     scansione · duplicati · metadati · spostamenti · verifica · trasferimenti
                           ├─ ExifTool, ffmpeg  (anteprime in /state/cache, file temporanei in /tmp tmpfs)
                           └─ /data             le tue librerie (spostamenti = rinomine atomiche)
```

Un container, un processo principale; i lavori lunghi girano in thread in background con lo stato nel
database. Dettagli: [app/README.md](app/README.md) (schema, migrazioni, procedure) e
[docker/README.md](docker/README.md) (tutti i parametri).

## Lingue

| Codice | Lingua | Stato |
|--------|--------|-------|
| `en` | English | completa (sorgente) |
| `it` | Italiano | needs-review |
| `es` | Español | needs-review |
| `fr` | Français | needs-review |
| `de` | Deutsch | needs-review |
| `pt-BR` | Português (Brasil) | needs-review |
| `zh-Hans` | 简体中文 | needs-review |
| `ja` | 日本語 | needs-review |
| `ru` | Русский | needs-review |
| `ar` | العربية (RTL) | needs-review |

*needs-review*: traduzione iniziale completa generata automaticamente, non ancora verificata da un
madrelingua. Ogni aiuto è benvenuto: vedi [CONTRIBUTING.md](CONTRIBUTING.md). I log sono sempre in
inglese per scelta.

## Limiti noti

* Nessun HTTPS nell'applicazione, per scelta (usa un tuo proxy).
* Le sessioni sono in memoria: un riavvio del container disconnette tutti.
* Solo duplicati esatti (nessuna somiglianza visiva, per scelta).
* Anteprime e scrittura di RAW/HEIC dipendono dai formati supportati da ffmpeg ed ExifTool inclusi;
  i file che ExifTool non può scrivere vengono saltati e segnalati.
* La scrittura dei metadati video copre i container della famiglia MOV/MP4; gli altri formati video
  vanno solo in revisione.
* Un container per database (garantito da un lock); nessuno scaling orizzontale.
* Le traduzioni diverse dall'inglese vanno riviste da madrelingua.

## Eseguire i test

```sh
python3 -B -m unittest discover -s app/tests -v          # unitari + integrazione (SQLite)
python3 -B app/tools/check_locales.py                     # traduzioni + ricerca di testo scritto nel codice
bash docker/tests/run_container_tests.sh                  # container: permessi, rete, kill/ripresa, MariaDB
```

Vedi [app/tests/README.md](app/tests/README.md) per MariaDB e il test con dataset grande.

## Come contribuire

Traduzioni, segnalazioni e codice sono benvenuti: leggi [CONTRIBUTING.md](CONTRIBUTING.md). Ogni pull
request esegue la verifica delle traduzioni e i test.

## Licenza

[GNU Affero General Public License v3.0](LICENSE): se offri una versione modificata come servizio di
rete, devi mettere il suo codice sorgente a disposizione degli utenti.
