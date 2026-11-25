"""Planning and execution of file moves: organisation by date, duplicates to the
"Duplicates" folder, and undo.  Every write is preceded by a plan (dry-run)
stored in ``plan_items`` and confirmed by the user; execution re-validates each
item and never overwrites anything."""
from __future__ import annotations

import json
import os

from ..db.manager import log
from ..db.schema import utcnow
from ..fsutil import path_hash, sha256_file
from ..jobs import JobContext, register
from ..mover import MoveError, move_file
from . import access
from .companions import group_directory, renamed_companion, stem
from .dedupe import duplicates_folder_target, group_members


def _candidate_names(name: str, n: int) -> str:
    if n == 0:
        return name
    if "." in name:
        s, e = name.rsplit(".", 1)
        return f"{s}_{n}.{e}"
    return f"{name}_{n}"


def unique_names(dst_dir: str, name: str, companions: list[str], reserved: set[str]) -> tuple[str, dict[str, str]]:
    """Smallest numeric suffix so that the primary AND all companions are free."""
    n = 0
    while True:
        new = _candidate_names(name, n)
        mapping = {c: renamed_companion(c, name, new) for c in companions}
        paths = [os.path.join(dst_dir, new)] + [os.path.join(dst_dir, m) for m in mapping.values()]
        if all(not os.path.lexists(p) and p not in reserved for p in paths):
            reserved.update(paths)
            return new, mapping
        n += 1


def _insert_plan(db, plan_id, owner, kind, seq, file_id, src, dst, payload, status="planned", note=None):
    db.execute("INSERT INTO plan_items (plan_job_id, owner_id, kind, seq, file_id, src, dst, payload, status, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (plan_id, owner, kind, seq, file_id, src, dst, json.dumps(payload, ensure_ascii=False), status, note))


def _owner_area(ctx: JobContext) -> dict:
    p = ctx.params
    return {"id": p["source_id"], "path": p["root"], "dest": p["dest"]}


@register("organize_plan", auto_resume=True)
def organize_plan(ctx: JobContext) -> dict:
    db = ctx.db
    area = _owner_area(ctx)
    org = ctx.app.settings_section("organize")
    no_date = org.get("no_date_folder", "No_date")
    dup_folder = org.get("duplicates_folder", "Duplicates")
    db.execute("DELETE FROM plan_items WHERE plan_job_id=?", (ctx.id,))
    reserved: set[str] = set()
    cache: dict[str, tuple] = {}
    stats = {"planned": 0, "already": 0, "not_analysed": 0, "no_date": 0, "skipped_duplicates": 0, "needs_recheck": 0}
    seq = 0
    last = 0
    dup_root = os.path.join(area["dest"], dup_folder)
    while True:
        ctx.check()
        rows = db.query("SELECT * FROM files WHERE owner_id=? AND source_id=? AND present=1 AND media_kind IN ('image','video') "
                        "AND id>? ORDER BY id LIMIT 500", (ctx.owner_id, area["id"], last))
        if not rows:
            break
        for f in rows:
            last = f["id"]
            user = ctx.app.user(ctx.owner_id)
            if not access.file_in_scope(ctx.app.settings.data_dir.as_posix(), user, f):
                continue
            if db.query_one("SELECT id FROM review_items WHERE file_id=? AND recheck_reason IS NOT NULL", (f["id"],)):
                seq += 1
                _insert_plan(db, ctx.id, ctx.owner_id, "organize", seq, f["id"], f["cur_path"], None, {},
                             status="skipped", note="needs_recheck")
                stats["needs_recheck"] += 1
                continue
            if f["status"] in ("dup_moved", "duplicate") or f["cur_path"].startswith(dup_root + "/"):
                stats["skipped_duplicates"] += 1
                continue
            if f["phase_meta"] not in ("analysed", "done"):
                stats["not_analysed"] += 1
                continue
            d = os.path.dirname(f["cur_path"])
            if d not in cache:
                if len(cache) > 2000:
                    cache.clear()
                try:
                    cache[d] = group_directory(os.listdir(d))
                except OSError:
                    cache[d] = ({}, set())
            groups, taken = cache[d]
            if f["name"] in taken:
                continue                                  # moves with its primary
            date = f["chosen_date"]
            if date:
                target = os.path.join(area["dest"], date[0:4], date[5:7], date[8:10])
            else:
                target = os.path.join(area["dest"], no_date)
                stats["no_date"] += 1
            if d == target:
                stats["already"] += 1
                continue
            comps = groups.get(f["name"], [])
            new, mapping = unique_names(target, f["name"], comps, reserved)
            comp_rows = []
            for c in comps:
                cp = os.path.join(d, c)
                cf = db.query_one("SELECT id FROM files WHERE owner_id=? AND cur_path_hash=?", (ctx.owner_id, path_hash(cp)))
                comp_rows.append({"file_id": cf["id"] if cf else None, "src": cp, "dst": os.path.join(target, mapping[c])})
            seq += 1
            _insert_plan(db, ctx.id, ctx.owner_id, "organize", seq, f["id"], f["cur_path"], os.path.join(target, new),
                         {"companions": comp_rows, "date": date, "renamed": new != f["name"]})
            stats["planned"] += 1
        ctx.progress(done=stats["planned"], total=0, phase="plan")
    return stats


@register("dedupe_plan", auto_resume=True)
def dedupe_plan(ctx: JobContext) -> dict:
    """Plan moving non-keepers to <destination>/Duplicates/<relative path>."""
    db = ctx.db
    scope = ctx.params.get("scope", f"user:{ctx.owner_id}")
    folder = ctx.app.settings_section("organize").get("duplicates_folder", "Duplicates")
    db.execute("DELETE FROM plan_items WHERE plan_job_id=?", (ctx.id,))
    reserved: set[str] = set()
    seq, recoverable = 0, 0
    users = ctx.app.vault.snapshot()["users"]
    actor = users.get(ctx.owner_id)
    for g in db.query("SELECT * FROM dup_groups WHERE scope=? AND status='pending' ORDER BY id", (scope,)):
        ctx.check()
        for m in group_members(db, g, actor, ctx.app.settings.data_dir.as_posix()):
            if m["id"] == g["keeper_file_id"] or m["status"] == "dup_moved":
                continue
            owner = users.get(m["owner_id"])
            area = None
            if owner:
                for a in access.areas(ctx.app.settings.data_dir.as_posix(), owner):
                    if m["cur_path"].startswith(a["path"].rstrip("/") + "/") or m["cur_path"].startswith(a["dest"].rstrip("/") + "/"):
                        area = a
                        break
            if area is None and not access.is_admin_role(actor):
                continue
            if area is None:
                root = ctx.params.get("root") or os.path.dirname(m["cur_path"])
                area = {"path": root, "dest": ctx.params.get("dest") or root}
            if f"/{folder}/" in m["cur_path"]:
                continue
            target = duplicates_folder_target(area, m["cur_path"], folder)
            d = os.path.dirname(m["cur_path"])
            try:
                groups, _ = group_directory(os.listdir(d))
            except OSError:
                groups = {}
            name = os.path.basename(m["cur_path"])
            comps = groups.get(name, [])
            new, mapping = unique_names(os.path.dirname(target), name, comps, reserved)
            comp_rows = [{"file_id": None, "src": os.path.join(d, c), "dst": os.path.join(os.path.dirname(target), mapping[c])} for c in comps]
            for cr in comp_rows:
                cf = db.query_one("SELECT id FROM files WHERE owner_id=? AND cur_path_hash=?", (m["owner_id"], path_hash(cr["src"])))
                cr["file_id"] = cf["id"] if cf else None
            seq += 1
            recoverable += g["size"]
            _insert_plan(db, ctx.id, m["owner_id"], "dup_move", seq, m["id"], m["cur_path"], os.path.join(os.path.dirname(target), new),
                         {"group_id": g["id"], "keeper_id": g["keeper_file_id"], "companions": comp_rows})
        ctx.progress(done=seq, total=0, phase="plan")
    return {"planned": seq, "recoverable_bytes": recoverable}


def _validate(ctx: JobContext, owner_id: str, entries: list[dict]) -> str | None:
    db = ctx.db
    user = ctx.app.user(owner_id)
    for e in entries:
        if not os.path.lexists(e["src"]):
            return "source_missing"
        if os.path.islink(e["src"]) or not os.path.isfile(e["src"]):
            return "source_not_regular"
        if not access.file_in_scope(ctx.app.settings.data_dir.as_posix(), user, {"owner_id": owner_id, "cur_path": e["src"]}):
            return "outside_source"
        if os.path.lexists(e["dst"]):
            return "destination_exists"
        if e.get("file_id"):
            f = db.query_one("SELECT size, sha256, owner_id, cur_path FROM files WHERE id=?", (e["file_id"],))
            if not f or f["owner_id"] != owner_id or not access.file_in_scope(ctx.app.settings.data_dir.as_posix(), user, f):
                return "outside_source"
            if f["cur_path"] != e["src"] or os.path.getsize(e["src"]) != f["size"] or sha256_file(e["src"]) != f["sha256"]:
                return "file_changed"
    return None


def run_move_plan(ctx: JobContext) -> dict:
    db = ctx.db
    plan_id = ctx.params["plan_job_id"]
    owner_filter = None if ctx.params.get("all_owners") else ctx.owner_id
    q = "SELECT * FROM plan_items WHERE plan_job_id=? AND status='planned'" + (" AND owner_id=?" if owner_filter else "") + " ORDER BY seq"
    args = (plan_id, owner_filter) if owner_filter else (plan_id,)
    total = int(db.scalar("SELECT COUNT(*) AS n FROM plan_items WHERE plan_job_id=?", (plan_id,)) or 0)
    done = total - len(db.query(q, args))
    counts = {"moved": 0, "skipped": 0, "cross_volume": 0}
    for item in db.query(q, args):
        ctx.check()
        payload = json.loads(item["payload"] or "{}")
        entries = [{"file_id": item["file_id"], "src": item["src"], "dst": item["dst"]}] + payload.get("companions", [])
        recheck = item["kind"] == "organize" and any(db.query_one(
            "SELECT id FROM review_items WHERE file_id=? AND recheck_reason IS NOT NULL", (entry["file_id"],))
            for entry in entries if entry.get("file_id"))
        problem = "needs_recheck" if recheck else _validate(ctx, item["owner_id"], entries)
        if problem:
            with db.transaction():
                db.execute("UPDATE plan_items SET status='skipped', note=? WHERE id=?", (problem, item["id"]))
                log(db, "MOVE_SKIPPED", level="WARNING", job_id=ctx.id, owner_id=item["owner_id"], src=item["src"], reason=problem)
                db.execute("INSERT INTO review_items (owner_id, file_id, kind, details, proposals, status, created_at, job_id, applied) "
                           "VALUES (?,?,?,?,?,?,?,?,?)", (item["owner_id"], item["file_id"], "move_conflict",
                                                          json.dumps({"src": item["src"], "dst": item["dst"], "reason": problem}), "[]",
                                                          "pending", utcnow(), ctx.id, 0))
            counts["skipped"] += 1
            done += 1
            ctx.progress(done=done, total=total, phase="move")
            continue
        action = {"organize": "move", "dup_move": "dup_move", "undo_move": "restore"}[item["kind"]]
        failed = None
        for idx, e in enumerate(entries):
            f = db.query_one("SELECT * FROM files WHERE id=?", (e["file_id"],)) if e.get("file_id") else None
            with db.transaction():
                mid = db.insert("INSERT INTO manifest (job_id, owner_id, file_id, action, orig_path, new_path, sha256_before, sha256_after, "
                                "meta_before, meta_after, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (ctx.id, item["owner_id"], e.get("file_id"), action, e["src"], e["dst"], f["sha256"] if f else None,
                                 f["sha256"] if f else None, f["meta_before"] if f else None, f["meta_after"] if f else None, utcnow()))

            def on_done(e=e, idx=idx, mid=mid):
                if e.get("file_id"):
                    status_sql = {"organize": "status=CASE WHEN status IN ('indexed','unique','normalized','review') THEN 'moved' ELSE status END, phase_org='done'",
                                  "dup_move": "status='dup_moved'",
                                  "undo_move": "status=CASE WHEN status='dup_moved' THEN 'duplicate' ELSE 'unique' END, phase_org='pending'"}[item["kind"]]
                    db.execute(f"UPDATE files SET cur_path=?, cur_path_hash=?, name=?, {status_sql}, updated_at=? WHERE id=?",
                               (e["dst"], path_hash(e["dst"]), os.path.basename(e["dst"]), utcnow(), e["file_id"]))
                if item["kind"] == "undo_move" and idx == 0 and payload.get("manifest_id"):
                    db.execute("UPDATE manifest SET undone_at=?, undo_job_id=? WHERE id=?", (utcnow(), ctx.id, payload["manifest_id"]))
                if item["kind"] == "undo_move" and idx > 0 and e.get("manifest_id"):
                    db.execute("UPDATE manifest SET undone_at=?, undo_job_id=? WHERE id=?", (utcnow(), ctx.id, e["manifest_id"]))
            try:
                res = move_file(db, job_id=ctx.id, owner_id=item["owner_id"], file_id=e.get("file_id"), src=e["src"], dst=e["dst"],
                                sha256=f["sha256"] if f else None, on_done=on_done)
                counts["cross_volume"] += 1 if res["cross_volume"] else 0
            except MoveError as exc:
                db.execute("UPDATE manifest SET undone_at=?, meta_after=? WHERE id=?", (utcnow(), json.dumps({"error": exc.code}), mid))
                failed = exc.code
                break
        with db.transaction():
            db.execute("UPDATE plan_items SET status=?, note=? WHERE id=?", ("done" if not failed else "failed", failed, item["id"]))
            if failed:
                log(db, "MOVE_SKIPPED", level="WARNING", job_id=ctx.id, owner_id=item["owner_id"], src=item["src"], reason=failed)
        counts["moved" if not failed else "skipped"] += 1
        done += 1
        ctx.progress(done=done, total=total, phase="move")
    return counts


@register("organize_apply", writes_files=True)
def organize_apply(ctx: JobContext) -> dict:
    return run_move_plan(ctx)


@register("dedupe_apply", writes_files=True)
def dedupe_apply(ctx: JobContext) -> dict:
    res = run_move_plan(ctx)
    db = ctx.db
    plan_id = ctx.params["plan_job_id"]
    for r in db.query("SELECT DISTINCT payload FROM plan_items WHERE plan_job_id=? AND status='done'", (plan_id,)):
        gid = json.loads(r["payload"]).get("group_id")
        if gid:
            db.execute("UPDATE dup_groups SET status='applied' WHERE id=?", (gid,))
    return res


def stem_of(name: str) -> str:
    return stem(name)
