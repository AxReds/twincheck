"""Scan job: index files (walk) and compute SHA-256 (hash).

Read-only on disk; resumes automatically after a restart.  Hashes are reused
when path, size and modification time are unchanged.
"""
from __future__ import annotations

import fnmatch
import os
import stat

from ..db.schema import utcnow
from ..fsutil import ext_of, media_kind, path_hash, sha256_file
from ..jobs import JobContext, register

BATCH = 200


def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns) or name.startswith(".twincheck-")


def iter_dirs(root: str, patterns: list[str], ctx: JobContext | None = None):
    """Sorted depth-first walk without following symbolic links.
    Yields (relative component tuple, absolute dir, [file entries])."""
    stack = [()]
    while stack:
        rel = stack.pop()
        d = os.path.join(root, *rel)
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as exc:
            if ctx:
                ctx.log("FILE_UNREADABLE", level="WARNING", path=d, error=exc.strerror or str(exc.errno))
            continue
        subdirs, files = [], []
        for e in entries:
            if _excluded(e.name, patterns):
                continue
            try:
                if e.is_symlink():
                    if ctx:
                        ctx.log("SYMLINK_SKIPPED", path=e.path)
                    continue
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(e.name)
                elif e.is_file(follow_symlinks=False):
                    files.append(e)
            except OSError:
                continue
        yield rel, d, files
        for name in reversed(subdirs):
            stack.append(rel + (name,))


def index_dir(ctx: JobContext, source_id: str, files: list, scan_job: int) -> int:
    db = ctx.db
    n = 0
    now = utcnow()
    with db.transaction():
        for e in files:
            kind = media_kind(e.name)
            if kind == "other":
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            p = e.path
            ph = path_hash(p)
            row = db.query_one("SELECT id, size, mtime_ns FROM files WHERE owner_id=? AND cur_path_hash=?", (ctx.owner_id, ph))
            if row:
                if row["size"] != st.st_size or row["mtime_ns"] != st.st_mtime_ns:
                    db.execute("UPDATE files SET size=?, mtime_ns=?, sha256=NULL, phase_hash='pending', phase_meta='pending', "
                               "present=1, last_scan_job=?, source_id=?, updated_at=? WHERE id=?",
                               (st.st_size, st.st_mtime_ns, scan_job, source_id, now, row["id"]))
                else:
                    db.execute("UPDATE files SET present=1, last_scan_job=?, source_id=? WHERE id=?", (scan_job, source_id, row["id"]))
            else:
                db.execute(
                    "INSERT INTO files (owner_id, source_id, orig_path, orig_path_hash, cur_path, cur_path_hash, name, ext, size, "
                    "mtime_ns, media_kind, status, phase_scan, phase_hash, phase_meta, phase_org, present, last_scan_job, first_seen, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ctx.owner_id, source_id, p, ph, p, ph, e.name, ext_of(e.name), st.st_size, st.st_mtime_ns, kind, "indexed",
                     "done", "pending", "pending", "pending", 1, scan_job, now, now),
                )
            n += 1
    return n


@register("scan", auto_resume=True)
def scan_job(ctx: JobContext) -> dict:
    roots: list[str] = ctx.params["roots"]
    source_id: str = ctx.params["source_id"]
    patterns = ctx.app.settings_section("scan").get("exclude", [])
    cp = ctx.checkpoint
    phase = cp.get("phase", "walk")
    indexed = cp.get("indexed", 0)

    if phase == "walk":
        for ri, root in enumerate(roots):
            if ri < cp.get("root_index", 0) or not os.path.isdir(root):
                continue
            last = tuple(cp.get("last_dir") or ()) if ri == cp.get("root_index", 0) else ()
            for rel, d, files in iter_dirs(root, patterns, ctx):
                ctx.check()
                if last and rel <= last:
                    continue
                if files:
                    indexed += index_dir(ctx, source_id, files, ctx.id)
                ctx.progress(done=indexed, total=0, phase="walk")
                ctx.save_checkpoint(phase="walk", root_index=ri, last_dir=list(rel), indexed=indexed)
            cp = ctx.checkpoint
            ctx.save_checkpoint(phase="walk", root_index=ri + 1, last_dir=[], indexed=indexed)
        ctx.save_checkpoint(phase="missing")
        phase = "missing"

    missing = cp.get("missing", 0)
    if phase == "missing":
        db = ctx.db
        rows = db.query("SELECT id, cur_path FROM files WHERE owner_id=? AND source_id=? AND present=1 AND "
                        "(last_scan_job IS NULL OR last_scan_job <> ?)", (ctx.owner_id, source_id, ctx.id))
        with db.transaction():
            for r in rows:
                if not os.path.lexists(r["cur_path"]):
                    db.execute("UPDATE files SET present=0, updated_at=? WHERE id=?", (utcnow(), r["id"]))
                    ctx.log("FILE_MISSING", level="WARNING", path=r["cur_path"])
                    missing += 1
                else:
                    # still present but outside the scanned roots (e.g. excluded pattern)
                    db.execute("UPDATE files SET last_scan_job=? WHERE id=?", (ctx.id, r["id"]))
        ctx.save_checkpoint(phase="hash", missing=missing, last_id=0)
        phase = "hash"

    hashed = cp.get("hashed", 0)
    if phase == "hash":
        db = ctx.db
        total = int(db.scalar("SELECT COUNT(*) AS n FROM files WHERE owner_id=? AND source_id=? AND present=1 AND phase_hash<>'done'",
                              (ctx.owner_id, source_id)) or 0) + hashed
        ctx.progress(done=hashed, total=total, phase="hash", force=True)
        last_id = ctx.checkpoint.get("last_id", 0)
        while True:
            ctx.check()
            rows = db.query("SELECT id, cur_path, size, mtime_ns FROM files WHERE owner_id=? AND source_id=? AND present=1 "
                            "AND phase_hash<>'done' AND id>? ORDER BY id LIMIT ?", (ctx.owner_id, source_id, last_id, BATCH))
            if not rows:
                break
            for r in rows:
                ctx.check()
                last_id = r["id"]
                try:
                    st = os.lstat(r["cur_path"])
                    if st.st_size != r["size"] or st.st_mtime_ns != r["mtime_ns"]:
                        db.execute("UPDATE files SET size=?, mtime_ns=? WHERE id=?", (st.st_size, st.st_mtime_ns, r["id"]))
                    digest = sha256_file(r["cur_path"], stop=ctx.check)
                    st2 = os.lstat(r["cur_path"])
                    if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns:
                        continue  # changed while hashing: retried at next scan
                except OSError as exc:
                    ctx.log("FILE_UNREADABLE", level="WARNING", path=r["cur_path"], error=exc.strerror or str(exc.errno))
                    continue
                db.execute("UPDATE files SET sha256=?, phase_hash='done', hashed_at=?, updated_at=? WHERE id=?",
                           (digest, utcnow(), utcnow(), r["id"]))
                hashed += 1
                ctx.progress(done=hashed, total=total, phase="hash")
            ctx.save_checkpoint(phase="hash", last_id=last_id, hashed=hashed)
    ready = int(ctx.db.scalar("SELECT COUNT(*) AS n FROM files WHERE owner_id=? AND source_id=? AND present=1 "
                              "AND phase_hash='done' AND sha256 IS NOT NULL", (ctx.owner_id, source_id)) or 0)
    unhashed = int(ctx.db.scalar("SELECT COUNT(*) AS n FROM files WHERE owner_id=? AND source_id=? AND present=1 "
                                 "AND (phase_hash<>'done' OR sha256 IS NULL)", (ctx.owner_id, source_id)) or 0)
    return {"indexed": indexed, "missing": missing, "hashed": hashed, "reused": max(0, ready - hashed), "unhashed": unhashed}
