#!/usr/bin/env python3
"""Verify translation files and the frontend's use of translation keys.

Checks for every app/frontend/locales/<code>.json (and optional extra dirs):
  * valid JSON with "meta" and "messages";
  * metadata: code (valid BCP 47, equal to the file name), name, dir (ltr/rtl),
    status (complete / partial / needs-review), authors (non-empty list);
  * keys missing or extra compared with en.json (missing keys are an error for
    status "complete", a warning otherwise: the UI falls back to English);
  * placeholders ({name}) identical to the English text;
  * plural forms: exactly the CLDR categories required by the language;
  * no log text: log messages are English-only and must never appear in locale
    files (compared with the event catalogue of the backend);
  * no HTML markup in texts.
Frontend checks (--frontend, default on):
  * every key literal used in the JavaScript exists in en.json;
  * no visible text hard-coded in index.html or in the JavaScript.

Prints a completeness report per language. Exit code 1 on any error.

Usage: python3 app/tools/check_locales.py [--dir EXTRA_DIR] [--no-frontend] [--json]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "frontend" / "locales"
STATIC = ROOT / "frontend" / "static"
EVENTS_FILE = ROOT / "backend" / "photoorg" / "events.py"

BCP47 = re.compile(r"^[A-Za-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|\d{3}))?(-[A-Za-z0-9]{5,8})*$|^qps-[a-z]{4}$")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
STATUSES = {"complete", "partial", "needs-review"}

# CLDR cardinal plural categories (same as Intl.PluralRules in current browsers)
PLURALS = {
    "en": {"one", "other"}, "it": {"one", "many", "other"}, "es": {"one", "many", "other"}, "fr": {"one", "many", "other"},
    "pt": {"one", "many", "other"}, "de": {"one", "other"}, "nl": {"one", "other"}, "sv": {"one", "other"}, "da": {"one", "other"},
    "nb": {"one", "other"}, "fi": {"one", "other"}, "el": {"one", "other"}, "tr": {"one", "other"}, "hu": {"one", "other"},
    "ca": {"one", "many", "other"}, "ro": {"one", "few", "other"}, "zh": {"other"}, "ja": {"other"}, "ko": {"other"},
    "vi": {"other"}, "th": {"other"}, "id": {"other"}, "ru": {"one", "few", "many", "other"}, "uk": {"one", "few", "many", "other"},
    "pl": {"one", "few", "many", "other"}, "cs": {"one", "few", "many", "other"}, "sk": {"one", "few", "many", "other"},
    "ar": {"zero", "one", "two", "few", "many", "other"}, "he": {"one", "two", "other"}, "qps": {"one", "other"},
}


def plural_categories(code: str) -> set[str] | None:
    return PLURALS.get(code) or PLURALS.get(code.split("-")[0].lower())


def load_event_texts() -> dict[str, str]:
    """Read EVENTS from events.py without importing the backend."""
    tree = ast.parse(EVENTS_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id == "EVENTS":
                return ast.literal_eval(node.value)
    return {}


def placeholders(value) -> set[str]:
    if isinstance(value, dict):
        out: set[str] = set()
        for v in value.values():
            out |= set(PLACEHOLDER.findall(str(v)))
        return out
    return set(PLACEHOLDER.findall(str(value)))


def _static_text(template: str) -> str:
    return re.sub(r"\s+", " ", PLACEHOLDER.sub(" ", template)).strip()


def check_file(path: Path, en: dict, events: dict[str, str]) -> dict:
    res = {"file": str(path), "code": path.stem, "errors": [], "warnings": [], "name": None, "status": None, "completeness": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        res["errors"].append(f"invalid JSON: {exc}")
        return res
    if not isinstance(data, dict) or not isinstance(data.get("meta"), dict) or not isinstance(data.get("messages"), dict):
        res["errors"].append('structure must be {"meta": {...}, "messages": {...}}')
        return res
    meta, msgs = data["meta"], data["messages"]
    res["name"], res["status"] = meta.get("name"), meta.get("status")
    code = meta.get("code")
    if code != path.stem:
        res["errors"].append(f"meta.code {code!r} differs from file name {path.stem!r}")
    if not code or not BCP47.match(str(code)):
        res["errors"].append(f"invalid language code {code!r} (BCP 47 expected, e.g. it, pt-BR, zh-Hans)")
    if not str(meta.get("name") or "").strip():
        res["errors"].append("meta.name missing (language name in the language itself)")
    if meta.get("dir") not in ("ltr", "rtl"):
        res["errors"].append("meta.dir must be 'ltr' or 'rtl'")
    if meta.get("status") not in STATUSES:
        res["errors"].append(f"meta.status must be one of {sorted(STATUSES)}")
    if not isinstance(meta.get("authors"), list) or not meta.get("authors"):
        res["errors"].append("meta.authors must be a non-empty list")
    cats = plural_categories(str(code or path.stem))
    if cats is None:
        res["warnings"].append("plural rules unknown to the checker: add the language to PLURALS")
    en_keys, keys = set(en), set(msgs)
    missing, extra = sorted(en_keys - keys), sorted(keys - en_keys)
    if extra:
        res["errors"].append(f"{len(extra)} keys not in en.json: {', '.join(extra[:10])}")
    if missing:
        (res["errors"] if meta.get("status") == "complete" else res["warnings"]).append(
            f"{len(missing)} keys missing (English is used): {', '.join(missing[:10])}")
    res["completeness"] = round(100.0 * len(en_keys & keys) / len(en_keys), 1) if en_keys else 100.0
    log_texts = [_static_text(t) for t in events.values() if len(_static_text(t)) >= 12]
    for key in sorted(en_keys & keys):
        src, val = en[key], msgs[key]
        if isinstance(src, dict) != isinstance(val, dict):
            res["errors"].append(f"{key}: plural/non-plural form differs from English")
            continue
        if isinstance(val, dict):
            if cats is not None and set(val) != cats:
                res["errors"].append(f"{key}: plural forms {sorted(val)} but {code} needs {sorted(cats)}")
            for form, txt in val.items():
                if not isinstance(txt, str) or not txt.strip():
                    res["errors"].append(f"{key}.{form}: empty text")
                if not set(PLACEHOLDER.findall(str(txt))) <= placeholders(src):
                    res["errors"].append(f"{key}.{form}: unknown placeholder")
            if placeholders(val) != placeholders(src):
                res["errors"].append(f"{key}: placeholders {sorted(placeholders(val))} != English {sorted(placeholders(src))}")
        else:
            if not isinstance(val, str) or not val.strip():
                res["errors"].append(f"{key}: empty text")
                continue
            if placeholders(val) != placeholders(src):
                res["errors"].append(f"{key}: placeholders {sorted(placeholders(val))} != English {sorted(placeholders(src))}")
        for txt in (val.values() if isinstance(val, dict) else [val]):
            if re.search(r"<\s*[a-zA-Z/]", str(txt)):
                res["errors"].append(f"{key}: HTML markup is not allowed")
            norm = _static_text(str(txt))
            for lt in log_texts:
                if lt and lt in norm:
                    res["errors"].append(f"{key}: contains a log text ({lt!r}); logs are English-only and never translated")
    for key in keys:
        if key.startswith(("log.", "event.", "events.")) or re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", key):
            res["errors"].append(f"{key}: log entries must not have translation keys")
    return res


# --- frontend -------------------------------------------------------------------------------
class _TextFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.texts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for k, v in attrs:
            if k in ("title", "placeholder", "alt", "aria-label") and v and v.strip():
                self.texts.append(f"{k}={v}")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.texts.append(data.strip())


STRING_RE = re.compile(r"""(?<![\w$])(['"`])((?:\\.|(?!\1).)*?)\1""", re.S)
VISIBLE_ATTR_RE = re.compile(r"""(?:placeholder|title|alt|'aria-label'|textContent)\s*[:=]\s*(['"`])([^'"`]*[A-Za-z][^'"`]*)\1""")
ALLOWED_LITERAL = re.compile(
    r"^(?:[a-z0-9_.\-/:#?&=%+]*|[A-Z0-9_]+|\.\.|…|\u2026|[\W\d_]*|[a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*)$")


def frontend_problems(en: dict) -> list[str]:
    problems = []
    # index.html: no visible text at all
    finder = _TextFinder()
    finder.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    for txt in finder.texts:
        problems.append(f"index.html: hard-coded visible text {txt!r}")
    namespaces = {k.split(".", 1)[0] for k in en}
    key_re = re.compile(r"^(" + "|".join(sorted(map(re.escape, namespaces))) + r")\.[a-z0-9_]+(\.[a-z0-9_]+)*\.?$")
    file_name = re.compile(r"\.(csv|json|svg|js|css|poarc|html|png|jpg)$")
    for js in sorted((STATIC / "js").rglob("*.js")):
        src = js.read_text(encoding="utf-8")
        rel = js.relative_to(STATIC)
        for lineno, line in enumerate(src.splitlines(), 1):
            if "i18n-ignore" in line or line.strip().startswith("//"):
                continue
            for m in STRING_RE.finditer(line):
                lit = m.group(2)
                if m.group(1) == "`":
                    lit = re.sub(r"\$\{[^}]*\}", "", lit)
                if key_re.match(lit) and not file_name.search(lit):
                    if lit.endswith((".", "_")):
                        if not any(k.startswith(lit) for k in en):
                            problems.append(f"{rel}:{lineno}: no key with prefix {lit!r}")
                    elif lit not in en and not any(k.startswith(lit + ".") for k in en):
                        problems.append(f"{rel}:{lineno}: key {lit!r} missing in en.json")
                    continue
                if ALLOWED_LITERAL.match(lit):
                    continue
                if re.search(r"[A-Z][a-z]+\s+\w|[a-z]\s+[a-z]+[.!?:]$|[^\x00-\x7F]{2,}", lit) and not lit.startswith(("/", "#", ".", "[")):
                    if " " in lit or re.search(r"[^\x00-\x7F]", lit):
                        problems.append(f"{rel}:{lineno}: possible hard-coded text {lit!r} (use t('key'))")
            for m in VISIBLE_ATTR_RE.finditer(line):
                val = m.group(2)
                if not re.fullmatch(r"[A-Z0-9_]+|[\d.,:+\- ]+|/[\w/.\-]*", val):
                    problems.append(f"{rel}:{lineno}: hard-coded attribute text {val!r}")
    return problems


def run(extra_dirs: list[Path] | None = None, frontend: bool = True, locales_dir: Path = LOCALES) -> dict:
    en_path = locales_dir / "en.json"
    en_data = json.loads(en_path.read_text(encoding="utf-8"))
    en = en_data["messages"]
    events = load_event_texts()
    files = sorted(locales_dir.glob("*.json"))
    for d in extra_dirs or []:
        files += sorted(Path(d).glob("*.json"))
    results = [check_file(f, en, events) for f in files]
    fe = frontend_problems(en) if frontend else []
    ok = not fe and all(not r["errors"] for r in results)
    return {"ok": ok, "languages": results, "frontend": fe}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", action="append", default=[], help="extra directory with locale files (e.g. /state/locales)")
    ap.add_argument("--no-frontend", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = run([Path(d) for d in args.dir], not args.no_frontend)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep["ok"] else 1
    print(f"{'code':<10} {'name':<22} {'status':<13} {'complete':>9}  result")
    for r in rep["languages"]:
        flag = "OK" if not r["errors"] else f"{len(r['errors'])} error(s)"
        print(f"{r['code']:<10} {str(r['name'] or '-'):<22} {str(r['status'] or '-'):<13} {r['completeness']:>8.1f}%  {flag}")
    for r in rep["languages"]:
        for e in r["errors"]:
            print(f"ERROR   {r['code']}: {e}")
        for w in r["warnings"]:
            print(f"warning {r['code']}: {w}")
    for p in rep["frontend"]:
        print(f"ERROR   frontend: {p}")
    print("RESULT:", "OK" if rep["ok"] else "FAILED")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
