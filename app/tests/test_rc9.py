"""Synthetic rc.9 regressions; no owner library or credentials."""
import json
import csv
import io
import tempfile
import unittest
from unittest import mock

from helpers import Instance, jpeg_bytes, make_user, wait_job, write
from test_database import backends, drop
from twincheck import events
from twincheck.db import manager as dbm
from twincheck.db.schema import CURRENT_VERSION, create_structure, migrate, set_meta


class MigrationEvents(unittest.TestCase):
    def test_log_visibility_by_role_and_owner_filter_also_applies_to_csv(self):
        inst = Instance().start()
        try:
            inst.setup()
            admin = inst.admin()
            (inst.data / "lib").mkdir()
            (inst.data / "other").mkdir()
            uid, user = make_user(admin, "synthetic", [{"path": "lib"}])
            other_id, _ = make_user(admin, "other", [{"path": "other"}])
            db = inst.core.db
            own_path = str(inst.data / "lib" / "synthetic.jpg")
            other_path = str(inst.data / "other" / "other.jpg")
            dbm.log(db, "REVIEW_SKIPPED", owner_id=uid, path=own_path)
            dbm.log(db, "REVIEW_DECIDED", owner_id=other_id, path=other_path, decision="saved")
            dbm.log(db, "REVIEW_DECIDED", owner_id=uid, path=other_path, decision="saved")
            dbm.log(db, "LOGIN_OK", owner_id=uid, username=uid, ip="127.0.0.1")
            dbm.log(db, "APP_STARTED", version="synthetic")
            job = db.insert("INSERT INTO jobs (owner_id,kind,status,params) VALUES (?,?,?,?)",
                            (other_id, "scan", "completed", "{}"))
            dbm.log(db, "JOB_COMPLETED", owner_id=uid, job_id=job, kind="scan")
            response = user.get("/api/logs?owner=" + other_id).json()
            self.assertEqual([row["event"] for row in response["items"]], ["REVIEW_SKIPPED"])
            self.assertNotIn("users", response)
            csv_rows = list(csv.DictReader(io.StringIO(user.get("/api/logs.csv?scope=all&owner=" + other_id).text)))
            self.assertEqual([row["event"] for row in csv_rows], ["REVIEW_SKIPPED"])
            all_admin = admin.get("/api/logs").json()
            self.assertIn("LOGIN_OK", {row["event"] for row in all_admin["items"]})
            self.assertIn("APP_STARTED", {row["event"] for row in all_admin["items"]})
            selected = admin.get("/api/logs?owner=" + other_id).json()
            self.assertEqual({row["owner_id"] for row in selected["items"]}, {other_id})
            self.assertTrue(any(row["username"] == "other" for row in all_admin["users"]))
        finally:
            inst.cleanup()

    def test_log_csv_exports_filtered_or_all_accessible_rows_with_iso_times(self):
        inst = Instance().start()
        try:
            inst.setup()
            db = inst.core.db
            for event in ("REVIEW_SKIPPED", "REVIEW_DECIDED"):
                dbm.log(db, event, owner_id="synthetic", path='/synthetic,"fixture".jpg', decision="saved")
            admin = inst.admin()
            response = admin.get("/api/logs.csv?event=REVIEW_SKIPPED&scope=filtered")
            rows = list(csv.DictReader(io.StringIO(response.text)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event"], "REVIEW_SKIPPED")
            self.assertTrue(rows[0]["time"].endswith("Z"))
            self.assertIn('/synthetic,"fixture".jpg', rows[0]["message"])
            all_rows = list(csv.DictReader(io.StringIO(admin.get("/api/logs.csv?event=REVIEW_SKIPPED&scope=all").text)))
            self.assertIn("REVIEW_DECIDED", {row["event"] for row in all_rows})
            self.assertEqual(admin.get("/api/logs.csv?scope=invalid").status, 400)
        finally:
            inst.cleanup()

    def test_log_event_catalogue_includes_zero_counts_under_filters(self):
        inst = Instance().start()
        try:
            inst.setup()
            dbm.log(inst.core.db, "REVIEW_SKIPPED", owner_id="synthetic", path="/synthetic")
            response = inst.admin().get("/api/logs?level=WARNING").json()
            self.assertEqual(set(response["event_counts"]), set(events.EVENTS))
            self.assertEqual(response["event_counts"]["REVIEW_SKIPPED"], 0)
            response = inst.admin().get("/api/logs?event=REVIEW_SKIPPED").json()
            self.assertEqual(response["total"], 1)
            self.assertEqual(response["event_counts"]["REVIEW_SKIPPED"], 1)
        finally:
            inst.cleanup()

    def test_timezone_catalogue_uses_container_zones_and_current_offsets(self):
        from zoneinfo import available_timezones
        inst = Instance().start()
        try:
            inst.setup()
            admin = inst.admin()
            admin.patch("/api/admin/settings", {"library_timezone": "Europe/Rome"})
            response = admin.get("/api/admin/settings").json()
            zones = {z["name"]: z["offset"] for z in response["timezones"]}
            self.assertEqual(set(zones), available_timezones())
            self.assertEqual(zones["UTC"], "+00:00")
            self.assertEqual(zones["Asia/Kathmandu"], "+05:45")
            self.assertEqual(response["settings"]["library_timezone"], "Europe/Rome")
        finally:
            inst.cleanup()

    def test_confirmed_migration_logs_once_with_real_versions_and_audits_flags(self):
        inst = Instance().start()
        try:
            inst.setup()
            inst.core.detach()
            db = inst.core.make_db(inst.core.vault.data["database"])
            fid = db.insert("INSERT INTO files (owner_id,cur_path_hash,mtime_ns,phase_org) VALUES (?,?,?,?)",
                            ("synthetic", "fixture", 0, "pending"))
            db.execute("INSERT INTO review_items (owner_id,file_id,status,decision,decision_source,applied) VALUES (?,?,?,?,?,?)",
                       ("synthetic", fid, "saved", json.dumps({"date": "1970-01-01T00:00:00"}), "unknown", 0))
            set_meta(db, "schema_version", 3)
            db.close()
            inst.core.connect_configured()
            admin = inst.admin()
            with mock.patch.object(events, "console", wraps=events.console) as console:
                response = admin.post("/api/admin/db/migrate-schema", {"confirm_backup": True})
            self.assertEqual(response.status, 200, response.text)
            migrated = [c for c in console.call_args_list if c.args[0] == "DB_MIGRATED"]
            self.assertEqual(len(migrated), 1)
            self.assertEqual((migrated[0].kwargs["from_version"], migrated[0].kwargs["to_version"]),
                             (3, CURRENT_VERSION))
            db = inst.core.db
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM run_log WHERE event='DB_MIGRATED'"), 1)
            audit = inst.core.vault.snapshot()["audit"]
            flagged = [e for e in audit if e["event"] == "DECISIONS_FLAGGED_FOR_RECHECK"]
            self.assertEqual(len(flagged), 1)
            self.assertEqual(flagged[0]["params"]["count"], 1)
        finally:
            inst.cleanup()


class ReviewPostponement(unittest.TestCase):
    def test_review_population_excludes_missing_files_and_explains_multiple_items(self):
        inst = Instance().start()
        try:
            inst.setup()
            for n in range(2):
                write(inst.data / "lib" / f"synthetic-{n}.jpg", jpeg_bytes(930 + n))
            uid, user = make_user(inst.admin(), "synthetic", [{"path": "lib"}])
            sid = user.get("/api/sources").json()["sources"][0]["id"]
            jid = user.post("/api/library/analyze", {"source_id": sid}).json()["job_id"]
            self.assertEqual(wait_job(inst.core, jid)["status"], "completed")
            db = inst.core.db
            items = user.get("/api/review").json()["items"]
            db.execute("INSERT INTO review_items (owner_id,file_id,kind,status) VALUES (?,?,?,?)",
                       (uid, items[0]["file_id"], "move_conflict", "pending"))
            db.execute("UPDATE files SET present=0 WHERE id=?", (items[1]["file_id"],))
            result = user.get("/api/metadata/results").json()
            self.assertEqual(result["summary"]["analysed"], 2)
            self.assertEqual(result["review_counts"]["total"], 2)
            self.assertEqual(result["review_file_count"], 1)
            self.assertEqual(user.get("/api/review").json()["total"], 2)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM review_items"), 3, "history must not be deleted")
        finally:
            inst.cleanup()

    def test_skip_only_migration_preserves_all_other_decisions_on_both_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]):
                    db = dbm.make_database(params)
                    try:
                        if db.kind == "mariadb":
                            db.create_database_if_missing()
                        create_structure(db, 5)
                        records = [
                            ("skipped", {"fields": {}, "date": None}, 0),
                            ("skipped", {"fields": {"title": "Chosen by owner"}, "date": None}, 0),
                            ("saved", {"date": "2001-02-03T04:05:06"}, 0),
                            ("kept", {"fields": {}}, 0),
                            ("needs_recheck", {"date": "2002-03-04T05:06:07"}, 0),
                            ("skipped", {"fields": {}}, 1),
                        ]
                        ids = [db.insert("INSERT INTO review_items (status,decision,decided_at,applied) VALUES (?,?,?,?)",
                                         (status, json.dumps(decision), "2026-11-24T10:00:00Z", applied))
                               for status, decision, applied in records]
                        originals = [db.query_one("SELECT * FROM review_items WHERE id=?", (rid,)) for rid in ids]
                        migrate(db)
                        rows = [db.query_one("SELECT * FROM review_items WHERE id=?", (rid,)) for rid in ids]
                        self.assertEqual(rows[0]["status"], "pending")
                        self.assertEqual(rows[0]["skipped_at"], originals[0]["decided_at"])
                        self.assertEqual(rows[0]["decided_at"], originals[0]["decided_at"])
                        for before, after in zip(originals[1:], rows[1:]):
                            self.assertEqual({k: after[k] for k in before}, before)
                        event = db.query_one("SELECT params FROM run_log WHERE event='REVIEW_SKIPS_RETURNED_TO_PENDING'")
                        self.assertEqual(json.loads(event["params"])["count"], 1)
                        migrate(db)
                        self.assertEqual(db.scalar("SELECT COUNT(*) FROM run_log WHERE event='REVIEW_SKIPS_RETURNED_TO_PENDING'"), 1)
                    finally:
                        db.close()
                        drop(params)

    def test_repeat_analysis_preserves_skip_keep_save_and_counts_on_both_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]):
                    inst = Instance().start()
                    try:
                        inst.setup(db=params if params["type"] == "mariadb" else None)
                        for n in range(3):
                            write(inst.data / "lib" / f"synthetic-{n}.jpg", jpeg_bytes(900 + n))
                        _, user = make_user(inst.admin(), "synthetic", [{"path": "lib"}])
                        sid = user.get("/api/sources").json()["sources"][0]["id"]
                        def run(path):
                            response = user.post(path, {"source_id": sid})
                            self.assertEqual(response.status, 202, response.text)
                            jid = response.json()["job_id"]
                            self.assertEqual(wait_job(inst.core, jid)["status"], "completed")
                            return jid
                        run("/api/library/analyze")
                        items = user.get("/api/review?status=all").json()["items"]
                        old_plan = run("/api/organize/plan")
                        skipped, saved, kept = items
                        for item, action in zip(items, ("skip", "save", "keep")):
                            body = {"action": action}
                            if action == "save":
                                body["date"] = "2001-02-03T04:05:06"
                            self.assertEqual(user.post(f"/api/review/{item['id']}", body).status, 200)
                        run("/api/library/analyze")
                        run("/api/library/analyze")
                        pending = user.get("/api/review").json()
                        decided = user.get("/api/review?status=decided").json()
                        self.assertEqual([r["id"] for r in pending["items"]], [skipped["id"]])
                        self.assertEqual({r["id"] for r in decided["items"]}, {saved["id"], kept["id"]})
                        self.assertEqual(pending["review_counts"], {"total": 3, "pending": 1, "decided": 2, "needs_recheck": 0})
                        for row in pending["items"] + decided["items"]:
                            detail = user.get(f"/api/review/{row['id']}").json()
                            self.assertEqual(detail["status"], row["status"])
                        self.assertEqual(pending["items"][0]["status"], "skipped")
                        self.assertEqual(user.get(f"/api/review/{saved['id']}").json()["decision"]["date"],
                                         "2001-02-03T04:05:06")
                        db = inst.core.db
                        self.assertEqual(db.scalar("SELECT COUNT(*) FROM run_log WHERE event='REVIEW_SKIPPED'"), 1)
                        self.assertEqual(db.scalar("SELECT COUNT(*) FROM run_log WHERE event='REVIEW_DECIDED'"), 2)
                        plan = run("/api/organize/plan")
                        move = db.query_one("SELECT * FROM plan_items WHERE plan_job_id=? AND file_id=?",
                                            (plan, skipped["file_id"]))
                        self.assertEqual((move["status"], move["note"]), ("skipped", "review_skipped"))
                        response = user.post(f"/api/plans/{old_plan}/confirm", {"confirm": True})
                        self.assertEqual(response.status, 202, response.text)
                        self.assertEqual(wait_job(inst.core, response.json()["job_id"])["status"], "completed")
                        old_move = db.query_one("SELECT status,note FROM plan_items WHERE plan_job_id=? AND file_id=?",
                                                (old_plan, skipped["file_id"]))
                        self.assertEqual((old_move["status"], old_move["note"]), ("skipped", "review_skipped"))
                    finally:
                        inst.cleanup()
                        drop(params)
