# Frontend

Plain HTML + ES modules + CSS, served by the backend. No build step, no framework, no CDN: it works
offline. The Content-Security-Policy allows only same-origin resources.

```
static/index.html          empty shell (no visible text: everything comes from the locale files)
static/css/app.css         logical CSS properties only (RTL-safe), no fixed widths
static/js/app.js           boot, fixed header with live metrics, navigation, router
static/js/i18n.js          minimal i18n: t(key, params), Intl.PluralRules, Intl number/date/size formats
static/js/api.js           JSON client with CSRF header and translated errors
static/js/dom.js           DOM helpers (text nodes only, never innerHTML)
static/js/components.js    job cards, polling (paused when the tab is hidden), folder browser
static/js/exclusions.js    validated managed scan-exclusion list and server-matched name test
static/js/icons.js         bundled inline SVG menu icons (Lucide ISC / Feather MIT)
static/js/views/*.js       setup, login, tool pages, review, administration, export & migration
static/img/logo.svg        logo (folder + photo + calendar), readable on light and dark backgrounds
static/img/favicon.svg     favicon
locales/*.json             translations (see locales/README.md)
```

## Rules for contributors

* No visible string in code: use `t('namespace.key')` and add the key to `locales/en.json`.
  `python3 app/tools/check_locales.py` fails on hard-coded text and on keys missing from `en.json`.
* Paths, file names, metadata values, ExifTool tag names and log entries are shown as they are.
* Insert text with `textContent` / `h()`; never `innerHTML`.

Navigation, user-menu rows and Settings tabs use bundled Lucide inline SVG
icons. Geometry is constructed with `createElementNS`, not fetched at runtime.
The [ISC/MIT notices](static/js/icons.LICENSE) ship with the app; both licenses
are compatible with GPL-3.0. Icons inherit text color, are `aria-hidden` and
non-focusable; visible labels remain at every width. Navigation links also keep
translated accessible names and tooltips. Narrow layouts wrap without hiding text.

## Theme

Auto follows live OS/browser light/dark changes and is the default. A sun/moon
button next to the user menu switches directly between Light and Dark; the menu
also offers Auto / Light / Dark. The header and metrics use the selected palette
as well as the content and native controls. Login offers the same controls without
rebuilding the form or losing typed values.

The per-user choice is validated and saved in the encrypted vault through
`POST /api/me/theme`, without a database schema change. Local storage and a non-secret `tc_theme` cookie remember
the last authenticated user's restored choice as well as selector changes for
the login page; after login the authenticated user's vault preference takes
precedence. Logout therefore retains that user's theme even when another user
previously chose a different theme in this browser. Browser storage failures
are reported explicitly.
The server renders the authenticated preference (or cookie) directly on `<html>`.
CSS follows `prefers-color-scheme` for Auto and loads in the head before the body.
No inline script or CSP exception is used, including for login and logout.

## Header metrics

Settings appears only in the administrative user menu, not the main
navigation. Admin pages highlight that menu button as the current section;
the internal admin navigation underlines its current page. Direct admin routes
redirect base users Home without calling admin APIs.

Scan initializes the selection from the current user's most recent scan when
still accessible, then the first assigned source. Otherwise an inline message
asks for a selection. Browsing alone does not select a folder: only the actual
selection is marked, and its label always matches the submitted source or path.
Base users can browse each assigned source and select its subfolders. The tree
stops at the source root; listing and scanning enforce that boundary server-side,
including resolved symlinks. Assigned destinations are not additional scan roots.
Selecting a subfolder scans and counts only that subtree; it does not scan the
destination or change missing/hash bookkeeping for files outside the selection.
Whole-source scans retain their existing source-plus-destination behaviour.

`GET /api/sysinfo` every 5 s while the tab is visible (`document.visibilitychange` pauses it). On
narrow screens the metrics collapse into a panel opened by the *System* button; logo and user menu
stay visible.

When an account must change its password, the normal shell and every poller are
replaced once by a header without live metrics and the password-change form.
Repeated `password_change_required` responses cannot recreate the form while
the user is typing. Validation errors keep the values and focus. A successful
change restores navigation and polling in place, without reloading the page.

On session expiry, the first unauthorized response stops all polling and opens the
login form once. Late responses cannot replace that form or sign out a newly
authenticated session. Typed values and focus stay intact; a failed login does
not restart polling. Polling resumes only after a successful login and bootstrap.

The tool flow is guided by one compact four-step bar:
Analyze → Review (Duplicates, Metadata, Review) → Organize → History & Undo.
Only Scan exposes an analysis action. The other pages show the last analysis time,
an empty/stale notice linking to Scan, and their read/action controls.
Job-trigger buttons are disabled while a request or the resulting job is active.

Without a valid completed analysis, Home, Duplicates, Metadata and Organize show a localized
no-results notice rather than zero-result claims. History remains independent of analysis.
The sign-in notice close button is positioned inside its first-line corner. Navigation focuses
the main container without a container ring; keyboard controls retain `:focus-visible` outlines.
Review dimensions are formatted as `width × height` in the field table and caption.

Filter selects have compact natural widths and wrap inside the viewport. `filterControls`
keeps applied values separate from pending text/number edits: discrete selections and dates
commit immediately, Enter/Apply commits text, and Clear restores page defaults.
Apply is present only for text/number filters, with a pending indicator. URL reconstruction
keeps applied filters; navigation resets them and table-size preferences stay per user.
Security date ranges are inclusive UTC dates, consistent with the API instants.
Excluded-name matching/validation is server-side using the scanner's actual basename-only,
case-sensitive shell semantics; custom descriptions stay in the vault, without a migration.

Operational timestamps use the browser timezone. EXIF wall-clock dates use a
timezone-free formatter, with a supplied EXIF offset appended rather than
converted silently.

The Review screen starts the item request and its preview request in parallel,
without a separate probe request. A completed preview of the next review item is
prefetched. The request is still scope-checked through the review item and file
before reading a generated preview. Preview artifacts live only under
`/state/cache/previews/<owner>/<size-mtime-hash>`, are pruned to the configured
limit, and are invalidated when the file signature changes.

ExifTool reads and writes share one long-lived `-stay_open` process. It is
protected by a lock, restarted when it exits, and closed with the application.

Completed scan cards show indexed files, newly computed hashes, reused hashes and
files still without a hash. Older scans show their recorded indexed/hashed counts
and explain that reused hashes were not counted; a stale phase is not shown as a
completion result.

## Accessible selectors and long tables

`components.comboBox` is a closed value/chevron control with an overlaid popup that flips
upward when needed. Its search input, clear button, section titles, listbox and filled
selected option support case-insensitive substring filtering, active-descendant keyboard
navigation, Enter selection and Escape/outside dismissal. Search text never becomes a value.
Pointer and touch select the same options. Library timezone and Logs Event code share it.
Timezone sections are Suggested/All timezones; administrator event sections are Present/Not present.
Normal users see only their own present codes, without Only events present.

`components.tablePaging` remembers 10/50/100/All in browser local storage keyed by stable
user id and table, with a visible warning when storage is unavailable. A remembered All
choice still confirms before loading more than 1,000 rows. Sticky headers and page ranges
remain independent of the selected size. Filter/action rows share bottom alignment;
Logs filters occupy the first row, wrapping one per line on narrow screens.
Natural-width, left-aligned CSV export buttons occupy the next row. Empty filtered exports
are disabled and zero-result pagers hidden. Security guidance is one link to Security Log,
with role-scoped instance events, never security events in operational Diagnostics.
Jobs polling refreshes only rows/cards, retaining the pagination controls. All automatic
card/metrics refreshes pause while controls in their view have focus or a popup is open,
including a focus change while a request is in flight; page scroll stays unchanged.

Metadata shows one summary with an information tooltip, followed directly by filters,
Start review and the queue, with rules collapsed below. Postponed items show a muted timestamped
still-to-review note in the always-visible Decision column, with no skipped badge.
Review inputs use italic examples explicitly prefixed with "e.g."
with date rules below the field. Fields/actions precede the preview in DOM/tab order:
desktop is approximately 60% fields left, 40% sticky preview right; mobile shows a thumbnail
and filename first and retains the fixed bottom action bar. Preview enlargement uses a
native dialog; Escape closes it and restores focus.

The signed-in version appears below the brand with a full version/build tooltip.
The menu order is Language, Theme, Settings (administrative roles), Security Log,
Diagnostics, Change Password and Log Out. Settings opens General; the separate audit
subpage is redirected to Security Log. Filter URLs survive reload while navigation links
reset them, independently of per-user rows-per-page preferences.
Organize previews retain their job identity and explain why a saved plan became stale.
History & Undo lists execution operations, with global undo retained until #110.
Unchanged auto-refresh data is a DOM no-op; changed nodes reconcile in place. Navigation
and pagehide abort outstanding reads and stop view timers.

## Replacing the logo

Replace `static/img/logo.svg` (shown in the header, login and wizard at 32×32 px) and
`static/img/favicon.svg` with your own SVG files of the same name. Keep them self-contained (no
external references: the CSP blocks them) and readable on both light and dark backgrounds. In a
container deployment rebuild the image (`docker compose build`) or mount your files read-only:

```yaml
    volumes:
      - ./my-logo.svg:/opt/twincheck/app/frontend/static/img/logo.svg:ro
      - ./my-favicon.svg:/opt/twincheck/app/frontend/static/img/favicon.svg:ro
```
