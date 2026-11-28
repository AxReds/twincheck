# Manual release acceptance checklist

Run this checklist before approving a release. Use the
[Manual acceptance test issue template](https://github.com/AxReds/twincheck/issues/new?template=manual-test.md)
to record the results. The step IDs match the template.

## Safety and recording results

- Always use disposable copies of photos and videos, never the originals.
  Keep an untouched baseline outside every source and destination.
- Use a separate test deployment: dedicated data and state folders, container
  name, master key, accounts and database. Do not mount production libraries,
  reuse production state, or connect to a production MariaDB database.
- Run Docker commands only from the test deployment's Compose directory and
  configuration. With Portainer, use a separate test stack and its own volumes.
  Do not run these commands against the production container.
- Record the release version, source commit, image tag, CPU architecture,
  browser, database type/version and test date in the test issue. Repeat for
  each supported deployment combination being certified; SQLite results alone
  do not certify MariaDB, or the other CPU architecture.
- Check a step only after both its action and expected result have been tested.
  Leave failed or untested steps unchecked and explain why in Problems found.
  Link a bug issue for each unexpected result. Never attach photos, databases,
  archives, passwords, keys, `.env` files or setup tokens to an issue.
- Select English for this run so the quoted interface labels below match.
  Use the checklist from the tested tag/commit when testing an older release.

These are checks of existing behavior, not requirements for unfinished
[roadmap](ROADMAP.md) features. In particular, duplicates must be
byte-identical and file-system modification times are preserved, not reset
to the capture date. Network-share write safeguards are not yet a release
guarantee: use local disposable storage for these write tests.

For every file-writing preview below, cancel once and inspect the copies before
executing a fresh preview. The normal confirmation sequence is
"Confirm and execute", select "I have checked the preview.", then "Execute".
Opening a preview or a confirmation dialog must not itself write media files.

## 0 Test data preparation

- [ ] 0.1 Isolate the test deployment.
  - Action: Configure dedicated test storage and an unused database following
    [Docker deployment](../docker/README.md); keep `/state` on a local disk.
    Check the mounted paths before starting the test container.
  - Expected: Only disposable copies and test state are writable by this
    deployment; no production folders or database are reachable through its configuration.
- [ ] 0.2 Keep an untouched recovery baseline.
  - Action: Store a separate baseline of the test media outside `/data`; keep
    the test master key separately in a password manager. Prepare to back up
    both the test state and the test media before changing them.
  - Expected: A complete repeat of the test is possible without touching originals;
    no secret or test media needs to be committed or uploaded.
- [ ] 0.3 Prepare exact duplicates and non-duplicates.
  - Action: Place three byte-identical copies of one file in different folders
    and under different names. Add different files with the same name and
    visually similar files. Set different modification dates on the exact copies.
  - Expected: The known duplicate group has exactly three members; equal names,
    similar appearance and file-system dates alone do not define duplicates.
- [ ] 0.4 Prepare metadata and review fixtures.
  - Action: Include a dated JPEG, a JPEG missing a date but with a reliable date
    in a matching `.xmp`, a valid image with no date anywhere, and a valid image
    with conflicting EXIF/XMP dates. Record the known existing metadata values.
    Do not edit the exact-duplicate fixtures after copying them.
  - Expected: Fixtures exercise reliable completion, unchanged existing values,
    missing dates and conflicts; metadata writes cannot accidentally invalidate
    the intended exact-duplicate group before testing it.
- [ ] 0.5 Prepare videos, companions and destination collisions.
  - Action: Include playable MP4, MOV/HEVC, a video with a QuickTime UTC date but
    no offset, `.xmp`, `.aae`, Google Takeout `.json`, and a Live Photo pair.
    Include different same-named images with capture dates on the same day.
    Keep fixtures in separate, non-overlapping folders for two base users.
  - Expected: Video previews, UTC ambiguity, supported companion handling,
    filename collisions and user isolation can all be tested with known samples.
- [ ] 0.6 Record the starting inventory.
  - Action: Record relative paths, file counts, sizes, SHA-256 hashes, relevant
    metadata and modification times using trusted local tools; include companions.
    Keep an additional larger disposable set for interruption tests.
  - Expected: Later changes can be reconciled with a private baseline; file loss,
    unintended byte changes and overwritten destination files can be detected.

## 1 Users and sign-in

- [ ] 1.1 Complete first setup on the empty test instance.
  - Action: Follow [Quick start](../docker/README.md#quick-start), generate the
    test key with `docker compose run --rm app generate-key`, start with
    `docker compose up -d`, and obtain the token using
    `docker compose logs app | grep SETUP_TOKEN`. In "First setup", choose the
    test database, use "Test connection" where offered, and "Complete setup".
  - Expected: The fixed `admin` account works, the database is connected, and
    reloading does not offer a reusable setup wizard. Keep the token and key private.
- [ ] 1.2 Reject incorrect sign-in credentials.
  - Action: On "Sign in", try one incorrect test password, then the correct one.
  - Expected: The incorrect attempt grants no access; the correct credentials
    open the application. No password is shown in application logs.
- [ ] 1.3 Create the test accounts and sources.
  - Action: As admin, open "Administration" > "Users" > "New user". Create two
    "Base user" accounts and one "Administrator", using "Add a source folder",
    "Choose this folder" and "Create". Assign separate sources and destinations
    to the base users; leave one temporary password empty to exercise generation.
  - Expected: The accounts have the selected roles and paths. A generated
    "Temporary password" is shown once, not placed in logs.
- [ ] 1.4 Require the first password change.
  - Action: Sign in as each new account in a separate browser session and
    complete "Change password" using "Current password", "New password",
    "Confirm new password" and "Save". While the form is open, type into it,
    try a wrong current password and a weak password, and keep it open while
    another account uses the application.
  - Expected: Normal use is blocked until the password is changed; the new
    password works and the temporary password no longer does. The form is
    rendered once without live metrics or polling, errors keep its values and
    focus, and normal use resumes without reloading the page. Health checks
    and other users remain responsive.
- [ ] 1.5 Enforce role and file isolation.
  - Action: Compare the two base-user sessions, the administrator and admin.
    Check sources, "Review", "Logs", "Report & undo", previews and downloads;
    try opening a copied test-only preview URL from the other base-user session.
  - Expected: Base users cannot access administration or another user's data.
    An administrator can manage base users but cannot manage administrators
    or use admin-only export/security controls.
- [ ] 1.6 Reject overlapping source assignments.
  - Action: In "Administration" > "Users", try assigning the second user the
    first user's source or a folder inside it, then cancel the invalid edit.
  - Expected: The assignment is rejected with a visible error; existing valid
    sources and permissions remain intact.
- [ ] 1.7 Apply account and group activation.
  - Action: Use "Deactivate" and "Activate" on a disposable base user. In
    "Groups", create a test group, assign that user with "Save", then deactivate
    and reactivate the group.
  - Expected: An inactive account or group cannot sign in; reactivation restores
    access without removing the user's media or operational history.
- [ ] 1.8 End the session explicitly.
  - Action: Use "Log out", then try revisiting a protected page before signing in again.
  - Expected: Authentication is required again; the old session no longer grants access.

## 2 Scan

- [ ] 2.1 Check the deployment information.
  - Action: Inspect "System", "Version", "Database", "Source" and "Destination"
    in the header, expanding the source disk details where necessary.
  - Expected: Version, database, paths and volume information match the test
    deployment, and any read-only or different-volume warning is visible.
- [ ] 2.2 Scan without changing media.
  - Action: As a base user, open "Scan", select "Source", click "Start scan" and
    follow progress in "Jobs" until the scan is completed.
  - Expected: The expected files are indexed once with progress visible;
    media paths, hashes, sizes and modification times remain unchanged.
- [ ] 2.3 Repeat an unchanged scan.
  - Action: Scan the same source again without changing the copies.
  - Expected: Counts remain stable, entries are not duplicated and unchanged
    files reuse their hashes. Inspect "Logs" for unexpected errors.
- [ ] 2.4 Reconcile changed test files.
  - Action: In a separate disposable source, add one file, change another, and
    move a third outside the source without deleting its recovery copy. Scan again.
  - Expected: The new/changed files are reflected in the index, the absent file
    is reported missing, and no unrelated file is modified to repair the difference.
- [ ] 2.5 Honor scan exclusions.
  - Action: Put a fixture in a dedicated excluded folder. In "Administration" >
    "Settings", add its name to "Excluded names (one pattern per line)" and "Save",
    then scan it with fresh test data.
  - Expected: Excluded files are not indexed; ordinary files remain visible.
    Restore the test setting afterwards.

## 3 Duplicates

- [ ] 3.1 Find only the known exact duplicate group.
  - Action: Before writing any metadata, open "Duplicates" and click "Analyze".
  - Expected: The three byte-identical fixtures form one group despite different
    names, folders or modification dates.
- [ ] 3.2 Keep different content out of exact duplicate groups.
  - Action: Compare results against the same-named and visually similar fixtures.
    Include copies whose embedded metadata differs if available.
  - Expected: Files with different bytes are not grouped as exact duplicates,
    even when they look identical or share a filename.
- [ ] 3.3 Choose the keeper and export the list.
  - Action: Check the default keeper, select a different copy using "Keep this file",
    and use "Export CSV".
  - Expected: The oldest applicable date is preferred by default; the chosen
    keeper is retained in the preview and the CSV reflects the duplicate group.
- [ ] 3.4 Cancel a duplicate-move preview.
  - Action: Click "Preview move to duplicates folder", inspect "From" and
    "To / changes", open "Confirm and execute", then "Cancel".
  - Expected: Nothing moves; the default destination is `Duplicates` under the
    configured destination, retaining the original relative paths.
- [ ] 3.5 Reject a stale duplicate plan.
  - Action: On fresh disposable duplicates, preview the move, then change a
    duplicate's bytes before executing the preview.
  - Expected: The changed item is skipped or blocked with a visible reason and
    sent to review; it is not moved on the strength of the old hash.
- [ ] 3.6 Execute a fresh duplicate-move plan.
  - Action: Re-scan and analyze a clean duplicate fixture set, create a fresh
    preview and complete the normal confirmation sequence.
  - Expected: Only non-keepers move to `Duplicates`; the keeper remains and every
    file is still present exactly once across source/destination locations.
    Moved file bytes are unchanged and manifest entries are available.

## 4 Metadata and review

- [ ] 4.1 Analyze without writing metadata.
  - Action: Open "Metadata", select the source and click "Analyze and preview".
  - Expected: A preview shows proposed fills and their sources; no media bytes
    or existing metadata values change during analysis.
- [ ] 4.2 Complete only missing fields from reliable sources.
  - Action: Inspect the dated and matching-XMP fixtures in the preview.
  - Expected: A missing field can be filled from the equivalent reliable tag or
    linked file; populated fields retain their original values.
- [ ] 4.3 Send uncertain dates to review.
  - Action: Open "Review" and inspect the missing-date, conflicting-date,
    QuickTime UTC and Google Takeout fixtures.
  - Expected: Missing/conflicting dates require review. Filename and modification
    dates are proposals, not invented metadata; UTC ambiguity is visible and a
    Takeout timestamp is not silently written as a local capture time.
- [ ] 4.4 Confirm metadata writes explicitly.
  - Action: Cancel the first metadata confirmation, verify the baseline, then
    execute a fresh reliable-fill preview using the normal confirmation sequence.
  - Expected: Cancellation writes nothing. Confirmation fills only missing fields
    in supported formats; existing values and file-system modification times are
    preserved. ExifTool may change file bytes, so do not expect the old SHA-256.
- [ ] 4.5 Display photo and video previews.
  - Action: In "Review", use "Open" on a photo, playable video and MOV/HEVC fixture.
  - Expected: Photos display and supported videos play. When transcoding is
    needed, a temporary preview is generated without changing the original.
    Unsupported media produces a visible message rather than a broken silent result.
- [ ] 4.6 Save a decision without writing the file.
  - Action: For a pending item, enter a known missing value, optionally select
    "Date to use for organizing", and click "Save and next". Reload the page.
  - Expected: The decision persists in the database, empty fields stay empty,
    populated metadata fields remain read-only, and media bytes have not changed yet.
- [ ] 4.7 Keep review decisions distinct.
  - Action: On different pending fixtures, use "Skip" and "Leave as it is".
    Filter by "Status", then open one decided item and use "Reopen".
  - Expected: Skipped and kept items have the corresponding statuses; reopening
    returns the selected item to pending without writing media metadata.
- [ ] 4.8 Limit bulk review decisions to the selected scope.
  - Action: On dedicated pending fixtures, test "Apply to" with "this file only",
    "all pending files of this folder" and "the whole group (duplicates, Live Photo)".
  - Expected: Only the selected scope is affected; other folders, groups and
    users retain their decisions.
- [ ] 4.9 Preview and write saved decisions.
  - Action: From "Review", click "Preview writing"; inspect the saved fills,
    cancel once, then confirm a fresh preview.
  - Expected: Only saved values for missing writable fields are written after
    confirmation. Unsupported writes or fields populated since planning are
    visibly skipped/reported; existing metadata is never overwritten.

## 5 Organize

- [ ] 5.1 Preview the organization without moving files.
  - Action: Open "Organize", select "Source" and click "Preview organization";
    inspect the destinations and cancel the first confirmation.
  - Expected: Dated files target `YYYY/MM/DD`; unresolved dates target the
    configured no-date folder, default `No_date`. No files have moved.
- [ ] 5.2 Resolve same-name collisions without overwriting.
  - Action: Preview the two different same-named fixtures dated on the same day.
  - Expected: Destinations remain distinct using numeric suffixes where needed;
    an existing destination file is never silently replaced.
- [ ] 5.3 Include linked files in the plan.
  - Action: Inspect the preview for matching `.xmp`, `.aae`, supported Takeout
    `.json` companions and the Live Photo image/video pair.
  - Expected: Recognized companions follow their primary file to consistent
    locations; unmatched sidecars are not silently discarded.
- [ ] 5.4 Protect a destination occupied after planning.
  - Action: In a dedicated fixture set, create a new test file at a planned
    destination after previewing, then confirm that preview.
  - Expected: The new destination's bytes remain untouched; the conflicting
    move is skipped or blocked with a visible reason, not overwritten.
- [ ] 5.5 Execute and inspect organization.
  - Action: Build a fresh valid preview and complete the confirmation sequence;
    inspect the resulting tree and compare it with the inventory.
  - Expected: Files and companions are in the planned locations, none is lost,
    and move-only bytes and modification times are preserved. Previously
    completed metadata writes are accounted for separately.

## 6 Undo and verification

- [ ] 6.1 Download the manifest and verify the current state.
  - Action: In "Report & undo", use "Download manifest (CSV)" and "Verify";
    inspect the verification job and relevant "Logs".
  - Expected: The manifest accounts for executed changes and verification finds
    no unexplained missing files or hash mismatches.
- [ ] 6.2 Reverse moves only after confirmation.
  - Action: Use "Preview undo of moves", cancel once, then execute a fresh undo
    preview. Repeat with fresh previews if chained moves require another pass.
  - Expected: Files return to their recorded locations without overwriting;
    remaining blocked steps are visible. Counts and bytes reconcile with the baseline.
- [ ] 6.3 Refuse an occupied original location during undo.
  - Action: On a separate moved fixture, place another disposable file at its
    original path, then use "Preview undo of moves".
  - Expected: The occupied original path is blocked and neither file is replaced.
    Move the blocking test file aside before completing that fixture's undo.
- [ ] 6.4 Restore metadata through its own preview.
  - Action: Use "Preview undo of metadata", inspect the tags to restore, cancel
    once, then execute a fresh preview.
  - Expected: Recorded original metadata values are restored and the undo is
    recorded. Compare tag values, not byte identity: an ExifTool rewrite need not
    recreate the original byte layout.
- [ ] 6.5 Detect a deliberate verification mismatch.
  - Action: Modify one disposable indexed file outside the app, then run "Verify"
    before rescanning; inspect "Review" and "Logs".
  - Expected: The mismatch is reported for review; verification does not silently
    rewrite or delete the file. Set this fixture aside or restore it before later checks.

## 7 Interruptions (container restart during jobs)

Use the larger disposable set and record the job ID and progress before each
restart. The commands below are documented in the
[Docker guide](../docker/README.md#backup):

```sh
docker compose stop
docker compose up -d
```

This is a graceful stop/start, not a forced crash. It can leave unfinished jobs
paused at a checkpoint. A deliberately paused job requires "Resume", even for a
scan. If a job instead remains interrupted after an unclean shutdown, eligible
read-only work resumes automatically; file-writing work still needs "Resume".
A job that finished before shutdown does not count as an interruption test:
repeat with a larger fresh set.

- [ ] 7.1 Recover a scan across a container restart.
  - Action: Start a sufficiently large scan, stop/start the test container while
    it is running, sign in again and find the job in "Jobs". Use "Resume" if paused.
  - Expected: Progress/checkpoints survive; the scan completes with no duplicate
    index entries. An interrupted, rather than paused, scan resumes automatically.
- [ ] 7.2 Recover duplicate moves across a container restart.
  - Action: Confirm a sufficiently large duplicate-move plan and stop/start
    during execution. Inspect "Jobs" before using "Resume".
  - Expected: Unfinished writes wait for explicit "Resume". After resuming, every
    duplicate exists once at a valid location; the keeper and unrelated files
    are untouched, and no partial move is left unexplained.
- [ ] 7.3 Recover organization across a container restart.
  - Action: Repeat the stop/start during a sufficiently large confirmed
    organization job, inspect its state and use "Resume" explicitly.
  - Expected: The job continues safely from recorded progress without losing
    files, overwriting destinations or leaving unexplained temporary files.
- [ ] 7.4 Recover metadata writes across a container restart.
  - Action: Repeat the stop/start during a sufficiently large confirmed metadata
    write job; inspect the saved preview, status and manifest before using "Resume".
  - Expected: Unfinished writes require explicit resume, existing tag values are
    preserved, and completed writes remain represented in the manifest.
- [ ] 7.5 Verify consistency after interruptions.
  - Action: Run "Verify", download the manifest and compare paths, counts, bytes
    and metadata against the recorded changes.
  - Expected: No file is lost or silently duplicated; verification and the manifest
    explain the recovered state. Record any anomaly as a release-blocking bug.

## 8 Administration

- [ ] 8.1 Reset the admin password from the console.
  - Action: Keep a test admin session open and run
    `docker compose exec app reset-admin-password` against the test deployment.
    Enter the new password through its hidden prompt.
  - Expected: The command works while the app runs, the old session/password
    stop working, and the new password signs in without modifying media.
- [ ] 8.2 Exercise the generated-password reset.
  - Action: Run `docker compose exec app reset-admin-password --generate`,
    sign in with the generated password and complete "Change password".
  - Expected: The temporary password is shown once in the command output,
    requires a change, and never appears in container logs.
- [ ] 8.3 Inspect the administrative audit trail.
  - Action: Open "Administration" > "Audit log"; locate the account, group,
    settings and password-reset actions performed during this run.
  - Expected: Entries identify the actions with English event codes/messages,
    including web `PASSWORD_RESET` and console `PASSWORD_RESET_CLI`, without
    passwords, keys or setup tokens. Web resets also emit the English
    `PASSWORD_RESET` console event with only the target user and actor.
- [ ] 8.4 Persist a user's language independently.
  - Action: Change "Language" in one user's menu, navigate and sign in again;
    compare a second user. Return the checklist session to English afterwards.
  - Expected: The chosen language persists for that account, other users keep
    their own choice, and execution/audit messages remain English.
- [ ] 8.5 Set the default language as admin.
  - Action: In "Administration" > "Settings", change "Default language" and
    "Save"; check a fresh browser session without a prior language preference.
  - Expected: The setting is saved and available as the instance default; the
    browser's preferred supported language can take precedence. An
    "Administrator" cannot change this admin-only setting. Restore the original value.
- [ ] 8.6 Test the database connection without switching.
  - Action: In "Administration" > "Database", use "Test connection" with valid
    dedicated test parameters and then unreachable test parameters.
  - Expected: Results distinguish reachable/unreachable databases and report
    their structure. Testing alone neither switches the active database nor
    changes the media.
- [ ] 8.7 Preview an encrypted export.
  - Action: As admin, open "Export & migration" > "Export", enter matching
    "Archive passphrase" and "Confirm passphrase" values, and click
    "Preview (dry run)". Cancel the first "Start" confirmation.
  - Expected: Table counts and estimates appear; cancellation does not start
    an export. Keep the archive passphrase private and recoverable.
- [ ] 8.8 Execute and download the export.
  - Action: Prepare a fresh export preview, click "Start", check
    "I have checked the preview." and confirm with "Start". Wait for completion,
    then use "Download" in "Archives".
  - Expected: The encrypted `.tcarc` archive is available, maintenance ends,
    normal use resumes and media files are unchanged. The archive is operational
    database data, not a backup of the vault or photos.
- [ ] 8.9 Restrict database transfers to admin.
  - Action: Compare the admin session with the test administrator and base users;
    try opening the admin's test "Export & migration" page from those sessions.
  - Expected: Only admin can export/import/migrate operational data; other
    roles do not gain access through a copied page URL.

## 9 Backup and restore

- [ ] 9.1 Take a consistent full test backup.
  - Action: Finish pending jobs and record the inventory. Follow
    [Backup](../docker/README.md#backup): run `docker compose stop`, back up the
    entire test `/state` and matching test media, and keep the key separately.
    For MariaDB, also take a consistent database backup using the documented
    `mariadb-dump --single-transaction twincheck > twincheck.sql`, substituting
    only the dedicated test database name and using its approved credentials.
  - Expected: State/vault, operational database, media and configuration needed
    for restoration describe the same checkpoint; the key is not stored with
    the backup. A `.tcarc` alone is not treated as a full application backup.
- [ ] 9.2 Restore the complete backup into isolated empty storage.
  - Action: Use new empty test data/state folders and, for MariaDB, a dedicated
    empty restore database. Restore the media, full state and matching database;
    preserve permissions and the same paths inside the container. Configure the
    matching test key and tested image version before `docker compose up -d`.
    Keep the original test deployment stopped and retain all backup copies.
  - Expected: No existing deployment is overwritten, no two containers share
    one operational database, and the restored instance opens without a new setup.
- [ ] 9.3 Validate the restored application and media.
  - Action: Sign in to the restored instance; inspect "Users", "Settings",
    "Audit log", "Report & undo", saved review decisions and the manifest.
    Run "Verify" and compare the restored inventory with the backup checkpoint.
  - Expected: Accounts, configuration, operational history and media correspond
    to the backed-up state, with no unexplained missing files or hash mismatches.
- [ ] 9.4 Verify the export by importing into an empty database.
  - Action: On disposable restored test state, open "Export & migration" >
    "Import". Use "Upload an archive" and "Upload" for the downloaded `.tcarc`,
    select "Archive", enter its passphrase and choose a new empty
    "Destination database". Use "Preview (dry run)", then confirm with "Start".
  - Expected: The archive is verified before copying, the source is preserved,
    and the completed job exposes a successful "Verification report" with
    matching table row counts and checksums. Existing data is not merged.
- [ ] 9.5 Reject invalid import inputs safely.
  - Action: Try an incorrect archive passphrase and, separately, preview an
    import into the already populated disposable import destination.
  - Expected: Both operations show an error without overwriting the destination,
    modifying the source, or exposing the passphrase.
- [ ] 9.6 Switch to the verified import and check recovery.
  - Action: On the completed import job, use "Switch to this database", check
    "I understand what changes.", and confirm with "Switch database". Wait for
    reconciliation, inspect "Database" and run "Verify".
  - Expected: The verified imported database becomes active, its provenance is
    visible, the previous database is retained, and restored test media still
    matches. Any disk differences go to review, not automatic destructive repair.
