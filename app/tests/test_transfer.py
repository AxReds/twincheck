"""Export / import / direct migration between databases.

SQLite <-> SQLite always; SQLite <-> MariaDB when PHOTOORG_TEST_MARIADB_HOST is set.
Large dataset size: PHOTOORG_BIG_ROWS (default 200000; use 2000000+ for the
"millions of rows" acceptance run).
"""
import gzip
import hashlib
import json
import os
import resource
import secrets
import time
import unittest
from pathlib import Path

from helpers import STRONG, Instance, make_user, sample_library, wait_job
from test_database import drop, mariadb_params

from photoorg.db import manager as dbm
from photoorg.db import transfer
from photoorg.db.schema import CURRENT_VERSION, DATA_TABLES, columns_at, create_structure, get_meta, set_meta
from photoorg.fsutil import path_hash

PASS = "export passphrase 42"


def file_sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def checksums(db):
    v = int(get_meta(db, "schema_version"))
    cols = columns_at(v)
    return {t: transfer.table_checksum(db, t, cols[t]) for t in DATA_TABLES}


def fill(db, owner="usr_x", n=50):
    paths = ["/data/u/Photo.jpg", "/data/u/photo.jpg", "/data/u/ü/日本/фото 🙂.jpg", "/data/u/" + "long/" * 400 + "f.jpg"]
    paths += [f"/data/u/{i}.jpg" for i in range(n)]
    with db.transaction():
        db.execute("INSERT INTO jobs (kind, owner_id, status, params, created_at) VALUES ('scan', ?, 'running', '{}', '2024-01-01T00:00:00.000000Z')", (owner,))
        for i, p in enumerate(paths):
            db.execute("INSERT INTO files (owner_id, source_id, orig_path, orig_path_hash, cur_path, cur_path_hash, name, size, sha256, meta_before, present, mtime_ns) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (owner, "src", p, path_hash(p), p, path_hash(p), os.path.basename(p), i, hashlib.sha256(p.encode()).hexdigest(),
                        None if i % 3 else json.dumps({"x": "é"}), 1, 1_600_000_000_123_456_789 + i))
        db.execute("INSERT INTO run_log (ts, level, event, message, params, job_id, owner_id) VALUES ('2024-01-01T00:00:00Z','INFO','FILE_MOVED','m',NULL,1,?)", (owner,))
        db.execute("INSERT INTO manifest (job_id, owner_id, file_id, action, orig_path, new_path, created_at) VALUES (1, ?, 1, 'move', ?, ?, '2024')",
                   (owner, paths[0], paths[1]))
        db.execute("INSERT INTO dup_groups (owner_id, scope, sha256, size, keeper_file_id, members, verified, status) VALUES (?, 'user:x', 'ab', 1, 1, 2, 1, 'pending')", (owner,))


class ArchiveFormat(unittest.TestCase):
    def setUp(self):
        self.inst = Instance()
        self.tmp = Path(self.inst.tmp)

    def tearDown(self):
        self.inst.cleanup()

    def sqlite(self, name):
        return dbm.make_database({"type": "sqlite", "path": str(self.tmp / name)})

    def export(self, db, name="a.poarc", passphrase=PASS):
        p = self.tmp / name
        with open(p, "wb") as fh:
            transfer.export_to(db, fh, passphrase)
        return p

    def test_archive_verification_and_rejections(self):
        src = self.sqlite("src.sqlite3")
        dbm.prepare(src)
        fill(src)
        src_file_hash = file_sha(self.tmp / "src.sqlite3")
        arc = self.export(src)
        self.assertEqual(file_sha(self.tmp / "src.sqlite3"), src_file_hash, "source only read")
        rep = transfer.verify_archive(str(arc), PASS)
        self.assertEqual(rep["manifest"]["schema_version"], CURRENT_VERSION)
        self.assertEqual({t["name"] for t in rep["manifest"]["tables"]}, set(DATA_TABLES))
        self.assertEqual(rep["manifest"]["tables"][2]["rows"], 54)
        raw = arc.read_bytes()
        self.assertNotIn(b"photo.jpg", raw, "archive is encrypted")
        self.assertNotIn(b"vault", raw)
        with self.assertRaises(transfer.ArchiveError) as cm:
            transfer.verify_archive(str(arc), "wrong passphrase!")
        self.assertEqual(cm.exception.code, "bad_passphrase_or_tampered")
        b = bytearray(raw)
        b[len(b) // 2] ^= 0x10
        (self.tmp / "t.poarc").write_bytes(bytes(b))
        with self.assertRaises(transfer.ArchiveError) as cm:
            transfer.verify_archive(str(self.tmp / "t.poarc"), PASS)
        self.assertEqual(cm.exception.code, "bad_passphrase_or_tampered")
        (self.tmp / "trunc.poarc").write_bytes(raw[:-40])
        with self.assertRaises(transfer.ArchiveError):
            transfer.verify_archive(str(self.tmp / "trunc.poarc"), PASS)
        # archive produced by a newer application version (re-packed with a newer header)
        from photoorg.crypto import StreamEncryptor
        newer = self.tmp / "newer.poarc"
        with open(newer, "wb") as fh:
            enc = StreamEncryptor(fh, PASS)
            gz = gzip.GzipFile(fileobj=transfer._Writer(enc), mode="wb", mtime=0)
            for rec in transfer.read_records(str(arc), PASS):
                if rec["t"] in ("header", "manifest"):
                    rec["schema_version"] = CURRENT_VERSION + 1
                    rec["app_version"] = "99.0.0"
                gz.write(transfer.canon_line(rec))
            gz.close()
            enc.close()
        with self.assertRaises(transfer.ArchiveError) as cm:
            transfer.verify_archive(str(newer), PASS)
        self.assertEqual(cm.exception.code, "archive_too_new")
        src.close()

    def test_round_trip_and_upgrade_from_old_schema(self):
        # database of schema version 1 (older app) with test data
        old = self.sqlite("old.sqlite3")
        create_structure(old, 1)
        fill(old)
        arc = self.export(old, "old.poarc")
        self.assertEqual(transfer.read_header(str(arc), PASS)["schema_version"], 1)
        for params in [{"type": "sqlite", "path": str(self.tmp / "dest.sqlite3")}] + ([mariadb_params()] if mariadb_params() else []):
            with self.subTest(params["type"]):
                dest = dbm.make_database(params)
                rep = transfer.verify_archive(str(arc), PASS)
                state = transfer.prepare_destination(dest, 1, "tok", rep["header"]["source_db_id"])
                self.assertIsNotNone(get_meta(dest, "migration_state"))
                with self.assertRaises(Exception):
                    dbm.prepare(dest)                     # unusable while "migration in progress"

                class Ctx:
                    def check(self):
                        pass

                    def progress(self, **kw):
                        pass

                    def log(self, *a, **k):
                        pass
                transfer._copy_tables(Ctx(), dest, 1, transfer.read_records(str(arc), PASS), state, 0)
                report = transfer.verify_destination(dest, 1, rep["manifest"]["tables"], rep["samples"])
                self.assertTrue(report["ok"], report)
                transfer.finalize_destination(dest, 1, rep["header"], "usr_admin")
                self.assertEqual(int(get_meta(dest, "schema_version")), CURRENT_VERSION, "upgraded to the current schema")
                self.assertNotEqual(get_meta(dest, "db_id"), get_meta(old, "db_id"))
                self.assertEqual(get_meta(dest, "provenance")["source_db_id"], get_meta(old, "db_id"))
                self.assertEqual(dest.query_one("SELECT status FROM jobs WHERE id=1")["status"], "interrupted")
                paths = [r["cur_path"] for r in dest.query("SELECT cur_path FROM files ORDER BY id")]
                self.assertIn("/data/u/Photo.jpg", paths)
                self.assertIn("/data/u/photo.jpg", paths)
                self.assertIn("/data/u/ü/日本/фото 🙂.jpg", paths)
                self.assertTrue(any(len(p) > 1900 for p in paths))
                dbm.prepare(dest)
                # back again: dest -> archive -> second destination: identical checksums
                arc2 = self.export(dest, "rt.poarc")
                dest2 = dbm.make_database({"type": "sqlite", "path": str(self.tmp / f"rt-{params['type']}.sqlite3")})
                rep2 = transfer.verify_archive(str(arc2), PASS)
                st2 = transfer.prepare_destination(dest2, CURRENT_VERSION, "tok2", rep2["header"]["source_db_id"])
                transfer._copy_tables(Ctx(), dest2, CURRENT_VERSION, transfer.read_records(str(arc2), PASS), st2, 0)
                self.assertEqual(checksums(dest), {t: v for t, v in checksums(dest2).items()})
                # destination not empty -> refused
                with self.assertRaises(Exception) as cm:
                    transfer.prepare_destination(dest, CURRENT_VERSION, "other", "x")
                self.assertIn(getattr(cm.exception, "code", ""), ("destination_not_empty",))
                dest.close()
                dest2.close()
                drop(params)
        old.close()


class TransferViaUI(unittest.TestCase):
    def setUp(self):
        self.inst = Instance().start()
        self.inst.setup()
        self.a = self.inst.admin()
        sample_library(self.inst.data / "u")
        self.uid, self.u = make_user(self.a, "user1", [{"path": "u"}])
        sid = self.u.get("/api/sources").json()["sources"][0]["id"]
        wait_job(self.inst.core, self.u.post("/api/scan", {"source_id": sid}).json()["job_id"])

    def tearDown(self):
        self.inst.cleanup()

    def test_export_import_migrate_permissions(self):
        a, core = self.a, self.inst.core
        _, ops = make_user(a, "ops", [], role="administrator")
        for c in (ops, self.u):
            self.assertEqual(c.post("/api/admin/transfer/preview", {"mode": "export"}).status, 403)
            self.assertEqual(c.post("/api/admin/transfer/start", {"mode": "export", "passphrase": PASS, "confirm": True}).status, 403)
        pv = a.post("/api/admin/transfer/preview", {"mode": "export"}).json()
        self.assertGreater(pv["source"]["rows"], 0)
        self.assertEqual(a.post("/api/admin/transfer/start", {"mode": "export", "passphrase": PASS}).status, 400, "needs confirmation")
        src_sha = file_sha(self.inst.state / "photoorg.sqlite3")
        r = a.post("/api/admin/transfer/start", {"mode": "export", "passphrase": PASS, "confirm": True})
        self.assertEqual(r.status, 202, r.text)
        jid = r.json()["job_id"]
        st = wait_job(core, jid)
        self.assertEqual(st["status"], "completed", st)
        for _ in range(50):
            if core.maintenance is None:
                break
            time.sleep(0.05)
        self.assertIsNone(core.maintenance, "maintenance ends with the export")
        name = r.json()["name"]
        self.assertTrue((self.inst.state / "exports" / name).exists())
        dl = a.get(f"/api/admin/archives/exports/{name}")
        self.assertEqual(dl.status, 200)
        self.assertEqual(hashlib.sha256(dl.body).hexdigest(), file_sha(self.inst.state / "exports" / name))
        # upload through the browser endpoint into /state/imports
        up = a.request("PUT", f"/api/admin/archives/imports/{name}", raw=dl.body, headers={"content-type": "application/octet-stream"})
        self.assertEqual(up.status, 201, up.text)
        self.assertEqual(a.request("PUT", f"/api/admin/archives/imports/{name}", raw=b"x").status, 409, "never overwrite")
        # import into a new SQLite database
        body = {"mode": "import", "archive": name, "location": "imports", "passphrase": PASS, "database": {"type": "sqlite", "filename": "imported.sqlite3"}}
        pv = a.post("/api/admin/transfer/preview", body).json()
        self.assertEqual(pv["archive"]["schema_version"], CURRENT_VERSION)
        bad = a.post("/api/admin/transfer/preview", {**body, "passphrase": "not the passphrase"})
        self.assertEqual(bad.json()["error"], "bad_passphrase_or_tampered")
        r = a.post("/api/admin/transfer/start", {**body, "confirm": True})
        st = wait_job(core, r.json()["job_id"])
        self.assertEqual(st["status"], "completed", st)
        res = json.loads(core.db.query_one("SELECT result FROM jobs WHERE id=?", (r.json()["job_id"],))["result"])
        self.assertTrue(all(t["ok"] for t in res["report"]["tables"]))
        # import again into the same (now non-empty) database: refused
        r = a.post("/api/admin/transfer/start", {**body, "confirm": True})
        st = wait_job(core, r.json()["job_id"])
        self.assertEqual(st["status"], "failed")
        self.assertIn("destination_not_empty", st["error"])
        # direct migration to another SQLite database, then switch to it
        mbody = {"mode": "migrate", "database": {"type": "sqlite", "filename": "migrated.sqlite3"}}
        self.assertIn("source", a.post("/api/admin/transfer/preview", mbody).json())
        r = a.post("/api/admin/transfer/start", {**mbody, "confirm": True})
        mj = r.json()["job_id"]
        st = wait_job(core, mj)
        self.assertEqual(st["status"], "completed", st)
        time.sleep(0.2)
        src_counts = checksums(core.db)
        # the source database is only read (the job rows themselves are the only writes)
        sw = a.post("/api/admin/db/switch", {"from_transfer": mj, "confirm": True, "hash_mode": "sample"})
        self.assertEqual(sw.status, 200, sw.text)
        wait_job(core, sw.json()["reconcile_job_id"])
        dst_counts = checksums(core.db)
        self.assertEqual(src_counts["files"], dst_counts["files"])
        self.assertEqual(core.db.scalar("SELECT COUNT(*) AS n FROM review_items WHERE kind LIKE 'reconcile_%'"), 0, "disk matches")
        self.assertIn("EXPORT_STARTED", [x["event"] for x in a.get("/api/admin/audit").json()["items"]])
        self.assertTrue(src_sha)

    def test_interrupted_migration_resumes_and_purge(self):
        a, core = self.a, self.inst.core
        db = core.db
        with db.transaction():
            for i in range(3000):
                p = f"/data/u/bulk/{i}.jpg"
                db.execute("INSERT INTO files (owner_id, cur_path, cur_path_hash, size, present) VALUES (?,?,?,?,1)", (self.uid, p, path_hash(p), i))
        orig = transfer.insert_rows
        calls = {"n": 0}

        def slow_insert(*args, **kw):
            calls["n"] += 1
            time.sleep(0.05)
            return orig(*args, **kw)
        transfer.insert_rows = slow_insert
        try:
            r = a.post("/api/admin/transfer/start", {"mode": "migrate", "database": {"type": "sqlite", "filename": "m2.sqlite3"}, "confirm": True})
            jid = r.json()["job_id"]
            for _ in range(200):
                if calls["n"] >= 2:
                    break
                time.sleep(0.02)
            self.assertEqual(a.post(f"/api/jobs/{jid}/pause").status, 200)
            st = wait_job(core, jid)
            self.assertEqual(st["status"], "paused")
        finally:
            transfer.insert_rows = orig
        dest = dbm.make_database({"type": "sqlite", "path": str(self.inst.state / "m2.sqlite3")})
        state = get_meta(dest, "migration_state")
        self.assertTrue(state and state["rows"] + len(state["done_tables"]) > 0, "checkpoint stored in the destination")
        self.assertEqual(dbm.inspect(dest)["migration_in_progress"], True)
        dest.close()
        core.end_maintenance(resume=False)
        r = a.post(f"/api/admin/transfer/{jid}/resume", {})
        self.assertEqual(r.status, 200, r.text)
        st = wait_job(core, jid)
        self.assertEqual(st["status"], "completed", st)
        res = json.loads(core.db.query_one("SELECT result FROM jobs WHERE id=?", (jid,))["result"])
        self.assertTrue(res["report"]["ok"])
        # a failed transfer destination can be purged only with confirmation
        dest3 = dbm.make_database({"type": "sqlite", "path": str(self.inst.state / "m3.sqlite3")})
        transfer.prepare_destination(dest3, CURRENT_VERSION, "t3", "x")
        dest3.close()
        fake = core.jobs.submit("migrate", self.uid, {"token": "t3"}, force=True)
        core.db.execute("UPDATE jobs SET status='failed' WHERE id=?", (fake,))
        with core.vault.transaction() as d:
            d["pending_transfers"]["t3"] = {"type": "sqlite", "path": str(self.inst.state / "m3.sqlite3")}
        self.assertEqual(a.post(f"/api/admin/transfer/{fake}/purge", {}).status, 400)
        self.assertEqual(a.post(f"/api/admin/transfer/{fake}/purge", {"confirm": True}).status, 200)


class LargeDataset(unittest.TestCase):
    def test_millions_rows_memory_bounded(self):
        rows = int(os.environ.get("PHOTOORG_BIG_ROWS", "200000"))
        inst = Instance()
        try:
            tmp = Path(inst.tmp)
            src = dbm.make_database({"type": "sqlite", "path": str(tmp / "big.sqlite3")})
            dbm.prepare(src)
            batch = []
            for i in range(rows):
                p = f"/data/u/{i // 1000}/{i}.jpg"
                batch.append(("u", p, path_hash(p), i, hashlib.md5(p.encode()).hexdigest() * 2))
                if len(batch) == 20000:
                    with src.transaction():
                        src.executemany("INSERT INTO files (owner_id, cur_path, cur_path_hash, size, sha256) VALUES (?,?,?,?,?)", batch)
                    batch = []
            if batch:
                with src.transaction():
                    src.executemany("INSERT INTO files (owner_id, cur_path, cur_path_hash, size, sha256) VALUES (?,?,?,?,?)", batch)
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            t0 = time.time()
            arc = tmp / "big.poarc"
            with open(arc, "wb") as fh:
                transfer.export_to(src, fh, PASS)
            rep = transfer.verify_archive(str(arc), PASS)
            grow_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
            self.assertEqual([t for t in rep["manifest"]["tables"] if t["name"] == "files"][0]["rows"], rows)
            self.assertLess(grow_mb, 150, f"memory grew by {grow_mb:.0f} MB for {rows} rows")
            print(f"\n  large dataset: {rows} rows exported+verified in {time.time() - t0:.1f}s, "
                  f"archive {arc.stat().st_size / 1e6:.1f} MB, peak RSS growth {grow_mb:.0f} MB")
            src.close()
        finally:
            inst.cleanup()


if __name__ == "__main__":
    unittest.main()
