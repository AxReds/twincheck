"""Operational database: structure creation/reuse, concurrency, schema versions,
instance lock, switch with paused jobs, reconciliation, rollback.

MariaDB variants run when PHOTOORG_TEST_MARIADB_HOST is set (see tests/README.md).
"""
import json
import multiprocessing
import os
import secrets
import tempfile
import time
import unittest
from pathlib import Path

from helpers import STRONG, Instance, make_user, sample_library, wait_job

from twincheck import jobs as jobs_mod
from twincheck.db import manager as dbm
from twincheck.db.engine import DBError
from twincheck.db.schema import CURRENT_VERSION, columns_at, create_structure, get_meta, set_meta, structure_version


@jobs_mod.register("test_slow", auto_resume=False, writes_files=True)
def _slow(ctx):
    i = ctx.checkpoint.get("i", 0)
    while i < ctx.params.get("n", 400):
        ctx.check()
        time.sleep(0.01)
        i += 1
        ctx.done = i
        ctx.save_checkpoint(i=i)
    return {"i": i}


def mariadb_params():
    host = os.environ.get("PHOTOORG_TEST_MARIADB_HOST")
    if not host:
        return None
    return {"type": "mariadb", "host": host, "port": int(os.environ.get("PHOTOORG_TEST_MARIADB_PORT", "3306")),
            "user": os.environ.get("PHOTOORG_TEST_MARIADB_USER", "root"), "password": os.environ.get("PHOTOORG_TEST_MARIADB_PASSWORD", ""),
            "database": "twincheck_t_" + secrets.token_hex(4)}


def backends(tmp):
    out = [{"type": "sqlite", "path": os.path.join(tmp, f"db-{secrets.token_hex(3)}.sqlite3")}]
    m = mariadb_params()
    if m:
        out.append(m)
    return out


def drop(params):
    if params["type"] == "mariadb":
        import pymysql
        c = pymysql.connect(host=params["host"], port=params["port"], user=params["user"], password=params["password"])
        with c.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{params['database']}`")
        c.close()


def _prepare_worker(params, q):
    try:
        db = dbm.make_database(params)
        res = dbm.prepare(db)
        q.put(("ok", res["db_id"]))
    except Exception as exc:  # pragma: no cover
        q.put(("err", repr(exc)))


class Structure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_create_reuse_versions(self):
        for params in backends(self.tmp):
            with self.subTest(params["type"]):
                db = dbm.make_database(params)
                try:
                    info = dbm.inspect(db)
                    self.assertEqual(info["structure"], "absent")
                    r = dbm.prepare(db)
                    self.assertTrue(r["created"])
                    self.assertEqual(structure_version(db), CURRENT_VERSION)
                    db.execute("INSERT INTO files (owner_id, cur_path, cur_path_hash, size) VALUES ('u', '/data/x', 'h', 1)")
                    r2 = dbm.prepare(db)
                    self.assertFalse(r2["created"])
                    self.assertEqual(r2["db_id"], r["db_id"])
                    self.assertEqual(db.scalar("SELECT COUNT(*) AS n FROM files"), 1, "reuse without data loss")
                    info = dbm.inspect(db)
                    self.assertEqual((info["structure"], info["files"], info["db_id"]), ("present", 1, r["db_id"]))
                    # newer schema -> refused
                    set_meta(db, "schema_version", CURRENT_VERSION + 1)
                    with self.assertRaises(DBError) as cm:
                        dbm.prepare(db)
                    self.assertEqual(cm.exception.code, "schema_too_new")
                finally:
                    db.close()
                    drop(params)

    def test_older_schema_needs_confirmation(self):
        for params in backends(self.tmp):
            with self.subTest(params["type"]):
                db = dbm.make_database(params)
                try:
                    if params["type"] == "mariadb":
                        db.create_database_if_missing()
                    create_structure(db, 1)
                    self.assertNotIn("duration_s", db.columns("files"))
                    db.execute("INSERT INTO jobs (kind, owner_id, status) VALUES ('scan', 'u', 'completed')")
                    with self.assertRaises(DBError) as cm:
                        dbm.prepare(db)
                    self.assertEqual(cm.exception.code, "needs_migration")
                    self.assertEqual(structure_version(db), 1, "nothing changed without confirmation")
                    dbm.prepare(db, allow_migrate=True)
                    self.assertEqual(structure_version(db), CURRENT_VERSION)
                    self.assertIn("duration_s", db.columns("files"))
                    self.assertIn("auto_resume", db.columns("jobs"))
                    self.assertEqual(db.scalar("SELECT COUNT(*) AS n FROM jobs"), 1)
                    dbm.prepare(db, allow_migrate=True)          # idempotent
                finally:
                    db.close()
                    drop(params)

    def test_simultaneous_starts(self):
        for params in backends(self.tmp):
            with self.subTest(params["type"]):
                ctx = multiprocessing.get_context("spawn")
                q = ctx.Queue()
                procs = [ctx.Process(target=_prepare_worker, args=(params, q)) for _ in range(4)]
                for p in procs:
                    p.start()
                for p in procs:
                    p.join(60)
                res = [q.get(timeout=5) for _ in procs]
                self.assertTrue(all(r[0] == "ok" for r in res), res)
                self.assertEqual(len({r[1] for r in res}), 1, "one structure, one db_id")
                drop(params)

    def test_instance_lock(self):
        for params in backends(self.tmp):
            with self.subTest(params["type"]):
                db1, db2 = dbm.make_database(params), dbm.make_database(params)
                dbm.prepare(db1)
                l1 = dbm.InstanceLock(db1, "one")
                l1.acquire()
                with self.assertRaises(DBError) as cm:
                    dbm.InstanceLock(db2, "two").acquire()
                self.assertEqual(cm.exception.code, "in_use")
                l1.release()
                l2 = dbm.InstanceLock(db2, "two")
                l2.acquire()
                l2.release()
                db1.close()
                db2.close()
                drop(params)

    def test_unicode_long_and_case_paths(self):
        for params in backends(self.tmp):
            with self.subTest(params["type"]):
                db = dbm.make_database(params)
                try:
                    dbm.prepare(db)
                    from twincheck.fsutil import path_hash
                    paths = ["/data/a/Photo.jpg", "/data/a/photo.jpg", "/data/ü/日本/фото 🙂.jpg", "/data/" + "x" * 3000 + "/f.jpg"]
                    for p in paths:
                        db.execute("INSERT INTO files (owner_id, cur_path, cur_path_hash, orig_path, size, sha256) VALUES (?,?,?,?,?,?)",
                                   ("u", p, path_hash(p), None, 1, None))
                    got = [r["cur_path"] for r in db.query("SELECT cur_path FROM files ORDER BY id")]
                    self.assertEqual(got, paths)
                    self.assertEqual(db.scalar("SELECT COUNT(*) AS n FROM files WHERE orig_path IS NULL"), 4)
                finally:
                    db.close()
                    drop(params)


class SwitchDatabase(unittest.TestCase):
    def test_switch_pause_reconcile_return_and_rollback(self):
        inst = Instance().start()
        try:
            inst.setup()
            a = inst.admin()
            sample_library(inst.data / "u")
            uid, u = make_user(a, "user1", [{"path": "u"}])
            sid = u.get("/api/sources").json()["sources"][0]["id"]
            wait_job(inst.core, u.post("/api/scan", {"source_id": sid}).json()["job_id"])
            old_db_id = get_meta(inst.core.db, "db_id")
            slow = inst.core.jobs.submit("test_slow", uid, {"n": 400})
            time.sleep(0.6)
            self.assertEqual(inst.core.db.query_one("SELECT status FROM jobs WHERE id=?", (slow,))["status"], "running")
            # 1-2: test connection
            info = a.post("/api/admin/db/test", {"database": {"type": "sqlite", "filename": "second.sqlite3"}}).json()
            self.assertEqual(info["structure"], "absent")
            # failure: unreachable MariaDB -> stays on the current database, job resumed
            r = a.post("/api/admin/db/switch", {"database": {"type": "mariadb", "host": "127.0.0.1", "port": 1, "database": "x",
                                                             "user": "x", "password": "secret-pw"}, "confirm": True})
            self.assertEqual(r.status, 400)
            self.assertNotIn("secret-pw", r.text)
            self.assertEqual(get_meta(inst.core.db, "db_id"), old_db_id)
            time.sleep(0.3)
            self.assertIn(inst.core.db.query_one("SELECT status FROM jobs WHERE id=?", (slow,))["status"], ("running", "queued", "completed"))
            # explicit confirmation required
            self.assertEqual(a.post("/api/admin/db/switch", {"database": {"type": "sqlite", "filename": "second.sqlite3"}}).status, 400)
            # 3-8: real switch
            r = a.post("/api/admin/db/switch", {"database": {"type": "sqlite", "filename": "second.sqlite3"}, "confirm": True, "hash_mode": "full"})
            self.assertEqual(r.status, 200, r.text)
            new_id = r.json()["db_id"]
            self.assertNotEqual(new_id, old_db_id)
            wait_job(inst.core, r.json()["reconcile_job_id"])
            self.assertEqual(inst.core.db.scalar("SELECT COUNT(*) AS n FROM files"), 0, "empty database starts from zero")
            new_files = inst.core.db.scalar("SELECT COUNT(*) AS n FROM review_items WHERE kind='reconcile_new'")
            self.assertGreater(new_files, 0, "files on disk unknown to the new database are reported, not imported")
            self.assertIn("DB_SWITCHED", [x["event"] for x in a.get("/api/admin/audit").json()["items"]])
            # the old database was not modified except for the paused job
            old = dbm.make_database({"type": "sqlite", "path": str(inst.state / "twincheck.sqlite3")})
            self.assertEqual(old.query_one("SELECT status FROM jobs WHERE id=?", (slow,))["status"], "paused")
            cp = json.loads(old.query_one("SELECT checkpoint FROM jobs WHERE id=?", (slow,))["checkpoint"])
            old.close()
            # change files on disk, then go back to the previous database
            victim = next((inst.data / "u" / "misc").glob("scan_without_exif.jpg"))
            victim.write_bytes(victim.read_bytes() + b"changed")
            os.remove(inst.data / "u" / "x" / "photo.jpg")
            prev = a.get("/api/admin/db").json()["previous"]
            self.assertEqual(len(prev), 1)
            r = a.post("/api/admin/db/switch", {"use_previous": 0, "confirm": True, "hash_mode": "none"})
            self.assertEqual(r.status, 200, r.text)
            self.assertEqual(r.json()["db_id"], old_db_id)
            wait_job(inst.core, r.json()["reconcile_job_id"])
            kinds = {x["kind"] for x in inst.core.db.query("SELECT kind FROM review_items")}
            self.assertTrue({"reconcile_modified", "reconcile_missing"} <= kinds, kinds)
            # the paused job restarts from its stored checkpoint (write job -> needs a click)
            self.assertEqual(inst.core.db.query_one("SELECT status FROM jobs WHERE id=?", (slow,))["status"], "paused")
            self.assertEqual(u.post(f"/api/jobs/{slow}/resume").status, 200)
            wait_job(inst.core, slow)
            res = json.loads(inst.core.db.query_one("SELECT result FROM jobs WHERE id=?", (slow,))["result"])
            self.assertEqual(res["i"], 400)
            self.assertGreater(cp["i"], 0)
            # base users and administrators: administrator may switch, base user may not
            self.assertEqual(u.post("/api/admin/db/test", {"database": {"type": "sqlite"}}).status, 403)
        finally:
            inst.cleanup()


class CrashRecovery(unittest.TestCase):
    def test_restart_marks_interrupted_and_resumes_read_only(self):
        inst = Instance().start()
        try:
            inst.setup()
            a = inst.admin()
            sample_library(inst.data / "u")
            uid, u = make_user(a, "user1", [{"path": "u"}])
            sid = u.get("/api/sources").json()["sources"][0]["id"]
            # simulate a kill: jobs left "running" with a checkpoint
            db = inst.core.db
            scan_id = inst.core.jobs.submit("scan", uid, {"source_id": sid, "root": str(inst.data / "u"), "dest": str(inst.data / "u"),
                                                          "roots": [str(inst.data / "u")]})
            wait_job(inst.core, scan_id)
            db.execute("UPDATE jobs SET status='running', checkpoint=? WHERE id=?", (json.dumps({"phase": "hash", "last_id": 3, "hashed": 3}), scan_id))
            db.execute("UPDATE files SET phase_hash='pending', sha256=NULL WHERE id>3")
            slow = inst.core.jobs.submit("test_slow", uid, {"n": 5})
            wait_job(inst.core, slow)
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (slow,))
            inst.core.jobs.recover_after_start()
            st = wait_job(inst.core, scan_id)
            self.assertEqual(st["status"], "completed", "read-only job resumed automatically")
            self.assertEqual(db.scalar("SELECT COUNT(*) AS n FROM files WHERE sha256 IS NULL AND present=1"), 0)
            self.assertEqual(db.query_one("SELECT status FROM jobs WHERE id=?", (slow,))["status"], "interrupted",
                             "writing job waits for confirmation")
        finally:
            inst.cleanup()

    def test_move_journal_recovery(self):
        from twincheck.mover import recover
        inst = Instance().start()
        try:
            inst.setup()
            db = inst.core.db
            d = inst.data
            cases = {}
            # 1) intent recorded, nothing moved
            (d / "a.jpg").write_bytes(b"A")
            cases["rolled_back"] = (d / "a.jpg", d / "dst" / "a.jpg")
            # 2) moved, completion not recorded
            (d / "dst").mkdir()
            (d / "dst" / "b.jpg").write_bytes(b"B")
            cases["done"] = (d / "b.jpg", d / "dst" / "b.jpg")
            # 3) linked, unlink of source pending
            (d / "c.jpg").write_bytes(b"C")
            os.link(d / "c.jpg", d / "dst" / "c.jpg")
            cases["done2"] = (d / "c.jpg", d / "dst" / "c.jpg")
            # 4) destination exists but is an unrelated file: keep both
            (d / "e.jpg").write_bytes(b"E")
            (d / "dst" / "e.jpg").write_bytes(b"other")
            cases["keep_both"] = (d / "e.jpg", d / "dst" / "e.jpg")
            import hashlib
            for name, (src, dst) in cases.items():
                sha = hashlib.sha256(b"E" if name == "keep_both" else b"x").hexdigest()
                db.execute("INSERT INTO move_journal (owner_id, op, src, dst, sha256, size, state) VALUES ('u','move',?,?,?,1,'intent')",
                           (str(src), str(dst), sha))
            res = {r["id"]: r["state"] for r in recover(db)}
            self.assertEqual(list(res.values()), ["rolled_back", "done", "done", "rolled_back"])
            self.assertTrue((d / "a.jpg").exists() and not (d / "dst" / "a.jpg").exists())
            self.assertTrue((d / "dst" / "b.jpg").exists())
            self.assertTrue((d / "dst" / "c.jpg").exists() and not (d / "c.jpg").exists())
            self.assertTrue((d / "e.jpg").exists() and (d / "dst" / "e.jpg").read_bytes() == b"other", "nothing lost or overwritten")
            self.assertEqual(db.scalar("SELECT COUNT(*) AS n FROM move_journal WHERE state='intent'"), 0)
        finally:
            inst.cleanup()


if __name__ == "__main__":
    unittest.main()
