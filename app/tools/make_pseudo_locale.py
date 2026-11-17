#!/usr/bin/env python3
"""Generate a pseudo-locale (qps-ploc) with accented, ~60% longer texts to test
that the layout never breaks with verbose languages.

Usage: python3 app/tools/make_pseudo_locale.py OUTPUT_DIR   (e.g. ./state/locales)
The file is NOT shipped in the repository; put it in /state/locales to try it.
"""
import json
import re
import sys
from pathlib import Path

MAP = str.maketrans("aceinousyACEINOUSY", "àçéîñöüšýÀÇÉÎÑÖÜŠÝ")
PH = re.compile(r"(\{\w+\})")


def pseudo(text: str) -> str:
    parts = PH.split(text)
    out = "".join(p if PH.fullmatch(p) else p.translate(MAP) for p in parts)
    pad = "·" * max(2, int(len(text) * 0.6))
    return f"⟦{out} {pad}⟧"


def build(en: dict) -> dict:
    msgs = {}
    for k, v in en["messages"].items():
        msgs[k] = {f: pseudo(t) for f, t in v.items()} if isinstance(v, dict) else pseudo(v)
        if isinstance(v, dict):
            msgs[k] = {"one": msgs[k].get("one", pseudo(v["other"])), "other": msgs[k]["other"]}
    return {"meta": {"code": "qps-ploc", "name": "Pseudo (long)", "dir": "ltr", "status": "partial", "authors": ["generated"]},
            "messages": msgs}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    en = json.loads((Path(__file__).resolve().parents[1] / "frontend" / "locales" / "en.json").read_text(encoding="utf-8"))
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "qps-ploc.json").write_text(json.dumps(build(en), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(out / "qps-ploc.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
