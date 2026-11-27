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
| accounts, roles, groups, sources, password hashes (scrypt), per-user language/theme, settings, MariaDB credentials, admin audit log | `/state/vault.enc` | AES-256-GCM, key from `APP_MASTER_KEY` (HKDF; scrypt for passphrases), decrypted only in memory |
| index, hashes, duplicates, jobs, checkpoints, execution log, move journal, manifest, metadata before/after, review decisions | operational database (SQLite in `/state` or MariaDB) | no credentials, no user names (only stable user ids); encrypt the disk/volume for encryption at rest |
| generated/transcoded previews | `/state/cache/previews/<user id>/` | deletable at any time; scope-checked before use |

Browser-compatible originals are served directly, without creating a disk-cache entry.
Only generated JPEG/H.264 previews use the directory above, bounded by `PREVIEW_CACHE_MAX_MB`
and invalidated by source identity/size/mtime. The browser also keeps preview promises and
blob ObjectURLs in memory and prefetches the next item; these are not files in `/state`.
Codec probing is cached in process memory. ExifTool/ffmpeg working files use `/tmp`.
Therefore `/state/cache` can intentionally stay empty after viewing many compatible originals;
this is not evidence that preview caching failed.

## Library analysis pipeline

`analyze_library` is a read-only, auto-resuming job started from the Analyze page. It runs the
existing components in this order:

1. scan the selected source;
2. hash changed or new files by size + SHA-256, reusing unchanged hashes;
3. read capture dates with ExifTool, reusing valid stored metadata for unchanged files;
4. build exact duplicate groups by byte comparison and choose the keeper only after every candidate
   has capture-date data or a confirmed unknown result;
5. compute metadata fills, conflicts and review items.

The job stores a separate checkpoint for every completed phase/batch. After interruption it resumes
from the last checkpoint; it never moves, writes metadata or organizes files. Those actions remain
separate plan-and-confirm jobs and revalidate the file immediately before execution.

The log records one `ANALYSIS_PHASE_STARTED` and one `ANALYSIS_PHASE_COMPLETED` per phase,
including on resume. Completion parameters contain scan found/new/changed/unchanged/removed,
hash candidates/hashed/unreadable, capture dates read/unknown, duplicate groups/files and
metadata analysed/proposed-completion/review counts (`fills` remains the API/checkpoint key). It also records
`ANALYSIS_RESUMED` (phase and checkpoint position). These are English-only operational event codes.
The UI displays the phase name, `n/5`, and one overall progress value.

Base-user pipeline results and all stored historical results are filtered through the current
assigned source/destination paths, not only `owner_id`. The same check is applied while reading
duplicates, metadata, reviews, plans, logs, exports, previews, report data and cache entries.

## Times and CSV

The admin-only **Library timezone** setting in Administration → Settings is stored in the
encrypted vault. It accepts an IANA name, defaults to `TZ` when set, otherwise `UTC` with a
visible recommendation to configure it; it is optional (example: `Europe/Rome`).
Organizing dates are local wall-clock values in that timezone. File-system mtime proposals
are converted from UTC; offset EXIF values are converted for organizing only, preserving
the original metadata; timezone-free EXIF and manual choices are never shifted.
Each new review decision stores its source and applied timezone, including per-field
provenance. Editing unrelated fields preserves the selected date's provenance; editing
the date itself records a manual choice. Schema migration 4 adds these columns to version 1–3 databases and flags
unapplied ambiguous historical dates as **needs re-check**, without changing their values.
Mtime-matching choices are always flagged, even if they look manual. Unambiguous metadata
choices and already organized/applied decisions remain untouched. Organize skips flagged
files (including at confirmation) until the owner explicitly confirms or changes the date.
One `DECISIONS_FLAGGED_FOR_RECHECK` event summarizes the migration.

| Vault setting | Description | Default | Required | Example |
|---------------|-------------|---------|----------|---------|
| `library_timezone` / Library timezone | IANA timezone for organizing dates; admin-only, recorded as `SETTING_CHANGED`. | `TZ`, otherwise `UTC` with an admin notice | no | `Europe/Rome` |
| `show_version_on_login` / Show version on login page | Admin-only ON/OFF footer; OFF omits the version from public bootstrap. Same `VERSION` source as the authenticated header. | `true` (ON) | no | `false` (OFF) |

These values live in the encrypted vault, not the operational database. Only `TZ` is an
environment parameter; the login-version switch is intentionally not one.

Schema migration 5 separates `files.file_changed_at`, inode and device from ordinary
record updates. The stale-analysis check compares current scoped paths, sizes, mtimes
and inode/device against the index, including new and removed files. Saving a review
decision or logging a per-file read warning does not invalidate a completed analysis.
Only a newer paused/interrupted analysis in the exact accessible scope makes it incomplete;
an older paused job cannot invalidate a later completed run. Duplicates and Metadata display
the latest accessible library run, not later standalone compatibility jobs. Review totals are
computed live across status filters: total = pending + decided, and needs re-check is a subset
of pending. Automatic-fill and manually saved-field write previews are offered only when
there are actual values to write.

Operational instants are stored and serialized as ISO 8601 UTC values ending in `Z`. The browser
renders job, log, file-system, report and manifest instants in its local timezone. EXIF capture
dates without a timezone are local wall-clock values and are rendered without conversion. A capture
offset, when present, is returned and displayed with the date.

Duplicate CSV columns are `group_id, sha256, size, role, file_id, path, exif_date,
filesystem_date`. `filesystem_date` is UTC ISO 8601 with `Z`; `exif_date` remains as stored and
contains an explicit offset only when the file supplied one. Other CSV exports use the same rule
for instant columns.

Log CSV columns are `time_utc, time_local, level, event, message, job_id, user`.
All roles see library-relative paths in the UI and the `path`/message columns, independent
of source labels. Administrative CSV exports additionally include `path_container`:
one container path for duplicates, a JSON array of event paths for Logs (empty when absent).
Base-user CSV never includes container paths. Events without an owner are labelled `system`.
Exports use descriptive `twincheck-logs-filtered[-<user>][-job<N>]-YYYYMMDD-HHMM.csv`,
`twincheck-logs-all-YYYYMMDD-HHMM.csv`, `twincheck-duplicates-own|global-YYYYMMDD-HHMM.csv`
and `twincheck-manifest-YYYYMMDD-HHMM.csv` filenames, in the library timezone (24-hour).
`time_utc` is ISO 8601 UTC ending in `Z`. `time_local` is numeric
`YYYY-MM-DD hh:mm:ss±hh:mm` (24-hour) in the configured library timezone, with the
offset for that instant, including daylight saving. Messages remain English.
`scope=filtered` applies the current Event code, Level, Job and administrative User filters;
`scope=all` ignores those filters, but never bypasses role or source visibility.
The list returns both filtered `total` and accessible `all_total`, using the same scope as CSV.

Review items, Logs, Duplicates and Jobs accept `limit=10|50|100|all` and `offset`.
The UI defaults to 50 and remembers a separate choice for each user id and table in
browser local storage (there is no generic server-side preferences table).
Choosing All for more than 1,000 rows requires confirmation before the full request,
including on a later visit. Other paginated endpoints retain their existing bounds.

Metadata and Review items share a live population summary from the same canonical review
rows as their lists. Queue items satisfy `total = pending + decided`; several reasons can
refer to one file. Current files satisfy `current = reliable + review_files + other`:
`reliable` counts dated files outside the review queue, `review_files` counts distinct files
with review items, and `other` is the remainder. Latest-analysis counts describe the selected
folder at analysis time; current counts cover all assigned folders and exclude missing or
moved-duplicate files. Unreadable analysis files can also have review items, so that diagnostic
count is not an additional disjoint category. No stored decisions are changed.

## Concurrent job submission

Job submission first checks the active (`queued`/`running`) rows for the same owner, kind and exact
scope parameters. SQLite serialises the check/insert with `BEGIN IMMEDIATE`; MariaDB uses a
per-submission advisory lock. A matching active job returns its existing id and is not inserted
again. Completed/cancelled/failed jobs do not block a new run, and a different source or user remains
independent.

## Review, UI and logging

`services/review_state.py` is the shared source for Pending, skipped, Decided and needs re-check
in item badges, filters, counts, Decision labels and Organize. Re-analysis preserves item ids and
all explicit saved/kept/re-check choices. Historical duplicate metadata queue rows resolve to
one canonical active record even through old links; stored history is not deleted.
Skip stores `skipped_at`, leaves status pending, emits `REVIEW_SKIPPED` and blocks Organize both
when previewing and when confirming an older plan. Skipped and needs re-check are subsets of
pending, never decided. Explicit Save/Accept/Keep can stay on the item or use the corresponding
next action. Newly confirmed re-checks store the explicit accepted/stored choice in decision JSON;
older genuine choices are never inferred or rewritten.

Metadata labels two different populations: latest selected-folder analysed files and current
all-assigned-folders review items/distinct files. Missing and moved-duplicate files do not appear
in current review totals. Multiple review reasons are labelled as items, not files.
The full organizing-date help is kept only in the item panel.

Every displayed date uses numeric `yyyy-mm-dd hh:mm:ss`, independent of language. Instants
are browser-local with offset/timezone on hover; timezone-free capture values remain unchanged.
Settings exposes every IANA zone available in the container in one always-visible, accessible
combo box with current UTC offsets and daylight-saving help. Invalid free text disables Save;
keyboard, pointer and touch selections share server validation. Tables use bounded scroll
containers with sticky headers and explained empty states; checkbox fields place checkbox
and label together with help below. Per-table rows-per-page preferences live in browser storage.

Logs uses the same combo box for the `events.EVENTS` catalogue. Only events present defaults
on; when disabled, zero-count events are muted after a separator. Counts respect the other
applied filters and role/User scope. Clear filters and an explained no-matches state remain.
`/api/logs.csv?scope=filtered|all` shares visibility with list/counts and includes the time
columns documented above. Counted export controls explain the applied scope.
Base users see only their own current-scope job/file/review events; system/authentication
events, mismatched job/file ownership and out-of-scope paths are excluded. The admin User
filter uses exactly that same visibility rule, including legacy scan jobs whose unused
destination was the data root and job-owned events without a stored event owner. Admin roles see
all users plus system events and get a User filter. Analyze shows only the latest three
analysis jobs with a full-history link; completion messages spell out hashed/extracted versus
reused values and unreadable/unknown counts, rather than describing reuse as zero items.

rc.10 uses schema 6 unchanged: no schema migration or `DB_MIGRATED` is needed from rc.9.
Before upgrading an older schema, stop the container and back up `/state` and MariaDB if applicable.
After upgrading an older schema, an administrator must run **Administration → Database → Migrate schema**.
Every signed-in user sees the database-required banner while unavailable/outdated; admin roles
can follow it directly to the migration action. Home uses the same explanation.

## Operational database schema (version 6)

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
| `files` | global index, one row per file | `owner_id`, `source_id`, `orig_path`, `cur_path` (+ hashes), `size`, `mtime_ns`, `inode`, `device`, `file_changed_at`, `sha256`, `media_kind`, `status` (indexed, unique, duplicate, dup_moved, review, normalized, moved), `dup_group_id`, `dup_of`, `chosen_date` (+ source, flags), `meta_before`, `meta_after`, `meta_read`, per-phase state `phase_scan/hash/meta/org`, `present`, `duration_s` |
| `dup_groups` | groups of identical files | `scope` (`user:<id>` or `global`), `sha256`, `size`, `keeper_file_id`, `members`, `verified` (byte comparison), `status` |
| `plan_items` | dry-run plans (moves, metadata writes, undo), executed only after confirmation | `plan_job_id`, `kind`, `seq`, `file_id`, `src`, `dst`, `payload` (companions, fills), `status`, `note` |
| `move_journal` | intent/completion of every move | `op` (move, dup_move, copy_verify_delete), `src`, `dst`, `sha256`, `size`, `state` (intent, done, failed, rolled_back) |
| `manifest` | what was changed, written **before** each change; source of undo and of the CSV export | `action` (move, dup_move, metadata, restore), `orig_path`, `new_path`, `sha256_before/after`, `meta_before/after`, `undone_at` |
| `meta_changes` | original and new value of each written tag | `tag`, `before_value`, `after_value`, `source`, `undone_at` |
| `review_items` | review queue and decisions | `kind` (meta_conflict, no_date, date_utc, move_conflict, verify_mismatch, reconcile_*), `details`, `proposals`, `status` (pending, needs_recheck, saved, kept; legacy skipped), `skipped_at`, `decision`, `decision_source`, `decision_timezone`, `decision_provenance` (per-field JSON), `recheck_reason`, `group_key`, `applied` |

Every record carries `owner_id` (stable user id from the vault): index, manifest, logs, previews and
review queues are separated per user, and every API checks it server-side.

## Migrations

`backend/twincheck/db/schema.py` defines the schema as an ordered list of migrations (`MIGRATIONS`):
version 1 creates the baseline, version 2 adds `files.duration_s`, `review_items.group_key` and an
index, version 3 adds `jobs.auto_resume` and an index on `run_log.event`;
version 4 adds decision provenance and flags unapplied ambiguous historical dates without
changing stored values; version 5 adds technical file-change tracking; version 6 adds `skipped_at`
and returns only unapplied skip-only records (no chosen date, nonempty fields or re-check reason)
to pending. The old timestamp is preserved, genuine decisions are untouched, and one
`REVIEW_SKIPS_RETURNED_TO_PENDING` event reports the converted count. Confirmed upgrades log
one `DB_MIGRATED` with numeric versions; converted/flagged counts are echoed at INFO and audited.
Every structural operation is
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
