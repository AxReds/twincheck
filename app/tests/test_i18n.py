"""Internationalisation: catalogue consistency, discovery, overrides, fallback,
plurals, RTL metadata, English-only logs."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import APP_DIR, STRONG, Instance, make_user, sample_library, wait_job

sys.path.insert(0, str(APP_DIR / "tools"))
import check_locales  # noqa: E402
import make_pseudo_locale  # noqa: E402

LOCALES = APP_DIR / "frontend" / "locales"
REQUIRED = ["en", "it", "es", "fr", "de", "pt-BR", "zh-Hans", "ja", "ru", "ar"]


class Catalogue(unittest.TestCase):
    def test_checker_passes_for_repository(self):
        rep = check_locales.run()
        errors = [(l["code"], e) for l in rep["languages"] for e in l["errors"]] + rep["frontend"]
        self.assertEqual(errors, [])

    def test_required_languages_present_with_same_keys(self):
        en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))["messages"]
        for code in REQUIRED:
            data = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
            self.assertEqual(set(data["messages"]), set(en), code)
            self.assertEqual(data["meta"]["code"], code)
            if code != "en":
                self.assertEqual(data["meta"]["status"], "needs-review", code)
        self.assertEqual(json.loads((LOCALES / "ar.json").read_text(encoding="utf-8"))["meta"]["dir"], "rtl")

    def test_plural_forms_ru_ar(self):
        for code, cats in (("ru", {"one", "few", "many", "other"}), ("ar", {"zero", "one", "two", "few", "many", "other"})):
            msgs = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))["messages"]
            plural = {k: v for k, v in msgs.items() if isinstance(v, dict)}
            self.assertTrue(plural)
            for k, v in plural.items():
                self.assertEqual(set(v), cats, f"{code}:{k}")

    def test_checker_reports_defective_file(self):
        d = tempfile.mkdtemp()
        try:
            bad = {"meta": {"code": "nl", "name": "", "dir": "sideways", "status": "complete", "authors": []},
                   "messages": {"common.save": "Opslaan {oops}", "app.name": "TwinCheck", "extra.key": "x",
                                "dup.summary": {"one": "{count} groep", "other": "{count} groepen", "few": "x"},
                                "logs.title": "File moved from {src} to {dst}"}}
            Path(d, "nl.json").write_text(json.dumps(bad), encoding="utf-8")
            Path(d, "xx.json").write_text("{not json", encoding="utf-8")
            rep = check_locales.run([Path(d)], frontend=False)
            self.assertFalse(rep["ok"])
            by = {l["code"]: " | ".join(l["errors"]) for l in rep["languages"] if l["file"].startswith(d)}
            for frag in ("meta.name", "meta.dir", "authors", "not in en.json", "keys missing", "placeholders", "plural forms", "log text"):
                self.assertIn(frag, by["nl"], frag)
            self.assertIn("invalid JSON", by["xx"])
            out = subprocess.run([sys.executable, "-B", str(APP_DIR / "tools" / "check_locales.py"), "--dir", d, "--no-frontend"],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 1)
            self.assertIn("RESULT: FAILED", out.stdout)
        finally:
            shutil.rmtree(d)

    def test_no_log_text_in_any_locale(self):
        events = check_locales.load_event_texts()
        texts = [check_locales._static_text(t) for t in events.values() if len(check_locales._static_text(t)) >= 12]
        for f in LOCALES.glob("*.json"):
            blob = f.read_text(encoding="utf-8")
            for t in texts:
                self.assertNotIn(t, blob, f"{f.name}: {t}")
            self.assertNotIn('"FILE_MOVED"', blob)


class Discovery(unittest.TestCase):
    def setUp(self):
        self.inst = Instance().start()

    def tearDown(self):
        self.inst.cleanup()

    def test_new_language_file_appears_and_override_wins(self):
        c = self.inst.client()
        codes = [l["code"] for l in c.get("/api/locales").json()]
        for code in REQUIRED:
            self.assertIn(code, codes)
        # a new language dropped into /state/locales, no code change
        test_lang = {"meta": {"code": "nl", "name": "Nederlands", "dir": "ltr", "status": "partial", "authors": ["test"]},
                     "messages": {"common.save": "Opslaan"}}
        (self.inst.state / "locales" / "nl.json").write_text(json.dumps(test_lang), encoding="utf-8")
        self.assertIn("nl", [l["code"] for l in c.get("/api/locales").json()])
        self.assertEqual(c.get("/api/locales/nl").json()["messages"]["common.save"], "Opslaan")
        # override of a shipped language
        (self.inst.state / "locales" / "it.json").write_text(json.dumps({"meta": {"code": "it", "name": "Italiano", "dir": "ltr",
                                                                                   "status": "needs-review", "authors": ["x"]},
                                                                          "messages": {"common.save": "Salva (personalizzato)"}}), encoding="utf-8")
        it = c.get("/api/locales/it").json()["messages"]
        self.assertEqual(it["common.save"], "Salva (personalizzato)")
        self.assertIn("nav.scan", it, "override merges with the shipped file")
        self.assertEqual(c.get("/api/locales/..%2Fvault").status, 404)
        # pseudo-locale generator for layout tests
        make_pseudo_locale.main.__globals__["sys"].argv = ["x", str(self.inst.state / "locales")]
        make_pseudo_locale.main()
        ps = c.get("/api/locales/qps-ploc").json()["messages"]
        en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))["messages"]
        self.assertGreater(len(ps["nav.scan"]), len(en["nav.scan"]) * 1.5)

    def test_language_saved_per_user(self):
        self.inst.setup()
        a = self.inst.admin()
        self.assertEqual(a.post("/api/me/lang", {"lang": "ar"}).status, 200)
        self.assertEqual(a.post("/api/me/lang", {"lang": "xx-bad"}).status, 400)
        b = self.inst.admin()
        self.assertEqual(b.get("/api/me").json()["lang"], "ar")


class EnglishOnlyLogs(unittest.TestCase):
    def test_logs_are_english_whatever_the_ui_language(self):
        inst = Instance(env={"APP_LANG": "ja"}).start()
        try:
            inst.setup()
            a = inst.admin()
            a.post("/api/me/lang", {"lang": "ar"})
            sample_library(inst.data / "u")
            uid, u = make_user(a, "jauser", [{"path": "u"}])
            u.post("/api/me/lang", {"lang": "ja"})
            sid = u.get("/api/sources").json()["sources"][0]["id"]
            wait_job(inst.core, u.post("/api/scan", {"source_id": sid}).json()["job_id"])
            wait_job(inst.core, u.post("/api/duplicates/analyze").json()["job_id"])
            jid = u.post("/api/duplicates/plan").json()["job_id"]
            wait_job(inst.core, jid)
            wait_job(inst.core, u.post(f"/api/plans/{jid}/confirm", {"confirm": True}).json()["job_id"])
            a.post(f"/api/admin/users/{uid}/password")
            rows = inst.core.db.query("SELECT event, message FROM run_log")
            audit = inst.core.vault.data["audit"]
            texts = [r["message"] for r in rows] + [x["message"] for x in audit]
            self.assertTrue(any(r["event"] == "FILE_MOVED" for r in rows))
            for t in texts:
                stripped = "".join(ch for ch in t if ord(ch) < 0x3000 or ch.isspace())
                # message text must be the English template (paths may contain any script)
                self.assertTrue(t.isascii() or any(w in t for w in ("File", "Job", "Database", "Scanning", "Duplicate", "Password")), t)
                self.assertNotRegex(stripped, r"[\u0600-\u06FF]")
            env_ja = {**inst.env}
            from test_crypto_vault import run_cli
            r = run_cli(["generate-key"], {"APP_LANG": "it"})
            self.assertIn("APP_MASTER_KEY=", r.stdout)
            self.assertNotIn("New master key", r.stdout, "console commands follow APP_LANG")
            self.assertIn("chiave", r.stdout.lower())
            del env_ja
        finally:
            inst.cleanup()


if __name__ == "__main__":
    unittest.main()
