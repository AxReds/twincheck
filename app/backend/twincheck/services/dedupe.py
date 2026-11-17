"""Duplicate detection: same size + same SHA-256 + byte-by-byte comparison.

Never visual similarity, names, dates or EXIF.  The keeper is the file with the
oldest date (capture date if known, otherwise modification time); ties are
broken by the "tidiest" path: fewer components, then shorter, then
lexicographic.  Duplicates are moved to the "Duplicates" folder, never deleted.
"""
from __future__ import annotations

import os
import time

from ..db.schema import utcnow
from ..fsutil import same_bytes
from ..jobs import JobContext, register


def _date_key(f: dict) -> str:
    if f.get("chosen_date"):
        return f["chosen_date"]
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((f.get("mtime_ns") or 0) / 1e9))


def keeper_order(f: dict):
    p = f["cur_path"]
    return (_date_key(f), p.count("/"), len(p), p)


def _groups(db, owner_id: str | None, after: tuple):
    where = "present=1 AND sha256 IS NOT NULL AND media_kind IN ('image','video') AND status<>'dup_moved'"
    params: list = []
    if owner_id:
        where = "owner_id=? AND " + where
        params.append(owner_id)
    sql = (f"SELECT size, sha256, COUNT(*) AS n FROM files WHERE {where} GROUP BY size, sha256 HAVING COUNT(*) > 1 "
           f"ORDER BY size, sha256")
    for g in db.query(sql, params):
        if after and (g["size"], g["sha256"]) <= after:
            continue
        yield g


def analyze(ctx: JobContext, scope: str, owner_filter: str | None) -> dict:
    db = ctx.db
    cp = ctx.checkpoint
    if not cp.get("started"):
        with db.transaction():
            if owner_filter:
                db.execute("DELETE FROM dup_groups WHERE scope=? AND status='pending'", (scope,))
                db.execute("UPDATE files SET dup_group_id=NULL, dup_of=NULL, status='unique' WHERE owner_id=? AND status IN ('duplicate','indexed','unique')",
                           (owner_filter,))
            else:
                db.execute("DELETE FROM dup_groups WHERE scope='global' AND status='pending'")
        ctx.save_checkpoint(started=True, after=None, groups=0, dups=0)
    after = tuple(ctx.checkpoint.get("after") or ())
    groups, dups = ctx.checkpoint.get("groups", 0), ctx.checkpoint.get("dups", 0)
    pending = list(_groups(db, owner_filter, after))
    total = len(pending) + groups
    for g in pending:
        ctx.check()
        members = db.query("SELECT id, owner_id, cur_path, size, mtime_ns, chosen_date FROM files WHERE size=? AND sha256=? AND present=1 "
                           "AND media_kind IN ('image','video') AND status<>'dup_moved'" + (" AND owner_id=?" if owner_filter else ""),
                           (g["size"], g["sha256"], owner_filter) if owner_filter else (g["size"], g["sha256"]))
        if not owner_filter and len({m["owner_id"] for m in members}) < 2:
            ctx.save_checkpoint(after=[g["size"], g["sha256"]])
            continue
        members.sort(key=keeper_order)
        keeper = members[0]
        confirmed = [keeper]
        for m in members[1:]:
            try:
                ok = same_bytes(keeper["cur_path"], m["cur_path"])
            except OSError:
                ok = False
            if ok:
                confirmed.append(m)
            else:
                ctx.log("DUP_MISMATCH", level="WARNING", a=keeper["cur_path"], b=m["cur_path"])
        if len(confirmed) > 1:
            with db.transaction():
                gid = db.insert("INSERT INTO dup_groups (owner_id, scope, sha256, size, keeper_file_id, members, verified, status, job_id, created_at) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (owner_filter or "*", scope, g["sha256"], g["size"], keeper["id"], len(confirmed), 1, "pending", ctx.id, utcnow()))
                if owner_filter:
                    db.execute("UPDATE files SET dup_group_id=?, dup_of=NULL, status='unique' WHERE id=?", (gid, keeper["id"]))
                    for m in confirmed[1:]:
                        db.execute("UPDATE files SET dup_group_id=?, dup_of=?, status='duplicate' WHERE id=?", (gid, keeper["id"], m["id"]))
                ctx.log("DUP_GROUP", sha256=g["sha256"], count=len(confirmed), keeper=keeper["cur_path"])
            groups += 1
            dups += len(confirmed) - 1
        ctx.progress(done=groups, total=total, phase="compare")
        ctx.save_checkpoint(after=[g["size"], g["sha256"]], groups=groups, dups=dups)
    return {"groups": groups, "duplicates": dups}


@register("dedupe_analyze", auto_resume=True)
def dedupe_analyze(ctx: JobContext) -> dict:
    return analyze(ctx, f"user:{ctx.owner_id}", ctx.owner_id)


@register("global_dedupe_analyze", auto_resume=True)
def global_dedupe_analyze(ctx: JobContext) -> dict:
    return analyze(ctx, "global", None)


def set_keeper(db, group_id: int, file_id: int) -> None:
    g = db.query_one("SELECT * FROM dup_groups WHERE id=?", (group_id,))
    if not g or g["status"] != "pending":
        raise ValueError("group_not_pending")
    f = db.query_one("SELECT id, owner_id FROM files WHERE id=? AND sha256=? AND size=?", (file_id, g["sha256"], g["size"]))
    if not f:
        raise ValueError("not_in_group")
    with db.transaction():
        db.execute("UPDATE dup_groups SET keeper_file_id=? WHERE id=?", (file_id, group_id))
        if g["scope"] != "global":
            db.execute("UPDATE files SET dup_of=?, status='duplicate' WHERE dup_group_id=? AND id<>?", (file_id, group_id, file_id))
            db.execute("UPDATE files SET dup_of=NULL, status='unique' WHERE id=?", (file_id,))


def group_members(db, g: dict) -> list[dict]:
    sql = ("SELECT id, owner_id, cur_path, size, mtime_ns, chosen_date, status FROM files WHERE size=? AND sha256=? AND present=1 "
           "AND media_kind IN ('image','video')")
    params: list = [g["size"], g["sha256"]]
    if g["scope"] != "global":
        sql += " AND owner_id=?"
        params.append(g["owner_id"])
    rows = db.query(sql, params)
    rows.sort(key=lambda r: (r["id"] != g["keeper_file_id"], keeper_order(r)))
    return rows


def duplicates_folder_target(area: dict, cur_path: str, folder: str) -> str:
    for root in (area["dest"], area["path"]):
        if cur_path.startswith(root.rstrip("/") + "/"):
            rel = os.path.relpath(cur_path, root)
            break
    else:
        rel = os.path.basename(cur_path)
    return os.path.join(area["dest"], folder, rel)
