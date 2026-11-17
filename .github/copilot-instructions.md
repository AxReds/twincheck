# Copilot instructions for TwinCheck

These instructions are read automatically by GitHub Copilot in this repository.
They describe what TwinCheck is, the rules that must never be broken and how to
work on the code. Read `README.md`, `app/README.md` and `docker/README.md` for
the full details before proposing larger changes.

## What TwinCheck is

A containerised, multi-user web application that screens photo and video
libraries, sets **exact** duplicates aside and reorganises files into
`YYYY/MM/DD` folders safely. Everything is done from the web UI; the console is
only used to generate the master key and to reset the admin password.

## Non-negotiable rules

Safety of the user's files
- Files are **moved, never copied or deleted**. Duplicates go to
  `<destination>/Duplicates/<original relative path>`, files without a reliable
  date to `No_date/` (both folder names are configurable in Settings).
- **Never overwrite** an existing file or an existing metadata value.
- Every write (metadata, duplicates, organisation) is a **plan (dry-run)
  first** and runs only after an explicit confirmation in the UI.
- Every move goes through the move journal (`mover.py`) and can be undone.
- Duplicates are detected **only** by size + SHA-256 + byte-by-byte comparison:
  never by visual similarity, file names, dates or EXIF.
- Metadata is completed **only from reliable sources**; nothing is invented.
  QuickTime dates are UTC and are not converted to local time silently.

Security and container
- Single Alpine container, single main process (uvicorn + the internal
  mini-ASGI layer in `web/framework.py`), long jobs in background threads with
  checkpoints stored in the database so they resume after interruptions.
- Non-root from PID 1 (`user:` in compose), `cap_drop: ALL`, no `cap_add`,
  `no-new-privileges`, read-only root filesystem, writes only to `/data`,
  `/state` and a `tmpfs` on `/tmp`. Never add `su`/`gosu`, runtime `chown`,
  `privileged`, host networking, the Docker socket or devices.
- Port 80 inside the container, published on 7777
  (`${BIND_ADDRESS:-0.0.0.0}:7777:80`) thanks to the
  `net.ipv4.ip_unprivileged_port_start=0` sysctl.
- The application **never calls the Internet**; the frontend has no CDN and
  works offline.
- All access data and configuration live in the encrypted vault
  `/state/vault.enc` (AES-256-GCM, key from `APP_MASTER_KEY` or
  `APP_MASTER_KEY_FILE`). Never log or return secrets.
- No compilers in the final image; prefer pure-Python libraries (e.g. PyMySQL).

Repository
- **English only** everywhere (code, comments, docs, logs, commit messages).
  The only exception is the translation files in `app/frontend/locales/`.
- Never commit photos, videos, databases, archives, keys, tokens, passwords or
  real `.env` files. Only `.env.example` with placeholder values.
- Line endings are LF (`.gitattributes`); shell scripts break with CRLF.
- License: GPL-3.0.

## Code map

- `app/backend/twincheck/`: backend package
  - `config.py`: `ENV_PARAMS`, the single list of environment parameters
  - `vault.py`, `crypto.py`, `passwords.py` (scrypt): encrypted vault and keys
  - `db/`: engine (SQLite or MariaDB), schema + migrations, database switch,
    export/import/migration archives (`transfer.py`)
  - `jobs.py`: background jobs with checkpoints
  - `mover.py`: journaled moves (link/unlink, copy-verify-delete across volumes)
  - `services/`: scan, dedupe, metadata, moves, companions (sidecars), undo,
    verify, access (roles and per-user sources)
  - `web/`: routes (`routes_tool.py`, `routes_admin.py`), security, framework
  - `events.py`: log events, always in English
  - `cli.py`: `generate-key` and `reset-admin-password`
- `app/frontend/static/`: offline HTML/JS/CSS UI; `app/frontend/locales/*.json`
  translations (`en.json` is the reference).
- `app/tools/check_locales.py`: translation checker (also run in CI).
- `app/tests/`: `unittest` suite; `fake_exiftool.py` is used when ExifTool is
  not installed.
- `docker/`: Dockerfile, compose file, entrypoint and healthcheck scripts,
  container acceptance tests (`docker/tests/run_container_tests.sh`).
- `.github/workflows/ci.yml`: CI (translations, tests on SQLite + MariaDB,
  container tests).

## How to work

- Run the tests from the repository root before every commit:
  `python -B -m unittest discover -s app/tests`
  (inside the project virtualenv: `source .venv/bin/activate`).
- MariaDB tests run only when `TWINCHECK_TEST_MARIADB_HOST` is set; see
  `app/tests/README.md`.
- When you add or change an environment parameter, update
  `config.ENV_PARAMS`, `app/backend/README.md`, `docker/README.md` and the
  compose file together: `test_docs_consistency.py` fails otherwise.
- UI text must come from the locale files, never hard-coded; add new keys to
  `en.json` and run `python3 -B app/tools/check_locales.py`.
- Database changes need a migration in `db/schema.py` and a test.
- Remember that a real ExifTool **rewrites the file bytes** when it writes tags;
  the fake one does not. Test fixtures must not rely on the fake behaviour.
- Commit messages follow Conventional Commits (`feat:`, `fix:`, `test:`,
  `docs:`, `chore:`, `build:`), one logical change per commit.
