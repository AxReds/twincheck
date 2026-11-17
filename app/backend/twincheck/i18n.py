"""Locale discovery (repository + /state/locales overrides) and console texts.

Adding a language = adding ``<code>.json``; nothing else changes.  Files in
/state/locales override or extend the ones shipped in the repository.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

CODE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def _load(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("messages"), dict) and isinstance(data.get("meta"), dict):
            return data
    except (OSError, ValueError):
        pass
    return None


def discover(repo_dir: Path, state_dir: Path | None) -> dict[str, dict]:
    """code -> {meta, sources:[paths]}"""
    out: dict[str, dict] = {}
    for d in [repo_dir] + ([state_dir] if state_dir else []):
        if not d or not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            code = name[:-5]
            if not CODE_RE.match(code):
                continue
            data = _load(Path(d) / name)
            if not data:
                continue
            entry = out.setdefault(code, {"meta": {}, "sources": []})
            entry["meta"].update(data["meta"])
            entry["sources"].append(str(Path(d) / name))
    return out


def messages(code: str, repo_dir: Path, state_dir: Path | None) -> dict | None:
    if not CODE_RE.match(code or ""):
        return None
    found = discover(repo_dir, state_dir).get(code)
    if not found:
        return None
    merged: dict = {}
    meta: dict = {}
    for src in found["sources"]:
        data = _load(Path(src))
        if data:
            merged.update(data["messages"])
            meta.update(data["meta"])
    return {"meta": meta, "messages": merged}


def listing(repo_dir: Path, state_dir: Path | None) -> list[dict]:
    out = []
    for code, entry in discover(repo_dir, state_dir).items():
        m = entry["meta"]
        out.append({"code": code, "name": m.get("name", code), "dir": m.get("dir", "ltr"), "status": m.get("status", "partial")})
    return sorted(out, key=lambda x: x["code"])


class ConsoleText:
    """Console commands use English or the language given by APP_LANG."""

    def __init__(self, lang: str, repo_dir: Path, state_dir: Path | None = None):
        self.en = (messages("en", repo_dir, state_dir) or {}).get("messages", {})
        self.loc = (messages(lang, repo_dir, state_dir) or {}).get("messages", {}) if lang != "en" else {}

    def __call__(self, key: str, **params) -> str:
        text = self.loc.get(key) or self.en.get(key) or key
        if isinstance(text, dict):
            text = text.get("other", key)
        for k, v in params.items():
            text = text.replace("{" + k + "}", str(v))
        return text
