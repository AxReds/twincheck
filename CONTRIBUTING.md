# Contributing to Photo Organizer

Thank you! Translations are the easiest and most valuable way to help. Code contributions are
welcome too. English is the source language of the project and of this guide.

## Add a new language (step by step)

1. **Fork** the repository and create a branch: `git checkout -b i18n-nl`.
2. **Copy** the English file, naming it with the BCP 47 code of your language
   (`nl`, `pt-PT`, `zh-Hant`, `sr-Latn`…):
   ```sh
   cp app/frontend/locales/en.json app/frontend/locales/nl.json
   ```
3. **Set the metadata** at the top of the file:
   ```json
   "meta": { "code": "nl", "name": "Nederlands", "dir": "ltr", "status": "partial", "authors": ["Your Name"] }
   ```
   `name` is written in the language itself; `dir` is `rtl` for right-to-left scripts; use
   `partial` while keys are missing, `complete` when every key is translated and reviewed.
4. **Translate** every value in `"messages"`. You can leave keys out: the UI shows English for them.
   Never add keys that are not in `en.json`.
5. **Plurals**: values like `{"one": "…", "other": "…"}` must list exactly the plural categories of your
   language (see [app/frontend/locales/README.md](app/frontend/locales/README.md#plurals)), e.g.
   Polish needs `one`, `few`, `many`, `other`.
6. **Run the checker** and fix every error:
   ```sh
   python3 app/tools/check_locales.py
   ```
   If your language is not yet in the checker's plural table (`PLURALS` in
   `app/tools/check_locales.py`), add it in the same pull request.
7. **Try it locally** (no rebuild needed for translation files placed in `state/locales`):
   ```sh
   mkdir -p state/locales && cp app/frontend/locales/nl.json state/locales/
   docker compose up -d        # then choose the language on the login page
   ```
   Or rebuild to test the file from the repository: `docker compose build && docker compose up -d`.
   Check long texts and, for RTL languages, the mirrored layout. A pseudo-language with very long
   texts helps: `python3 app/tools/make_pseudo_locale.py state/locales`.
8. **Add your language** to the table in `README.md` and `README.it.md`.
9. **Open a pull request** using the template. CI runs the checker and the tests; it must be green.

## Improve an existing translation

Edit the value in `app/frontend/locales/<code>.json`, run the checker, open a pull request. If you are
a native speaker and you reviewed the whole file, change `"status": "needs-review"` to `"complete"`
and add yourself to `authors`. To report a mistake without editing, open a *Translation error* issue.

## Style rules for translators

* **Tone**: short, clear, neutral; address the user the way common software does in your language.
* **Placeholders** `{count}`, `{path}`, `{size}`… must stay exactly as they are (you may move them).
* **Do not translate**: product name *Photo Organizer*, technical terms and values such as `/data`,
  `/state`, `.env`, `APP_MASTER_KEY`, `BIND_ADDRESS`, `ALLOWED_NETWORKS`, SHA-256, EXIF, IPTC, XMP,
  QuickTime, ExifTool, SQLite, MariaDB, UTC, HTTP/HTTPS, file extensions, the account name `admin`.
* **Consistency**: use the same word for the same concept everywhere (source, destination, preview,
  review, job, duplicates, vault, manifest).
* **No HTML** in texts. Use the typographic quotes of your language.
* **Logs are never translated**: they are English by design and are not in these files.

## Code contributions

* Python 3.12, standard library first; new runtime dependencies must be pure Python and justified.
* No visible text in code: add keys to `app/frontend/locales/en.json` and use `t('key')` in the
  frontend; backend errors return codes (`{"error": "code"}`) translated by the UI as `error.<code>`.
* Logs: add an event code with an English template to `app/backend/photoorg/events.py`.
* Every write to user files must go through a plan (preview) + confirmation, the manifest and
  `mover.move_file` (journal, never overwrite).
* New environment variables: add them to `ENV_PARAMS` in `config.py` **and** to the tables in
  `app/backend/README.md` and `docker/README.md` (a test checks it).
* Database changes: add a migration (see [app/README.md](app/README.md#migrations)) and a test.
* Run before pushing:
  ```sh
  python3 -B app/tools/check_locales.py
  python3 -B -m unittest discover -s app/tests
  ```
* Never commit real photos, databases, `.env` files, keys, passwords or tokens.

## Security issues

Do not open public issues for vulnerabilities: contact the maintainers privately (see the repository
security policy).

By contributing you agree that your contribution is licensed under the AGPL-3.0.
