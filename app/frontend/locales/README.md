# Translation files

One file per language: `<code>.json`, where `<code>` is a BCP 47 tag (`it`, `pt-BR`, `zh-Hans`).
Languages are discovered automatically from this folder (and from `/state/locales` in a running
instance): adding a file adds the language to every language selector. No code change, no rebuild.

## Structure

```json
{
  "meta": {
    "code": "nl",
    "name": "Nederlands",
    "dir": "ltr",
    "status": "partial",
    "authors": ["Your Name <you@example.org>"]
  },
  "messages": {
    "common.save": "Opslaan",
    "header.cpu_value": "{cores} kernen · {pct}",
    "dup.summary": { "one": "{count} groep · {size} terug te winnen", "other": "{count} groepen · {size} terug te winnen" }
  }
}
```

| Field | Meaning |
|-------|---------|
| `meta.code` | BCP 47 code, identical to the file name |
| `meta.name` | name of the language **in the language itself** |
| `meta.dir` | `ltr` or `rtl` (Arabic, Hebrew, Persian…) |
| `meta.status` | `complete` (every key, reviewed by a native speaker), `partial` (some keys missing: English is used for them), `needs-review` (complete but not verified by a native speaker, e.g. machine-assisted) |
| `meta.authors` | list of translators |

## Keys

Keys are `namespace.name` (`nav.scan`, `error.bad_credentials`, `field.gps`). `en.json` is the
source and the fallback: a key missing in a language is shown in English, never as a raw key.
Never add keys that are not in `en.json`.

## Placeholders

`{name}` placeholders are replaced at run time: keep them exactly as in English (same names, not
translated), you can move them inside the sentence. Numbers in placeholders are formatted with the
user's locale.

## Plurals

A value can be an object of plural forms, chosen with `Intl.PluralRules` (CLDR). Provide exactly the
categories your language uses:

| Language | Categories |
|----------|-----------|
| en, de, nl, sv, tr | `one`, `other` |
| it, es, fr, pt-BR, ca | `one`, `many`, `other` (`many` = large round numbers, e.g. 1 000 000) |
| ru, uk, pl, cs | `one`, `few`, `many`, `other` |
| ar | `zero`, `one`, `two`, `few`, `many`, `other` |
| he | `one`, `two`, `other` |
| zh-Hans, ja, ko | `other` |

Check with `node -e "console.log(new Intl.PluralRules('xx').resolvedOptions().pluralCategories)"`.

## Not translated

Paths, file names, metadata values, ExifTool tag names (shown next to the translated label) and
**logs**. Log entries are always English: they have stable event codes and live in
`app/backend/twincheck/events.py`, never in these files (the checker rejects log texts here).

## Overrides without rebuilding

Put files with the same format in `/state/locales` (the `state` folder of your deployment): a new
code adds a language, an existing code overrides only the keys it contains.

## Checking

```sh
python3 app/tools/check_locales.py                 # all files + frontend scan, report per language
python3 app/tools/check_locales.py --dir state/locales
```
