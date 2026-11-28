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
* **Exact duplicates** (same size + SHA-256 + byte comparison), shared internal capture date
  then tidiest path (otherwise oldest mtime then tidiest path), the others moved to
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

See the [TwinCheck roadmap](docs/ROADMAP.md) for planned improvements.
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

## Is TwinCheck running?

You do not need to manage Docker or the server to check. In a browser, open
**http://<server>:7777/health**, replacing `<server>` with the same server name
or address you use for TwinCheck. A healthy answer is `{"status": "ok"}`.
This means the web application is responding; it does not verify the library or database.

If it does not answer, wait a minute after an update, reload, and try another browser.
If it still fails, report to whoever manages the server:
the time (including timezone), what you were doing, the `/health` result
(or that it did not answer), and the exact error shown by the browser.
Do not send passwords, keys or photos.

On a server with **Portainer** you do not need the source code: use the published image
`ghcr.io/axreds/twincheck` and the stack file `docker/portainer-stack.yml`, as described in
[docker/README.md](docker/README.md#deploy-with-portainer).

## Step-by-step usage

1. **Generate the master key** - `docker compose run --rm app generate-key`, paste the line into `.env`
   (`chmod 600 .env`) and store the key in a password manager.
2. **Setup** - open the web UI, enter the setup token from the log, choose the database (SQLite, or
   MariaDB with *Test connection*: the structure is created automatically), set the password of the
   fixed `admin` account, confirm. The wizard then locks forever.
3. **Users and roles** - *Settings → Users*: create base users with their source folders under
   `/data` (and optional destination), group them, create administrators (admin only). New users must
   change their temporary password at first sign-in.
   The fixed admin can assign his own source using *Settings → Users → admin →
   Edit → Sources*. Settings is available only in the administrative user menu,
   above Change Password; empty Analyze and Organize pages link to source assignment
   for admin roles. Base users get an instruction to ask an administrator instead.
4. **Analyze** - on *Analyze*, pick a source and start *Analyze library*. This is the single
   entry point for reading the library: one resumable,
   read-only job performs scan, SHA-256 hashing, capture-date reading, duplicate grouping and
   metadata proposals in order. Unchanged files reuse their stored hash and capture date. Separate
   analysis buttons are intentionally absent from the review pages; compatibility APIs remain for
   automation and older clients.
5. **Check: Duplicates and Metadata** - these are review-and-action pages. They show only the latest
   accessible library analysis, with one status notice and *History in Jobs*.
   *Update analysis* leads back to Analyze; a stale notice explains file changes or an incomplete
   analysis, never a database-only review edit. Review duplicate
   results, including EXIF and file-system dates (change the
   keeper if needed), *Preview move*, confirm.
   Duplicates go to `<destination>/Duplicates/<original relative path>`.
   A capture date read from any copy's internal metadata applies to all byte-identical
   copies for the keeper decision; unread metadata is not treated as an older date.
   Without an internal capture date the oldest mtime wins, then the tidiest path
   (fewest components, shortest length, lexicographic order). The group shows the
   effective date and rule. Metadata is not written by duplicate analysis.
6. **Metadata** - one tab contains automatic completions and the review queue. One summary shows
   analysed/unreadable/dated/review counts, with technical population details in its information tooltip.
   Filters and the list follow directly; **Start review** opens the first item to review.
   Filter All / To review / Decided, then filter by reason.
   Preview automatic completions only when fills exist, then confirm to write (original values
   are saved first). Zero fills is explained and does not mean there are no files to review.
   The latest selected-folder analysis counts files; the review queue spans all assigned folders
   and explains item counts versus distinct current-file counts in the tooltip. Missing files and superseded
   metadata queue rows are excluded without deleting their history.
7. **Review within Metadata** - for each file: media on one side, every field on the other; save, skip, leave as it
   is, or apply to the whole folder/group. Decisions are stored immediately; *Preview writing*
   writes the values you entered (only into missing fields).
   Choosing a date for organizing clears unsaved capture/creation date edits; that choice
   is stored only in the database and does not itself write date tags.
   **Postpone** leaves the item To review with a grey timestamped note, emits `REVIEW_SKIPPED`
   and is not organized. A shared status keeps badges, filters, counts and Organize consistent
   across re-analysis and old item links; saved/kept/re-check choices are never silently changed.
   **Save**, **Accept** and **Keep stored date** stay on the item and display the result;
   the corresponding **and next** controls advance explicitly. The Decided list explains each
   decision. Save controls stay visible while scrolling; previews and review items have back links.
8. **Organize** - preview the `Year/Month/Day` structure, confirm the move. Dates use the configured
   library timezone. Ambiguous historical decisions stay unchanged in the needs re-check queue:
   accept the converted proposal, keep the stored date or choose another. Organize skips them until
   explicit confirmation, including when executing a previously prepared plan.
9. **History & Undo** - preview and execute the reversal of moves and/or metadata, read from
   the manifest; *Verify* checks files and hashes against index and manifest; download the manifest CSV.
10. **Change database** - *Settings → Database*: test, switch (jobs paused, old database
    untouched, reconciliation with the disk, rollback on failure), or go back to a previous one.
11. **Export and migration** - *Settings → Export & Migration* (admin): encrypted archive,
    import into an empty database, or direct migration; preview, checkpoints, final verification,
    then optionally switch to the new database.
12. **Admin password reset** - `docker compose exec app reset-admin-password` (or `--generate`,
    `--user <name>`). Works with the app running; all sessions of that user are closed.

All displayed dates use **yyyy-mm-dd hh:mm:ss**, independent of browser language. Operational
instants are browser-local, with offset/timezone on hover; timezone-free capture dates stay
unchanged. Tables scroll inside bounded containers with sticky headers in both themes.
Analyze shows the last three analysis jobs and links to their full history. Phase-completion
logs distinguish newly hashed/extracted values from reused values and unreadable files.

**Logs** offers an accessible searchable Event code select, present events by default, optional
muted zero-count events, Clear filters and counted **Export filtered / Export all** controls.
CSV includes UTC `time_utc` and 24-hour `time_local` in the library timezone with offset;
messages stay English.
Base users see only their own scoped job/file/review events, never system/login events.
Administrators see all events and can filter by User. Empty lists explain why nothing is shown.
The admin User filter matches the User column, including events outside that user's library.
Its note counts events excluded from the user's private view; filtered export uses the same rows.
All roles see library-relative paths; admin CSV adds `path_container` for technical support.
CSV files have descriptive, timestamped names rather than a generic export name.
Normal users see only present event codes and no catalogue checkbox. Filters form the first
row; naturally sized exports form the second. Security events are separate:
**Security Log** in the user menu is a trial, backed by the encrypted instance
audit store even without a working database. Sign-in banners show the previous retained
sign-in and failed attempts since then. Banners persist across pages until dismissed;
dismissal lasts until the next sign-in. Admins use the same Security Log page with a User filter.

The step guide is **1 Analyze → 2 Check (Duplicates · Metadata) → 3 Organize → 4 History & Undo**.
There is no separate Review menu tab; existing Scan and Review links redirect to their new locations.

### Instance settings

The fixed admin sets these in **Settings → General**; other roles cannot edit them:

| Setting | Description | Default | Required | Example |
|---------|-------------|---------|----------|---------|
| Library timezone | Searchable container IANA-zone list with current UTC offsets and daylight-saving help. Mtime proposals convert from UTC, offset EXIF converts only for organizing, timezone-free EXIF/manual dates stay unchanged. | container `TZ`, otherwise `UTC` with a visible recommendation | no | `Europe/Rome` |
| Show version on login page | Small `TwinCheck <version>` footer, using the same version as the authenticated header. When disabled no version is sent to unauthenticated clients. | ON | no | OFF |

Both settings are stored in the encrypted vault and audited as `SETTING_CHANGED`.
The login-version option is not an environment variable. Each new review decision records its
source and applied timezone; historical dates are never silently corrected during migration.

Review items, Logs, Duplicates and Jobs default to 50 rows per page and offer
10 / 50 / 100 / All. The browser remembers each user's choice separately for each table.
Loading All above 1,000 rows requires confirmation, including after returning to the page.

### Upgrade to 1.0.1-rc.13

From rc.12, no schema migration is required: schema remains 6 and an already-current
database emits no `DB_MIGRATED`. User decisions are not changed.
Deploy `ghcr.io/axreds/twincheck:1.0.1-rc.13` (or the moving `rc` alias) only after publication.

Sign-in banners have an accessible close button inside the top corner. Keyboard controls
retain visible focus rings; navigation focus does not outline the whole content area.
Counted texts use each language's plural forms, and review dimensions use `width × height`.
History & Undo describes performed operations, undo and final verification; Undone is numeric.
Without a completed Analyze library run, Duplicates, Metadata, Organize and Home show
**No results yet: run Analyze library**, not zero-result claims. Older-version notices explain
that analysis only reads files. History remains accessible independently.

All filter selects are compact. Dropdowns, checkboxes, searchable selections and date pickers
apply immediately; free-text and numbers apply only with Enter or **Apply**. Pending text
highlights Apply and is not committed by changing a dropdown. Pages without text/number
filters have no Apply button. **Clear filters** remains available. Security dates are inclusive UTC dates.
Users and Groups have name/state filters (Users also has Role).

**Settings → General → Scan → Excluded names** is a managed list with optional descriptions,
built-in explanations and Edit/Remove. Add/Edit validates unique, non-empty, basename-only
shell patterns, blocks patterns excluding all names, and **Test a name** reports matching
patterns using the scanner's case-sensitive rules. Remove and Restore defaults ask for
confirmation; Save previews Added/Removed/Edited names and asks for confirmation.
Changes apply from the next analysis and never touch files. Existing pattern values keep
their meaning, descriptions are stored in the vault, and no migration is needed.
This setting has no server/environment override. Internal `.twincheck-*` names always stay excluded.

The version is shown below the logo with a build-date tooltip. Diagnostics and Security Log
are in the user menu; administrative Settings opens General. Filters stay in the page URL
through F5; navigation links reset filters, while rows-per-page remains a per-user preference.
Organize shows the last saved preview with Open, Recompute and Discard. Changed analysis,
decisions, files, assignments or settings disable execution until recomputed; older previews
also require recomputation. Unchanged polling data never replaces DOM nodes.
History & Undo lists operations newest first and links moved-file results to their operation.
Per-operation undo is deferred to #110; existing global undo previews and confirmations remain.
History does not require a private analysis to inspect a manifest or undo operations.

From an older version, stop the container and back up `/state` (and the operational MariaDB database, if used).
After upgrading, an administrator must open **Settings → Database → Migrate schema**
and confirm the backup. Until the database is ready, every signed-in user sees
**Database update required. Contact an administrator.** Administrators can follow the banner.
Schema 6 returns only historical skip-only records to Pending, preserving the skipped marker
and timestamp. Chosen dates, Leave as it is and accepted/kept re-check decisions are not changed.
Migration logs show the real versions once, plus converted/flagged counts and audit entries.

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
* **Per-user isolation**: database ownership is not enough on its own. Every read, result, export,
  preview and file action is re-checked against the user's current assigned source/destination
  paths immediately before use. Stale rows, cached preview files and files that leave the scope
  between preview and confirmation are hidden or skipped, never exposed or moved.
* **Concurrent jobs**: identical queued/running jobs for the same user and scope return the existing
  job instead of queueing a second copy.
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
