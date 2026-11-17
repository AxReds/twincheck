"""Final verification and reconciliation with the disk (both read-only on disk).

Differences are reported and queued for review, never corrected automatically.
"""
from __future__ import annotations

import json
import os
import random

from ..db.schema import utcnow
from ..fsutil import path_hash, sha256_file
from ..jobs import JobContext, register
from . import access
from .scan import iter_dirs


def _review(db, owner, file_id, kind, details, job_id):
    db.execute("INSERT INTO review_items (owner_id, file_id, kind, details, proposals, status, created_at, job_id, applied) VALUES (?,?,?,?,?,?,?,?,?)",
               (owner, file_id, kind, json.dumps(details, ensure_ascii=False), "[]", "pending", utcnow(), job_id, 0))


@register("verify", auto_resume=True)
def verify(ctx: JobContext) -> dict:
    """Files and hashes match the index and the manifest."""
    db = ctx.db
    last = ctx.checkpoint.get("last_id", 0)
    stats = ctx.checkpoint.get("stats") or {"checked": 0, "mismatches": 0}
    total = int(db.scalar("SELECT COUNT(*) AS n FROM files WHERE owner_id=? AND present=1", (ctx.owner_id,)) or 0)
    while True:
        ctx.check()
        rows = db.query("SELECT id, cur_path, size, sha256 FROM files WHERE owner_id=? AND present=1 AND id>? ORDER BY id LIMIT 200",
                        (ctx.owner_id, last))
        if not rows:
            break
        for f in rows:
            ctx.check()
            last = f["id"]
            reason = None
            if not os.path.lexists(f["cur_path"]):
                reason = "missing"
            elif os.path.getsize(f["cur_path"]) != f["size"]:
                reason = "size_changed"
            elif f["sha256"] and sha256_file(f["cur_path"], stop=ctx.check) != f["sha256"]:
                reason = "hash_changed"
            else:
                m = db.query_one("SELECT new_path FROM manifest WHERE file_id=? AND action IN ('move','dup_move','restore') "
                                 "AND (undone_at IS NULL OR action='restore') ORDER BY id DESC LIMIT 1", (f["id"],))
                if m and m["new_path"] != f["cur_path"]:
                    reason = "manifest_mismatch"
            if reason:
                stats["mismatches"] += 1
                ctx.log("VERIFY_MISMATCH", level="WARNING", path=f["cur_path"], reason=reason)
                _review(db, ctx.owner_id, f["id"], "verify_mismatch", {"reason": reason}, ctx.id)
            stats["checked"] += 1
            ctx.progress(done=stats["checked"], total=total, phase="verify")
        ctx.save_checkpoint(last_id=last, stats=stats)
    if not stats["mismatches"]:
        ctx.log("VERIFY_OK", count=stats["checked"])
    return stats


@register("reconcile", auto_resume=True)
def reconcile(ctx: JobContext) -> dict:
    """After a database switch: compare the stored state with the disk.
    hash_mode: none | sample | full."""
    db = ctx.db
    mode = ctx.params.get("hash_mode", "none")
    data_dir = ctx.app.settings.data_dir.as_posix()
    cp = ctx.checkpoint
    stats = cp.get("stats") or {"missing": 0, "modified": 0, "new": 0, "moved": 0, "checked": 0}
    last = cp.get("last_id", 0)
    phase = cp.get("phase", "index")
    rnd = random.Random(ctx.id)
    total = int(db.scalar("SELECT COUNT(*) AS n FROM files WHERE present=1") or 0)
    if phase == "index":
        while True:
            ctx.check()
            rows = db.query("SELECT id, owner_id, cur_path, size, mtime_ns, sha256 FROM files WHERE present=1 AND id>? ORDER BY id LIMIT 500", (last,))
            if not rows:
                break
            with db.transaction():
                for f in rows:
                    last = f["id"]
                    stats["checked"] += 1
                    try:
                        st = os.lstat(f["cur_path"])
                    except OSError:
                        _review(db, f["owner_id"], f["id"], "reconcile_missing", {"path": f["cur_path"], "size": f["size"], "sha256": f["sha256"]}, ctx.id)
                        stats["missing"] += 1
                        continue
                    changed = st.st_size != f["size"] or st.st_mtime_ns != f["mtime_ns"]
                    if not changed and f["sha256"] and (mode == "full" or (mode == "sample" and rnd.random() < 0.02)):
                        changed = sha256_file(f["cur_path"]) != f["sha256"]
                    if changed:
                        _review(db, f["owner_id"], f["id"], "reconcile_modified", {"path": f["cur_path"]}, ctx.id)
                        stats["modified"] += 1
            ctx.progress(done=stats["checked"], total=total, phase="index")
            ctx.save_checkpoint(phase="index", last_id=last, stats=stats)
        ctx.save_checkpoint(phase="disk", stats=stats, user_index=0)
    # new files on disk (and moved = missing + new with identical content)
    users = [u for u in ctx.app.vault.snapshot()["users"].values() if u.get("sources")]
    users.sort(key=lambda u: u["id"])
    missing = {}
    for r in db.query("SELECT r.id, f.size, f.sha256, f.owner_id FROM review_items r JOIN files f ON f.id=r.file_id "
                      "WHERE r.kind='reconcile_missing' AND r.job_id=?", (ctx.id,)):
        missing.setdefault((r["owner_id"], r["size"]), []).append(r)
    start = ctx.checkpoint.get("user_index", 0)
    for ui, u in enumerate(users):
        if ui < start:
            continue
        for a in access.areas(data_dir, u):
            for root in {a["path"], a["dest"]}:
                if not os.path.isdir(root):
                    continue
                for _rel, _d, files in iter_dirs(root, ctx.app.settings_section("scan").get("exclude", [])):
                    ctx.check()
                    for e in files:
                        p = e.path
                        if db.query_one("SELECT id FROM files WHERE owner_id=? AND cur_path_hash=?", (u["id"], path_hash(p))):
                            continue
                        size = e.stat(follow_symlinks=False).st_size
                        cands = missing.get((u["id"], size))
                        kind, details = "reconcile_new", {"path": p, "size": size}
                        if cands:
                            sha = sha256_file(p)
                            hit = next((c for c in cands if c["sha256"] == sha), None)
                            if hit:
                                kind, details = "reconcile_moved", {"path": p, "review_missing_id": hit["id"]}
                                stats["moved"] += 1
                        if kind == "reconcile_new":
                            stats["new"] += 1
                        _review(db, u["id"], None, kind, details, ctx.id)
                        ctx.log("RECONCILE_DIFF", kind=kind, path=p)
        ctx.save_checkpoint(phase="disk", stats=stats, user_index=ui + 1)
    ctx.log("RECONCILE_DONE", **{k: stats[k] for k in ("missing", "modified", "new", "moved")})
    return stats
