# Application

Python backend (`backend/`), offline web frontend (`frontend/`), tests (`tests/`) and tools
(`tools/`). Deployment parameters are in [`../docker/README.md`](../docker/README.md) (container)
and [`backend/README.md`](backend/README.md) (all environment variables read by the code).

| Folder | Content |
|--------|---------|
| [`backend/`](backend/README.md) | package `twincheck`: web server, jobs, database layer, encryption |
| [`frontend/`](frontend/README.md) | HTML/JS/CSS served by the backend, logo, translations |
| [`frontend/locales/`](frontend/locales/README.md) | `<code>.json` translation files |
| [`tests/`](tests/README.md) | test-suite (`python3 -B -m unittest discover -s app/tests`) |
| `tools/check_locales.py` | translation checker (also used by CI) |
| `tools/make_pseudo_locale.py` | generates a long pseudo-language to test layouts |

## Where data lives

| Data | Where | Protection |
|------|-------|-----------|
| accounts, roles, groups, sources, password hashes (scrypt), per-user language, settings, MariaDB credentials, admin audit log | `/state/vault.enc` | AES-256-GCM, key from `APP_MASTER_KEY` (HKDF; scrypt for passphrases), decrypted only in memory |
| index, hashes, duplicates, jobs, checkpoints, execution log, move journal, manifest, metadata before/after, review decisions | operational database (SQLite in `/state` or MariaDB) | no credentials, no user names (only stable user ids); encrypt the disk/volume for encryption at rest |
| previews | `/state/cache/previews/<user id>/` | deletable at any time |

## Operational database schema (version 3)

Column types are portable: `key` = indexable text (VARCHAR(191) utf8mb4_bin on MariaDB), `hash` =
lower-case hex (ASCII binary collation), `ts` = UTC ISO 8601 text (`2024-03-15T10:00:00.000000Z`),
`path` = unlimited text with binary (case-sensitive) collation, `text` = JSON documents. Paths are
indexed through a companion `*_path_hash` column (SHA-256 of the path), so very long paths and paths
that differ only in case work identically on SQLite and MariaDB.

| Table | Purpose | Main columns |
|-------|---------|--------------|
| `schema_meta` | key/value metadata | `schema_version`, `db_id` (unique id of this database), `created_at`, `updated_at`, `provenance` (source db, date, app version after an import/migration), `migration_state` (present while a transfer is in progress: the database is unusable) |
| `instance_lock` | who is using the database (informative; the real lock is `flock`/`GET_LOCK`) | `instance_id`, `hostname`, `heartbeat_at` |
| `jobs` | every long operation | `kind`, `owner_id`, `status` (queued, running, paused, interrupted, completed, failed, cancelled), `params`, `progress_done/total`, `phase`, `checkpoint` (JSON), `result`, `error`, `parent_job_id`, `auto_resume` |
| `run_log` | execution log, English only | `ts`, `level`, `event` (stable code, e.g. `FILE_MOVED`), `message` (English), `params` (JSON), `job_id`, `owner_id`, `actor` (user id) |
| `files` | global index, one row per file | `owner_id`, `source_id`, `orig_path`, `cur_path` (+ hashes), `size`, `mtime_ns`, `sha256`, `media_kind`, `status` (indexed, unique, duplicate, dup_moved, review, normalized, moved), `dup_group_id`, `dup_of`, `chosen_date` (+ source, flags), `meta_before`, `meta_after`, `meta_read`, per-phase state `phase_scan/hash/meta/org`, `present`, `duration_s` |
| `dup_groups` | groups of identical files | `scope` (`user:<id>` or `global`), `sha256`, `size`, `keeper_file_id`, `members`, `verified` (byte comparison), `status` |
| `plan_items` | dry-run plans (moves, metadata writes, undo), executed only after confirmation | `plan_job_id`, `kind`, `seq`, `file_id`, `src`, `dst`, `payload` (companions, fills), `status`, `note` |
| `move_journal` | intent/completion of every move | `op` (move, dup_move, copy_verify_delete), `src`, `dst`, `sha256`, `size`, `state` (intent, done, failed, rolled_back) |
| `manifest` | what was changed, written **before** each change; source of undo and of the CSV export | `action` (move, dup_move, metadata, restore), `orig_path`, `new_path`, `sha256_before/after`, `meta_before/after`, `undone_at` |
| `meta_changes` | original and new value of each written tag | `tag`, `before_value`, `after_value`, `source`, `undone_at` |
| `review_items` | review queue and decisions | `kind` (meta_conflict, no_date, date_utc, move_conflict, verify_mismatch, reconcile_*), `details`, `proposals`, `status` (pending, saved, skipped, kept), `decision`, `group_key` |

Every record carries `owner_id` (stable user id from the vault): index, manifest, logs, previews and
review queues are separated per user, and every API checks it server-side.

## Migrations

`backend/twincheck/db/schema.py` defines the schema as an ordered list of migrations (`MIGRATIONS`):
version 1 creates the baseline, version 2 adds `files.duration_s`, `review_items.group_key` and an
index, version 3 adds `jobs.auto_resume` and an index on `run_log.event`. Every operation is
idempotent (a table, column or index is created only if missing).

On **every** connection (first start, restart, database switch) the application:

1. takes the schema lock (`BEGIN IMMEDIATE` on SQLite, `GET_LOCK` on MariaDB), so two simultaneous
   starts never corrupt anything;
2. creates the structure if absent (and on MariaDB creates the database if the user has `CREATE`;
   otherwise it shows the privileges needed);
3. reuses an existing structure without touching data;
4. if the schema is **older**, refuses to use it until the admin confirms the migration (after a
   backup warning) in *Administration → Database*; then applies the missing steps in order;
5. if the schema is **newer** than the application, refuses to use it and explains why;
6. takes the instance lock: only one container writes to a database (`flock` on
   `<db>.instance-lock` for SQLite, `GET_LOCK` on a dedicated connection for MariaDB; both vanish
   with the process, so a killed container restarts immediately).

To add a migration: append `MIGRATIONS[n+1]`, bump `CURRENT_VERSION`, add a test building version
`n` and migrating it.

## Database switch (admin and administrator)

*Administration → Database*:

1. enter the new parameters, **Test connection**: shows whether the structure is absent (it will be
   created) or present, with `db_id`, schema version, indexed files, open jobs, last update;
2. **Switch database**, confirm the summary (what changes; the old database is neither modified nor
   deleted); choose how to compare with the disk (path/size/mtime, + sampled SHA-256, + full SHA-256);
3. running jobs are paused on a checkpoint in the current database; no job starts during the switch;
4. the new database is prepared and locked; only then the configuration in the vault is changed; if
   anything fails you stay on the previous database (`DB_SWITCH_FAILED`), and paused jobs resume;
5. a **reconciliation** job compares the stored state with the files on disk; differences (missing,
   modified, new, moved) go to the review queue and are never corrected automatically;
6. jobs restart from what is stored in the new database (read-only phases automatically, writing
   phases after your click); an empty database starts from zero and needs a scan;
7. the operation is recorded in the admin audit log and in the new database's log. Previous
   databases are listed for a quick return.

## Export, import and migration (admin only)

*Administration → Export & migration*. Format: see [`../docker/README.md`](../docker/README.md#migration-archive-format-tcarc).

* **Export:** choose a passphrase (≥ 12 characters), *Preview* (tables, rows, estimated size and
  time, warnings), confirm. The app enters maintenance mode (no job runs, writes blocked), reads a
  consistent snapshot through a dedicated read-only connection, writes
  `/state/exports/twincheck-export-<db_id>-<time>.tcarc` (downloadable, streamed), then leaves
  maintenance. The source database is only read.
* **Import:** upload the archive from the browser (streamed to `/state/imports`) or copy it there,
  choose the destination database (must be **empty**; the structure is created automatically),
  *Preview*, confirm. The whole archive is verified first (passphrase, authenticity, checksums,
  version: archives of a newer application are refused). Rows are copied in blocks with a checkpoint
  per table stored in the destination, which stays marked "migration in progress" until the end.
* **Migrate:** direct copy from the current database to an empty destination, same checks, no file.
  Rows created during the migration (its own job and log) are excluded so checksums are exact.
* **Verification:** row counts and SHA-256 per table, referential integrity and a random sample are
  compared; a report is stored with the job. The destination records its provenance (source `db_id`,
  date, app version), receives a new `db_id`, jobs that were "running" in the source become
  "interrupted", and the schema is then upgraded to the current version (old-version archives).
* **Interruption:** paused/interrupted transfers resume from the checkpoints
  (*Resume* asks the passphrase again). A failed destination can be emptied only after explicit
  confirmation (*Empty destination*).
* At the end you can **switch** to the new database with the procedure above.

Everything is recorded in the admin audit log and in the logs of the databases involved.

## Undo

*Report & undo* builds a preview of the reversal of moves or metadata writes from the manifest of the
**current** database, then executes it after confirmation (never overwriting: an occupied original
location blocks that item). Operations done with another database can only be undone after switching
back to it (or after migrating its data).
