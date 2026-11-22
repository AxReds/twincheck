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

Serve `app/` locally with `python3 -B -m http.server 18767 --bind 127.0.0.1 --directory app`
and open `http://127.0.0.1:18767/tests/frontend.html` in a browser. The page runs
automated DOM/CSS regression checks against the real frontend modules with synthetic,
in-memory API responses only; every result must show `"passed": true`.
The session-expiry regression overlaps system/job 401 responses while typing,
checks values, focus and caret, waits longer than the system refresh interval to
check that polling stops, and verifies failed login, successful login and a late
old-session response.
Stop the server afterwards. These browser checks supplement the Python suite.

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
