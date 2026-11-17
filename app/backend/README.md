# Backend

Python 3.12+ package `twincheck` (standard library + `cryptography`, `uvicorn`/`h11`, `PyMySQL`).
The HTTP layer is a minimal ASGI toolkit written with the standard library only
(`twincheck/web/framework.py`); `uvicorn` serves it.

```
twincheck/
  cli.py          entry point: serve | generate-key | reset-admin-password | healthcheck
  config.py       environment variables (ENV_PARAMS: the single source of truth)
  core.py         application state: vault, current database, jobs, sessions, maintenance
  crypto.py       master key (HKDF / scrypt), AES-256-GCM blobs and chunked archive streams
  vault.py        encrypted vault /state/vault.enc (atomic writes, flock, backup, rotation)
  passwords.py    scrypt password hashes and strength policy
  events.py       English-only log catalogue (stable event codes)
  i18n.py         locale discovery (+ /state/locales overrides), console texts (APP_LANG)
  fsutil.py       confinement, symlink refusal, hashing, mounts, statvfs
  mover.py        journalled, never-overwriting moves; crash recovery
  jobs.py         persistent jobs, checkpoints, pause/resume, scheduler
  sysinfo.py      header metrics from /proc, /sys/fs/cgroup, statvfs
  previews.py     temporary H.264/JPEG previews (ffmpeg) in /state/cache
  exif.py         ExifTool wrapper (create-only writes, -P keeps mtime)
  db/             engine (SQLite/MariaDB), versioned schema, manager (prepare, locks), transfer
  services/       scan, dedupe, metadata, moves, undo, verify/reconcile, access (roles, sources)
  web/            ASGI app, security (sessions, CSRF, throttling, networks), routes
```

## Running without Docker (development)

```sh
cd app/backend
export APP_MASTER_KEY=$(python3 -m twincheck generate-key | sed -n 's/^APP_MASTER_KEY=//p')
export DATA_DIR=$PWD/../../library STATE_DIR=$PWD/../../state APP_PORT=8080
PYTHONDONTWRITEBYTECODE=1 python3 -m twincheck serve
```

## Environment variables

The table below is checked by `app/tests/test_docs_consistency.py` against `twincheck/config.py`
(`ENV_PARAMS`): every variable read by the application is documented here and every documented
variable exists.

<!-- params:start -->
| Name | Description | Default | Required | Example |
|------|-------------|---------|----------|---------|
| `APP_MASTER_KEY` | Master key: 64 hex chars (32 bytes), base64 of 32 bytes, or a passphrase ≥ 20 characters; `file` = read `APP_MASTER_KEY_FILE`. Encryption key = HKDF-SHA256 (scrypt first for passphrases). | none | yes (or the file) | `3f9c…` |
| `APP_MASTER_KEY_FILE` | File holding the key (Docker secret). | empty | no | `/run/secrets/twincheck_master_key` |
| `APP_LANG` | Console language and initial instance language. | `en` | no | `it` |
| `HOST_LABEL` | Host name shown in the header. | container host name | no | `nas` |
| `ALLOWED_NETWORKS` | Allowed client CIDRs, comma-separated (loopback always allowed). | empty = all | no | `192.168.1.0/24` |
| `TRUSTED_PROXIES` | Proxies whose `X-Forwarded-For` is trusted for login throttling. | empty | no | `172.18.0.10/32` |
| `DISK_WARN_FREE_PERCENT` | Warning threshold of free disk space (%). | `15` | no | `20` |
| `DISK_CRIT_FREE_PERCENT` | Critical threshold of free disk space (%). | `5` | no | `3` |
| `DATA_DIR` | Libraries mount point. | `/data` | no | `/srv/photos` |
| `STATE_DIR` | State mount point. | `/state` | no | `/srv/twincheck-state` |
| `APP_PORT` | Internal HTTP port. | `80` | no | `8080` |
| `MAX_REQUEST_KB` | Max normal request body (KiB). | `1024` | no | `2048` |
| `MAX_UPLOAD_MB` | Max archive upload (MiB). | `102400` | no | `20480` |
| `MARIADB_PASSWORD_FILE` | MariaDB password file (overrides the vault). | empty | no | `/run/secrets/db_pw` |
| `JOB_WORKERS` | Concurrent background jobs. | `2` | no | `4` |
| `PREVIEW_CACHE_MAX_MB` | Preview cache size limit (MiB). | `2048` | no | `4096` |
| `LOG_LEVEL` | Log level. | `INFO` | no | `DEBUG` |
| `EXIFTOOL_PATH` | ExifTool executable. | `exiftool` | no | `/usr/bin/exiftool` |
| `FFMPEG_PATH` | ffmpeg executable (ffprobe is looked up next to it). | `ffmpeg` | no | `/usr/bin/ffmpeg` |
<!-- params:end -->

## Security model in the code

* Every request passes `web/app.py::ASGIApp.dispatch`: `ALLOWED_NETWORKS` on the peer address, body
  size limit, session lookup (sessions are in memory, tokens stored hashed; expiry by age and idle
  time; invalidated by `session_epoch`), CSRF (same origin + `X-CSRF-Token` bound to the session),
  role check (`services/access.py::PERMISSIONS`), password-change enforcement, maintenance mode.
* Files and previews are served only by `/api/files/<id>/…`, after checking the owner **and** that
  the real path (symlinks resolved) lies inside one of the user's areas.
* Logs never contain secrets; the only secret ever printed is the setup token.
* Nothing contacts the Internet.
