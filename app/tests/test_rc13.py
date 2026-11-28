"""rc.13 managed-exclusion regressions with synthetic data on both backends."""
import tempfile
import unittest

from helpers import make_user, write
from test_database import backends
from test_rc10 import instance
from twincheck.services.scan import _excluded, validate_exclusions


class Rc13Exclusions(unittest.TestCase):
    def test_exclude_all_and_malformed_patterns_are_rejected_but_shell_literals_work(self):
        for patterns, error in ((["*"], "exclusion_all"), (["**"], "exclusion_all"),
                                (["?*"], "exclusion_all"), (["*?*"], "exclusion_all"),
                                ([""], "exclusion_empty"), ([" "], "exclusion_empty"),
                                (["Backup", "Backup"], "exclusion_duplicate"),
                                (["[abc"], "exclusion_invalid"), (["[z-a]"], "exclusion_invalid"),
                                (["[]"], "exclusion_invalid"), (["a/b"], "exclusion_invalid")):
            with self.subTest(patterns=patterns), self.assertRaisesRegex(ValueError, error):
                validate_exclusions(patterns)
        patterns = [".*", "@eaDir", "#recycle", "$RECYCLE.BIN", "lost+found", "?", "??*", "[[]", "[]]", "[!a]"]
        validate_exclusions(patterns)
        for name in ("a", "ab", "[", "]", ".hidden", "@eaDir", "#recycle", "$RECYCLE.BIN", "lost+found"):
            self.assertTrue(_excluded(name, patterns), name)
        self.assertFalse(_excluded("Backup", ["backup"]), "matching must stay case-sensitive")
        self.assertTrue(_excluded(".twincheck-source", []), "internal exclusions cannot be removed")

    def test_list_and_description_save_is_confirmed_atomic_and_keeps_scanner_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    admin = inst.admin()
                    write(inst.data / "mine/Backup/keep.txt", b"synthetic")
                    _, user = make_user(admin, "synthetic", [{"path": "mine"}])
                    url = "/api/admin/settings/exclusions/test"
                    self.assertEqual(user.post(url, {"exclude": [], "name": "x"}).status, 403)
                    initial = admin.get("/api/admin/settings").json()
                    defaults = initial["scan_exclude_defaults"]
                    self.assertEqual(initial["settings"]["scan"]["exclude"], defaults)
                    patterns = [p for p in defaults if p != "#recycle"] + ["Backup"]
                    body = {"scan": {"exclude": patterns, "exclude_descriptions": {"Backup": "Synthetic backup"}}}
                    self.assertEqual(admin.patch("/api/admin/settings", body).status, 400)
                    self.assertEqual(admin.get("/api/admin/settings").json()["settings"]["scan"],
                                     initial["settings"]["scan"])
                    body["scan"]["confirm"] = True
                    response = admin.patch("/api/admin/settings", body)
                    self.assertEqual(response.status, 200, response.text)
                    current = admin.get("/api/admin/settings").json()["settings"]["scan"]
                    self.assertEqual(current["exclude"], patterns)
                    self.assertEqual(current["exclude_descriptions"], {"Backup": "Synthetic backup"})
                    for name in ("Backup", "backup", "#recycle", ".hidden", ".twincheck-source", "Thumbs.db"):
                        response = admin.post(url, {"exclude": patterns, "name": name})
                        self.assertEqual(response.status, 200, response.text)
                        self.assertEqual(bool(response.json()["matches"]), _excluded(name, patterns))
                    for invalid in (["*"], ["Backup", "Backup"], ["[abc"], [None], [" "], ["x"] * 101):
                        self.assertEqual(admin.post(url, {"exclude": invalid, "name": "x"}).status, 400)
                        self.assertEqual(admin.patch("/api/admin/settings", {"scan": {"exclude": invalid, "confirm": True}}).status, 400)
                    self.assertEqual(admin.get("/api/admin/settings").json()["settings"]["scan"], current)
                    self.assertEqual((inst.data / "mine/Backup/keep.txt").read_bytes(), b"synthetic")
                    self.assertEqual(inst.core.db.scalar("SELECT COUNT(*) FROM jobs"), 0,
                                     "editing/testing exclusions must not launch jobs")
                    inst.core.vault.load()
                    self.assertEqual(inst.core.settings_section("scan"), current)
