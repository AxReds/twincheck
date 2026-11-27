"""rc.11 log identity, first-paint theme and export regressions."""
import csv
import io
import json
import tempfile
import unittest

from helpers import Instance, make_user
from test_database import backends
from test_rc10 import instance
from twincheck.db import manager as dbm


class Rc11Data(unittest.TestCase):
    def test_legacy_scan_and_duplicate_events_share_one_user_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    admin = inst.admin()
                    (inst.data / "mine").mkdir()
                    (inst.data / "other").mkdir()
                    uid, user = make_user(admin, "synthetic", [{"path": "mine"}])
                    other, _ = make_user(admin, "other", [{"path": "other"}])
                    db = inst.core.db
                    root = str(inst.data / "mine")
                    scan = db.insert("INSERT INTO jobs (owner_id,kind,status,params) VALUES (?,?,?,?)",
                                     (uid, "scan", "completed", json.dumps({"root": root, "dest": str(inst.data)})))
                    for event in ("JOB_QUEUED", "JOB_STARTED", "JOB_COMPLETED"):
                        dbm.log(db, event, owner_id=uid, job_id=scan, kind="scan")
                    for _ in range(2):
                        jid = db.insert("INSERT INTO jobs (owner_id,kind,status,params) VALUES (?,?,?,?)",
                                        (uid, "dedupe", "completed", json.dumps({"root": root})))
                        dbm.log(db, "DUP_GROUP", job_id=jid, keeper=root + "/synthetic.jpg", count=2, sha256="a")
                    foreign = db.insert("INSERT INTO jobs (owner_id,kind,status,params) VALUES (?,?,?,?)",
                                        (other, "scan", "completed", json.dumps({"root": str(inst.data / "other")})))
                    dbm.log(db, "JOB_STARTED", owner_id=uid, job_id=foreign, kind="scan")
                    dbm.log(db, "FILE_UNREADABLE", owner_id=uid, path=str(inst.data / "other/private.jpg"))
                    dbm.log(db, "LOGIN_OK", owner_id=uid, username="synthetic", ip="127.0.0.1")
                    dbm.log(db, "REVIEW_SKIPPED", owner_id=other, actor=uid, path=str(inst.data / "other/private.jpg"))
                    mine = user.get("/api/logs?limit=all").json()
                    filtered = admin.get(f"/api/logs?owner={uid}&limit=all").json()
                    self.assertEqual(mine["total"], 5)
                    self.assertEqual(mine["items"], filtered["items"])
                    self.assertEqual(mine["event_counts"], {code: count for code, count in filtered["event_counts"].items() if count})
                    for client, query in ((user, ""), (admin, f"&owner={uid}")):
                        rows = list(csv.DictReader(io.StringIO(client.get("/api/logs.csv?scope=filtered" + query).text)))
                        self.assertEqual(len(rows), mine["total"])
                        self.assertEqual({row["user"] for row in rows}, {"synthetic"})
                        for row in rows:
                            self.assertNotIn(str(inst.data), row["message"])
                            self.assertEqual("path_container" in row, client is admin)
                    self.assertTrue(all(row["params"] is None for row in mine["items"]))
                    for event, count in (("JOB_STARTED", 1), ("DUP_GROUP", 2)):
                        own = user.get("/api/logs?event=" + event).json()
                        selected = admin.get(f"/api/logs?owner={uid}&event={event}").json()
                        self.assertEqual(own["total"], count)
                        self.assertEqual(own["items"], selected["items"])

    def test_html_theme_is_rendered_before_scripts_with_account_precedence(self):
        inst = Instance().start()
        try:
            inst.setup()
            client = inst.admin()
            self.assertEqual(client.post("/api/me/theme", {"theme": "dark"}).status, 200)
            self.assertEqual(client.cookies.get("tc_theme"), "dark")
            for path in ("/", "/index.html", "/static/index.html"):
                self.assertIn('data-theme="dark"', client.get(path).text)
            client.cookies["tc_theme"] = "light"
            self.assertIn('data-theme="dark"', client.get("/").text)
            client.post("/api/logout")
            self.assertEqual(client.cookies.get("tc_theme"), "dark")
            self.assertIn('data-theme="dark"', client.get("/").text)
            client.cookies["tc_theme"] = '"><script>alert(1)</script>'
            html = client.get("/").text
            self.assertIn('data-theme="auto"', html)
            self.assertNotIn("alert(1)", html)
            self.assertNotIn("'unsafe-inline'", client.get("/").headers["content-security-policy"])
        finally:
            inst.cleanup()

    def test_export_names_relative_paths_and_system_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    admin = inst.admin()
                    (inst.data / "mine").mkdir()
                    uid, user = make_user(admin, "synthetic", [{"path": "mine", "label": "Custom label"}])
                    path = str(inst.data / "mine/synthetic.jpg")
                    dbm.log(inst.core.db, "REVIEW_SKIPPED", owner_id=uid, path=path)
                    dbm.log(inst.core.db, "APP_STARTED", version="synthetic")
                    for client in (admin, user):
                        self.assertEqual(client.get("/api/logs?event=REVIEW_SKIPPED").json()["items"][0]["message"],
                                         "Review postponed for mine/synthetic.jpg")
                    exported = admin.get(f"/api/logs.csv?scope=filtered&owner={uid}&job_id=35")
                    self.assertRegex(exported.headers["content-disposition"],
                                     r'filename="twincheck-logs-filtered-synthetic-job35-\d{8}-\d{4}\.csv"')
                    for route, name in (("logs.csv?scope=all", "logs-all"), ("duplicates.csv", "duplicates-own"),
                                        ("manifest.csv", "manifest")):
                        response = admin.get("/api/" + route)
                        self.assertEqual(response.status, 200)
                        self.assertRegex(response.headers["content-disposition"],
                                         rf'filename="twincheck-{name}-\d{{8}}-\d{{4}}\.csv"')
                    rows = list(csv.DictReader(io.StringIO(admin.get("/api/logs.csv?event=REVIEW_SKIPPED").text)))
                    self.assertEqual(json.loads(rows[0]["path_container"]), [path])
                    system = admin.get("/api/logs?owner=__system__&event=APP_STARTED").json()
                    self.assertEqual(system["items"][0]["user"], "system")
                    self.assertFalse(user.get("/api/logs?owner=__system__&event=APP_STARTED").json()["items"])
                    self.assertNotIn("APP_STARTED", user.get("/api/logs").json()["event_counts"])
