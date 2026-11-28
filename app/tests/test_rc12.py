"""Owner-follow-up regressions with synthetic libraries and accounts only."""
import asyncio
import csv
import io
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from helpers import Instance, jpeg_bytes, make_user, wait_job, write
from test_database import backends
from test_rc10 import instance
from twincheck.db import manager as dbm
from twincheck.web.framework import JSONResponse


class Rc12Data(unittest.TestCase):
    def test_admin_filter_matches_362_event_users_without_changing_357_private_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    admin = inst.admin()
                    (inst.data / "mine").mkdir()
                    (inst.data / "other").mkdir()
                    uid, user = make_user(admin, "synthetic", [{"path": "mine"}])
                    db = inst.core.db
                    for _ in range(357):
                        dbm.log(db, "REVIEW_SKIPPED", owner_id=uid, path=str(inst.data / "mine/synthetic.jpg"))
                    jid = db.insert("INSERT INTO jobs (owner_id,kind,status,params) VALUES (?,?,?,?)",
                                    (uid, "scan", "completed", json.dumps({"root": str(inst.data / "other"),
                                                                         "dest": str(inst.data)})))
                    for event in ("JOB_QUEUED", "JOB_STARTED", "JOB_COMPLETED"):
                        dbm.log(db, event, owner_id=uid, job_id=jid, kind="scan")
                    for _ in range(2):
                        dbm.log(db, "DUP_GROUP", owner_id=uid, keeper=str(inst.data / "other/Duplicates/03.jpg"),
                                count=2, sha256="synthetic")
                    own = user.get("/api/logs?limit=all").json()
                    selected = admin.get(f"/api/logs?owner={uid}&limit=all").json()
                    self.assertEqual((own["total"], selected["total"], selected["not_visible"]), (357, 362, 5))
                    self.assertNotIn("not_visible", own)
                    self.assertEqual({row["user"] for row in selected["items"]}, {"synthetic"})
                    for client, query, count in ((user, "", 357), (admin, "&owner=" + uid, 362)):
                        exported = client.get("/api/logs.csv?scope=filtered" + query)
                        self.assertEqual(len(list(csv.DictReader(io.StringIO(exported.text)))), count)
                    selected = admin.get(f"/api/logs?owner={uid}&event=DUP_GROUP").json()
                    self.assertEqual((selected["total"], selected["not_visible"]), (2, 2))

    def test_status_explains_legacy_and_disk_changes_not_admin_global_report_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    admin = inst.admin()
                    write(inst.data / "other/synthetic.jpg", jpeg_bytes(970))
                    _, user = make_user(admin, "synthetic", [{"path": "other"}])
                    sid = user.get("/api/sources").json()["sources"][0]["id"]
                    analysis = user.post("/api/library/analyze", {"source_id": sid}).json()["job_id"]
                    self.assertEqual(wait_job(inst.core, analysis)["status"], "completed")
                    self.assertGreater(sum(row["n"] for row in admin.get("/api/report").json()["files"]), 0)
                    self.assertEqual(admin.get("/api/report").json()["review_counts"]["pending"], 0)
                    self.assertEqual(user.get("/api/report").json()["review_counts"], user.get("/api/review").json()["review_counts"])
                    self.assertEqual(admin.get("/api/library/status").json()["reason"], "never_analyzed")
                    write(inst.data / "other/second.jpg", jpeg_bytes(971))
                    status = user.get("/api/library/status").json()
                    self.assertEqual(status["reason"], "files_changed")
                    self.assertEqual(status["changes"]["new"], 1)
                    self.assertTrue(status["analyzed_at"])
                    inst.core.db.execute("UPDATE jobs SET kind='scan' WHERE id=?", (analysis,))
                    self.assertEqual(user.get("/api/library/status").json()["reason"], "older_version")
                    self.assertEqual(inst.core.db.scalar("SELECT COUNT(*) FROM jobs"), 1,
                                     "reading status must never launch analysis")

    def test_preview_is_reusable_but_new_analysis_decisions_disk_and_discard_block_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    write(inst.data / "mine/synthetic.jpg", jpeg_bytes(972))
                    uid, user = make_user(inst.admin(), "synthetic", [{"path": "mine"}])
                    sid = user.get("/api/sources").json()["sources"][0]["id"]

                    def run(path):
                        response = user.post(path, {"source_id": sid})
                        self.assertEqual(response.status, 202, response.text)
                        jid = response.json()["job_id"]
                        job = wait_job(inst.core, jid)
                        self.assertEqual(job["status"], "completed", job["error"])
                        return jid

                    run("/api/library/analyze")
                    plan = run("/api/organize/plan")
                    preview = user.get("/api/organize/preview?source_id=" + sid).json()["preview"]
                    self.assertEqual((preview["id"], preview["count"], preview["stale"]), (plan, 1, False))
                    self.assertTrue(user.get(f"/api/plans/{plan}").json()["can_confirm"])
                    run("/api/library/analyze")
                    self.assertEqual(user.get("/api/organize/preview").json()["preview"]["reason"], "analysis")
                    self.assertEqual(user.post(f"/api/plans/{plan}/confirm", {"confirm": True}).status, 409)
                    plan = run("/api/organize/plan")
                    item = user.get("/api/review?status=all").json()["items"][0]
                    self.assertEqual(user.post(f"/api/review/{item['id']}", {"action": "save", "date": "2001-02-03T04:05:06"}).status, 200)
                    self.assertEqual(user.get("/api/organize/preview").json()["preview"]["reason"], "decisions")
                    self.assertFalse(user.get(f"/api/plans/{plan}").json()["can_confirm"])
                    plan = run("/api/organize/plan")
                    write(inst.data / "mine/new.jpg", jpeg_bytes(973))
                    self.assertEqual(user.get("/api/organize/preview").json()["preview"]["reason"], "files")
                    # The service must also reject stale plans submitted outside the confirmation endpoint.
                    apply = inst.core.jobs.submit("organize_apply", uid, {"plan_job_id": plan}, parent_job_id=plan)
                    self.assertEqual(wait_job(inst.core, apply)["status"], "failed")
                    self.assertTrue((inst.data / "mine/synthetic.jpg").exists())
                    plan = run("/api/organize/plan")
                    (inst.data / "foreign").mkdir()
                    _, foreign = make_user(inst.admin(), "foreign", [{"path": "foreign"}])
                    self.assertEqual(foreign.post(f"/api/organize/preview/{plan}/discard").status, 404)
                    self.assertEqual(user.post(f"/api/organize/preview/{plan}/discard").status, 200)
                    self.assertIsNone(user.get("/api/organize/preview").json()["preview"])
                    self.assertEqual(user.post(f"/api/plans/{plan}/confirm", {"confirm": True}).status, 409)
                    self.assertEqual(inst.core.db.scalar("SELECT COUNT(*) FROM plan_items WHERE plan_job_id=?", (plan,)), 1)

    def test_history_lists_scoped_operations_and_existing_global_undo_stays_journaled(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    write(inst.data / "mine/synthetic.jpg", jpeg_bytes(974))
                    _, user = make_user(inst.admin(), "synthetic", [{"path": "mine"}])
                    sid = user.get("/api/sources").json()["sources"][0]["id"]
                    for path in ("/api/library/analyze", "/api/organize/plan"):
                        plan = user.post(path, {"source_id": sid}).json()["job_id"]
                        self.assertEqual(wait_job(inst.core, plan)["status"], "completed")
                    applied = user.post(f"/api/plans/{plan}/confirm", {"confirm": True}).json()["job_id"]
                    self.assertEqual(wait_job(inst.core, applied)["status"], "completed")
                    operations = user.get("/api/report").json()["operations"]
                    self.assertEqual([(op["id"], op["type"], op["count"], op["user"], op["undone"]) for op in operations],
                                     [(applied, "move", 1, "synthetic", 0)])
                    self.assertTrue(operations[0]["date"])
                    (inst.data / "foreign").mkdir()
                    _, foreign = make_user(inst.admin(), "foreign", [{"path": "foreign"}])
                    self.assertEqual(foreign.get("/api/report").json()["operations"], [])
                    undo = user.post("/api/undo/moves/plan").json()["job_id"]
                    self.assertEqual(wait_job(inst.core, undo)["status"], "completed")
                    preview = user.get(f"/api/plans/{undo}").json()["items"][0]
                    self.assertEqual(preview["dst"], "mine/synthetic.jpg")
                    applied_undo = user.post(f"/api/plans/{undo}/confirm", {"confirm": True}).json()["job_id"]
                    self.assertEqual(wait_job(inst.core, applied_undo)["status"], "completed")
                    self.assertTrue((inst.data / "mine/synthetic.jpg").exists())
                    self.assertEqual(user.get("/api/report").json()["operations"][0]["undone"], 1)
                    self.assertGreater(inst.core.db.scalar("SELECT COUNT(*) FROM move_journal"), 1)
                    file = inst.core.db.query_one("SELECT * FROM files WHERE owner_id=?", (user.get("/api/bootstrap").json()["user"]["id"],))
                    for action, kind, date in (("metadata", "metadata_apply", "2099-01-01T00:00:00Z"),
                                               ("dup_move", "dedupe_apply", "2099-01-02T00:00:00Z")):
                        jid = inst.core.db.insert("INSERT INTO jobs (owner_id,kind,status) VALUES (?,?,?)",
                                                  (file["owner_id"], kind, "completed"))
                        inst.core.db.execute("INSERT INTO manifest (job_id,owner_id,file_id,action,orig_path,new_path,created_at) "
                                             "VALUES (?,?,?,?,?,?,?)", (jid, file["owner_id"], file["id"], action,
                                                                      file["cur_path"], file["cur_path"], date))
                    listed = user.get("/api/report").json()["operations"]
                    self.assertEqual([op["type"] for op in listed], ["dup_move", "metadata", "move"])
                    self.assertEqual([op["count"] for op in listed], [1, 1, 1])

    def test_banner_dismissal_survives_bootstrap_until_next_sign_in(self):
        inst = Instance().start()
        try:
            inst.setup()
            client = inst.admin()
            client.post("/api/logout")
            self.assertEqual(client.login("admin", "wrong").status, 401)
            client = inst.admin()
            self.assertEqual(client.get("/api/bootstrap").json()["security_banner"]["failed"], 1)
            self.assertEqual(client.post("/api/me/security-banner/dismiss").status, 200)
            for _ in range(3):
                self.assertTrue(client.get("/api/bootstrap").json()["security_banner"]["dismissed"])
            client.post("/api/logout")
            self.assertNotIn("dismissed", inst.admin().get("/api/bootstrap").json()["security_banner"])
        finally:
            inst.cleanup()

    def test_thirty_rapid_reload_navigation_requests_leave_health_and_home_under_two_seconds(self):
        inst = Instance().start()
        try:
            inst.setup()
            client = inst.admin()
            paths = ["/", "/api/bootstrap", "/api/jobs", "/api/sysinfo", "/api/report"]
            with ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(pool.map(lambda n: client.get(paths[n % len(paths)]), range(30)))
            self.assertTrue(all(response.status == 200 for response in responses))
            for path in ("/health", "/healthz", "/"):
                started = time.monotonic()
                self.assertEqual(client.get(path).status, 200)
                self.assertLess(time.monotonic() - started, 2, path)
            self.assertEqual(inst.app.active_requests, 0)
        finally:
            inst.cleanup()

    def test_request_limit_is_bounded_and_health_bypasses_saturated_handlers(self):
        inst = Instance().start()
        try:
            async def exercise():
                release = asyncio.Event()
                async def blocked(scope, receive):
                    if scope["path"] == "/health":
                        return JSONResponse({"status": "ok"})
                    await release.wait()
                    return JSONResponse({"ok": True})
                async def receive():
                    return {"type": "http.request", "body": b""}
                async def request(path):
                    output = []
                    async def send(message):
                        output.append(message)
                    await inst.app({"type": "http", "method": "GET", "path": path}, receive, send)
                    return output[0]["status"]
                with mock.patch.object(inst.app, "dispatch", blocked):
                    pending = [asyncio.create_task(request("/api/jobs")) for _ in range(32)]
                    await asyncio.sleep(0)
                    self.assertEqual(await request("/api/jobs"), 503)
                    self.assertEqual(await asyncio.wait_for(request("/health"), 2), 200)
                    release.set()
                    self.assertEqual(await asyncio.gather(*pending), [200] * 32)
            asyncio.run(exercise())
            self.assertEqual(inst.app.active_requests, 0)
        finally:
            inst.cleanup()
