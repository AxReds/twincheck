# Tests

Standard-library `unittest` (also runnable with `pytest`). No network, no real photos: every test
builds its own sample library and instance in a temporary directory.

```sh
python3 -B -m unittest discover -s app/tests -v        # from the repository root
```

Requirements on the host: Python ≥ 3.12, `cryptography`; `ffmpeg` with an HEVC encoder for the HEVC
preview test (skipped otherwise). When `exiftool` is not installed the metadata tests use
`fake_exiftool.py`, a stand-in that stores tags in a JSON file and honours create-only writes; the
container test-suite always uses the real ExifTool.

| File | Covers |
|------|--------|
| `test_crypto_vault.py` | key parsing/generation, vault encryption, fresh nonce, tamper detection, wrong key, rotation + rollback, archive stream encryption, no plaintext in `/state`, startup refusal, no secrets in API responses |
| `test_web_security.py` | wizard (token, lock after use), cookie flags, security headers, CSRF, `ALLOWED_NETWORKS` (wizard and login included), `TRUSTED_PROXIES`, login throttling, no directory listing, request limits, no TLS code, role matrix, administrator limits, isolation between users, path traversal and symlinks, overlapping sources, groups, `reset-admin-password` |
| `test_database.py` | automatic structure creation, reuse, schema older (migration after confirmation) and newer (refused), simultaneous starts, instance lock, Unicode/long/case-different paths, database switch with paused jobs + reconciliation + return + failed switch rollback, crash recovery of jobs and of the move journal |
| `test_workflow.py` | metadata rules (never invent, never overwrite, conflicts, QuickTime UTC, Takeout), the whole flow on the sample folder (duplicates in different folders, no EXIF, date conflicts, HEVC video, sidecars, same names), undo, verification, HEVC preview, cross-volume moves |
| `test_transfer.py` | archive verification (wrong passphrase, tampering, truncation, newer version), old-schema archive upgraded, round trip with identical checksums, Unicode/long/case paths, non-empty destination refused, export/import/migrate through the UI, permissions, interrupted migration resumed from checkpoints, purge after confirmation, large dataset with bounded memory |
| `test_i18n.py` | same keys and placeholders in every language, plural categories (ru, ar), checker detects defective files, discovery of a new language file, `/state/locales` override, pseudo-locale, per-user language, English-only logs |
| `test_sysinfo.py` | header metrics vs. `statvfs`/`nproc`/`/proc/meminfo`, cgroup limits, `n/a` values, disk levels, read-only mounts, different volumes, offline assets, RTL-safe CSS |
| `test_docs_consistency.py` | every parameter in the README tables exists in the code/compose files and vice versa; release workflow tag outputs keep pre-releases separate from stable images |

## Optional parameters

| Variable | Effect | Default |
|----------|--------|---------|
| `TWINCHECK_TEST_MARIADB_HOST` | run the database and transfer tests also on MariaDB at this host | unset (MariaDB skipped) |
| `TWINCHECK_TEST_MARIADB_PORT` | MariaDB port | `3306` |
| `TWINCHECK_TEST_MARIADB_USER` | MariaDB user (needs CREATE/DROP DATABASE) | `root` |
| `TWINCHECK_TEST_MARIADB_PASSWORD` | MariaDB password | empty |
| `TWINCHECK_BIG_ROWS` | rows of the large-dataset test (use `2000000` for the millions-of-rows run) | `200000` |
| `TWINCHECK_FAKE_EXIFTOOL` | force the fake ExifTool even if the real one is installed | unset |

MariaDB for tests: `docker compose -f docker/docker-compose.test.yml up -d`, then
`TWINCHECK_TEST_MARIADB_HOST=127.0.0.1 TWINCHECK_TEST_MARIADB_PORT=33306 TWINCHECK_TEST_MARIADB_PASSWORD=test-only-password python3 -B -m unittest discover -s app/tests`.

Container-level tests (permissions, network, kill/resume): `bash docker/tests/run_container_tests.sh`.

## Frontend regressions

rc.9 adds SQLite/MariaDB fixtures in `test_rc9.py` for skip-only migration preservation,
re-analysis and historical duplicate links, Organize preview/confirmation, migration logging,
review populations, timezone catalogue and role-scoped log list/count/CSV export.
The browser fixture checks numeric 24-hour dates (rejecting AM/PM on every routed page),
skip/decision badge-filter-count agreement, timezone search, stay/next actions, sticky table
headers, checkbox alignment, log controls/empty states and compact analysis history.
Run both themes at 1600/1280/900/375 px; fixtures contain synthetic data only.

Serve `app/` locally with `python3 -B -m http.server 18767 --bind 127.0.0.1 --directory app`
and open `http://127.0.0.1:18767/tests/frontend.html` in a browser. The page runs
automated DOM/CSS regression checks against the real frontend modules with synthetic,
in-memory API responses only; every result must show `"passed": true`.
rc.10 extends this fixture with combobox ARIA and keyboard selection/restore, invalid free-text
protection, zero-event grouping, per-user/per-table pagination persistence, confirmation before
All above 1,000 (also during Jobs polling), dash/tooltip postponements, translated Report & Undo,
count wording, role-aware CSV counts/notes and shared population summaries. Export-button and
Job-input bottom bounding boxes must differ by at most 2 px at every matrix width.
Playwright checks should additionally use real keyboard, mouse and touch input and a full
reload to verify persistence; optional browser tooling must stay outside application dependencies.
`test_rc10.py` covers the matching API pagination/visibility/counts, UTC/local CSV columns
including daylight saving, current population identities, invalid timezone preservation and
readable singular/plural analysis events on SQLite and MariaDB.
The session-expiry regression overlaps system/job 401 responses while typing,
checks values, focus and caret, waits longer than the system refresh interval to
check that polling stops, and verifies failed login, successful login and a late
old-session response.
Stop the server afterwards. These browser checks supplement the Python suite.

rc.11 extends the same fixture to 52 scenarios, including focus/scroll-preserving Jobs
polling, closed searchable selects, role-specific catalogues, zero-result controls,
direct Metadata review, short placeholder measurements, fields-first preview layout,
Escape enlargement and database-independent sign-in banners. Run Light/Dark at
1600/1280/900/375 px. `test_rc11.py` covers legacy scan/duplicate log identity across
list/count/CSV, relative paths, admin `path_container`, descriptive filenames, initial
HTML theme precedence, instance security isolation/retention, unknown-account failures,
password resets, session expiry, role/group changes and lockouts.
Real-browser checks must preserve the production CSP, inspect the computed background
at DOMContentLoaded on login/reload/logout, open the native Jobs select through a refresh
cycle and choose 10/100/All, and exercise searchable-select keyboard/mouse/touch.
Use synthetic accounts/libraries only. Browser tools and their optional dependencies must
remain outside application dependencies; the fixture tests need no build step or Node.js project.

The required-password regression covers a new account and an admin reset,
including five preceding failed logins. It checks the single change-password
page, stopped polling, preserved values on wrong/weak passwords, in-place
recovery and continued responsiveness for health checks and another user.

rc.12 adds `test_rc12.py`: the exact 362-admin/357-private event fixture, explicit analysis
reasons versus global History counts, reusable/stale/discarded previews, execution-level stale
guards, scoped operation history with journaled global undo, per-sign-in banner dismissal,
30 concurrent reload/navigation requests and health/home responses below two seconds.
The browser fixture covers two Organize preview refresh cycles with a scrolled table and
retained select, History/Settings/Security Log naming, URL filters, clear example placeholders,
banner persistence/dismissal, timezone order, opaque headers, specific empty results and
double-submit prevention. Run the existing Light/Dark × 1600/1280/900/375 px matrix.
The rc.8/rc.9 old-plan tests now assert complete 409 stale-plan rejection, not partial execution;
all original private scope, decision, date and no-overwrite assertions remain.
`browser_live.mjs` exports `runLiveRegressions(page)` for a Playwright page signed in to an
isolated synthetic application with a fresh, scrollable Organize preview. It uses a real
native open select through two refresh cycles, performs 30 actual reloads/navigations,
requires `/health` and `/` below two seconds, and verifies F5/default navigation filters.
Run it at the same eight viewport/theme combinations; never point it at an owner's library.

The rc.6 regressions cover stale per-user records outside current source scope,
capture-date reading before keeper selection, the resumable five-stage library
analysis, duplicate date columns, readable metadata summaries, persistent
ExifTool process recovery and preview cache hit/invalidation/scope checks.

rc.13 adds `test_rc13.py` for validated, confirmed exclusion-list persistence, scanner-matched
name tests, unchanged file bytes and no job submission on SQLite/MariaDB. The catalogue test
requires all plural categories for every counted message and rejects a deliberately removed
form. The frontend fixture renders 0/1/2 in all ten languages and tests banner geometry/focus,
both dimension displays, exact History wording, missing/legacy-analysis states, compact
selects, immediate versus deferred filters on every applicable page, and exclusion dialogs.
Run Light/Dark × 1600/1280/900/375 px. `runRc13Regressions(page)` in `browser_live.mjs`
adds real keyboard focus, selector/date/Enter/Apply/F5/Clear tests and a server-validated
exclusion dialog with cancelled save. It requires the synthetic admin/pippo instance
described above; never use it on real libraries. Documentation tests request both health
endpoints and compare their exact response with the README instructions.
rc.7 also interrupts the library job in hash and capture-date batches, restarts
the job runner and verifies checkpoint resume plus `ANALYSIS_RESUMED`; concurrent
same-scope submissions are tested atomically on SQLite and MariaDB.

Theme checks exercise Auto/Light/Dark, the sun/moon button, whole-header palette
and active-section contrast (at least 4.5:1). An embedded synthetic app checks
actual reload/logout/login persistence and authenticated preference precedence.
Alternating synthetic Dark/Light users in the same browser verifies that logout
remembers the last authenticated user, including a first visit with no history.
Menu-icon checks cover inline SVG, decorative accessibility, visible labels,
translated accessible names/tooltips, inherited contrast and viewport bounds.
For live system-preference changes, `frontend.html?interactive` leaves the
synthetic app running and persists its synthetic user theme/session in local
storage. No real credentials are used.
