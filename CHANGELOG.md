# Changelog

All notable changes are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/).

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

[1.0.1-rc.2]: https://github.com/AxReds/twincheck/compare/v1.0.1-rc.1...v1.0.1-rc.2
[1.0.1-rc.1]: https://github.com/AxReds/twincheck/compare/v1.0.0...v1.0.1-rc.1
