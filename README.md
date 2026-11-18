<p align="center"><img src="app/frontend/static/img/logo.svg" width="96" alt=""></p>

# TwinCheck

**Screen your photo and video libraries, set exact duplicates aside and reorganise everything safely - multi-user, in a single, hardened, offline container.**

[Contributing](CONTRIBUTING.md) · [Docker deployment](docker/README.md) · [Application internals](app/README.md) · License: GPL-3.0

TwinCheck indexes your photos and videos, finds exact duplicates, completes missing metadata
**only from reliable sources**, lets you review doubtful files, and moves everything into a
`Year/Month/Day` structure. Files are **moved, never copied or deleted**, nothing is ever
overwritten, every write is previewed and needs your click, and everything can be undone.

## Features

* **Web interface only** - setup, users, databases, scans, reviews, exports: all in the browser
  (works offline, no CDN). The console is needed only to generate the key and to reset a password.
* **Multi-user** with three roles (admin, administrator, base user), groups and per-user source
  folders that can never overlap; strict isolation of files, previews, logs and manifests.
* **Exact duplicates** (same size + SHA-256 + byte comparison), oldest file kept, the others moved to
  `Duplicates/`, CSV export.
* **Metadata without inventing** (EXIF, IPTC, XMP, QuickTime via ExifTool): missing fields are copied
  only from equivalent tags or linked files (`.xmp`, Google Takeout `.json`); guesses are proposals
  for review; existing values are never overwritten; QuickTime UTC ambiguity is reported.
* **Review screen**: photo or playable video (HEVC/MOV get a temporary H.264 preview), all target
  fields, sources, proposals, validation, "apply to folder/group".
* **Organisation** into `YYYY/MM/DD`, `No_date` without a reliable date, companions (`.xmp`,
  `.aae`, Live Photo) moved together, same names get a numeric suffix, mtimes preserved.
* **Resumable jobs** with real-time progress, checkpoints, journalled moves, automatic recovery
  after a crash or `docker kill`.
* **Two database options**: built-in SQLite or external MariaDB; switching, export/import and direct
  migration (SQLite ↔ MariaDB, across versions) with verification.
* **Security**: encrypted vault for all access data (AES-256-GCM), scrypt password hashes, non-root
  read-only container without capabilities, network allow-list, login throttling, CSRF protection.
* **Live system header**: host, OS, CPU, memory (cgroup aware), disks, volumes of every source and
  destination, uptime, database, jobs.
* **10 languages** with RTL support; add one by dropping a JSON file.

## Roadmap

See the [TwinCheck roadmap](ROADMAP.md) for planned improvements.
[Vote on roadmap issues](https://github.com/AxReds/twincheck/issues?q=is:open+label:roadmap+sort:reactions-+1-desc)
with a 👍, or propose new ideas in [Discussions](https://github.com/AxReds/twincheck/discussions/categories/ideas).

## Requirements

* Docker Engine with Docker Compose **v2.20+** (Linux host; x86-64 or ARM64).
* Your libraries in one or more folders that you can mount under `/data`.
* Recommended: source and destination of each user on the **same filesystem** (moves become atomic
  renames; otherwise files are copied, verified and then the original removed).

## Quick start

```sh
git clone https://github.com/AxReds/twincheck.git && cd twincheck
cp .env.example .env && chmod 600 .env
docker compose run --rm app generate-key          # copy the printed APP_MASTER_KEY=… line into .env
mkdir -p library state                            # put (or mount) your photos in ./library
sudo chown 1000:1000 library state                # = PUID:PGID in .env
docker compose up -d
docker compose logs app | grep SETUP_TOKEN        # one-time setup token
```

Open **http://<host>:7777** and follow the wizard.

On a server with **Portainer** you do not need the source code: use the published image
`ghcr.io/axreds/twincheck` and the stack file `docker/portainer-stack.yml`, as described in
[docker/README.md](docker/README.md#deploy-with-portainer).

## Step-by-step usage

1. **Generate the master key** - `docker compose run --rm app generate-key`, paste the line into `.env`
   (`chmod 600 .env`) and store the key in a password manager.
2. **Setup** - open the web UI, enter the setup token from the log, choose the database (SQLite, or
   MariaDB with *Test connection*: the structure is created automatically), set the password of the
   fixed `admin` account, confirm. The wizard then locks forever.
3. **Users and roles** - *Administration → Users*: create base users with their source folders under
   `/data` (and optional destination), group them, create administrators (admin only). New users must
   change their temporary password at first sign-in.
4. **Scan** - pick a source; files are indexed and hashed (SHA-256) in the background; you can
   close the browser, the job resumes after a restart.
5. **Duplicates** - *Analyze*, check the groups (change which file to keep), *Preview move*, confirm.
   Duplicates go to `<destination>/Duplicates/<original relative path>`.
6. **Metadata** - *Analyze and preview*: see what would be completed and from which source; confirm
   to write (original values are saved first). Doubtful files go to review.
7. **Review** - for each file: media on one side, every field on the other; save, skip, leave as it
   is, or apply to the whole folder/group. Decisions are stored immediately; *Preview writing*
   writes the values you entered (only into missing fields).
8. **Organise** - preview the `Year/Month/Day` structure, confirm the move.
9. **Undo** - *Report & undo*: preview and execute the reversal of moves and/or metadata, read from
   the manifest; *Verify* checks files and hashes against index and manifest; download the manifest CSV.
10. **Change database** - *Administration → Database*: test, switch (jobs paused, old database
    untouched, reconciliation with the disk, rollback on failure), or go back to a previous one.
11. **Export and migration** - *Administration → Export & migration* (admin): encrypted archive,
    import into an empty database, or direct migration; preview, checkpoints, final verification,
    then optionally switch to the new database.
12. **Admin password reset** - `docker compose exec app reset-admin-password` (or `--generate`,
    `--user <name>`). Works with the app running; all sessions of that user are closed.

## Safety rules

* **Move, never copy** (except across volumes: copy → SHA-256 verification → removal of the
  original, with a warning). **Never overwrite**: same names get `_1`, `_2`…; an occupied destination
  blocks the item. **Never delete** without an explicit confirmation in the UI; duplicates are moved,
  not deleted.
* **Preview and confirmation** before every write: plans are stored in the database and re-validated
  item by item when executed; anything changed in the meantime is skipped and sent to review.
* **Journal and manifest** are written *before* each change; after a crash every half-done move is
  completed or rolled back by inspecting the disk, never left ambiguous.
* **Encryption**: accounts, roles, groups, sources, password hashes, settings, MariaDB credentials
  and the admin audit log live only in `/state/vault.enc` (AES-256-GCM, fresh nonce on every write,
  tampering detected, atomic writes, encrypted backup). No plaintext copy exists, not even temporarily.
  The key never lives in `/state`, the image or the repository. **A lost key cannot be recovered.**
* **Network**: plain HTTP only (see below), `BIND_ADDRESS`, `ALLOWED_NETWORKS`, login throttling,
  short sessions, CSRF protection, strict CSP, no Internet calls.
* **Backups**: back up your photos before the first run; back up `/state` (and MariaDB) regularly;
  keep the key separately.

### Plain HTTP - read this

The application deliberately serves **only HTTP** on port 80 inside the container (published on
7777). Passwords and session cookies travel **unencrypted**: use it on trusted networks only. You can:

* restrict it to the host: `BIND_ADDRESS=127.0.0.1` in `.env`;
* restrict clients: `ALLOWED_NETWORKS=192.168.1.0/24` (checked on every request, login and wizard
  included);
* add HTTPS yourself with any reverse proxy (Caddy, Traefik, nginx…) in front of port 7777, and set
  `TRUSTED_PROXIES` to its address. The application needs no change.

## Architecture

```
browser ──HTTP──▶ :7777 ─▶ container (non-root, read-only, no capabilities)
                           uvicorn + minimal ASGI app (Python)
                           ├─ encrypted vault  /state/vault.enc   (accounts, settings, audit)
                           ├─ operational DB   SQLite /state/*.sqlite3  or  external MariaDB
                           ├─ job scheduler    scan · dedupe · metadata · moves · verify · transfer
                           ├─ ExifTool, ffmpeg (previews in /state/cache, temp files in /tmp tmpfs)
                           └─ /data            your libraries (moves = atomic renames)
```

One container, one main process; long work runs in background threads with state in the database.
Details: [app/README.md](app/README.md) (schema, migrations, procedures) and
[docker/README.md](docker/README.md) (every parameter).

## Languages

| Code | Language | Status |
|------|----------|--------|
| `en` | English | complete (source) |
| `it` | Italiano | needs-review |
| `es` | Español | needs-review |
| `fr` | Français | needs-review |
| `de` | Deutsch | needs-review |
| `pt-BR` | Português (Brasil) | needs-review |
| `zh-Hans` | 简体中文 | needs-review |
| `ja` | 日本語 | needs-review |
| `ru` | Русский | needs-review |
| `ar` | العربية (RTL) | needs-review |

*needs-review*: complete, machine-assisted initial translation not yet verified by a native speaker.
Help is welcome - see [CONTRIBUTING.md](CONTRIBUTING.md). Run `python3 app/tools/check_locales.py`
for the current completeness report. Logs are always in English by design.

## Known limitations

* No HTTPS in the application by design (use your own proxy).
* Sessions are kept in memory: a container restart signs everybody out.
* Duplicates are exact copies only (no visual similarity, by design).
* RAW/HEIC preview generation and writing depend on the formats supported by the bundled ffmpeg and
  ExifTool; files ExifTool cannot write are skipped and reported.
* Metadata writing for videos covers MOV/MP4 family containers; other video formats are reviewed only.
* One container per database (enforced by a lock); no horizontal scaling.
* Translations other than English need native review.

## Running the tests

```sh
python3 -B -m unittest discover -s app/tests -v          # unit + integration (SQLite)
python3 -B app/tools/check_locales.py                     # translations + hard-coded text scan
bash docker/tests/run_container_tests.sh                  # container: permissions, network, kill/resume, MariaDB
```

See [app/tests/README.md](app/tests/README.md) for MariaDB and large-dataset options.

## Contributing

Translations, bug reports and code are welcome: read [CONTRIBUTING.md](CONTRIBUTING.md). Every pull
request runs the translation checker and the test-suite.

## License

[GNU General Public License v3.0](LICENSE) - you may use, study, modify and share TwinCheck; if you
distribute it (original or modified), you must do so under the same license and provide the source code.
