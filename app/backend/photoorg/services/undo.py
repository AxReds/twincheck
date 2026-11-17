"""Undo of moves and metadata changes, read from the manifest of the CURRENT
database.  Operations done with another database can only be undone by going
back to it (or after migrating its data)."""
from __future__ import annotations

import json
import os

from ..db.schema import utcnow
from ..exif import ExifError, ExifTool
from ..fsutil import sha256_file
from ..jobs import JobContext, register
from .moves import run_move_plan


@register("undo_moves_plan", auto_resume=True)
def undo_moves_plan(ctx: JobContext) -> dict:
    db = ctx.db
    db.execute("DELETE FROM plan_items WHERE plan_job_id=?", (ctx.id,))
    only_job = ctx.params.get("job_id")
    sql = ("SELECT * FROM manifest WHERE owner_id=? AND action IN ('move','dup_move') AND undone_at IS NULL"
           + (" AND job_id=?" if only_job else "") + " ORDER BY id DESC")
    rows = db.query(sql, (ctx.owner_id, only_job) if only_job else (ctx.owner_id,))
    # companions (no file_id or sidecars) of the same job follow their primary entry
    seq, blocked = 0, 0
    reserved: set[str] = set()
    for m in rows:
        ctx.check()
        note = None
        if not os.path.lexists(m["new_path"]):
            note = "source_missing"
        elif os.path.lexists(m["orig_path"]) or m["orig_path"] in reserved:
            note = "origin_occupied"
        reserved.add(m["orig_path"])
        seq += 1
        db.execute("INSERT INTO plan_items (plan_job_id, owner_id, kind, seq, file_id, src, dst, payload, status, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (ctx.id, ctx.owner_id, "undo_move", seq, m["file_id"], m["new_path"], m["orig_path"],
                    json.dumps({"manifest_id": m["id"], "companions": []}), "blocked" if note else "planned", note))
        blocked += 1 if note else 0
    return {"planned": seq - blocked, "blocked": blocked}


@register("undo_moves_apply", writes_files=True)
def undo_moves_apply(ctx: JobContext) -> dict:
    return run_move_plan(ctx)


@register("undo_meta_plan", auto_resume=True)
def undo_meta_plan(ctx: JobContext) -> dict:
    db = ctx.db
    db.execute("DELETE FROM plan_items WHERE plan_job_id=?", (ctx.id,))
    only_job = ctx.params.get("job_id")
    sql = ("SELECT m.id, m.file_id, f.cur_path FROM manifest m JOIN files f ON f.id=m.file_id WHERE m.owner_id=? AND m.action='metadata' "
           "AND m.undone_at IS NULL" + (" AND m.job_id=?" if only_job else "") + " ORDER BY m.id DESC")
    seq = 0
    for m in db.query(sql, (ctx.owner_id, only_job) if only_job else (ctx.owner_id,)):
        ctx.check()
        changes = db.query("SELECT tag, before_value, after_value FROM meta_changes WHERE manifest_id=? AND undone_at IS NULL", (m["id"],))
        if not changes:
            continue
        seq += 1
        db.execute("INSERT INTO plan_items (plan_job_id, owner_id, kind, seq, file_id, src, payload, status) VALUES (?,?,?,?,?,?,?,?)",
                   (ctx.id, ctx.owner_id, "meta_restore", seq, m["file_id"], m["cur_path"],
                    json.dumps({"manifest_id": m["id"], "restore": {c["tag"]: json.loads(c["before_value"]) for c in changes},
                                "expected": {c["tag"]: json.loads(c["after_value"]) for c in changes}}), "planned"))
    return {"planned": seq}


def restore_meta_item(ctx: JobContext, item: dict, et: ExifTool) -> str:
    db = ctx.db
    payload = json.loads(item["payload"])
    f = db.query_one("SELECT * FROM files WHERE id=?", (item["file_id"],))
    if not f or not os.path.exists(f["cur_path"]):
        return "file_missing"
    restore = payload["restore"]
    try:
        et.write(f["cur_path"], restore, create_only=False)
    except ExifError as exc:
        return exc.args[0]
    st = os.stat(f["cur_path"])
    sha = sha256_file(f["cur_path"])
    with db.transaction():
        db.execute("UPDATE meta_changes SET undone_at=? WHERE manifest_id=?", (utcnow(), payload["manifest_id"]))
        db.execute("UPDATE manifest SET undone_at=?, undo_job_id=? WHERE id=?", (utcnow(), ctx.id, payload["manifest_id"]))
        db.execute("UPDATE files SET sha256=?, size=?, mtime_ns=?, phase_meta='analysed', meta_after=NULL, updated_at=? WHERE id=?",
                   (sha, st.st_size, st.st_mtime_ns, utcnow(), f["id"]))
        ctx.log("META_RESTORED", path=f["cur_path"], tags=", ".join(sorted(restore)))
    return "done"


@register("undo_meta_apply", writes_files=True)
def undo_meta_apply(ctx: JobContext) -> dict:
    from .metadata import run_meta_plan
    return run_meta_plan(ctx)
