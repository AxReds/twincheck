"""End-to-end workflow on the sample folder: scan, duplicates, metadata (never
invent / never overwrite / conflicts / QuickTime UTC), review, organise
(companions, same names, mtimes), undo of moves and metadata, verification,
HEVC preview, cross-volume moves."""
import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import Instance, make_user, sample_library, using_fake_exiftool, wait_job

from twincheck.services.metadata import analyse_file, collect, date_from_filename


def set_tags(path, tags):
    """Write tags with the real ExifTool, or into the fake ExifTool database."""
    if using_fake_exiftool():
        db = os.environ["FAKE_EXIF_DB"]
        data = json.loads(Path(db).read_text()) if os.path.exists(db) else {}
        data.setdefault(str(os.stat(path).st_ino), {}).update(tags)
        Path(db).write_text(json.dumps(data))
        return
    args = ["exiftool", "-q", "-overwrite_original", "-P"]
    for k, v in tags.items():
        if k.startswith("Composite:"):
            continue
        args.append(f"-{k}={v}")
    subprocess.run(args + [str(path)], check=True, capture_output=True)


def read_tags(path):
    if using_fake_exiftool():
        return json.loads(Path(os.environ["FAKE_EXIF_DB"]).read_text()).get(str(os.stat(path).st_ino), {})
    out = subprocess.run(["exiftool", "-j", "-G1", "-a", "-n", str(path)], capture_output=True, check=True).stdout
    return json.loads(out)[0]


def make_hevc(path: Path) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    enc = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    codec = next((c for c in ("libx265", "libkvazaar") if f" {c} " in enc), None)
    if not codec:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "1",
                        "-c:v", codec, "-pix_fmt", "yuv420p", str(path)], capture_output=True)
    return r.returncode == 0


class PureMetadataRules(unittest.TestCase):
    F = {"name": "IMG_20210704_101500.jpg", "media_kind": "image", "ext": "jpg", "mtime_ns": 1_600_000_000 * 10 ** 9}

    def test_copy_from_equivalent_tag(self):
        own = collect({"XMP-exif:DateTimeOriginal": "2020:05:06 07:08:09"}, "file")
        r = analyse_file(self.F, own, [], {"datetime_original"})
        self.assertEqual(r["fills"]["datetime_original"]["tags"], {"ExifIFD:DateTimeOriginal": "2020:05:06 07:08:09"})
        self.assertEqual(r["chosen"], "2020-05-06T07:08:09")
        self.assertFalse(r["proposals"])

    def test_never_invent(self):
        r = analyse_file(self.F, collect({}, "file"), [], {"datetime_original", "gps", "make"})
        self.assertEqual(r["fills"], {})
        self.assertIsNone(r["chosen"])
        origins = {p["origin"] for p in r["proposals"]}
        self.assertEqual(origins, {"filename", "mtime_utc"}, "deduced values are only proposals")
        self.assertEqual(date_from_filename("IMG_20210704_101500.jpg"), "2021-07-04T10:15:00")

    def test_never_overwrite_and_conflict(self):
        own = collect({"ExifIFD:DateTimeOriginal": "2020:01:01 10:00:00", "XMP-exif:DateTimeOriginal": "2019:01:01 10:00:00"}, "file")
        r = analyse_file(self.F, own, [], {"datetime_original"})
        self.assertIn("datetime_original", r["conflicts"])
        self.assertNotIn("datetime_original", r["fills"])
        self.assertIsNone(r["chosen"])

    def test_quicktime_utc_not_converted(self):
        f = {"name": "clip.mov", "media_kind": "video", "ext": "mov", "mtime_ns": 0}
        own = collect({"QuickTime:CreateDate": "2022:08:01 22:30:00", "Track1:MediaCreateDate": "2022:08:01 22:30:00"}, "file")
        r = analyse_file(f, own, [], {"create_date", "datetime_original"})
        self.assertEqual(r["flags"], "utc_ambiguous")
        self.assertNotIn("datetime_original", r["fills"], "no local time written from a UTC value")
        own2 = collect({"QuickTime:CreateDate": "2022:08:01 22:30:00", "Keys:CreationDate": "2022:08:02 00:30:00+02:00"}, "file")
        r2 = analyse_file(f, own2, [], {"create_date"}, "Europe/Rome")
        self.assertEqual(r2["chosen"], "2022-08-02T00:30:00")
        self.assertEqual(r2["conflicts"], {}, "UTC and local differing by a whole offset are consistent")

    def test_takeout_sidecar(self):
        from twincheck.services.metadata import takeout_candidates
        import tempfile
        p = Path(tempfile.mkdtemp()) / "a.jpg.json"
        p.write_text(json.dumps({"photoTakenTime": {"timestamp": "1600000000"}, "geoData": {"latitude": 45.4642, "longitude": 9.19}}))
        c = takeout_candidates(str(p))
        r = analyse_file(self.F, collect({}, "file"), [c], {"gps", "datetime_original"})
        self.assertEqual(r["fills"]["gps"]["value"], "45.464200,9.190000")
        self.assertNotIn("datetime_original", r["fills"], "Takeout timestamp is UTC: not written as local time")


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst = Instance().start()
        cls.inst.setup()
        cls.admin = cls.inst.admin()
        root = cls.inst.data / "lib"
        cls.files = sample_library(root)
        f = cls.files
        # dup_1/dup_2/dup_3 stay byte-identical: no tags are written to them (a real
        # ExifTool write would change dup_1's bytes and break the duplicate group).
        # Without capture dates the keeper is the file with the oldest mtime (dup_1).
        set_tags(f["sidecar_img"], {"IFD0:Make": "Canon"})
        set_tags(f["sidecar_xmp"], {"XMP-exif:DateTimeOriginal": "2018:12:24 20:00:00"})
        set_tags(f["samename_1"], {"ExifIFD:DateTimeOriginal": "2020:02:02 02:02:02"})
        set_tags(f["samename_2"], {"ExifIFD:DateTimeOriginal": "2020:02:02 03:03:03"})
        set_tags(f["live_img"], {"ExifIFD:DateTimeOriginal": "2021:01:01 12:00:00"})
        # date conflict between two equivalent tags
        conflict = f["named_date"].with_name("conflict.jpg")
        from helpers import jpeg_bytes
        conflict.write_bytes(jpeg_bytes(9))
        set_tags(conflict, {"ExifIFD:DateTimeOriginal": "2020:01:01 10:00:00", "XMP-exif:DateTimeOriginal": "2010:01:01 10:00:00"})
        cls.conflict = conflict
        # HEVC video with QuickTime UTC date
        cls.hevc = root / "video" / "clip_hevc.mov"
        cls.has_hevc = make_hevc(cls.hevc)
        if cls.has_hevc:
            set_tags(cls.hevc, {"QuickTime:CreateDate": "2022:08:01 22:30:00"})
        cls.uid, cls.u = make_user(cls.admin, "photographer", [{"path": "lib"}])
        cls.sid = cls.u.get("/api/sources").json()["sources"][0]["id"]
        cls.mtimes = {k: os.stat(p).st_mtime_ns for k, p in f.items()}

    @classmethod
    def tearDownClass(cls):
        cls.inst.cleanup()

    def run_job(self, url, body=None, client=None):
        r = (client or self.u).post(url, body or {})
        self.assertEqual(r.status, 202, r.text)
        st = wait_job(self.inst.core, r.json()["job_id"], timeout=120)
        self.assertEqual(st["status"], "completed", st)
        return r.json()["job_id"]

    def confirm(self, plan_id):
        r = self.u.post(f"/api/plans/{plan_id}/confirm", {"confirm": True})
        self.assertEqual(r.status, 202, r.text)
        st = wait_job(self.inst.core, r.json()["job_id"], timeout=120)
        self.assertEqual(st["status"], "completed", st)
        return r.json()["job_id"]

    def test_full_flow(self):
        core = self.inst.core
        db = core.db
        f = self.files
        all_bytes_before = sorted(p.read_bytes() for p in (self.inst.data / "lib").rglob("*") if p.is_file())
        self.run_job("/api/scan", {"source_id": self.sid})
        n = db.scalar("SELECT COUNT(*) AS n FROM files WHERE owner_id=?", (self.uid,))
        self.assertGreaterEqual(n, 13)
        # rescan reuses hashes
        hashed_at = {r["id"]: r["hashed_at"] for r in db.query("SELECT id, hashed_at FROM files")}
        self.run_job("/api/scan", {"source_id": self.sid})
        self.assertEqual(hashed_at, {r["id"]: r["hashed_at"] for r in db.query("SELECT id, hashed_at FROM files")})

        # --- metadata analysis (preview) before duplicates so dates are known
        meta_plan = self.run_job("/api/metadata/analyze", {})
        plan = self.u.get(f"/api/plans/{meta_plan}?limit=500").json()
        fills = {Path(i["src"]).name: i["fills"] for i in plan["items"]}
        self.assertIn("pic.jpg", fills, "date copied from the .xmp sidecar")
        self.assertEqual(fills["pic.jpg"]["datetime_original"]["value"], "2018-12-24T20:00:00")
        self.assertNotIn("scan_without_exif.jpg", fills, "nothing invented for files without metadata")
        review = self.u.get("/api/review?limit=200").json()["items"]
        kinds = {(r["file"]["name"] if r.get("file") else None): r["kind"] for r in review}
        self.assertEqual(kinds.get("conflict.jpg"), "meta_conflict")
        self.assertEqual(kinds.get("scan_without_exif.jpg"), "no_date")
        if self.has_hevc:
            self.assertEqual(kinds.get("clip_hevc.mov"), "date_utc")
        make_before = read_tags(f["sidecar_img"]).get("IFD0:Make")
        self.confirm(meta_plan)
        tags = read_tags(f["sidecar_img"])
        self.assertEqual(tags.get("IFD0:Make"), make_before, "existing value untouched")
        self.assertIn("2018:12:24 20:00:00", str(tags.get("ExifIFD:DateTimeOriginal")))
        self.assertEqual(os.stat(f["sidecar_img"]).st_mtime_ns, self.mtimes["sidecar_img"], "file-system mtime preserved")
        mc = db.query("SELECT tag, before_value FROM meta_changes WHERE owner_id=?", (self.uid,))
        self.assertTrue(mc and all(json.loads(x["before_value"]) in (None, "") for x in mc), "original values stored before writing")

        # --- duplicates: oldest (mtime) kept, others moved to Duplicates, nothing deleted
        self.run_job("/api/duplicates/analyze")
        groups = self.u.get("/api/duplicates").json()
        self.assertEqual(groups["total"], 1)
        g = groups["groups"][0]
        keeper = [m for m in g["members"] if m["id"] == g["keeper_file_id"]][0]
        self.assertTrue(keeper["path"].endswith("2019 trip/IMG_0001.jpg"), keeper)
        csv = self.u.get("/api/duplicates.csv").text
        self.assertIn("keep", csv)
        dup_plan = self.run_job("/api/duplicates/plan")
        self.confirm(dup_plan)
        self.assertTrue((self.inst.data / "lib" / "Duplicates" / "phone" / "DSC_9.jpg").exists())
        self.assertTrue((self.inst.data / "lib" / "Duplicates" / "backup" / "copy" / "IMG_0001 (1).jpg").exists())

        # --- review: give a date to the file without EXIF (database only)
        item = [r for r in review if r.get("file") and r["file"]["name"] == "scan_without_exif.jpg"][0]
        detail = self.u.get(f"/api/review/{item['id']}").json()
        self.assertTrue(any(not row["present"] for row in detail["fields"]), "missing fields shown")
        self.assertEqual(self.u.post(f"/api/review/{item['id']}", {"action": "save", "fields": {"gps": "200,1"}}).status, 400)
        r = self.u.post(f"/api/review/{item['id']}", {"action": "save", "fields": {}, "date": "2015-06-07T08:09:10"})
        self.assertEqual(r.status, 200)

        # --- organise
        org_plan = self.run_job("/api/organize/plan", {"source_id": self.sid})
        items = self.u.get(f"/api/plans/{org_plan}?limit=500").json()["items"]
        by = {Path(i["src"]).name + "|" + str(Path(i["src"]).parent.name): i for i in items}
        self.assertTrue(by["photo.jpg|x"]["dst"].endswith("2020/02/02/photo.jpg"))
        self.assertTrue(by["photo.jpg|y"]["dst"].endswith("2020/02/02/photo_1.jpg"), "same name, different content: suffix")
        self.assertEqual(len(by["pic.jpg|side"]["companions"]), 2, ".xmp and .aae travel with the photo")
        self.assertEqual(len(by["IMG_7000.jpg|live"]["companions"]), 1, "Live Photo video travels with the photo")
        self.assertTrue(by["scan_without_exif.jpg|misc"]["dst"].endswith("2015/06/07/scan_without_exif.jpg"))
        self.assertIn("No_date", by["IMG_20210704_101500.jpg|misc"]["dst"], "filename date is not used without review")
        self.confirm(org_plan)
        lib = self.inst.data / "lib"
        self.assertTrue((lib / "2020/02/02/photo.jpg").exists() and (lib / "2020/02/02/photo_1.jpg").exists())
        self.assertTrue((lib / "2018/12/24/pic.xmp").exists() and (lib / "2018/12/24/pic.aae").exists())
        self.assertTrue((lib / "2021/01/01/IMG_7000.mov").exists())
        self.assertEqual(os.stat(lib / "2020/02/02/photo.jpg").st_mtime_ns, self.mtimes["samename_1"])
        # nothing lost, nothing duplicated (metadata writes change only the written files)
        after = [p for p in lib.rglob("*") if p.is_file()]
        self.assertEqual(len(after), len(all_bytes_before))

        # --- verification
        self.run_job("/api/verify")
        self.assertEqual(db.scalar("SELECT COUNT(*) AS n FROM review_items WHERE kind='verify_mismatch'"), 0)
        self.assertIn("FILE_MOVED", self.u.get("/api/logs?event=FILE_MOVED").text)
        self.assertIn("original_path", self.u.get("/api/manifest.csv").text)

        # --- undo moves and metadata
        undo = self.run_job("/api/undo/moves/plan")
        self.confirm(undo)
        self.assertTrue(f["samename_2"].exists() and f["sidecar_aae"].exists() and f["live_mov"].exists() and f["dup_3"].exists())
        self.assertFalse((lib / "Duplicates" / "phone" / "DSC_9.jpg").exists())
        undo_meta = self.run_job("/api/undo/metadata/plan")
        self.confirm(undo_meta)
        self.assertFalse(read_tags(f["sidecar_img"]).get("ExifIFD:DateTimeOriginal"), "metadata restored")
        restored = sorted(p.read_bytes() for p in lib.rglob("*") if p.is_file())
        if using_fake_exiftool():
            restored = sorted(b.replace(b"\x00fake-exif-write", b"") for b in restored)
        if not using_fake_exiftool():
            return
        self.assertEqual(restored, all_bytes_before, "library back to the original state")

    def test_hevc_preview(self):
        if not self.has_hevc:
            self.skipTest("no HEVC encoder in this ffmpeg")
        self.run_job("/api/scan", {"source_id": self.sid})
        fid = self.inst.core.db.query_one("SELECT id FROM files WHERE name='clip_hevc.mov'")["id"]
        for _ in range(120):
            r = self.u.get(f"/api/files/{fid}/preview?probe=1").json()
            if r["status"] == "ready":
                break
            time.sleep(0.5)
        self.assertEqual(r["status"], "ready")
        resp = self.u.get(f"/api/files/{fid}/preview", headers={"range": "bytes=0-99"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.headers["content-type"], "video/mp4")
        self.assertTrue(list((self.inst.state / "cache" / "previews").rglob("*.mp4")))
        self.assertTrue(self.hevc.exists())

    def test_preview_cache_scope_headers_and_review_parallel_endpoint(self):
        import hashlib
        from twincheck import previews

        photo = self.inst.data / "lib" / "preview-scope.jpg"
        photo.write_bytes(jpeg_bytes := b"preview-synthetic-bytes")
        self.run_job("/api/scan", {"source_id": self.sid})
        row = self.inst.core.db.query_one("SELECT * FROM files WHERE owner_id=? AND name='preview-scope.jpg'",
                                          (self.uid,))
        self.assertIsNotNone(row)
        review_id = self.inst.core.db.insert(
            "INSERT INTO review_items (owner_id,file_id,kind,details,proposals,status,created_at,job_id,applied) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.uid, row["id"], "no_date", "{}", "[]", "pending", "2026-11-25T00:00:00Z", 0, 0))

        started = time.monotonic()
        item = self.u.get(f"/api/review/{review_id}")
        item_elapsed = time.monotonic() - started
        self.assertEqual(item.status, 200, item.text)
        self.assertLess(item_elapsed, 2.0)
        preview = self.u.get(f"/api/review/{review_id}/preview")
        preview_elapsed = time.monotonic() - started - item_elapsed
        self.assertEqual(preview.status, 200)
        self.assertTrue(preview.headers["content-type"].startswith("image/"))
        self.assertEqual(preview.body, photo.read_bytes())
        self.assertIn("no-store", preview.headers["cache-control"])

        (self.inst.data / "preview-other").mkdir(exist_ok=True)
        other = make_user(self.admin, "preview-other", [{"path": "preview-other"}])[1]
        self.assertEqual(other.get(f"/api/files/{row['id']}/preview").status, 404)
        self.assertEqual(other.get(f"/api/review/{review_id}/preview").status, 404)

        source_path = row["cur_path"]
        fake = {
            **row, "media_kind": "video", "sha256": hashlib.sha256(jpeg_bytes).hexdigest(),
            "name": "scope.mov", "cur_path": source_path,
        }
        with mock.patch("twincheck.previews.needs_preview", return_value=True):
            status, cache_file = previews.ensure(
                self.inst.core.settings.ffmpeg, self.inst.state / "cache", fake, 720, 28, 1024)
            self.assertEqual(status, "pending")
            signature = f"{fake['sha256']}-{fake['size']}-{fake['mtime_ns']}"
            cached = Path(previews.cache_path(
                self.inst.state / "cache", fake["owner_id"], signature, fake["media_kind"]))
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(b"synthetic-video-cache")
            ready, hit = previews.ensure(
                self.inst.core.settings.ffmpeg, self.inst.state / "cache", fake, 720, 28, 1024)
            self.assertEqual((ready, hit), ("ready", str(cached)))
            fake["mtime_ns"] += 1
            changed, changed_path = previews.ensure(
                self.inst.core.settings.ffmpeg, self.inst.state / "cache", fake, 720, 28, 1024)
            self.assertEqual(changed, "pending")
            self.assertIsNone(changed_path)

        oversized = cached.parent / "old.mp4"
        oversized.write_bytes(b"x" * 2048)
        os.utime(oversized, (1, 1))
        previews.prune(self.inst.state / "cache", 512)
        self.assertFalse(oversized.exists())
        print(f"Preview endpoint timing: review item {item_elapsed:.4f}s; original preview {preview_elapsed:.4f}s")

    def test_scan_hash_completion_and_reuse(self):
        from helpers import jpeg_bytes
        root = self.inst.data / "scan-progress"
        root.mkdir()
        photo = root / "photo.jpg"
        photo.write_bytes(jpeg_bytes(101))
        uid, client = make_user(self.admin, "scan-progress", [{"path": "scan-progress"}])
        sid = client.get("/api/sources").json()["sources"][0]["id"]
        first = self.run_job("/api/scan", {"source_id": sid}, client)
        job = client.get(f"/api/jobs/{first}").json()
        self.assertEqual(job["phase"], "hash", "fast scans must not retain the throttled walk phase")
        self.assertEqual((job["progress_done"], job["progress_total"]), (1, 1))
        self.assertEqual(job["result"], {"indexed": 1, "missing": 0, "hashed": 1, "reused": 0, "unhashed": 0})
        hashed_at = self.inst.core.db.query_one("SELECT hashed_at FROM files WHERE owner_id=?", (uid,))["hashed_at"]
        second = self.run_job("/api/scan", {"source_id": sid}, client)
        job = client.get(f"/api/jobs/{second}").json()
        self.assertEqual(job["phase"], "hash")
        self.assertEqual((job["progress_done"], job["progress_total"]), (0, 0))
        self.assertEqual(job["result"], {"indexed": 1, "missing": 0, "hashed": 0, "reused": 1, "unhashed": 0})
        self.assertEqual(self.inst.core.db.query_one("SELECT hashed_at FROM files WHERE owner_id=?", (uid,))["hashed_at"], hashed_at)
        photo.write_bytes(jpeg_bytes(102) + b"changed")
        third = self.run_job("/api/scan", {"source_id": sid}, client)
        self.assertEqual(client.get(f"/api/jobs/{third}").json()["result"]["hashed"], 1)
        with mock.patch("twincheck.services.scan.sha256_file", side_effect=OSError("test read failure")):
            photo.write_bytes(jpeg_bytes(103) + b"unreadable")
            fourth = self.run_job("/api/scan", {"source_id": sid}, client)
        result = client.get(f"/api/jobs/{fourth}").json()["result"]
        self.assertEqual((result["hashed"], result["reused"], result["unhashed"]), (0, 0, 1))

    def test_duplicate_group_shared_capture_date_and_keeper_rule(self):
        from helpers import jpeg_bytes

        for mode in ("one-dated", "none-dated", "both-dated", "unread-dated", "placeholder-dated"):
            with self.subTest(mode=mode):
                root = self.inst.data / ("dup-" + mode)
                first = root / "known" / "one.jpg"
                second = root / "copies" / "nested" / "two.jpg"
                first.parent.mkdir(parents=True)
                second.parent.mkdir(parents=True)
                first.write_bytes(jpeg_bytes(155))
                if mode not in ("none-dated", "placeholder-dated"):
                    set_tags(first, {"ExifIFD:DateTimeOriginal": "2007:07:31 10:00:00"})
                shutil.copyfile(first, second)        # real ExifTool may have changed the bytes
                self.assertEqual(first.read_bytes(), second.read_bytes())
                os.utime(first, (1_700_000_000, 1_700_000_000))
                os.utime(second, (1_000_000_000, 1_000_000_000))
                uid, client = make_user(self.admin, "dup-" + mode,
                                        [{"path": str(first.parent.relative_to(self.inst.data))},
                                         {"path": str((root / "copies").relative_to(self.inst.data))}])
                sources = client.get("/api/sources").json()["sources"]
                for source in sources:
                    self.run_job("/api/scan", {"source_id": source["id"]}, client)
                if mode in ("one-dated", "both-dated"):
                    self.run_job("/api/metadata/analyze", {"source_id": sources[0]["id"]} if mode == "one-dated" else {}, client)
                if mode == "placeholder-dated":
                    placeholder = json.dumps({"fields": {"datetime_original": {"candidates": [
                        {"value": "0000:00:00 00:00:00", "tag": "ExifIFD:DateTimeOriginal",
                         "source": "file", "semantic": "local", "offset": None}
                    ]}}})
                    self.inst.core.db.execute("UPDATE files SET meta_read=?, chosen_date=NULL, phase_meta='analysed' WHERE owner_id=?",
                                              (placeholder, uid))
                if mode == "one-dated":
                    copy = self.inst.core.db.query_one("SELECT chosen_date, phase_meta FROM files WHERE owner_id=? AND name='two.jpg'", (uid,))
                    self.assertIsNone(copy["chosen_date"])
                    self.assertEqual(copy["phase_meta"], "pending", "the unread copy reproduces n/a despite identical internal tags")
                self.run_job("/api/duplicates/analyze", {}, client)
                group = client.get("/api/duplicates").json()["groups"][0]
                self.assertTrue(all(member["filesystem_date"].endswith("Z") for member in group["members"]))
                keeper = next(m for m in group["members"] if m["id"] == group["keeper_file_id"])
                if mode not in ("none-dated", "placeholder-dated"):
                    self.assertTrue(keeper["path"].endswith("one.jpg"), "shared capture date makes path tidiness decide, not missing metadata or mtime")
                if mode in ("none-dated", "placeholder-dated"):
                    self.assertIsNone(group["capture_date"])
                    self.assertEqual(group["keeper_rule"], "mtime")
                    self.assertTrue(keeper["path"].endswith("two.jpg"))
                    for photo in (first, second):
                        os.utime(photo, (1_000_000_000, 1_000_000_000))
                    for source in sources:
                        self.run_job("/api/scan", {"source_id": source["id"]}, client)
                    self.run_job("/api/duplicates/analyze", {}, client)
                    tied = client.get("/api/duplicates").json()["groups"][0]
                    self.assertEqual(tied["keeper_rule"], "mtime_tidy_path")
                    self.assertTrue(next(m for m in tied["members"] if m["id"] == tied["keeper_file_id"])["path"].endswith("one.jpg"))
                    other = next(m for m in tied["members"] if m["id"] != tied["keeper_file_id"])
                    self.assertEqual(client.post(f"/api/duplicates/{tied['id']}/keeper", {"file_id": other["id"]}).status, 200)
                    self.assertEqual(client.get("/api/duplicates").json()["groups"][0]["keeper_rule"], "manual")
                else:
                    self.assertEqual(group["capture_date"], "2007-07-31T10:00:00")
                    self.assertEqual({m["date"] for m in group["members"]}, {"2007-07-31T10:00:00"})
                    self.assertEqual(group["keeper_rule"], "capture_date_tidy_path")
                    self.assertTrue(keeper["path"].endswith("one.jpg"), "shared capture date makes path tidiness decide, not missing metadata or mtime")
                self.assertTrue(first.exists() and second.exists(), "analysis must not move or delete files")
                csv_text = client.get("/api/duplicates.csv").text
                self.assertRegex(csv_text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

    def test_analyze_library_pipeline_is_read_only_resumable_and_scoped(self):
        from helpers import jpeg_bytes
        root = self.inst.data / "pipeline" / "photos"
        root.mkdir(parents=True)
        first = root / "a.jpg"
        second = root / "nested" / "b.jpg"
        second.parent.mkdir()
        first.write_bytes(jpeg_bytes(211))
        shutil.copyfile(first, second)
        set_tags(first, {"ExifIFD:DateTimeOriginal": "2021:04:05 06:07:08"})
        shutil.copyfile(first, second)
        uid, client = make_user(self.admin, "pipeline-user", [{"path": "pipeline/photos"}])
        source = client.get("/api/sources").json()["sources"][0]

        response = client.post("/api/library/analyze", {"source_id": source["id"]})
        self.assertEqual(response.status, 202, response.text)
        job_id = response.json()["job_id"]
        job = wait_job(self.inst.core, job_id)
        self.assertEqual(job["status"], "completed", job["error"])
        public = client.get(f"/api/jobs/{job_id}").json()
        self.assertEqual(public["phase"], "5/5 complete")
        self.assertEqual(public["progress_done"], 5)
        self.assertEqual(set(public["result"]), {"scan", "capture_dates", "duplicates", "metadata"})
        checkpoint = json.loads(self.inst.core.db.query_one("SELECT checkpoint FROM jobs WHERE id=?", (job_id,))["checkpoint"])
        self.assertEqual(set(checkpoint["completed"]), {"scan", "capture_dates", "duplicates", "metadata"})
        files = self.inst.core.db.query("SELECT phase_hash, phase_meta, sha256 FROM files WHERE owner_id=?", (uid,))
        self.assertTrue(files and all(f["phase_hash"] == "done" and f["phase_meta"] == "analysed" and f["sha256"] for f in files))
        self.assertEqual(client.get("/api/duplicates").json()["total"], 1)
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(client.post("/api/library/analyze", {"source_id": "not-assigned"}).status, 403)
        self.assertIn(client.post("/api/library/analyze", {"path": "../outside"}).status, (400, 403))

        from twincheck.jobs import REGISTRY
        self.assertTrue(REGISTRY["analyze_library"].auto_resume)
        self.assertFalse(REGISTRY["analyze_library"].writes_files)

    def test_cross_volume_move(self):
        """Different volumes: copy, verify hash, then delete the original."""
        import errno
        from twincheck import mover
        src = self.inst.data / "xv" / "a.jpg"
        src.parent.mkdir()
        src.write_bytes(b"cross volume content")
        dst = self.inst.data / "xv2" / "a.jpg"
        real_link = os.link

        calls = []

        def link_once_exdev(a, b, **kw):
            calls.append(a)
            if len(calls) == 1:
                raise OSError(errno.EXDEV, "cross-device")
            return real_link(a, b, **kw)
        with mock.patch("twincheck.mover.os.link", side_effect=link_once_exdev):
            res = mover.move_file(self.inst.core.db, job_id=None, owner_id="t", file_id=None, src=str(src), dst=str(dst), sha256=None)
        self.assertTrue(res["cross_volume"])
        self.assertFalse(src.exists())
        self.assertEqual(dst.read_bytes(), b"cross volume content")
        # never overwrite
        src.write_bytes(b"new")
        with self.assertRaises(mover.MoveError):
            mover.move_file(self.inst.core.db, job_id=None, owner_id="t", file_id=None, src=str(src), dst=str(dst), sha256=None)
        self.assertEqual(dst.read_bytes(), b"cross volume content")


if __name__ == "__main__":
    unittest.main()
