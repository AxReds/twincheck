# Changelog

All notable changes are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/).

## [1.0.1-rc.5] - 2026-11-24

### Fixed

- A user required to change a password now gets one stable password-change page with no navigation, live metrics or polling; errors keep the form, and a successful change restores normal use without reloading. Web password resets clear login throttling, and password hashing no longer holds the vault transaction lock ([#39](https://github.com/AxReds/twincheck/issues/39)).
- Administration password resets now emit the English `PASSWORD_RESET` console event with the target user and administrator while keeping the existing audit entry; neither the generated password nor its hash is logged ([#39](https://github.com/AxReds/twincheck/issues/39)).

### Verified by owner

- Login now remembers the last authenticated user's theme after logout; B12 was verified on rc.4 ([#35](https://github.com/AxReds/twincheck/issues/35)).
- Initial scan selection, active-section highlighting, Administration placement, whole-interface themes and persistent theme controls were verified on rc.3 ([#15](https://github.com/AxReds/twincheck/issues/15), [#16](https://github.com/AxReds/twincheck/issues/16), [#19](https://github.com/AxReds/twincheck/issues/19), [#29](https://github.com/AxReds/twincheck/issues/29), [#31](https://github.com/AxReds/twincheck/issues/31)).
- User-menu dismissal, stable header boxes, reserved user-button space and expired-session login remain verified from rc.2 ([#12](https://github.com/AxReds/twincheck/issues/12), [#13](https://github.com/AxReds/twincheck/issues/13), [#14](https://github.com/AxReds/twincheck/issues/14), [#26](https://github.com/AxReds/twincheck/issues/26)).

### Awaiting owner retest

- Assigned subfolder browsing/scanning and offline menu icons await owner retest ([#34](https://github.com/AxReds/twincheck/issues/34), [#36](https://github.com/AxReds/twincheck/issues/36)).
- Review date edits, scan hash reporting and shared duplicate capture dates remain open and untouched for retest ([#17](https://github.com/AxReds/twincheck/issues/17), [#18](https://github.com/AxReds/twincheck/issues/18), [#28](https://github.com/AxReds/twincheck/issues/28)).
- The required-password hotfix awaits owner retest on rc.5 and remains open ([#39](https://github.com/AxReds/twincheck/issues/39)).

### Tracked for later

- The implemented U9 item is listed above awaiting retest; all other open enhancements remain deferred: readable preview summaries ([#20](https://github.com/AxReds/twincheck/issues/20)), review preview/undo navigation ([#21](https://github.com/AxReds/twincheck/issues/21)), side-by-side date decisions ([#22](https://github.com/AxReds/twincheck/issues/22)), timestamp wording ([#23](https://github.com/AxReds/twincheck/issues/23)), side-by-side media previews ([#30](https://github.com/AxReds/twincheck/issues/30)), CI package-install resilience ([#10](https://github.com/AxReds/twincheck/issues/10)) and roadmap enhancements [#1](https://github.com/AxReds/twincheck/issues/1) through [#8](https://github.com/AxReds/twincheck/issues/8).
- All open feature/design items remain tracking only: Settings/Preferences ([#24](https://github.com/AxReds/twincheck/issues/24)), guarded duplicate management ([#25](https://github.com/AxReds/twincheck/issues/25)), user folders/root analysis ([#32](https://github.com/AxReds/twincheck/issues/32)), guarded file-system/trash management ([#33](https://github.com/AxReds/twincheck/issues/33)), group semantics ([#37](https://github.com/AxReds/twincheck/issues/37)) and optional Microsoft AI evaluation ([#38](https://github.com/AxReds/twincheck/issues/38)).

## [1.0.1-rc.4] - 2026-11-22

### Fixed

- Base users can browse and scan subfolders of their assigned sources without leaving those source boundaries. Partial scans process and count only the chosen subtree; unrelated files and destinations are left alone ([#34](https://github.com/AxReds/twincheck/issues/34)).
- After logout, the login page remembers the last authenticated user's theme in that browser, even when another user previously selected a different theme; a first visit defaults to Auto ([#35](https://github.com/AxReds/twincheck/issues/35)).

### Changed

- Main navigation, user-menu entries and Administration tabs now include bundled inline SVG icons with GPL-compatible ISC/MIT notices, visible labels, accessible names and readable light/dark contrast; no CDN or runtime network dependency ([#36](https://github.com/AxReds/twincheck/issues/36)).

### Verified by owner

- Initial scan selection, active-section highlighting, Administration placement, complete light/dark themes and the persistent theme selector were verified on rc.3 ([#15](https://github.com/AxReds/twincheck/issues/15), [#16](https://github.com/AxReds/twincheck/issues/16), [#19](https://github.com/AxReds/twincheck/issues/19), [#29](https://github.com/AxReds/twincheck/issues/29), [#31](https://github.com/AxReds/twincheck/issues/31)).
- User-menu dismissal, stable header boxes, reserved user-button space and expired-session login remain verified on rc.2 ([#12](https://github.com/AxReds/twincheck/issues/12), [#13](https://github.com/AxReds/twincheck/issues/13), [#14](https://github.com/AxReds/twincheck/issues/14), [#26](https://github.com/AxReds/twincheck/issues/26)).

### Awaiting owner retest

- Review date edits, scan hash reporting and shared duplicate capture dates have not yet been retested by the owner; those issues remain open and were not updated in this task ([#17](https://github.com/AxReds/twincheck/issues/17), [#18](https://github.com/AxReds/twincheck/issues/18), [#28](https://github.com/AxReds/twincheck/issues/28)).
- Assigned subfolder browsing/scanning, last-user login theme and menu icons need rc.4 acceptance ([#34](https://github.com/AxReds/twincheck/issues/34), [#35](https://github.com/AxReds/twincheck/issues/35), [#36](https://github.com/AxReds/twincheck/issues/36)).
- This is the last planned candidate before stable 1.0.1, not a stable release or approval to merge draft PR #27. The current never-delete and offline rules remain unchanged.

### Tracked for later

- User groups: evaluate meaningful group folders/policies, an administrators group, or removal for 1.1.0; no design or permission change implemented ([#37](https://github.com/AxReds/twincheck/issues/37)).
- Readable preview summaries ([#20](https://github.com/AxReds/twincheck/issues/20)), discarding previews and metadata undo links ([#21](https://github.com/AxReds/twincheck/issues/21)), side-by-side date decisions ([#22](https://github.com/AxReds/twincheck/issues/22)) and coordinated timestamp wording ([#23](https://github.com/AxReds/twincheck/issues/23)).
- Admin Settings/user Preferences beyond themes ([#24](https://github.com/AxReds/twincheck/issues/24)), guarded duplicate management pending deletion design ([#25](https://github.com/AxReds/twincheck/issues/25)) and side-by-side media previews ([#30](https://github.com/AxReds/twincheck/issues/30)).
- User folders/root analysis ([#32](https://github.com/AxReds/twincheck/issues/32)) and guarded file-system/trash management ([#33](https://github.com/AxReds/twincheck/issues/33)); tracking/design only, no user-file deletion.
- Existing roadmap issues #1-#8 and CI enhancement [#10](https://github.com/AxReds/twincheck/issues/10) remain deferred.

## [1.0.1-rc.3] - 2026-11-22

### Fixed

- Scan initializes the actual selected folder from the user's latest accessible scan or first assigned source; the label, browser marker and submitted folder stay consistent ([#15](https://github.com/AxReds/twincheck/issues/15)).
- Administration appears only in the administrative user menu, highlights that menu on admin pages, and gives appropriate source-assignment guidance without exposing admin links to other users ([#19](https://github.com/AxReds/twincheck/issues/19)).
- Exact duplicate groups share known internal capture dates for keeper decisions, explain the deciding rule, and clearly show unknown dates with the file-system-date fallback ([#28](https://github.com/AxReds/twincheck/issues/28)).
- Light and dark palettes now apply to the whole interface, including the header; Auto follows live system-preference changes ([#29](https://github.com/AxReds/twincheck/issues/29)).

### Added

- Always-visible sun/moon button and Auto/Light/Dark selector in the user menu and login page, with per-user preferences and local login-page persistence ([#31](https://github.com/AxReds/twincheck/issues/31)).

### Verified by owner

- User-menu dismissal, stable header boxes, reserved user-button space and usable expired-session login were verified on rc.2 ([#12](https://github.com/AxReds/twincheck/issues/12), [#13](https://github.com/AxReds/twincheck/issues/13), [#14](https://github.com/AxReds/twincheck/issues/14), [#26](https://github.com/AxReds/twincheck/issues/26)).

### Awaiting owner retest

- Initial scan selection, Administration placement, duplicate keeper rules and theme controls need rc.3 acceptance ([#15](https://github.com/AxReds/twincheck/issues/15), [#19](https://github.com/AxReds/twincheck/issues/19), [#28](https://github.com/AxReds/twincheck/issues/28), [#29](https://github.com/AxReds/twincheck/issues/29)).
- Active-section highlighting was verified only in dark mode; light-mode retest remains pending ([#16](https://github.com/AxReds/twincheck/issues/16)).
- Review date edits and scan hash reporting still await owner retest; no additional changes were requested or made to those fixes ([#17](https://github.com/AxReds/twincheck/issues/17), [#18](https://github.com/AxReds/twincheck/issues/18)).
- This remains a pre-release candidate on the separate fix branch; draft PR #27 is not merged. The current never-delete rule and root-source restriction remain unchanged.

### Tracked for later

- Readable preview summaries and preference-gated technical details ([#20](https://github.com/AxReds/twincheck/issues/20)).
- Discarding review previews and linking to metadata undo ([#21](https://github.com/AxReds/twincheck/issues/21)).
- Side-by-side capture, creation and file date choices ([#22](https://github.com/AxReds/twincheck/issues/22)).
- Updating Organize timestamp wording together with roadmap 1c ([#23](https://github.com/AxReds/twincheck/issues/23)).
- Admin Settings and user Preferences within admin-defined limits, beyond the theme-selector exception ([#24](https://github.com/AxReds/twincheck/issues/24)).
- Duplicates folder management and the required design discussion before any guarded deletion ([#25](https://github.com/AxReds/twincheck/issues/25)).
- Side-by-side duplicate media previews ([#30](https://github.com/AxReds/twincheck/issues/30)).
- User folders and guarded administrator root analysis; design/tracking only ([#32](https://github.com/AxReds/twincheck/issues/32)).
- Guarded administrator file-system management and trash; design/tracking only, with no deletion implementation ([#33](https://github.com/AxReds/twincheck/issues/33)).

## [1.0.1-rc.2] - 2026-11-19

### Fixed

- After session expiry, background polling stops and the login form stays usable without losing typed values or focus; polling resumes only after a successful login ([#26](https://github.com/AxReds/twincheck/issues/26)).

### Known issues / not fixed

- This remains a test candidate. The owner's manual acceptance test is pending;
  the draft PR is not merged and bug issues remain open for retesting.
- The limitations and deferred work listed under 1.0.1-rc.1 remain unchanged.

## [1.0.1-rc.1] - 2026-11-19

### Fixed

- The user menu closes on outside click, Escape and navigation, including after a language change ([#12](https://github.com/AxReds/twincheck/issues/12)).
- System information stays on one desktop header row with stable widths and ellipsis; compact screens use the System toggle ([#13](https://github.com/AxReds/twincheck/issues/13)).
- The user button has reserved header space and remains accessible without overlapping system information ([#14](https://github.com/AxReds/twincheck/issues/14)).
- The selected scan folder remains selected and usable after visiting another page, including the data root ([#15](https://github.com/AxReds/twincheck/issues/15)).
- Preview pages highlight their owning section with readable contrast in light and dark themes ([#16](https://github.com/AxReds/twincheck/issues/16)).
- Choosing an organizing date discards stale unsaved capture/creation date edits instead of planning those old values for writing ([#17](https://github.com/AxReds/twincheck/issues/17)).
- Completed scans clearly distinguish newly computed hashes, reused hashes and files without a hash; older scans no longer show a misleading walk phase ([#18](https://github.com/AxReds/twincheck/issues/18)).
- Administration is easy to find, empty source lists link to assignment, and the fixed admin can edit his own source folders ([#19](https://github.com/AxReds/twincheck/issues/19)).

### Known issues / not fixed

- This is a test candidate, not an approved stable release. The owner's manual
  acceptance test is pending; bug issues remain open until that retest.
- Older scan records do not contain reuse counters. Their recorded indexed/hashed
  values are shown with an explanation, rather than inventing historical counts
  ([#18](https://github.com/AxReds/twincheck/issues/18)).
- The review date selector remains database-only, for organizing; it does not
  automatically write date tags. Missing metadata still needs explicit field input,
  preview and confirmation. Side-by-side date decisions are deferred to
  [#22](https://github.com/AxReds/twincheck/issues/22).
- File-system modification dates remain preserved. Changing them and their UI
  wording is deferred to [#2](https://github.com/AxReds/twincheck/issues/2) and
  [#23](https://github.com/AxReds/twincheck/issues/23).

### Tracked for later

- Readable preview summaries and preference-gated technical details ([#20](https://github.com/AxReds/twincheck/issues/20)).
- Discarding review previews and linking to metadata undo ([#21](https://github.com/AxReds/twincheck/issues/21)).
- Side-by-side capture, creation and file date choices ([#22](https://github.com/AxReds/twincheck/issues/22)).
- Updating Organize timestamp wording together with roadmap 1c, not before ([#23](https://github.com/AxReds/twincheck/issues/23)).
- Admin Settings and user Preferences within admin-defined limits ([#24](https://github.com/AxReds/twincheck/issues/24)).
- Duplicates folder preview/restore and a design discussion on guarded deletion;
  the current "never delete" rule is unchanged ([#25](https://github.com/AxReds/twincheck/issues/25)).

[1.0.1-rc.5]: https://github.com/AxReds/twincheck/compare/v1.0.1-rc.4...v1.0.1-rc.5
[1.0.1-rc.4]: https://github.com/AxReds/twincheck/compare/v1.0.1-rc.3...v1.0.1-rc.4
[1.0.1-rc.3]: https://github.com/AxReds/twincheck/compare/v1.0.1-rc.2...v1.0.1-rc.3
[1.0.1-rc.2]: https://github.com/AxReds/twincheck/compare/v1.0.1-rc.1...v1.0.1-rc.2
[1.0.1-rc.1]: https://github.com/AxReds/twincheck/compare/v1.0.0...v1.0.1-rc.1
