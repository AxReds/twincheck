"""Synthetic regressions for library timezone, provenance and rc.8 review safety."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime

from helpers import Instance, jpeg_bytes, make_user, wait_job, write
from test_database import backends, drop
from twincheck.db import manager as dbm
from twincheck.db.schema import create_structure, migrate, utcnow
from twincheck.services.metadata import analyse_file, collect


class LibraryDates(unittest.TestCase):
    def test_mtime_summer_winter_and_midnight(self):
        for instant, expected in [
            ("2007-08-29T19:51:02+00:00", "2007-08-29T21:51:02"),
            ("2007-01-29T19:51:02+00:00", "2007-01-29T20:51:02"),
            ("2007-08-29T23:51:02+00:00", "2007-08-30T01:51:02"),
        ]:
            with self.subTest(instant=instant):
                f = {"name": "synthetic.jpg", "media_kind": "image", "ext": "jpg",
                     "mtime_ns": int(datetime.fromisoformat(instant).timestamp()) * 10**9}
                result = analyse_file(f, collect({}, "file"), [], set(), "Europe/Rome")
                proposal = result["proposals"][0]
                self.assertEqual(proposal["value"], expected)
                self.assertEqual((proposal["source"], proposal["timezone"]), ("filesystem_mtime", "Europe/Rome"))

    def test_offset_conversion_preserves_original_and_timezone_free_dates(self):
        f = {"name": "synthetic.jpg", "media_kind": "image", "ext": "jpg"}
        for offset, expected in [("", "2007-08-29T23:51:02"), ("+00:00", "2007-08-30T01:51:02")]:
            own = collect({"ExifIFD:DateTimeOriginal": "2007:08:29 23:51:02" + offset}, "file")
            result = analyse_file(f, own, [], set(), "Europe/Rome")
            self.assertEqual(result["chosen"], expected)
            self.assertEqual(result["fields"]["datetime_original"]["candidates"][0]["value"], "2007-08-29T23:51:02")

    def test_historical_migration_is_value_preserving_on_both_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                for version in (1, 3):
                    db = dbm.make_database(params)
                    try:
                        if db.kind == "mariadb":
                            db.create_database_if_missing()
                        create_structure(db, version)
                        instant = int(datetime.fromisoformat("2007-08-29T19:51:02+00:00").timestamp()) * 10**9
                        values = ["2007-08-29T19:51:02", "2007-08-29T19:51:02", "2001-02-03T04:05:06", "2007-08-29T19:51:02"]
                        ids = []
                        for i, value in enumerate(values):
                            meta = {"fields": {"datetime_original": {"candidates": [
                                {"value": value, "source": "file", "semantic": "local", "tag": "ExifIFD:DateTimeOriginal"}]}}} if i == 2 else {}
                            fid = db.insert("INSERT INTO files (owner_id, cur_path_hash, mtime_ns, chosen_date, meta_read, phase_org) "
                                            "VALUES (?,?,?,?,?,?)", ("synthetic", "fixture-" + str(i), instant, value, json.dumps(meta), "done" if i == 3 else "pending"))
                            ids.append(db.insert("INSERT INTO review_items (owner_id, file_id, status, decision, applied) VALUES (?,?,?,?,?)",
                                                 ("synthetic", fid, "saved", json.dumps({"date": value, "fields": {}}), 0)))
                        migrate(db)
                        rows = [db.query_one("SELECT * FROM review_items WHERE id=?", (rid,)) for rid in ids]
                        self.assertEqual([r["status"] for r in rows], ["needs_recheck", "needs_recheck", "saved", "saved"])
                        self.assertEqual([json.loads(r["decision"])["date"] for r in rows], values)
                        event = db.query_one("SELECT params FROM run_log WHERE event='DECISIONS_FLAGGED_FOR_RECHECK'")
                        self.assertEqual(json.loads(event["params"])["count"], 2)
                        migrate(db)
                        self.assertEqual(db.scalar("SELECT COUNT(*) FROM run_log WHERE event='DECISIONS_FLAGGED_FOR_RECHECK'"), 1)
                    finally:
                        db.close()
                        drop(params)
                    # Each historical fixture starts with an empty operational database.
                    if params["type"] == "sqlite":
                        os.unlink(params["path"])


class ReviewDates(unittest.TestCase):
    def setUp(self):
        self.inst = Instance().start()
        self.inst.setup()
        self.admin = self.inst.admin()
        root = self.inst.data / "lib"
        for i in range(2):
            write(root / ("00650005.jpg" if not i else "other.jpg"), jpeg_bytes(300 + i),
                  datetime.fromisoformat("2007-08-29T23:51:02+00:00").timestamp())
        self.uid, self.user = make_user(self.admin, "synthetic", [{"path": "lib"}])
        self.sid = self.user.get("/api/sources").json()["sources"][0]["id"]
        self.assertEqual(self.admin.patch("/api/admin/settings", {"library_timezone": "Europe/Rome"}).status, 200)
        self.run_job("/api/library/analyze", {"source_id": self.sid})

    def tearDown(self):
        self.inst.cleanup()

    def run_job(self, path, body):
        response = self.user.post(path, body)
        self.assertEqual(response.status, 202, response.text)
        jid = response.json()["job_id"]
        self.assertEqual(wait_job(self.inst.core, jid)["status"], "completed")
        return jid

    def test_proposal_manual_bulk_and_organize_local_day(self):
        item = self.user.get("/api/review").json()["items"][0]
        full = self.user.get("/api/review/" + str(item["id"])).json()
        proposal = full["proposals"][0]
        self.assertEqual(proposal["value"], "2007-08-30T01:51:02")
        response = self.user.post("/api/review/" + str(item["id"]), {
            "action": "save", "date": proposal["value"], "proposal": {"source": "filesystem_mtime"}, "scope": "folder"})
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(response.json()["updated"], 2)
        db = self.inst.core.db
        for row in db.query("SELECT * FROM review_items WHERE owner_id=?", (self.uid,)):
            self.assertEqual((row["decision_source"], row["decision_timezone"]), ("filesystem_mtime", "Europe/Rome"))
        jid = self.run_job("/api/organize/plan", {"source_id": self.sid})
        self.assertTrue(all("/2007/08/30/" in it["dst"] for it in db.query("SELECT * FROM plan_items WHERE plan_job_id=?", (jid,))))
        response = self.user.post("/api/review/" + str(item["id"]), {"action": "save", "date": "2000-01-02T03:04:05"})
        self.assertEqual(response.status, 200)
        row = db.query_one("SELECT * FROM review_items WHERE id=?", (item["id"],))
        self.assertEqual(row["decision_source"], "manual")
        self.assertIsNone(row["decision_timezone"])
        self.assertEqual(db.scalar("SELECT chosen_date FROM files WHERE id=?", (item["file_id"],)), "2000-01-02T03:04:05")

    def test_recheck_skipped_at_preview_and_confirmation(self):
        item = self.user.get("/api/review").json()["items"][0]
        db = self.inst.core.db
        original_plan = self.run_job("/api/organize/plan", {"source_id": self.sid})
        file = db.query_one("SELECT * FROM files WHERE id=?", (item["file_id"],))
        mtime = int(datetime.fromisoformat("2007-08-29T19:51:02+00:00").timestamp()) * 10**9
        os.utime(file["cur_path"], ns=(mtime, mtime))
        db.execute("UPDATE files SET mtime_ns=?, chosen_date=? WHERE id=?", (mtime, "2007-08-29T19:51:02", item["file_id"]))
        db.execute("UPDATE review_items SET status='needs_recheck', recheck_reason='mtime_utc_match', decision=? WHERE id=?",
                   (json.dumps({"date": "2007-08-29T19:51:02"}), item["id"]))
        plan = self.run_job("/api/organize/plan", {"source_id": self.sid})
        rows = db.query("SELECT * FROM plan_items WHERE plan_job_id=? AND file_id=?", (plan, item["file_id"]))
        self.assertEqual((rows[0]["status"], rows[0]["note"]), ("skipped", "needs_recheck"))
        self.assertEqual(self.user.post("/api/review/" + str(item["id"]), {"action": "keep"}).json()["updated"], 0)
        self.assertEqual(db.scalar("SELECT status FROM review_items WHERE id=?", (item["id"],)), "needs_recheck")
        full = self.user.get("/api/review/" + str(item["id"])).json()
        self.assertEqual(full["decision"]["date"], "2007-08-29T19:51:02")
        self.assertEqual(full["recheck_proposal"]["value"], "2007-08-29T21:51:02")
        self.run_job("/api/plans/" + str(original_plan) + "/confirm", {"confirm": True})
        stale_item = db.query_one("SELECT * FROM plan_items WHERE plan_job_id=? AND file_id=?", (original_plan, item["file_id"]))
        self.assertEqual((stale_item["status"], stale_item["note"]), ("skipped", "needs_recheck"))
        self.assertTrue(os.path.isfile(file["cur_path"]))

    def test_use_proposal_and_manual_field_entry_store_provenance(self):
        item = self.user.get("/api/review").json()["items"][0]
        proposal = self.user.get("/api/review/" + str(item["id"])).json()["proposals"][0]
        self.assertEqual(self.user.post("/api/review/" + str(item["id"]), {
            "action": "save", "fields": {"datetime_original": proposal["value"].replace("T", " "),
                                        "title": "Manual title after Use"},
            "proposal": {"source": "filesystem_mtime"}}).status, 200)
        row = self.inst.core.db.query_one("SELECT * FROM review_items WHERE id=?", (item["id"],))
        self.assertEqual(json.loads(row["decision_provenance"])["datetime_original"],
                         {"source": "filesystem_mtime", "timezone": "Europe/Rome"})
        self.user.post("/api/review/" + str(item["id"]), {"action": "save", "fields": {"create_date": "2001-02-03T04:05:06"}})
        row = self.inst.core.db.query_one("SELECT * FROM review_items WHERE id=?", (item["id"],))
        self.assertEqual(json.loads(row["decision_provenance"])["create_date"], {"source": "manual", "timezone": None})
        db = self.inst.core.db
        stored = json.loads(db.query_one("SELECT meta_read FROM files WHERE id=?", (item["file_id"],))["meta_read"])
        candidate = {"tag": "EXIF:DateTimeOriginal", "source": "file", "semantic": "tz",
                     "value": "2007-08-29T20:51:02", "offset": "+01:00"}
        stored["fields"]["datetime_original"].update(candidates=[candidate], present=True, canonical_present=True)
        db.execute("UPDATE files SET meta_read=? WHERE id=?", (json.dumps(stored), item["file_id"]))
        full = self.user.get("/api/review/" + str(item["id"])).json()
        exif = next(c for c in full["date_candidates"] if c["tag"] == candidate["tag"])
        self.assertEqual(exif["value"], "2007-08-29T21:51:02")
        self.assertEqual(self.user.post("/api/review/" + str(item["id"]), {
            "action": "save", "date": exif["value"],
            "proposal": {"source": "exif", "tag": candidate["tag"]}}).status, 200)
        row = db.query_one("SELECT * FROM review_items WHERE id=?", (item["id"],))
        self.assertEqual((row["decision_source"], row["decision_timezone"]), ("exif", "Europe/Rome"))
        self.assertEqual(json.loads(db.query_one("SELECT meta_read FROM files WHERE id=?", (item["file_id"],))["meta_read"])
                         ["fields"]["datetime_original"]["candidates"][0], candidate)

    def test_timezone_permissions_validation_and_audit(self):
        self.assertEqual(self.user.patch("/api/admin/settings", {"library_timezone": "UTC"}).status, 403)
        self.assertEqual(self.admin.patch("/api/admin/settings", {"library_timezone": "../etc/passwd"}).status, 400)
        self.assertEqual(self.inst.core.library_timezone, "Europe/Rome")
        self.assertTrue(any(e["event"] == "SETTING_CHANGED" for e in self.inst.core.vault.snapshot()["audit"]))

    def test_duplicate_capture_refresh_preserves_saved_decision(self):
        item = self.user.get("/api/review").json()["items"][0]
        self.user.post("/api/review/" + str(item["id"]), {"action": "save", "date": "2000-01-02T03:04:05"})
        db = self.inst.core.db
        file = db.query_one("SELECT * FROM files WHERE id=?", (item["file_id"],))
        other = db.query_one("SELECT * FROM files WHERE owner_id=? AND id<>?", (self.uid, item["file_id"]))
        Path(other["cur_path"]).write_bytes(Path(file["cur_path"]).read_bytes())
        self.run_job("/api/scan", {"source_id": self.sid})
        db.execute("UPDATE files SET phase_meta='pending' WHERE owner_id=?", (self.uid,))
        self.run_job("/api/duplicates/analyze", {})
        self.assertEqual(db.query_one("SELECT chosen_date FROM files WHERE id=?", (item["file_id"],))["chosen_date"],
                         "2000-01-02T03:04:05")

    def test_older_paused_analysis_does_not_invalidate_newer_completed_run(self):
        db = self.inst.core.db
        db.execute("INSERT INTO jobs (kind,owner_id,status,params,created_at) VALUES (?,?,?,?,?)",
                   ("analyze_library", self.uid, "paused", json.dumps({"source_id": self.sid, "root": str(self.inst.data / "lib")}),
                    "2024-01-01T00:00:00Z"))
        self.assertFalse(self.user.get("/api/library/status").json()["stale"])

    def test_organize_path_changes_mark_results_stale(self):
        plan = self.run_job("/api/organize/plan", {"source_id": self.sid})
        self.run_job("/api/plans/" + str(plan) + "/confirm", {"confirm": True})
        status = self.user.get("/api/library/status").json()
        self.assertEqual(status["changes"]["changed"], 2)
        self.assertTrue(status["stale"])

    def test_stale_only_for_disk_changes_not_decisions_or_unreadable_warning(self):
        status = lambda: self.user.get("/api/library/status").json()
        self.assertFalse(status()["stale"])
        item = self.user.get("/api/review").json()["items"][0]
        self.assertEqual(self.user.post("/api/review/" + str(item["id"]),
                                       {"action": "save", "date": "2000-01-02T03:04:05"}).status, 200)
        self.assertFalse(status()["stale"], "database-only review decision does not change the file")
        root = self.inst.data / "lib"
        unreadable = root / "unreadable.jpg"
        write(unreadable, jpeg_bytes(309))
        unreadable.chmod(0)
        try:
            self.run_job("/api/library/analyze", {"source_id": self.sid})
            self.assertFalse(status()["stale"], "FILE_UNREADABLE does not invalidate completed analysis")
            target = root / "other.jpg"
            old = target.stat()
            os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns + 10**9))
            self.assertEqual(status()["changes"]["changed"], 1)
            self.run_job("/api/library/analyze", {"source_id": self.sid})
            self.assertFalse(status()["stale"])
            new = root / "new.jpg"
            write(new, jpeg_bytes(310))
            self.assertEqual(status()["changes"]["new"], 1)
            self.run_job("/api/library/analyze", {"source_id": self.sid})
            new.unlink()
            self.assertEqual(status()["changes"]["removed"], 1)
        finally:
            unreadable.chmod(0o600)

    def test_latest_pipeline_results_ignore_newer_per_step_jobs(self):
        pipeline = self.inst.core.db.query_one("SELECT * FROM jobs WHERE kind='analyze_library' AND owner_id=?", (self.uid,))
        self.run_job("/api/metadata/analyze", {})
        data = self.user.get("/api/metadata/results").json()
        self.assertEqual(data["analyzed_at"], pipeline["finished_at"])
        self.assertEqual(data["plan_job_id"], pipeline["id"])
        self.assertEqual(self.user.get("/api/duplicates").json()["analyzed_at"], pipeline["finished_at"])

    def test_phase_events_have_real_counts_once_even_after_resume(self):
        jid = self.run_job("/api/library/analyze", {"source_id": self.sid})
        db = self.inst.core.db
        events = db.query("SELECT event, params FROM run_log WHERE job_id=? AND event LIKE 'ANALYSIS_PHASE_%' ORDER BY id", (jid,))
        names = ["scan", "hash", "capture_dates", "duplicates", "metadata"]
        self.assertEqual([(r["event"], json.loads(r["params"])["phase"]) for r in events],
                         [(event, name) for name in names for event in ("ANALYSIS_PHASE_STARTED", "ANALYSIS_PHASE_COMPLETED")])
        scan = json.loads(events[1]["params"])
        self.assertEqual((scan["found"], scan["new"], scan["changed"], scan["unchanged"], scan["removed"]), (2, 0, 0, 2, 0))
        hash_counts = json.loads(events[3]["params"])
        self.assertEqual((hash_counts["candidates"], hash_counts["hashed"], hash_counts["unreadable"]), (0, 0, 0))
        self.assertEqual(json.loads(events[5]["params"])["unknown"], 2)
        from twincheck.jobs import JobContext
        from twincheck.services.scan import analyze_library
        ctx = JobContext(self.inst.core.jobs, db.query_one("SELECT * FROM jobs WHERE id=?", (jid,)))
        analyze_library(ctx)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM run_log WHERE job_id=? AND event LIKE 'ANALYSIS_PHASE_%'", (jid,)), 10)

    def test_pipeline_counts_include_unreadable_and_mtime_only_changes(self):
        root = self.inst.data / "lib"
        unreadable = write(root / "unreadable.jpg", jpeg_bytes(320))
        unreadable.chmod(0)
        try:
            target = root / "other.jpg"
            st = target.stat()
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
            jid = self.run_job("/api/library/analyze", {"source_id": self.sid})
            public = self.user.get("/api/jobs/" + str(jid)).json()
            counts = public["result"]["scan"]
            self.assertEqual((counts["found"], counts["new"], counts["changed"], counts["unchanged"]), (3, 1, 1, 1))
            self.assertEqual((counts["candidates"], counts["hashed"], counts["unreadable"]), (2, 1, 1))
            warnings = self.inst.core.db.query("SELECT * FROM run_log WHERE job_id=? AND event='FILE_UNREADABLE' AND level='WARNING'", (jid,))
            self.assertEqual(len(warnings), 1)
        finally:
            unreadable.chmod(0o600)

    def test_phase_events_resume_after_interruption_during_hash(self):
        from unittest.mock import patch
        from twincheck.jobs import JobContext, JobPaused
        from twincheck.services.scan import analyze_library
        write(self.inst.data / "lib" / "new.jpg", jpeg_bytes(330))
        db = self.inst.core.db
        params = json.loads(db.query_one("SELECT params FROM jobs WHERE owner_id=? AND kind='analyze_library'", (self.uid,))["params"])
        self.inst.core.jobs.blocked = True
        jid = self.inst.core.jobs.submit("analyze_library", self.uid, params, force=True)
        ctx = JobContext(self.inst.core.jobs, db.query_one("SELECT * FROM jobs WHERE id=?", (jid,)))
        with patch("twincheck.services.scan.sha256_file", side_effect=JobPaused):
            with self.assertRaises(JobPaused):
                analyze_library(ctx)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM run_log WHERE job_id=? AND event LIKE 'ANALYSIS_PHASE_%'", (jid,)), 3)
        db.execute("UPDATE jobs SET status='paused' WHERE id=?", (jid,))
        resumed = JobContext(self.inst.core.jobs, db.query_one("SELECT * FROM jobs WHERE id=?", (jid,)))
        result = analyze_library(resumed)
        self.assertEqual((result["scan"]["candidates"], result["scan"]["hashed"]), (1, 1))
        events = db.query("SELECT event, params FROM run_log WHERE job_id=? AND event LIKE 'ANALYSIS_PHASE_%' ORDER BY id", (jid,))
        self.assertEqual([(r["event"], json.loads(r["params"])["phase"]) for r in events],
                         [(event, phase) for phase in ["scan", "hash", "capture_dates", "duplicates", "metadata"]
                          for event in ["ANALYSIS_PHASE_STARTED", "ANALYSIS_PHASE_COMPLETED"]])

    def test_review_counts_are_live_and_independent_of_status_filter(self):
        item = self.user.get("/api/review").json()["items"][0]
        self.user.post("/api/review/" + str(item["id"]), {"action": "save", "date": "2000-01-02T03:04:05"})
        expected = {"total": 2, "decided": 1, "pending": 1, "needs_recheck": 0}
        for status in ("pending", "saved", "decided", "all"):
            self.assertEqual(self.user.get("/api/review?status=" + status).json()["review_counts"], expected)
        self.assertEqual(self.user.get("/api/metadata/results").json()["review_counts"], expected)
        self.user.post("/api/review/" + str(item["id"]) + "/reopen")
        self.assertEqual(self.user.get("/api/metadata/results").json()["review_counts"]["pending"], 2)


class LoginVersion(unittest.TestCase):
    def test_default_on_off_permissions_and_vault_audit(self):
        from twincheck import VERSION
        inst = Instance().start()
        try:
            inst.setup()
            admin = inst.admin()
            public = inst.client()
            self.assertEqual(public.get("/api/bootstrap").json()["login_version"], VERSION)
            _, user = make_user(admin, "version-user", [])
            _, administrator = make_user(admin, "version-administrator", [], role="administrator")
            for client in (user, administrator):
                self.assertEqual(client.patch("/api/admin/settings", {"show_version_on_login": False}).status, 403)
            self.assertNotIn("show_version_on_login", administrator.get("/api/admin/settings").json()["settings"])
            self.assertEqual(admin.patch("/api/admin/settings", {"show_version_on_login": "false"}).status, 400)
            self.assertEqual(admin.patch("/api/admin/settings", {"show_version_on_login": False}).status, 200)
            for path in ("/api/bootstrap", "/healthz", "/", "/api/locales", "/api/sysinfo"):
                self.assertNotIn(VERSION, public.get(path).text, path)
            self.assertFalse(inst.core.vault.snapshot()["settings"]["show_version_on_login"])
            self.assertTrue(any(row["event"] == "SETTING_CHANGED" and row["params"].get("setting") == "show_version_on_login"
                                for row in inst.core.vault.snapshot()["audit"]))
            self.assertEqual(admin.patch("/api/admin/settings", {"show_version_on_login": True}).status, 200)
            self.assertEqual(public.get("/api/bootstrap").json()["login_version"], VERSION)
        finally:
            inst.cleanup()
