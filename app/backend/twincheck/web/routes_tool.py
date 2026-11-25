"""Tool endpoints (every role, on the caller's own sources)."""
from __future__ import annotations

import csv
import io
import json
import os
import time

from ..db.schema import utcnow
from ..fsutil import PathError, media_kind, is_within
from ..previews import ensure as ensure_preview
from ..services import access, review_state
from ..services.dedupe import group_capture_date, group_members, keeper_decision, set_keeper
from ..services.metadata import FIELDS, WRITABLE, date_candidate, field_table, mtime_proposal, parse_dt
from ..services.scan import iter_dirs
from . import common
from .app import router
from .common import disp, fail, is_admin, need_db, own_row, owner_filter, paging, require, scoped_file, scoped_rows
from .framework import JSONResponse, Request, StreamResponse, file_response

APPLY_KIND = {"organize_plan": "organize_apply", "dedupe_plan": "dedupe_apply", "metadata_analyze": "metadata_apply",
              "analyze_library": "metadata_apply",
              "review_plan": "metadata_apply", "undo_moves_plan": "undo_moves_apply", "undo_meta_plan": "undo_meta_apply"}


def utcnow_from_mtime(mtime_ns) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((mtime_ns or 0) / 1e9)) + "Z"


def _submit(req: Request, kind: str, params: dict, owner: str | None = None) -> JSONResponse:
    core = common.app()
    need_db()
    if core.maintenance:
        fail(503, "maintenance", reason=core.maintenance)
    try:
        jid, existing = core.jobs.submit_with_status(kind, owner or req.user["id"], params)
    except RuntimeError:
        fail(503, "maintenance")
    return JSONResponse({"job_id": jid, "existing": existing}, 200 if existing else 202)


def _area(req: Request, body: dict) -> dict:
    core = common.app()
    data_dir = core.settings.data_dir.as_posix()
    try:
        if body.get("path") is not None and is_admin(req):
            a = access.adhoc_source(data_dir, body["path"])
            access.check_path_allowed(data_dir, req.user, a["path"])
            return a
        return access.find_area(data_dir, req.user, body.get("source_id") or "")
    except PathError as exc:
        fail(400 if exc.code != "source_not_assigned" else 403, exc.code)


def _area_params(a: dict) -> dict:
    return {"source_id": a["id"], "root": a["path"], "dest": a["dest"], "roots": sorted({a["path"], a["dest"]})}


# --- sources & browsing ------------------------------------------------------------------
@router.get("/api/sources")
def sources(req: Request):
    core = common.app()
    data_dir = core.settings.data_dir.as_posix()
    out = [{"id": a["id"], "label": a["label"], "path": disp(req, a["path"]), "path_rel": a["path_rel"], "dest": disp(req, a["dest"]),
            "same_as_source": a["path"] == a["dest"]} for a in access.areas(data_dir, req.user)]
    return JSONResponse({"sources": out, "can_browse": is_admin(req) or bool(out), "scan_selection": _scan_selection(req)})


def _scan_selection(req: Request) -> dict | None:
    core = common.app()
    data_dir = core.settings.data_dir.as_posix()
    assigned = access.areas(data_dir, req.user)
    latest = core.db.query_one("SELECT params FROM jobs WHERE owner_id=? AND kind IN ('scan','analyze_library') ORDER BY id DESC LIMIT 1",
                              (req.user["id"],)) if core.db_ready and core.db else None
    candidates = []
    if latest:
        root = json.loads(latest["params"]).get("root")
        if root:
            candidates.append(root)
    if assigned:
        candidates.append(assigned[0]["path"])
    for path in candidates:
        try:
            real = access.check_path_allowed(data_dir, req.user, path)
        except PathError:
            continue                 # inaccessible history is not a usable default
        if not os.path.isdir(real) or not os.access(real, os.R_OK | os.X_OK):
            continue
        if is_admin(req):
            area = next((a for a in assigned if os.path.realpath(a["path"]) == real), None)
        else:
            try:
                area, real = access.source_for_path(data_dir, req.user, real)
            except PathError:
                continue
        return {"path": os.path.relpath(real, os.path.realpath(data_dir)) if real != os.path.realpath(data_dir) else "",
                "source_id": area["id"] if area else None}
    return None


@router.get("/api/fs/browse")
def browse(req: Request):
    """Directories under /data for admins, or within an assigned source for users."""
    core = common.app()
    data_dir = core.settings.data_dir.as_posix()
    boundary = os.path.realpath(data_dir)
    try:
        if is_admin(req):
            real = access.check_path_allowed(data_dir, req.user, req.query.get("path", ""))
        else:
            area, real = access.source_for_path(data_dir, req.user, req.query.get("path", ""))
            boundary = os.path.realpath(area["path"])
    except PathError as exc:
        fail(403 if not is_admin(req) and exc.code in ("outside_source", "source_not_assigned") else 400, exc.code)
    root = os.path.realpath(data_dir)
    dirs = []
    try:
        for e in sorted(os.scandir(real), key=lambda e: e.name):
            if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                dirs.append(e.name)
    except OSError:
        fail(400, "not_readable")
    return JSONResponse({"path": os.path.relpath(real, root) if real != root else "", "dirs": dirs[:2000],
                         "root": os.path.relpath(boundary, root) if boundary != root else "", "display_path": disp(req, real)})


# --- jobs -------------------------------------------------------------------------------
def _job_public(req: Request, j: dict) -> dict:
    out = {k: j[k] for k in ("id", "kind", "owner_id", "status", "progress_done", "progress_total", "phase", "message", "error",
                             "created_at", "started_at", "updated_at", "finished_at", "parent_job_id")}
    params = json.loads(j["params"] or "{}")
    out["params"] = {k: (disp(req, v) if k in ("root", "dest", "archive") and isinstance(v, str) else v)
                     for k, v in params.items() if k not in ("roots", "token")}
    out["result"] = json.loads(j["result"]) if j.get("result") else None
    u = common.app().user(j["owner_id"]) if j["owner_id"] else None
    out["owner"] = u["username"] if u else None
    out["apply_kind"] = APPLY_KIND.get(j["kind"])
    return out


@router.get("/api/jobs")
def jobs(req: Request):
    db = need_db()
    off, lim = paging(req)
    where, args = ["1=1"], []
    owner = owner_filter(req)
    if owner:
        where.append("owner_id=?")
        args.append(owner)
    st = req.query.get("status")
    if st == "open":
        where.append("status IN ('queued','running','paused','interrupted')")
    elif st:
        where.append("status=?")
        args.append(st)
    if req.query.get("kind"):
        where.append("kind=?")
        args.append(req.query["kind"])
    w = " AND ".join(where)
    rows = db.query(f"SELECT * FROM jobs WHERE {w} ORDER BY id DESC LIMIT {lim} OFFSET {off}", args)
    total = db.scalar(f"SELECT COUNT(*) AS n FROM jobs WHERE {w}", args)
    return JSONResponse({"items": [_job_public(req, j) for j in rows], "total": int(total or 0)})


@router.get("/api/jobs/{id:int}")
def job(req: Request):
    db = need_db()
    j = own_row(req, db.query_one("SELECT * FROM jobs WHERE id=?", (req.path_params["id"],)))
    return JSONResponse(_job_public(req, j))


@router.post("/api/jobs/{id:int}/{action}", maintenance_ok=True)
def job_action(req: Request):
    core = common.app()
    db = need_db()
    j = own_row(req, db.query_one("SELECT * FROM jobs WHERE id=?", (req.path_params["id"],)))
    action = req.path_params["action"]
    if j["kind"] in ("export", "import", "migrate") and req.user["role"] != "admin":
        fail(403, "forbidden")
    if action == "pause":
        core.jobs.pause(j["id"])
    elif action == "cancel":
        core.jobs.cancel(j["id"])
    elif action == "resume":
        if core.maintenance:
            fail(503, "maintenance")
        if j["kind"] in ("export", "import", "migrate"):
            fail(400, "use_transfer_resume")
        try:
            core.jobs.resume(j["id"])
        except RuntimeError:
            fail(503, "maintenance")
    else:
        fail(404, "not_found")
    return JSONResponse({"ok": True})


# --- scan / duplicates / metadata / organise / undo / verify ---------------------------------
@router.post("/api/scan")
def start_scan(req: Request):
    body = req.json()
    a = _area(req, body)
    partial = False
    if not is_admin(req) and body.get("path") is not None:
        try:
            _, selected = access.source_for_path(common.app().settings.data_dir.as_posix(), req.user, body["path"], a["id"])
        except PathError as exc:
            fail(403 if exc.code in ("outside_source", "source_not_assigned") else 400, exc.code)
        partial = selected != os.path.realpath(a["path"])
        a = {**a, "path": selected}
    if not os.path.isdir(a["path"]):
        fail(400, "not_found")
    params = _area_params(a)
    if partial:
        params.update(roots=[a["path"]], partial_scan=True)
    return _submit(req, "scan", params)


@router.post("/api/library/analyze")
def analyze_library(req: Request):
    body = req.json()
    a = _area(req, body)
    partial = False
    if not is_admin(req) and body.get("path") is not None:
        try:
            _, selected = access.source_for_path(
                common.app().settings.data_dir.as_posix(), req.user, body["path"], a["id"])
        except PathError as exc:
            fail(403 if exc.code in ("outside_source", "source_not_assigned") else 400, exc.code)
        partial = selected != os.path.realpath(a["path"])
        a = {**a, "path": selected}
    if not os.path.isdir(a["path"]):
        fail(400, "not_found")
    params = _area_params(a)
    if partial:
        params.update(roots=[a["path"]], partial_scan=True)
    return _submit(req, "analyze_library", params)


@router.get("/api/library/status")
def library_status(req: Request):
    db = need_db()
    owner = req.user["id"]
    source_id = req.query.get("source_id")
    latest = _latest_analysis(req, source_id)
    core = common.app()
    active = None
    for job in db.query("SELECT id, status, phase, progress_done, progress_total, checkpoint, params, created_at "
                        "FROM jobs WHERE owner_id=? AND kind='analyze_library' "
                        "AND status IN ('queued','running','paused','interrupted') ORDER BY created_at DESC, id DESC", (owner,)):
        params = json.loads(job["params"] or "{}")
        if source_id and params.get("source_id") != source_id:
            continue
        if not params.get("root") or not access.path_in_scope(core.settings.data_dir.as_posix(), req.user, params["root"]):
            continue
        if latest and (job["created_at"], job["id"]) <= (latest["created_at"], latest["id"]):
            continue
        active = job
        break
    changes = {"new": 0, "changed": 0, "removed": 0}
    if latest:
        params = json.loads(latest["params"] or "{}")
        roots = params.get("roots") or [params.get("root")]
        roots = [root for root in roots if root and access.path_in_scope(core.settings.data_dir.as_posix(), req.user, root)]
        rows = db.query("SELECT * FROM files WHERE owner_id=?", (owner,))
        rows = [row for row in rows if any(is_within(row["cur_path"], root) for root in roots)
                and access.file_in_scope(core.settings.data_dir.as_posix(), req.user, row)]
        indexed = {row["cur_path"]: row for row in rows}
        patterns = core.settings_section("scan").get("exclude", [])
        seen = set()
        for root in roots:
            for _, _, entries in iter_dirs(root, patterns):
                for entry in entries:
                    if media_kind(entry.name) == "other" or entry.path in seen:
                        continue
                    seen.add(entry.path)
                    row = indexed.get(entry.path)
                    if not row or not row["present"]:
                        changes["new"] += 1
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue  # read warnings alone do not invalidate a completed analysis
                    if ((st.st_size, st.st_mtime_ns) != (row["size"], row["mtime_ns"])
                            or row["inode"] is not None and (st.st_ino, st.st_dev) != (row["inode"], row["device"])
                            or row["file_changed_at"] and row["file_changed_at"] > latest["finished_at"]):
                        changes["changed"] += 1
        for row in rows:
            if row["cur_path"] in seen:
                continue
            if row["present"] and not os.path.lexists(row["cur_path"]) or not row["present"] and (
                    row["file_changed_at"] and row["file_changed_at"] > latest["finished_at"]):
                changes["removed"] += 1
    incomplete = bool(active and active["status"] in ("paused", "interrupted"))
    stale = bool(any(changes.values()) or incomplete)
    return JSONResponse({"exists": bool(latest), "empty": not bool(latest),
                         "stale": stale, "changes": changes, "incomplete": incomplete,
                         "analyzed_at": latest["finished_at"] if latest else None,
                         "active_job": ({k: active[k] for k in ("id", "status", "phase", "progress_done", "progress_total")}
                                        if active else None)})


def _latest_analysis(req: Request, source_id: str | None = None) -> dict | None:
    core = common.app()
    for row in need_db().query("SELECT * FROM jobs WHERE owner_id=? AND kind='analyze_library' AND status='completed' "
                              "ORDER BY id DESC", (req.user["id"],)):
        params = json.loads(row["params"] or "{}")
        if source_id and params.get("source_id") != source_id:
            continue
        root = params.get("root")
        if root and access.path_in_scope(core.settings.data_dir.as_posix(), req.user, root):
            return row
    return None


@router.post("/api/duplicates/analyze")
def dup_analyze(req: Request):
    return _submit(req, "dedupe_analyze", {})


@router.post("/api/duplicates/plan")
def dup_plan(req: Request):
    return _submit(req, "dedupe_plan", {"scope": f"user:{req.user['id']}"})


@router.get("/api/duplicates")
def duplicates(req: Request):
    db = need_db()
    off, lim = paging(req, 20, 200)
    scope = req.query.get("scope", "user")
    if scope == "global":
        require(req, "global_dedupe")
        sc = "global"
    else:
        sc = f"user:{req.user['id']}"
    latest = _latest_analysis(req, req.query.get("source_id"))
    candidates = db.query("SELECT * FROM dup_groups WHERE scope=? AND status='pending'" +
                          (" AND job_id=?" if latest and sc != "global" else "") + " ORDER BY size DESC, id",
                          (sc, latest["id"]) if latest and sc != "global" else (sc,))
    visible = []
    data_dir = common.app().settings.data_dir.as_posix()
    for g in candidates:
        members = group_members(db, g, None if is_admin(req) else req.user, data_dir if not is_admin(req) else None)
        if len(members) < 2:
            continue
        if not any(m["id"] == g["keeper_file_id"] for m in members):
            continue
        decision = keeper_decision(members, g["keeper_file_id"])
        member_rows = []
        for m in members:
            member_date = group_capture_date([m])
            member_rows.append({"id": m["id"], "path": disp(req, m["cur_path"]),
                                "exif_date": member_date["capture_date"],
                                "exif_offset": member_date.get("capture_offset"),
                                "filesystem_date": utcnow_from_mtime(m["mtime_ns"]),
                                "date": decision["capture_date"],
                                "owner": (common.app().user(m["owner_id"]) or {}).get("username") if is_admin(req) else None})
        visible.append({"id": g["id"], "size": g["size"], "sha256": g["sha256"], "keeper_file_id": g["keeper_file_id"],
                        **decision, "recoverable": g["size"] * (len(members) - 1),
                        "members": member_rows})
    groups = visible[off:off + lim]
    recoverable = sum(group["recoverable"] for group in visible)
    return JSONResponse({"groups": groups, "total": len(visible), "recoverable": recoverable,
                         "analyzed_at": latest["finished_at"] if latest else None})


@router.post("/api/duplicates/{gid:int}/keeper")
def dup_keeper(req: Request):
    db = need_db()
    g = db.query_one("SELECT * FROM dup_groups WHERE id=?", (req.path_params["gid"],))
    if not g or (g["scope"] != f"user:{req.user['id']}" and not (g["scope"] == "global" and req.user["role"] == "admin")):
        fail(404, "not_found")
    try:
        set_keeper(db, g["id"], int(req.json().get("file_id") or 0),
                   None if is_admin(req) else req.user,
                   common.app().settings.data_dir.as_posix() if not is_admin(req) else None)
    except ValueError as exc:
        fail(404 if str(exc) == "not_in_group" else 400, str(exc))
    return JSONResponse({"ok": True})


@router.get("/api/duplicates.csv")
def duplicates_csv(req: Request):
    db = need_db()
    sc = "global" if req.query.get("scope") == "global" and req.user["role"] == "admin" else f"user:{req.user['id']}"

    def factory():
        def gen():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["group_id", "sha256", "size", "role", "file_id", "path", "exif_date", "filesystem_date"])
            last = 0
            while True:
                rows = db.query("SELECT * FROM dup_groups WHERE scope=? AND status='pending' AND id>? ORDER BY id LIMIT 200", (sc, last))
                if not rows:
                    break
                for g in rows:
                    last = g["id"]
                    members = group_members(db, g, None if is_admin(req) else req.user,
                                            None if is_admin(req) else common.app().settings.data_dir.as_posix())
                    if len(members) < 2:
                        continue
                    for m in members:
                        if disp(req, m["cur_path"]) is None:
                            continue
                        capture = group_capture_date([m])
                        exif_date = capture["capture_date"] or ""
                        if exif_date and capture.get("capture_offset"):
                            exif_date += capture["capture_offset"]
                        w.writerow([g["id"], g["sha256"], g["size"], "keep" if m["id"] == g["keeper_file_id"] else "duplicate",
                                    m["id"], disp(req, m["cur_path"]), exif_date,
                                    utcnow_from_mtime(m["mtime_ns"])])
                yield buf.getvalue().encode("utf-8")
                buf.seek(0)
                buf.truncate()
            db.close_thread()
        return gen()
    return StreamResponse(factory, content_type="text/csv; charset=utf-8",
                          headers={"content-disposition": 'attachment; filename="duplicates.csv"'})


@router.post("/api/metadata/analyze")
def meta_analyze(req: Request):
    body = req.json()
    params = {}
    if body.get("source_id"):
        params["source_id"] = _area(req, body)["id"]
    return _submit(req, "metadata_analyze", params)


@router.get("/api/metadata/results")
def meta_results(req: Request):
    db = need_db()
    latest = _latest_analysis(req, req.query.get("source_id"))
    summary = None
    if latest:
        result = json.loads(latest["result"] or "{}")
        if latest and isinstance(result.get("metadata"), dict):
            result = result["metadata"]
        summary = {"analysed": int(result.get("analysed") or 0),
                   "fills": int(result.get("fills") or 0),
                   "review": int(result.get("review") or 0)}
        fills = db.query("SELECT p.*, f.cur_path FROM plan_items p JOIN files f ON f.id=p.file_id "
                         "WHERE p.plan_job_id=? AND p.kind='meta_fill' AND p.status='planned' AND p.owner_id=?", (latest["id"], req.user["id"]))
        summary["fills"] = sum(1 for row in fills if access.file_in_scope(common.app().settings.data_dir.as_posix(), req.user, row))
    review_rows = _visible_reviews(req)
    counts, totals = _review_counts(review_rows)
    write_pending = sum(1 for row in review_rows if row["status"] == "saved" and not row["applied"]
                        and json.loads(row["decision"] or "{}").get("fields"))
    if summary:
        summary["review"] = totals["total"]
    return JSONResponse({"analyzed_at": latest["finished_at"] if latest else None, "summary": summary,
                         "plan_job_id": latest["id"] if latest else None,
                         "review_file_count": len({row["file_id"] for row in review_rows}),
                         "review_pending": totals["pending"], "review_counts": totals, "review_write_pending": write_pending})


@router.post("/api/organize/plan")
def organize_plan(req: Request):
    a = _area(req, req.json())
    return _submit(req, "organize_plan", _area_params(a))


@router.post("/api/undo/{what}/plan")
def undo_plan(req: Request):
    what = req.path_params["what"]
    if what not in ("moves", "metadata"):
        fail(404, "not_found")
    body = req.json()
    params = {"job_id": int(body["job_id"])} if body.get("job_id") else {}
    return _submit(req, "undo_moves_plan" if what == "moves" else "undo_meta_plan", params)


@router.post("/api/verify")
def start_verify(req: Request):
    return _submit(req, "verify", {})


@router.post("/api/review/plan")
def review_plan(req: Request):
    return _submit(req, "review_plan", {})


# --- plans (dry-run) and confirmation ------------------------------------------------------
@router.get("/api/plans/{id:int}")
def plan(req: Request):
    db = need_db()
    j = own_row(req, db.query_one("SELECT * FROM jobs WHERE id=?", (req.path_params["id"],)))
    off, lim = paging(req, 100, 500)
    owner = None if is_admin(req) else req.user["id"]
    where, args = "plan_job_id=?", [j["id"]]
    if owner:
        where += " AND owner_id=?"
        args.append(owner)
    if req.query.get("status"):
        where += " AND status=?"
        args.append(req.query["status"])
    items = db.query(f"SELECT * FROM plan_items WHERE {where} ORDER BY seq", args)
    if not is_admin(req):
        scoped_ids = {row["id"] for row in scoped_rows(req, db.query(
            "SELECT f.* FROM files f JOIN plan_items p ON p.file_id=f.id WHERE p.plan_job_id=?", (j["id"],)))}
        items = [item for item in items if item["file_id"] is None or item["file_id"] in scoped_ids]
    summary = []
    for it in items:
        key = (it["kind"], it["status"])
        found = next((s for s in summary if (s["kind"], s["status"]) == key), None)
        if found:
            found["n"] += 1
        else:
            summary.append({"kind": it["kind"], "status": it["status"], "n": 1})
    items = items[off:off + lim]
    out = []
    for it in items:
        p = json.loads(it["payload"] or "{}")
        comps = [{"src": disp(req, c["src"]), "dst": disp(req, c["dst"])} for c in p.get("companions", [])]
        fills = {k: {"value": v.get("value"), "from": v.get("from"), "source": v.get("source"), "tags": list(v.get("tags", {}))}
                 for k, v in (p.get("fills") or {}).items()}
        restore = list((p.get("restore") or {}).keys())
        out.append({"id": it["id"], "kind": it["kind"], "file_id": it["file_id"], "src": disp(req, it["src"]),
                    "dst": disp(req, it["dst"]), "status": it["status"], "note": it["note"], "companions": comps,
                    "fills": fills, "restore": restore, "renamed": p.get("renamed", False), "date": p.get("date")})
    applies = db.query("SELECT id, status FROM jobs WHERE parent_job_id=? ORDER BY id DESC", (j["id"],))
    return JSONResponse({"job": _job_public(req, j), "items": out, "summary": summary, "apply_jobs": applies,
                         "review_counts": _review_counts(_visible_reviews(req))[1],
                         "can_confirm": j["status"] == "completed" and j["kind"] in APPLY_KIND})


@router.post("/api/plans/{id:int}/confirm")
def plan_confirm(req: Request):
    db = need_db()
    j = own_row(req, db.query_one("SELECT * FROM jobs WHERE id=?", (req.path_params["id"],)))
    if j["kind"] not in APPLY_KIND or j["status"] != "completed":
        fail(409, "plan_not_ready")
    if not req.json().get("confirm"):
        fail(400, "confirmation_required")
    params = json.loads(j["params"] or "{}")
    if params.get("scope") == "global":
        require(req, "global_dedupe")
    running = db.query_one("SELECT id FROM jobs WHERE parent_job_id=? AND status IN ('queued','running')", (j["id"],))
    if running:
        fail(409, "already_running", job_id=running["id"])
    core = common.app()
    if core.maintenance:
        fail(503, "maintenance")
    jid, existing = core.jobs.submit_with_status(
        APPLY_KIND[j["kind"]], j["owner_id"],
        {"plan_job_id": j["id"], "all_owners": params.get("scope") == "global"}, parent_job_id=j["id"])
    return JSONResponse({"job_id": jid, "existing": existing}, 200 if existing else 202)


# --- review -----------------------------------------------------------------------------
def _review_row(req: Request, r: dict, full: bool = False) -> dict:
    db = common.app().db
    f = db.query_one("SELECT * FROM files WHERE id=?", (r["file_id"],)) if r["file_id"] else None
    if not is_admin(req) and f and not access.file_in_scope(common.app().settings.data_dir.as_posix(), req.user, f):
        fail(404, "not_found")
    out = {"id": r["id"], "kind": r["kind"], "status": review_state.status(r), "created_at": r["created_at"],
           "skipped_at": r.get("skipped_at"),
           "details": json.loads(r["details"] or "{}"), "proposals": json.loads(r["proposals"] or "[]"),
           "decision": json.loads(r["decision"]) if r["decision"] else None, "file_id": r["file_id"]}
    out.update(decision_source=r["decision_source"], decision_timezone=r["decision_timezone"],
               recheck_reason=r["recheck_reason"], library_timezone=common.app().library_timezone,
               applied=bool(r["applied"] or f and f["phase_org"] == "done"))
    for k in ("path", "src", "dst"):
        if isinstance(out["details"].get(k), str):
            out["details"][k] = disp(req, out["details"][k])
    if out["details"].get("fields"):
        for cands in out["details"]["fields"].values():
            for c in cands:
                c.pop("offset", None) if c.get("offset") is None else None
    if f:
        out["file"] = {"id": f["id"], "name": f["name"], "path": disp(req, f["cur_path"]), "folder": disp(req, os.path.dirname(f["cur_path"])),
                       "origin": disp(req, f["orig_path"]), "media_kind": f["media_kind"], "size": f["size"],
                       "chosen_date": f["chosen_date"], "chosen_date_source": f["chosen_date_source"], "date_flags": f["date_flags"],
                       "present": bool(f["present"]), "duration": f.get("duration_s")}
        if full:
            out["fields"] = field_table(f)
            offset = next((c["value"] for row in out["fields"] if row["field"] == "offset" for c in row["candidates"]), None)
            out["date_candidates"] = [date_candidate({**c, "offset": c.get("offset") or offset}, common.app().library_timezone)
                                      for row in out["fields"] if row["field"] in ("datetime_original", "create_date")
                                      for c in row["candidates"]]
            out["proposals"] = [mtime_proposal(f, common.app().library_timezone) if p.get("origin") == "mtime_utc" else p
                                for p in out["proposals"]]
            if r["recheck_reason"]:
                out["recheck_proposal"] = mtime_proposal(f, common.app().library_timezone)
    return out


@router.get("/api/review")
def review_list(req: Request):
    db = need_db()
    off, lim = paging(req)
    st = req.query.get("status", "pending")
    rows = _visible_reviews(req)
    counts, totals = _review_counts(rows)
    statuses = {"pending", "skipped", "needs_recheck"} if st == "pending" else {st}
    rows = [row for row in rows if (st == "all" or review_state.status(row) in statuses)
            and (not req.query.get("kind") or row["kind"] == req.query["kind"])]
    page = rows[off:off + lim]
    return JSONResponse({"items": [_review_row(req, r) for r in page], "total": len(rows),
                         "counts": counts, "review_counts": totals, "fields": FIELDS, "writable": sorted(WRITABLE)})


def _visible_reviews(req: Request) -> list[dict]:
    owner = req.query.get("owner") if is_admin(req) and req.query.get("owner") else req.user["id"]
    rows = need_db().query("SELECT r.*, f.cur_path FROM review_items r JOIN files f ON f.id=r.file_id "
                          "AND f.owner_id=r.owner_id WHERE r.owner_id=? AND f.present=1 "
                          "AND f.media_kind IN ('image','video') AND f.status<>'dup_moved' ORDER BY r.id", (owner,))
    return [row for row in rows if is_admin(req) or access.file_in_scope(common.app().settings.data_dir.as_posix(), req.user, row)]


def _review_counts(rows: list[dict]) -> tuple[dict, dict]:
    counts = {}
    for row in rows:
        state = review_state.status(row)
        counts[state] = counts.get(state, 0) + 1
    return counts, {"total": len(rows), "decided": counts.get("decided", 0),
                    "pending": sum(counts.get(s, 0) for s in ("pending", "skipped", "needs_recheck")),
                    "needs_recheck": counts.get("needs_recheck", 0)}


@router.get("/api/review/{id:int}")
def review_get(req: Request):
    db = need_db()
    r = own_row(req, db.query_one("SELECT * FROM review_items WHERE id=?", (req.path_params["id"],)))
    if not is_admin(req):
        scoped_file(req, db.query_one("SELECT * FROM files WHERE id=?", (r["file_id"],)))
    out = _review_row(req, r, full=True)
    nxt = _next_review(req, r["owner_id"], r["id"])
    out["next_id"] = nxt["id"] if nxt else None
    return JSONResponse(out)


def validate_fields(fields: dict) -> dict:
    clean = {}
    for k, v in (fields or {}).items():
        if k not in WRITABLE:
            fail(400, "unknown_field", field=k)
        if v in (None, ""):
            continue                                  # empty stays empty
        v = str(v).strip()
        if k in ("datetime_original", "create_date"):
            p = parse_dt(v.replace("T", " ") if len(v) > 10 else v + " 00:00:00")
            if not p:
                fail(400, "invalid_date", field=k)
            v = p[0]
        elif k == "gps":
            parts = [x.strip() for x in v.split(",")]
            try:
                lat, lon = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                fail(400, "invalid_coordinates", field=k)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                fail(400, "invalid_coordinates", field=k)
            v = f"{lat:.6f},{lon:.6f}"
        elif k == "offset":
            import re
            if not re.match(r"^[+\-](0\d|1[0-4]):[0-5]\d$", v):
                fail(400, "invalid_offset", field=k)
        elif k == "orientation":
            if v not in [str(i) for i in range(1, 9)]:
                fail(400, "invalid_orientation", field=k)
        elif len(v) > 2000:
            fail(400, "too_long", field=k)
        clean[k] = v
    return clean


@router.post("/api/review/{id:int}")
def review_decide(req: Request):
    db = need_db()
    r = own_row(req, db.query_one("SELECT * FROM review_items WHERE id=?", (req.path_params["id"],)))
    if not is_admin(req):
        scoped_file(req, db.query_one("SELECT * FROM files WHERE id=?", (r["file_id"],)))
    if r["owner_id"] != req.user["id"] and not is_admin(req):
        fail(404, "not_found")
    f = db.query_one("SELECT * FROM files WHERE id=?", (r["file_id"],))
    if r["applied"] or f and f["phase_org"] == "done":
        fail(409, "already_applied")
    body = req.json()
    action = body.get("action")
    if action not in ("save", "skip", "keep"):
        fail(400, "bad_action")
    if action == "skip" and review_state.status(r) not in ("pending", "skipped"):
        fail(409, "bad_action")
    fields = validate_fields(body.get("fields") or {}) if action == "save" else {}
    date = fields.get("datetime_original") or fields.get("create_date")
    if action == "save" and body.get("date"):
        p = parse_dt(str(body["date"]).replace("T", " "))
        if not p:
            fail(400, "invalid_date", field="date")
        date = p[0]
    source, applied_timezone = "manual", None
    provenance = {k: {"source": "manual", "timezone": None} for k in fields}
    selected = body.get("proposal")
    if action == "save" and selected is not None:
        if not f or not isinstance(selected, dict):
            fail(400, "invalid_proposal")
        candidates = _review_row(req, r, full=True)
        choices = candidates.get("date_candidates", []) + candidates["proposals"]
        if candidates.get("recheck_proposal"):
            choices.append(candidates["recheck_proposal"])
        match = next((c for c in choices if c.get("source", c.get("origin")) == selected.get("source")
                      and c["value"] == date and c.get("tag") == selected.get("tag")), None)
        if not match:
            fail(400, "invalid_proposal")
        source, applied_timezone = match.get("source", match.get("origin", "other")), match.get("timezone")
        for k in ("datetime_original", "create_date"):
            if fields.get(k) == date:
                provenance[k] = {"source": source, "timezone": applied_timezone}
    scope = body.get("scope", "item")
    targets = [r]
    if scope == "folder" and r.get("group_key"):
        targets = db.query("SELECT * FROM review_items WHERE owner_id=? AND status='pending' AND group_key=? AND kind=?",
                           (r["owner_id"], r["group_key"], r["kind"]))
        if r not in targets:
            targets.append(r)
    elif scope == "group" and r["file_id"]:
        f = db.query_one("SELECT dup_group_id, primary_file_id FROM files WHERE id=?", (r["file_id"],))
        ids = [r["file_id"]]
        if f and f["dup_group_id"]:
            ids += [x["id"] for x in db.query("SELECT id FROM files WHERE dup_group_id=?", (f["dup_group_id"],))]
        targets = [x for x in db.query("SELECT * FROM review_items WHERE owner_id=? AND status='pending' AND kind=?", (r["owner_id"], r["kind"]))
                   if x["file_id"] in ids] or [r]
    status = {"save": "saved", "skip": "pending", "keep": "kept"}[action]
    decision = {"fields": fields, "date": date, "offset": fields.get("offset"), "scope": scope,
                "source": source, "timezone": applied_timezone}
    if date:
        provenance["date"] = {"source": source, "timezone": applied_timezone}
    with db.transaction():
        updated = 0
        for t in targets:
            target_file = db.query_one("SELECT * FROM files WHERE id=?", (t["file_id"],))
            if t["applied"] or target_file and target_file["phase_org"] == "done":
                continue
            if not is_admin(req) and target_file and not access.file_in_scope(common.app().settings.data_dir.as_posix(), req.user, target_file):
                continue
            if t["recheck_reason"] and (action != "save" or not date):
                continue
            if action == "skip":
                if review_state.status(t) not in ("pending", "skipped"):
                    continue
                db.execute("UPDATE review_items SET status='pending', skipped_at=? WHERE id=?", (utcnow(), t["id"]))
            else:
                db.execute("UPDATE review_items SET status=?, decision=?, decided_by=?, decided_at=?, decision_source=?, "
                           "decision_timezone=?, decision_provenance=?, recheck_reason=NULL, skipped_at=NULL WHERE id=?",
                           (status, json.dumps(decision, ensure_ascii=False), req.user["id"], utcnow(), source,
                            applied_timezone, json.dumps(provenance), t["id"]))
            updated += 1
            if action == "save" and date and t["file_id"]:
                db.execute("UPDATE files SET chosen_date=?, chosen_date_source='review', date_flags='', updated_at=? WHERE id=?",
                           (date, utcnow(), t["file_id"]))
            if t["file_id"]:
                f = db.query_one("SELECT cur_path FROM files WHERE id=?", (t["file_id"],))
                from ..db.manager import log
                log(db, "REVIEW_SKIPPED" if action == "skip" else "REVIEW_DECIDED",
                    owner_id=t["owner_id"], actor=req.user["username"], decision=status, path=(f or {}).get("cur_path"))
    nxt = _next_review(req, r["owner_id"], r["id"]) or _next_review(req, r["owner_id"], 0)
    return JSONResponse({"ok": True, "updated": updated, "next_id": nxt["id"] if nxt else None})


def _next_review(req: Request, owner: str, after: int) -> dict | None:
    db = need_db()
    for row in _visible_reviews(req):
        if row["owner_id"] == owner and row["id"] > after and review_state.status(row) in ("pending", "skipped", "needs_recheck"):
            return {"id": row["id"]}
    return None


@router.post("/api/review/{id:int}/reopen")
def review_reopen(req: Request):
    db = need_db()
    r = own_row(req, db.query_one("SELECT * FROM review_items WHERE id=?", (req.path_params["id"],)))
    if not is_admin(req):
        scoped_file(req, db.query_one("SELECT * FROM files WHERE id=?", (r["file_id"],)))
    f = db.query_one("SELECT phase_org FROM files WHERE id=?", (r["file_id"],))
    if r["applied"] or f and f["phase_org"] == "done":
        fail(409, "already_applied")
    db.execute("UPDATE review_items SET status='pending', skipped_at=NULL WHERE id=?", (r["id"],))
    return JSONResponse({"ok": True})


def _preview_response(req: Request, f: dict):
    core = common.app()
    pv = core.settings_section("previews")
    status, path = ensure_preview(core.settings.ffmpeg, core.settings.cache_dir, f,
                                  int(pv.get("transcode_height", 720)), int(pv.get("crf", 28)),
                                  core.settings.preview_cache_max_bytes)
    if req.query.get("probe"):
        return JSONResponse({"status": status, "media_kind": f["media_kind"]})
    if status == "original":
        return file_response(f["cur_path"], req)
    if status == "ready":
        return file_response(path, req, "video/mp4" if f["media_kind"] == "video" else "image/jpeg")
    return JSONResponse({"status": "pending"}, 202)


@router.get("/api/review/{id:int}/preview")
def review_preview(req: Request):
    db = need_db()
    r = own_row(req, db.query_one("SELECT * FROM review_items WHERE id=?", (req.path_params["id"],)))
    f = scoped_file(req, db.query_one("SELECT * FROM files WHERE id=?", (r["file_id"],))) if r["file_id"] else None
    if not f:
        fail(404, "not_found")
    return _preview_response(req, f)


# --- files (originals and previews, permission-checked) -------------------------------------
def _file(req: Request) -> dict:
    db = need_db()
    f = scoped_file(req, db.query_one("SELECT * FROM files WHERE id=?", (req.path_params["id"],)))
    core = common.app()
    try:
        access.check_path_allowed(core.settings.data_dir.as_posix(), req.user, f["cur_path"])
    except PathError:
        fail(404, "not_found")
    return f


@router.get("/api/files/{id:int}/content")
def file_content(req: Request):
    f = _file(req)
    return file_response(f["cur_path"], req)


@router.get("/api/files/{id:int}/preview")
def file_preview(req: Request):
    f = _file(req)
    return _preview_response(req, f)


# --- report, logs, manifest ---------------------------------------------------------------------
@router.get("/api/report")
def report(req: Request):
    db = need_db()
    owner = owner_filter(req)
    w, a = ("owner_id=?", [owner]) if owner else ("1=1", [])
    files = scoped_rows(req, db.query(f"SELECT * FROM files WHERE {w}", a))
    by_status = []
    for f in files:
        if not f["present"]:
            continue
        found = next((x for x in by_status if x["status"] == f["status"] and x["media_kind"] == f["media_kind"]), None)
        if found:
            found["n"] += 1
            found["bytes"] += f["size"]
        else:
            by_status.append({"status": f["status"], "media_kind": f["media_kind"], "n": 1, "bytes": f["size"]})
    allowed_ids = {f["id"] for f in files}
    manifest_rows = [m for m in db.query(f"SELECT * FROM manifest WHERE {w}", a) if m["file_id"] in allowed_ids]
    manifest = []
    for m in manifest_rows:
        found = next((x for x in manifest if x["action"] == m["action"]), None)
        if found:
            found["n"] += 1
            found["undone"] += 1 if m["undone_at"] else 0
        else:
            manifest.append({"action": m["action"], "n": 1, "undone": 1 if m["undone_at"] else 0})
    review_rows = [r for r in db.query(f"SELECT * FROM review_items WHERE {w}", a) if r["file_id"] in allowed_ids]
    review = []
    for r in review_rows:
        state = review_state.status(r)
        found = next((x for x in review if x["status"] == state), None)
        if found:
            found["n"] += 1
        else:
            review.append({"status": state, "n": 1})
    duplicate_groups = 0
    recoverable = 0
    data_dir = common.app().settings.data_dir.as_posix()
    for g in db.query("SELECT * FROM dup_groups WHERE status='pending'" + (" AND scope=?" if owner else ""),
                      [f"user:{owner}"] if owner else []):
        members = group_members(db, g, None if is_admin(req) else req.user, data_dir if not is_admin(req) else None)
        if len(members) > 1:
            duplicate_groups += 1
            recoverable += g["size"] * (len(members) - 1)
    missing = sum(1 for f in files if not f["present"])
    return JSONResponse({"files": by_status, "manifest": manifest, "review": review, "missing": int(missing or 0),
                         "duplicates": {"groups": duplicate_groups, "recoverable": recoverable}})


@router.get("/api/logs")
def logs(req: Request):
    db = need_db()
    off, lim = paging(req, 100, 1000)
    where, args = ["1=1"], []
    owner = owner_filter(req)
    if owner:
        where.append("owner_id=?")
        args.append(owner)
    for k in ("job_id", "event", "level"):
        if req.query.get(k):
            where.append(f"{k}=?")
            args.append(int(req.query[k]) if k == "job_id" else req.query[k])
    w = " AND ".join(where)
    rows = db.query(f"SELECT id, ts, level, event, message, params, job_id, owner_id, actor FROM run_log WHERE {w} ORDER BY id DESC", args)
    if not is_admin(req):
        filtered = []
        for r in rows:
            p = json.loads(r["params"] or "{}")
            paths = [v for v in p.values() if isinstance(v, str) and v.startswith("/")]
            if any(not access.path_in_scope(common.app().settings.data_dir.as_posix(), req.user, path) for path in paths):
                continue
            for k, v in p.items():
                if isinstance(v, str) and v.startswith("/"):
                    r["message"] = r["message"].replace(v, disp(req, v) or v)
            r["params"] = None
            filtered.append(r)
        rows = filtered
    total = len(rows)
    rows = rows[off:off + lim]
    return JSONResponse({"items": rows, "total": total})


@router.get("/api/manifest.csv")
def manifest_csv(req: Request):
    db = need_db()
    owner = owner_filter(req)

    def factory():
        def gen():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["id", "job_id", "file_id", "action", "original_path", "new_path", "sha256_before", "sha256_after",
                        "metadata_before", "metadata_after", "created_at", "undone_at"])
            last = 0
            while True:
                q = "SELECT * FROM manifest WHERE id>?" + (" AND owner_id=?" if owner else "") + " ORDER BY id LIMIT 1000"
                rows = db.query(q, (last, owner) if owner else (last,))
                if not rows:
                    break
                for m in rows:
                    last = m["id"]
                    if not is_admin(req) and not access.path_in_scope(
                            common.app().settings.data_dir.as_posix(), req.user, m["orig_path"]):
                        continue
                    w.writerow([m["id"], m["job_id"], m["file_id"], m["action"], disp(req, m["orig_path"]), disp(req, m["new_path"]),
                                m["sha256_before"] or "", m["sha256_after"] or "", m["meta_before"] or "", m["meta_after"] or "",
                                m["created_at"], m["undone_at"] or ""])
                yield buf.getvalue().encode("utf-8")
                buf.seek(0)
                buf.truncate()
            db.close_thread()
        return gen()
    return StreamResponse(factory, content_type="text/csv; charset=utf-8",
                          headers={"content-disposition": 'attachment; filename="manifest.csv"'})
