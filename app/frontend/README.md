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

Navigation, user-menu rows and Administration tabs use bundled Lucide inline SVG
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
`POST /api/me/theme`, without a database schema change. Local storage remembers
the last authenticated user's restored choice as well as selector changes for
the login page; after login the authenticated user's vault preference takes
precedence. Logout therefore retains that user's theme even when another user
previously chose a different theme in this browser. Browser storage failures
are reported explicitly.

## Header metrics

Administration appears only in the administrative user menu, not the main
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

On session expiry, the first unauthorized response stops all polling and opens the
login form once. Late responses cannot replace that form or sign out a newly
authenticated session. Typed values and focus stay intact; a failed login does
not restart polling. Polling resumes only after a successful login and bootstrap.

Completed scan cards show indexed files, newly computed hashes, reused hashes and
files still without a hash. Older scans show their recorded indexed/hashed counts
and explain that reused hashes were not counted; a stale phase is not shown as a
completion result.

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
