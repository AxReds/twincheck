"""Synthetic rc.9 regressions; no owner library or credentials."""
import json
import tempfile
import unittest
from unittest import mock

from helpers import Instance, jpeg_bytes, make_user, wait_job, write
from test_database import backends, drop
from twincheck import events
from twincheck.db import manager as dbm
from twincheck.db.schema import CURRENT_VERSION, create_structure, migrate, set_meta


class MigrationEvents(unittest.TestCase):
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
