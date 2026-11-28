# Docker deployment

Everything needed to build and run TwinCheck as **one container with one main process**,
non-root from the very first process, on a read-only root filesystem, without any capability.

| File | Purpose |
|------|---------|
| `Dockerfile` | Multi-stage build on Alpine. Build stage installs pure-Python wheels; the final stage contains only `python3`, `py3-cryptography`, `exiftool`, `ffmpeg` (from `apk`) and the application. No compiler. |
| `docker-compose.yml` | The service, with every hardening option. Included by the root `compose.yaml`, so `docker compose up -d` works from the repository root. |
| `requirements.txt` | Pinned pure-Python dependencies (uvicorn, h11, click, PyMySQL). |
| `bin/twincheck-entrypoint` | Entry point: `exec`s Python (no shell left running, no `su`, no `chown`). |
| `bin/generate-key`, `bin/reset-admin-password` | Console commands (`docker compose run --rm app generate-key`, `docker compose exec app reset-admin-password`). |
| `bin/twincheck-healthcheck` | Healthcheck: `wget` on `http://127.0.0.1:80/healthz` inside the container. |
| `portainer-stack.yml` | Same service as `docker-compose.yml`, but using the published image instead of building it: for Portainer or any Docker host without the source code. |
| `docker-compose.test.yml` | Throw-away MariaDB for the test-suite only. |
| `tests/run_container_tests.sh` | Container acceptance tests (permissions, network, kill/resume, test-suite inside the image). |

## Quick start

```sh
cp .env.example .env && chmod 600 .env          # from the repository root
docker compose run --rm app generate-key        # prints APP_MASTER_KEY=<64 hex chars>
# paste that line into .env (replace the CHANGE_ME placeholder)
mkdir -p library state                          # owned by PUID:PGID (see below)
docker compose up -d
docker compose logs app | grep SETUP_TOKEN      # one-time token for the setup wizard
```

Open `http://<host>:7777`. Requires Docker Compose v2.20+ (for `include`). With an older
Compose run it from `docker/`: `cd docker && docker compose --env-file ../.env up -d`.

## Parameters

All parameters go in the `.env` file next to `compose.yaml` (repository root). "Compose" in the
*Read by* column means the value is used by Docker Compose itself, "App" that it is passed to the
application inside the container.

<!-- params:start -->
| Name | Description | Default | Required | Example | Read by |
|------|-------------|---------|----------|---------|---------|
| `APP_MASTER_KEY` | Master key that encrypts `/state/vault.enc`: 32 random bytes as 64 hex characters (or base64), or a passphrase of at least 20 characters (derived with scrypt). Use `file` to read it from `APP_MASTER_KEY_FILE`. Compose refuses to start when it is missing. | none | yes | `APP_MASTER_KEY=3f9c…(64 hex)` | Compose, App |
| `APP_MASTER_KEY_FILE` | Path *inside the container* of a file holding the key (Docker secret). Used when `APP_MASTER_KEY=file`. | empty | no | `/run/secrets/twincheck_master_key` | App |
| `BIND_ADDRESS` | Host address on which port 7777 is published. `127.0.0.1` = only this host. | `0.0.0.0` | no | `127.0.0.1` | Compose |
| `ALLOWED_NETWORKS` | Comma-separated CIDR list allowed to use the app, enforced server-side on every request (login page and setup wizard included). Empty = no restriction. Loopback is always allowed (healthcheck). | empty | no | `192.168.1.0/24,10.8.0.0/16` | App |
| `TRUSTED_PROXIES` | CIDR list of your own reverse proxies. Only used to read the real client IP (`X-Forwarded-For`) for login rate limiting; never for `ALLOWED_NETWORKS`. | empty | no | `172.18.0.10/32` | App |
| `HOST_LABEL` | Host name shown in the header (the real host name is not visible from the container). Empty = container name. | empty | no | `nas-living-room` | App |
| `APP_LANG` | Language of console commands (`generate-key`, `reset-admin-password`) and initial default language of the instance (the admin can change the latter in the UI). Logs are always English. | `en` | no | `it` | App |
| `TZ` | Initial IANA library timezone; Settings → General → Library timezone takes precedence and is stored in the vault. | empty (UTC, with admin notice) | no | `Europe/Rome` | App |
| `DISK_WARN_FREE_PERCENT` | Free-space % under which a disk is shown in the warning colour. | `15` | no | `20` | App |
| `DISK_CRIT_FREE_PERCENT` | Free-space % under which a disk is shown in the critical colour. | `5` | no | `3` | App |
| `PUID` | UID the whole container runs as (`user:`). Must own the library and state folders. Never 0. | `1000` | no | `1026` | Compose |
| `PGID` | GID the container runs as. | `1000` | no | `100` | Compose |
| `DATA_PATH` | Host folder mounted on `/data` (photo libraries, read/write). Relative paths are relative to `docker/`. | `../library` | no | `/volume1/photo` | Compose |
| `STATE_PATH` | Host folder mounted on `/state`. | `../state` | no | `/volume1/docker/twincheck` | Compose |
| `TMP_SIZE` | Size of the `/tmp` tmpfs (ExifTool and ffmpeg temporary files). | `512m` | no | `2g` | Compose |
| `CONTAINER_NAME` | Container name (also the default host label). | `twincheck` | no | `twincheck` | Compose |
| `JOB_WORKERS` | Background jobs running at the same time. | `2` | no | `4` | App |
| `MAX_REQUEST_KB` | Maximum size of a normal API request body (KiB). | `1024` | no | `2048` | App |
| `MAX_UPLOAD_MB` | Maximum size of a migration archive uploaded from the browser (MiB). Larger archives: copy them into `/state/imports`. | `102400` | no | `20480` | App |
| `PREVIEW_CACHE_MAX_MB` | Size of the preview cache in `/state/cache` before old entries are pruned. | `2048` | no | `8192` | App |
| `MARIADB_PASSWORD_FILE` | File (Docker secret) with the MariaDB password; overrides the password stored in the vault. | empty | no | `/run/secrets/twincheck_mariadb_password` | App |
| `LOG_LEVEL` | Container log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. | `INFO` | no | `WARNING` | App |
| `ALPINE_VERSION` | Build arg: Alpine base image version. | `3.24` | no | `3.24` | Compose (build) |
| `APP_VERSION` | Build arg and image tag. | `1.0.0` | no | `1.0.0` | Compose (build) |
<!-- params:end -->

Fixed values (not configurable on purpose): internal port **80**, published port **7777**,
volumes `/data` and `/state`, `/tmp` on tmpfs.

### Library timezone

The image includes IANA timezone data. Optional `TZ` (example: `Europe/Rome`) is passed by
both Compose and the Portainer stack and supplies the default **Library timezone**.
If unset, the default is `UTC` and Settings → General recommends configuring it.
The fixed admin can override it in that page; the override is stored in `/state/vault.enc`,
not `.env`, and audited as `SETTING_CHANGED`.
The field is one accessible searchable select with a closed value/chevron and a popup
IANA-zone list with current UTC offsets. Suggested zones precede All timezones.
Typing filters by case-insensitive substring; arrows and Enter select,
Escape closes without changing the selection, and search text cannot be saved as a zone. Mouse and touch also work.
The saved zone is highlighted and scrolled into view; offsets can change with daylight saving.

Organizing dates are local wall-clock values in the library timezone: file-system mtime
proposals convert from UTC; offset EXIF converts only for organizing while its original
metadata is preserved; timezone-free EXIF and manual values are not shifted. Upgrading old
databases flags ambiguous unapplied decisions for re-check without changing saved dates.
Organize skips flagged files until the owner explicitly confirms or changes them.

**Show version on login page** is another admin-only vault setting (default ON), not a
container environment parameter. When OFF, public bootstrap contains no application version.

Parameters that exist only inside the image and are set by the Dockerfile (`DATA_DIR=/data`,
`STATE_DIR=/state`, `APP_PORT=80`, `EXIFTOOL_PATH`, `FFMPEG_PATH`) are documented in
[`app/backend/README.md`](../app/backend/README.md); do not change them in a container deployment.

## Upgrading to 1.0.1-rc.12

Stop the container and back up `/state` and the operational MariaDB database if used.
Deploy the exact candidate image `ghcr.io/axreds/twincheck:1.0.1-rc.12` (or the moving `rc`
alias) after publication. From rc.11 no schema migration is needed: schema remains 6 and there is no
`DB_MIGRATED` event for an already-current database. Existing decisions remain unchanged.
From older schemas, an administrator must run **Settings → Database → Migrate schema**
and confirm the backup. All signed-in users see **Database update required. Contact an
administrator.** until the operational database is ready; admins can follow the banner.

Schema 6 returns only historical skip-only records to Pending, preserving their skipped marker
and timestamp. Chosen dates, Leave as it is and accepted/kept re-check decisions are unchanged.
Postpone blocks Organize at preview and confirmation. A shared review
status and canonical old links keep badges, filters and counts stable across Analyze library.
Metadata distinguishes latest-run files from current all-folders review items/distinct files.

The offline UI uses numeric 24-hour dates everywhere, with browser-local offsets on hover for
instants; capture wall-clock dates are not shifted. It adds stay/next review actions, a Decision
column, sticky table headers, inline checkbox labels and explained empty lists. Analyze retains
three recent job cards, links to full analysis history, and logs hashed/extracted versus reused
counts. Logs offers present events by default, optional muted zero-count events for admins, Clear filters
and counted Export filtered / Export all buttons. CSV stays English, with `time_utc` in UTC
and `time_local` in the library timezone, numeric 24-hour format and the applicable offset.
Review items, Logs, Duplicates and Jobs remember 10/50/100/All per user and table in the browser;
All above 1,000 rows requires confirmation before loading.
Base users see only their own scoped jobs/files/decisions, never system/login events; admin roles
see all events with a User filter. No new environment parameter or runtime network access is added.
rc.11 keeps polling controls and focus intact, renders the theme into the initial HTML,
uses descriptive timestamped CSV filenames and adds the trial Security Log page
and sign-in banner. Security activity lives in the existing encrypted instance vault,
independently of the operational database. No existing user decisions are changed.

rc.12 renames the report to History & Undo, moves Diagnostics to the user menu, and unifies
Security Log for both roles. Administrative Settings opens General. Filters persist in URLs;
sign-in banners persist until dismissed for that sign-in. Saved Organize previews are reusable,
but changed inputs or old preview formats require recomputation before execution.
The logo version tooltip uses the UTC build timestamp stamped during image construction.
The existing global undo remains available; per-operation undo is tracked separately in #110.
No environment parameter or schema change is added.

## The master key

* **Generate:** `docker compose run --rm app generate-key`. It prints `APP_MASTER_KEY=<64 hex>`.
  Paste the line into `.env`. The `.env.example` ships with `APP_MASTER_KEY=CHANGE_ME` so that this
  command can run; the container refuses to start with the placeholder and prints how to generate a key.
* **Permissions:** `chmod 600 .env` (owner read/write only). Never commit `.env`.
* **Where it lives:** only in `.env` (or the secret file). Never in `/state`, never in the image,
  never in the repository.
* **Docker secret instead of `.env`:** put the key in a file (e.g. `docker/master_key.txt`,
  `chmod 600`), set `APP_MASTER_KEY=file` and `APP_MASTER_KEY_FILE=/run/secrets/twincheck_master_key`
  in `.env`, and uncomment the `secrets:` blocks in `docker-compose.yml`.
* **Missing key:** Compose stops with `APP_MASTER_KEY missing in .env`. **Wrong key:** the container
  logs `VAULT_DECRYPT_FAILED` and exits without modifying anything.
* **Rotation:** *Settings → Security → Rotate master key* (admin only). The app shows a new key
  once, asks you to paste it back, re-encrypts the vault (keeping `vault.enc.pre-rotation`, encrypted
  with the old key, for rollback), then you replace `APP_MASTER_KEY` in `.env` and restart. If the
  rotation fails the vault is restored and the old key stays valid.
* **Loss:** a lost key means the vault (accounts, roles, settings, MariaDB credentials, audit log)
  **cannot be recovered**: there is no back door. Keep it in a password manager. Recovery: stop the
  container, move `state/vault.enc*` away, start again, complete the setup with the new token and
  recreate the accounts. The operational database keeps all the data; the admin reassigns the old
  records to the recreated users in *Settings → Users → Owners without account*.

## Reset of the admin password (console only)

```sh
docker compose exec app reset-admin-password                 # asks the new password twice (hidden input)
docker compose exec app reset-admin-password --generate      # random password, shown once, must be changed at login
docker compose exec app reset-admin-password --user alice    # another account
```

It uses the key already in the container, takes the same lock as the running application (which
detects the change and reloads), changes only the password hash, closes all the sessions of that
user, clears login blocks, and writes `PASSWORD_RESET_CLI` only to the instance audit log -
never the password. It fails without modifying anything if the key is missing or wrong. There is no
web endpoint and no extra port for it. **Warning:** anyone who can run commands in the container
already controls the host.

## Port 80 without root: the sysctl

The server listens on port 80 inside the container while running as a non-root user without
capabilities. Linux normally reserves ports below 1024 for root, so the compose file sets

```yaml
sysctls:
  net.ipv4.ip_unprivileged_port_start: 0
```

This sysctl is *namespaced*: it changes only the container's own network namespace, not the host.
It is the only accommodation made. If your runtime refuses it (e.g. rootless Podman, some
restricted Kubernetes/NAS platforms):

* rootless Podman: remove the `sysctls:` block and set the same value on the host:
  `sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0` (persist it in `/etc/sysctl.d/`), or
* keep the sysctl out and set `APP_PORT=8080` in the `environment:` block and the port mapping to
  `"${BIND_ADDRESS:-0.0.0.0}:7777:8080"` (and adjust the healthcheck); this deviates from the
  standard layout and is not covered by the tests.

## Hardening in `docker-compose.yml`

| Option | Value | Why |
|--------|-------|-----|
| `user:` | `${PUID}:${PGID}` | non-root from PID 1; no `su`/`gosu`, no `chown` at runtime |
| `cap_drop` | `ALL` (no `cap_add`) | effective capabilities are empty |
| `security_opt` | `no-new-privileges:true` | no privilege escalation through setuid binaries |
| `read_only` | `true` | root filesystem read-only; Python writes no bytecode (`PYTHONDONTWRITEBYTECODE=1`) |
| `tmpfs` | `/tmp` (`noexec,nosuid,nodev`) | temporary files of ExifTool and ffmpeg |
| `sysctls` | `net.ipv4.ip_unprivileged_port_start=0` | bind port 80 without root (see above) |
| never | `privileged`, `network_mode: host`, `devices`, Docker socket | not needed, not allowed |

## Volumes

`DATA_PATH` is the host directory mounted as `/data`. For example, with
`DATA_PATH=/volume1/photo`, the UI path `pippo/source/vacanze.jpg` refers to
`/volume1/photo/pippo/source/vacanze.jpg` on the host and
`/data/pippo/source/vacanze.jpg` inside the container. All app roles see the relative
path, including administrators who do not manage Docker. Administrative CSV exports
add `path_container` for technical support; user exports do not.
Additional bind mounts below `/data` follow their own host-to-container mapping.

| Path | Content | Notes |
|------|---------|-------|
| `/data` | photo/video libraries | read/write. Each user's source **and** destination should be on the same mount so moves are atomic renames; otherwise files are copied, verified by SHA-256, and only then the original is removed (the header warns). Several host folders can be mounted below `/data` (e.g. `- /mnt/disk2/photos:/data/disk2`). |
| `/state` | only: `vault.enc` (+ `vault.enc.bak`, encrypted), the SQLite database if chosen (`*.sqlite3`, WAL), `exports/`, `imports/`, `locales/` (optional custom translations), `cache/` (previews, deletable) | `chmod 700` recommended |
| `/state/exports` | migration archives produced by *Export* (also downloadable from the browser) | |
| `/state/imports` | archives to import (upload from the browser or copy files here) | |
| `/state/locales` | extra or overriding translation files (`<code>.json`), no rebuild needed | |
| `/tmp` | tmpfs | |

Create the host folders before the first start and give them to `PUID:PGID`:
`mkdir -p library state && sudo chown 1000:1000 library state`.

## Setup token

At first start (until setup is completed) the container prints a one-time token:
`docker compose logs app | grep SETUP_TOKEN`. It is the only secret ever written to the logs, and it
becomes useless once the wizard is completed (the wizard is then locked forever).

## MariaDB (optional external database)

Configured in the web UI (setup wizard or *Settings → Database*): host, port (3306), database
name, user, password, with *Test connection*. Credentials are stored in the encrypted vault; the
password can come from a Docker secret instead (`MARIADB_PASSWORD_FILE`). Character set utf8mb4,
InnoDB; paths use a binary collation (case-sensitive).

Required privileges for the application user:

```sql
CREATE USER 'twincheck'@'%' IDENTIFIED BY '…';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP ON twincheck.* TO 'twincheck'@'%';
-- only if the application must create the database itself:
GRANT CREATE ON *.* TO 'twincheck'@'%';
```

`DROP` is used only to empty a destination left half-way by a failed migration, after your
confirmation. `GET_LOCK` (no privilege needed) serialises schema changes and guarantees that only one
container uses a database. Set `max_allowed_packet` to 16 MB or more for very long rows.

## Migration archive format (`.tcarc`)

Technology-independent export of the operational database (never credentials, never vault data):

```
"TCARC\0\1\n" | scrypt N (4 bytes) | salt (16) | nonce prefix (7)
then chunks of 1 MiB: length (4) | final flag (1) | AES-256-GCM(ciphertext + tag)
plaintext = gzip(NDJSON):
  {"t":"header","format":1,"app_version":…,"schema_version":…,"source_db_id":…,"source_type":…,"created_at":…,"tables":[{name,columns}]}
  {"t":"table","name":"files"}  {"t":"rows","d":[[…],[…]]}  …  {"t":"end","name":"files","rows":N,"sha256":"…"}
  …
  {"t":"manifest","format":1,"app_version":…,"schema_version":…,"source_db_id":…,"tables":[{name,rows,sha256}],"total_sha256":"…"}
```

The key is derived from the passphrase chosen at export time (scrypt). Every chunk is authenticated
and the last one is flagged, so tampering, reordering and truncation are detected. Dates are UTC ISO
8601, hashes lower-case hex, text full UTF-8, NULL is `null`.

## Healthcheck

`twincheck-healthcheck` runs `wget -q http://127.0.0.1:80/healthz` inside the container every 30 s
(timeout 5 s, 3 retries, 20 s start period). `/healthz` needs no login and returns no data.

## Build arguments

`ALPINE_VERSION` (default `3.24`) and `APP_VERSION` (default `1.0.0`), set in `.env`. Build with
`docker compose build --pull` to get the latest Alpine security updates.

## Backup

* **Key:** in your password manager (not with the backups of `/state`).
* **`/state`:** stop the container (`docker compose stop`) or use *Export* from the UI, then copy the
  folder. `vault.enc` is useless without the key, so it is safe to back up.
* **MariaDB:** `mariadb-dump --single-transaction twincheck > twincheck.sql`, or *Export* in the UI.
* **`/data`:** your photos - back them up before the first organisation run.
* Encryption at rest of the database and of the volumes is done at disk level (LUKS, encrypted NAS
  volume, encrypted cloud disk): the operational database contains paths and hashes, not credentials.

## Update

```sh
git pull
docker compose build --pull
docker compose up -d
```

If the new version has a newer database schema, the app asks the admin (after a backup warning) to
confirm the migration in *Settings → Database*. A database with a schema newer than the
application is refused.

## Plain HTTP and exposure

The application serves **HTTP only** (no TLS code, certificates or parameters): passwords and
session cookies travel unencrypted. Use it on trusted networks; restrict it with
`BIND_ADDRESS=127.0.0.1` and/or `ALLOWED_NETWORKS`. If you want HTTPS, put your own reverse proxy in
front of it (Caddy, Traefik, nginx) and set `TRUSTED_PROXIES` to the proxy address; the application
does not need to know.

## Published image and Portainer

Every version tag (`v1.0.0`, ...) pushed to GitHub runs `.github/workflows/release.yml`: it builds
the image for **linux/amd64 and linux/arm64**, starts it once with the full hardening and checks
`/healthz`, then publishes it to the GitHub Container Registry:

| Tag | Content |
|-----|---------|
| `ghcr.io/axreds/twincheck:1.0.0` | exact version (use this one on servers) |
| `ghcr.io/axreds/twincheck:1.0` | latest patch of 1.0 |
| `ghcr.io/axreds/twincheck:latest` | latest version tag |
| `ghcr.io/axreds/twincheck:edge` | current `main`, only when the workflow is started by hand |
| `ghcr.io/axreds/twincheck:1.0.1-rc.1` | exact pre-release candidate, for isolated manual testing |
| `ghcr.io/axreds/twincheck:rc` | most recently published pre-release candidate |

Pre-release tags such as `v1.0.1-rc.1` publish **only** the exact version and `rc`,
never `latest`, `1`, `1.0` or `1.0.0`. The smoke test does not print container logs
(which can contain the one-time setup token). Git tags are immutable: a failed
candidate is followed by a new candidate number, never by moving its tag.
GitHub candidate releases must be marked **Pre-release**, not stable.

The first time, open the package page on GitHub (*Packages → twincheck → Package settings*) and set
the visibility to **Public**, or add a GHCR registry with a read-only token in Portainer.

### Deploy with Portainer

1. On the Docker host create the two folders and give them to the container user:
   ```sh
   sudo mkdir -p /srv/twincheck/library /srv/twincheck/state
   sudo chown -R 1000:1000 /srv/twincheck        # same values as PUID / PGID
   ```
   `library` is where the photos are (or will be); `state` holds `vault.enc`, the SQLite
   database, exports and imports.
2. Generate the master key once (on the host, or in Portainer with a one-off container):
   ```sh
   docker run --rm ghcr.io/axreds/twincheck:1.0.0 generate-key
   ```
   Keep the printed `APP_MASTER_KEY=...` line in your password manager.
3. In Portainer: *Stacks → Add stack*, name `twincheck`, *Web editor*, paste the content of
   `docker/portainer-stack.yml`.
4. In *Environment variables → Advanced mode* paste at least:
   ```
   APP_MASTER_KEY=<the 64 hex characters>
   DATA_PATH=/srv/twincheck/library
   STATE_PATH=/srv/twincheck/state
   PUID=1000
   PGID=1000
   APP_VERSION=1.0.0
   HOST_LABEL=test-server
   ```
   Every other parameter of the table above can be added the same way.
5. *Deploy the stack*. Then open the container logs and look for `SETUP_TOKEN`: it is the one-time
   token for the setup wizard at `http://<host>:7777`.

Portainer stores the stack variables, including `APP_MASTER_KEY`, in its own database: protect the
Portainer instance accordingly, or use `APP_MASTER_KEY_FILE` with a Docker secret (see
"The master key"). If the host refuses the `sysctls` option, see "Port 80 without root".

**Update:** change `APP_VERSION` in the stack variables and choose *Update the stack* with
*Re-pull image*. The volumes are kept; a newer database schema is migrated only after the admin
confirms it in the UI.

## Container tests

```sh
bash docker/tests/run_container_tests.sh
```

Builds the image and checks, among others: non-root PID 1, empty `CapEff`, no privileged/socket/
devices/host network, read-only root, writes only in `/data` `/state` `/tmp`, ports 80/7777,
`BIND_ADDRESS`, `ALLOWED_NETWORKS`, wizard lock, cookie flags, `docker kill` during a scan and during
moves, `reset-admin-password` with the app running, then runs the whole Python suite inside the
hardened container with real ExifTool/ffmpeg and a throw-away MariaDB.
