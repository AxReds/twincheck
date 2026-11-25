"""Duplicate detection: same size + same SHA-256 + byte-by-byte comparison.

Never visual similarity, names, dates or EXIF.  The keeper is the file with the
shared internal capture date if known, otherwise oldest modification time; ties are
broken by the "tidiest" path: fewer components, then shorter, then
lexicographic.  Duplicates are moved to the "Duplicates" folder, never deleted.
"""
from __future__ import annotations

import os
import time
import json

from ..db.schema import utcnow
from ..fsutil import same_bytes
from ..jobs import JobContext, register
from .metadata import analyse_file, ensure_capture_dates, parse_dt


def _date_key(f: dict) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((f.get("mtime_ns") or 0) / 1e9)) + "Z"


def keeper_order(f: dict, capture_date: str | None = None):
    p = f["cur_path"]
    return (capture_date or _date_key(f), p.count("/"), len(p), p)


def group_capture_date(members: list[dict]) -> dict:
    dates = []
    for member in members:
        fields = json.loads(member.get("meta_read") or "{}").get("fields", {})
        own = {field: [c for c in info.get("candidates", [])
                       if c.get("source") == "file"
                       and (field not in ("datetime_original", "create_date") or parse_dt(c.get("value")))]
               for field, info in fields.items()}
        result = analyse_file(member, own, [], set(), None)
        if result["chosen"]:
            dates.append(result)
    if not dates:
        return {"capture_date": None, "date_flags": ""}
    chosen = min(dates, key=lambda r: r["chosen"])
    return {"capture_date": chosen["chosen"], "capture_offset": chosen.get("chosen_offset"),
            "date_flags": chosen["flags"]}


def keeper_decision(members: list[dict], keeper_id: int) -> dict:
    date = group_capture_date(members)
    ordered = sorted(members, key=lambda f: keeper_order(f, date["capture_date"]))
    keeper = next(m for m in members if m["id"] == keeper_id)
    if keeper_id != ordered[0]["id"]:
        rule = "manual"
    elif date["capture_date"]:
        rule = "capture_date_tidy_path"
    else:
        rule = "mtime_tidy_path" if len(ordered) > 1 and _date_key(ordered[0]) == _date_key(ordered[1]) else "mtime"
    return {**date, "keeper_rule": rule, "keeper_date": date["capture_date"] or _date_key(keeper)}


def _groups(db, owner_id: str | None, after: tuple, scope_filter=None):
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
        if scope_filter:
            scoped = db.query("SELECT cur_path FROM files WHERE size=? AND sha256=? AND present=1 "
                              "AND media_kind IN ('image','video') AND status<>'dup_moved' AND owner_id=?",
                              (g["size"], g["sha256"], owner_id))
            if sum(1 for row in scoped if scope_filter(row)) < 2:
                continue
        yield g


def analyze(ctx: JobContext, scope: str, owner_filter: str | None) -> dict:
    db = ctx.db
    from . import access
    user = ctx.app.user(owner_filter) if owner_filter else None
    scope_filter = (lambda row: access.path_in_scope(ctx.app.settings.data_dir.as_posix(), user, row["cur_path"])) if user else None
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
    pending = list(_groups(db, owner_filter, after, scope_filter))
    total = len(pending) + groups
    for g in pending:
        ctx.check()
        members = db.query("SELECT id, owner_id, cur_path, name, ext, media_kind, size, mtime_ns, meta_read, phase_meta FROM files WHERE size=? AND sha256=? AND present=1 "
                           "AND media_kind IN ('image','video') AND status<>'dup_moved'" + (" AND owner_id=?" if owner_filter else ""),
                           (g["size"], g["sha256"], owner_filter) if owner_filter else (g["size"], g["sha256"]))
        if scope_filter:
            members = [member for member in members if scope_filter(member)]
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
            ensure_capture_dates(ctx, confirmed)
            capture_date = group_capture_date(confirmed)["capture_date"]
            confirmed.sort(key=lambda f: keeper_order(f, capture_date))
            keeper = confirmed[0]
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


def set_keeper(db, group_id: int, file_id: int, user=None, data_dir=None) -> None:
    g = db.query_one("SELECT * FROM dup_groups WHERE id=?", (group_id,))
    if not g or g["status"] != "pending":
        raise ValueError("group_not_pending")
    f = db.query_one("SELECT id, owner_id FROM files WHERE id=? AND sha256=? AND size=?", (file_id, g["sha256"], g["size"]))
    if not f:
        raise ValueError("not_in_group")
    if user and not f["owner_id"] == user["id"]:
        raise ValueError("not_in_group")
    if user and data_dir:
        from . import access
        row = db.query_one("SELECT id, owner_id, cur_path FROM files WHERE id=?", (file_id,))
        if not access.file_in_scope(data_dir, user, row):
            raise ValueError("not_in_group")
    with db.transaction():
        db.execute("UPDATE dup_groups SET keeper_file_id=? WHERE id=?", (file_id, group_id))
        if g["scope"] != "global":
            db.execute("UPDATE files SET dup_of=?, status='duplicate' WHERE dup_group_id=? AND id<>?", (file_id, group_id, file_id))
            db.execute("UPDATE files SET dup_of=NULL, status='unique' WHERE id=?", (file_id,))


def group_members(db, g: dict, user=None, data_dir=None) -> list[dict]:
    sql = ("SELECT id, owner_id, cur_path, name, ext, media_kind, size, mtime_ns, chosen_date, meta_read, phase_meta, status FROM files WHERE size=? AND sha256=? AND present=1 "
           "AND media_kind IN ('image','video')")
    params: list = [g["size"], g["sha256"]]
    if g["scope"] != "global":
        sql += " AND owner_id=?"
        params.append(g["owner_id"])
    rows = db.query(sql, params)
    if user and data_dir:
        from . import access
        rows = [row for row in rows if access.file_in_scope(data_dir, user, row)]
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
