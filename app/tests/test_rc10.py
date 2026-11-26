"""rc.10 role-scoped counts, CSV times, pagination and population regressions."""
import csv
import io
import json
import tempfile
import unittest
from contextlib import contextmanager

from helpers import Instance, make_user
from test_database import backends, drop
from twincheck.db import manager as dbm


@contextmanager
def instance(params):
    inst = Instance().start()
    try:
        inst.setup(db=params if params["type"] == "mariadb" else None)
        yield inst
    finally:
        inst.cleanup()
        drop(params)


class Rc10Data(unittest.TestCase):
    def test_log_counts_and_csv_columns_share_role_and_filter_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    admin = inst.admin()
                    (inst.data / "lib").mkdir()
                    (inst.data / "other").mkdir()
                    uid, user = make_user(admin, "synthetic", [{"path": "lib"}])
                    other, _ = make_user(admin, "other", [{"path": "other"}])
                    self.assertEqual(admin.patch("/api/admin/settings", {"library_timezone": "Europe/Rome"}).status, 200)
                    for owner, event, ts in ((uid, "REVIEW_SKIPPED", "2026-01-15T20:51:02Z"),
                                             (uid, "REVIEW_DECIDED", "2026-07-15T20:51:02Z"),
                                             (other, "REVIEW_SKIPPED", "2026-01-15T20:51:02Z")):
                        dbm.log(inst.core.db, event, owner_id=owner, path=str(inst.data / ("lib" if owner == uid else "other") / 'comma,"file".jpg'), decision="saved")
                        inst.core.db.execute("UPDATE run_log SET ts=? WHERE id=(SELECT n FROM (SELECT MAX(id) n FROM run_log) latest)", (ts,))
                    for client in (admin, user):
                        query = f"owner={uid}&event=REVIEW_SKIPPED"
                        data = client.get("/api/logs?" + query).json()
                        filtered = list(csv.DictReader(io.StringIO(client.get("/api/logs.csv?scope=filtered&" + query).text)))
                        all_rows = list(csv.DictReader(io.StringIO(client.get("/api/logs.csv?scope=all&" + query).text)))
                        self.assertEqual(data["total"], len(filtered))
                        self.assertEqual(data["all_total"], len(all_rows))
                        self.assertEqual(data["event_counts"]["REVIEW_SKIPPED"], 1)
                        self.assertEqual(data["event_counts"]["REVIEW_DECIDED"], 1)
                        self.assertEqual(filtered[0]["time_utc"], "2026-01-15T20:51:02Z")
                        self.assertEqual(filtered[0]["time_local"], "2026-01-15 21:51:02+01:00")
                        self.assertEqual(set(filtered[0]), {"time_utc", "time_local", "level", "event", "message", "job_id", "user"})
                        self.assertIn('comma,"file".jpg', filtered[0]["message"])
                        summer = next(row for row in all_rows if row["event"] == "REVIEW_DECIDED")
                        self.assertEqual(summer["time_local"], "2026-07-15 22:51:02+02:00")
                        if client is user:
                            self.assertEqual(data["all_total"], 2)
                            self.assertEqual({row["user"] for row in all_rows}, {"synthetic"})
                            self.assertEqual(user.get("/api/logs?event=LOGIN_OK").json()["total"], 0)
                        else:
                            self.assertGreater(data["all_total"], 2)

    def test_all_rows_is_not_silently_capped_on_any_long_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    admin = inst.admin()
                    db = inst.core.db
                    owner = admin.get("/api/me").json()["id"]
                    root = inst.data / "lib"
                    root.mkdir()
                    with db.transaction():
                        for n in range(1101):
                            dbm.log(db, "REVIEW_SKIPPED", owner_id=owner, path=str(root / str(n)))
                            db.execute("INSERT INTO jobs (owner_id,kind,status,params) VALUES (?,?,?,?)", (owner, "scan", "completed", "{}"))
                            fid = db.insert("INSERT INTO files (owner_id,cur_path,cur_path_hash,media_kind,present,status) VALUES (?,?,?,?,?,?)",
                                            (owner, str(root / str(n)), str(n), "image", 1, "review"))
                            db.execute("INSERT INTO review_items (owner_id,file_id,kind,status) VALUES (?,?,?,?)", (owner, fid, "no_date", "pending"))
                    for path, key in (("/api/jobs?kind=scan&", "items"), ("/api/review?status=all&", "items"),
                                      ("/api/logs?event=REVIEW_SKIPPED&", "items")):
                        small = admin.get(path + "limit=10&offset=10").json()
                        self.assertEqual(len(small[key]), 10)
                        all_rows = admin.get(path + "limit=all").json()
                        self.assertEqual(len(all_rows[key]), 1101)
                        self.assertEqual(all_rows["total"], 1101)
                        default = admin.get(path).json()
                        self.assertEqual(len(default[key]), 50)
                        self.assertEqual(admin.get(path + "limit=invalid").status, 400)
                    # Duplicates uses the same paging contract but counts groups, not files.
                    from twincheck.services.dedupe import group_members
                    from unittest.mock import patch
                    with db.transaction():
                        for n in range(105):
                            db.execute("INSERT INTO dup_groups (scope,size,sha256,keeper_file_id,members,status) VALUES (?,?,?,?,?,?)",
                                       (f"user:{owner}", 1, str(n), 1, 2, "pending"))
                    db.execute("UPDATE files SET name='synthetic.jpg',ext='.jpg',mtime_ns=0 WHERE id IN (1,2)")
                    members = db.query("SELECT * FROM files ORDER BY id LIMIT 2")
                    with patch("twincheck.web.routes_tool.group_members", spec=group_members, return_value=members):
                        self.assertEqual(len(admin.get("/api/duplicates?limit=all").json()["groups"]), 105)
                        self.assertEqual(len(admin.get("/api/duplicates?limit=10&offset=100").json()["groups"]), 5)

    def test_population_identities_use_the_same_current_reviews_as_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    admin = inst.admin()
                    (inst.data / "lib").mkdir()
                    uid, user = make_user(admin, "synthetic", [{"path": "lib"}])
                    db = inst.core.db
                    root = str(inst.data / "lib")
                    jid = db.insert("INSERT INTO jobs (owner_id,kind,status,params,result,finished_at) VALUES (?,?,?,?,?,?)",
                                    (uid, "analyze_library", "completed", json.dumps({"root": root}),
                                     json.dumps({"metadata": {"analysed": 5}, "scan": {"unreadable": 1}}), "2026-01-15T20:00:00Z"))
                    files = [db.insert("INSERT INTO files (owner_id,cur_path,cur_path_hash,media_kind,present,status,chosen_date,chosen_date_source) VALUES (?,?,?,?,?,?,?,?)",
                                       (uid, root + "/" + str(n), str(n), "image", n != 4, "indexed",
                                        "2001-02-03T04:05:06" if n == 2 else None, "exif" if n == 2 else None)) for n in range(5)]
                    for fid, kind, status in ((files[0], "no_date", "pending"), (files[0], "move_conflict", "pending"),
                                              (files[1], "no_date", "saved"), (files[4], "no_date", "pending")):
                        db.execute("INSERT INTO review_items (owner_id,file_id,kind,status,decision,job_id) VALUES (?,?,?,?,?,?)",
                                   (uid, fid, kind, status, json.dumps({"date": "2001-02-03T04:05:06"}) if status == "saved" else None, jid))
                    meta = user.get("/api/metadata/results").json()
                    review = user.get("/api/review?status=all").json()
                    population = meta["population"]
                    self.assertEqual(population, review["population"])
                    self.assertEqual(population["total"], population["pending"] + population["decided"])
                    self.assertEqual(population["current"], population["reliable"] + population["review_files"] + population["other"])
                    self.assertEqual((population["analysed"], population["unreadable"], population["current"]), (5, 1, 4))
                    self.assertEqual(population["review_files"], meta["review_file_count"])
                    self.assertEqual(population["total"], review["total"])
                    self.assertEqual(population["total"], 3)
                    for status in ("pending", "decided"):
                        self.assertEqual(user.get("/api/review?status=" + status).json()["total"], population[status])
                    self.assertEqual(user.get("/api/review?kind=move_conflict").json()["population"], population)

    def test_invalid_timezone_never_changes_the_saved_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            for params in backends(tmp):
                with self.subTest(backend=params["type"]), instance(params) as inst:
                    admin = inst.admin()
                    self.assertEqual(admin.patch("/api/admin/settings", {"library_timezone": "Europe/Rome"}).status, 200)
                    self.assertEqual(admin.patch("/api/admin/settings", {"library_timezone": "rome"}).status, 400)
                    self.assertEqual(admin.get("/api/admin/settings").json()["settings"]["library_timezone"], "Europe/Rome")
