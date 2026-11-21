"""Tool endpoints (every role, on the caller's own sources)."""
from __future__ import annotations

import csv
import io
import json
import os

from ..db.schema import utcnow
from ..fsutil import PathError
from ..previews import ensure as ensure_preview
from ..services import access
from ..services.dedupe import group_members, set_keeper
from ..services.metadata import FIELDS, WRITABLE, field_table, parse_dt
from . import common
from .app import router
from .common import disp, fail, is_admin, need_db, own_row, owner_filter, paging, require
from .framework import JSONResponse, Request, StreamResponse, file_response

APPLY_KIND = {"organize_plan": "organize_apply", "dedupe_plan": "dedupe_apply", "metadata_analyze": "metadata_apply",
              "review_plan": "metadata_apply", "undo_moves_plan": "undo_moves_apply", "undo_meta_plan": "undo_meta_apply"}


def _submit(req: Request, kind: str, params: dict, owner: str | None = None) -> JSONResponse:
    core = common.app()
    need_db()
    if core.maintenance:
        fail(503, "maintenance", reason=core.maintenance)
    try:
        jid = core.jobs.submit(kind, owner or req.user["id"], params)
    except RuntimeError:
        fail(503, "maintenance")
    return JSONResponse({"job_id": jid}, 202)


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
    return JSONResponse({"sources": out, "can_browse": is_admin(req), "scan_selection": _scan_selection(req)})


def _scan_selection(req: Request) -> dict | None:
    core = common.app()
    data_dir = core.settings.data_dir.as_posix()
    assigned = access.areas(data_dir, req.user)
    latest = core.db.query_one("SELECT params FROM jobs WHERE owner_id=? AND kind='scan' ORDER BY id DESC LIMIT 1",
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
        area = next((a for a in assigned if os.path.realpath(a["path"]) == real), None)
        if not area and not is_admin(req):
            continue
        return {"path": os.path.relpath(real, os.path.realpath(data_dir)) if real != os.path.realpath(data_dir) else "",
                "source_id": area["id"] if area else None}
    return None


@router.get("/api/fs/browse")
def browse(req: Request):
    """Directories under /data (admin roles only: they choose sources and ad-hoc roots)."""
    require(req, "see_all_sources")
    core = common.app()
    try:
        real = access.check_path_allowed(core.settings.data_dir.as_posix(), req.user, req.query.get("path", ""))
    except PathError as exc:
        fail(400, exc.code)
    root = os.path.realpath(core.settings.data_dir.as_posix())
    dirs = []
    try:
        for e in sorted(os.scandir(real), key=lambda e: e.name):
            if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                dirs.append(e.name)
    except OSError:
        fail(400, "not_readable")
    return JSONResponse({"path": os.path.relpath(real, root) if real != root else "", "dirs": dirs[:2000]})


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
    a = _area(req, req.json())
    if not os.path.isdir(a["path"]):
        fail(400, "not_found")
    db = need_db()
    busy = db.query_one("SELECT id FROM jobs WHERE kind='scan' AND owner_id=? AND status IN ('queued','running') AND params LIKE ?",
                        (req.user["id"], f'%"source_id": "{a["id"]}"%'))
    if busy:
        fail(409, "already_running", job_id=busy["id"])
    return _submit(req, "scan", _area_params(a))


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
    rows = db.query(f"SELECT * FROM dup_groups WHERE scope=? AND status='pending' ORDER BY size DESC, id LIMIT {lim} OFFSET {off}", (sc,))
    agg = db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(size*(members-1)),0) AS rec FROM dup_groups WHERE scope=? AND status='pending'", (sc,))
    groups = []
    for g in rows:
        members = group_members(db, g)
        groups.append({"id": g["id"], "size": g["size"], "sha256": g["sha256"], "keeper_file_id": g["keeper_file_id"],
                       "recoverable": g["size"] * (len(members) - 1),
                       "members": [{"id": m["id"], "path": disp(req, m["cur_path"]) if (is_admin(req) or m["owner_id"] == req.user["id"]) else None,
                                    "date": m["chosen_date"], "owner": (common.app().user(m["owner_id"]) or {}).get("username") if is_admin(req) else None}
                                   for m in members]})
    return JSONResponse({"groups": groups, "total": int(agg["n"] or 0), "recoverable": int(agg["rec"] or 0)})


@router.post("/api/duplicates/{gid:int}/keeper")
def dup_keeper(req: Request):
    db = need_db()
    g = db.query_one("SELECT * FROM dup_groups WHERE id=?", (req.path_params["gid"],))
    if not g or (g["scope"] != f"user:{req.user['id']}" and not (g["scope"] == "global" and req.user["role"] == "admin")):
        fail(404, "not_found")
    try:
        set_keeper(db, g["id"], int(req.json().get("file_id") or 0))
    except ValueError as exc:
        fail(400, str(exc))
    return JSONResponse({"ok": True})


@router.get("/api/duplicates.csv")
def duplicates_csv(req: Request):
    db = need_db()
    sc = "global" if req.query.get("scope") == "global" and req.user["role"] == "admin" else f"user:{req.user['id']}"

    def factory():
        def gen():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["group_id", "sha256", "size", "role", "file_id", "path", "date"])
            last = 0
            while True:
                rows = db.query("SELECT * FROM dup_groups WHERE scope=? AND status='pending' AND id>? ORDER BY id LIMIT 200", (sc, last))
                if not rows:
                    break
                for g in rows:
                    last = g["id"]
                    for m in group_members(db, g):
                        w.writerow([g["id"], g["sha256"], g["size"], "keep" if m["id"] == g["keeper_file_id"] else "duplicate",
                                    m["id"], disp(req, m["cur_path"]), m["chosen_date"] or ""])
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
    items = db.query(f"SELECT * FROM plan_items WHERE {where} ORDER BY seq LIMIT {lim} OFFSET {off}", args)
    summary = db.query(f"SELECT kind, status, COUNT(*) AS n FROM plan_items WHERE plan_job_id=?{' AND owner_id=?' if owner else ''} "
                       "GROUP BY kind, status", [j["id"]] + ([owner] if owner else []))
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
    jid = core.jobs.submit(APPLY_KIND[j["kind"]], j["owner_id"],
                           {"plan_job_id": j["id"], "all_owners": params.get("scope") == "global"}, parent_job_id=j["id"])
    return JSONResponse({"job_id": jid}, 202)


# --- review -----------------------------------------------------------------------------
def _review_row(req: Request, r: dict, full: bool = False) -> dict:
    db = common.app().db
    f = db.query_one("SELECT * FROM files WHERE id=?", (r["file_id"],)) if r["file_id"] else None
    out = {"id": r["id"], "kind": r["kind"], "status": r["status"], "created_at": r["created_at"],
           "details": json.loads(r["details"] or "{}"), "proposals": json.loads(r["proposals"] or "[]"),
           "decision": json.loads(r["decision"]) if r["decision"] else None, "file_id": r["file_id"]}
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
    return out


@router.get("/api/review")
def review_list(req: Request):
    db = need_db()
    off, lim = paging(req)
    where, args = ["owner_id=?"], [req.user["id"]]
    if is_admin(req) and req.query.get("owner"):
        args[0] = req.query["owner"]
    st = req.query.get("status", "pending")
    where.append("status=?")
    args.append(st)
    if req.query.get("kind"):
        where.append("kind=?")
        args.append(req.query["kind"])
    w = " AND ".join(where)
    rows = db.query(f"SELECT * FROM review_items WHERE {w} ORDER BY id LIMIT {lim} OFFSET {off}", args)
    counts = db.query("SELECT status, COUNT(*) AS n FROM review_items WHERE owner_id=? GROUP BY status", (args[0],))
    return JSONResponse({"items": [_review_row(req, r) for r in rows], "total": int(db.scalar(f"SELECT COUNT(*) AS n FROM review_items WHERE {w}", args) or 0),
                         "counts": {c["status"]: c["n"] for c in counts}, "fields": FIELDS, "writable": sorted(WRITABLE)})


@router.get("/api/review/{id:int}")
def review_get(req: Request):
    db = need_db()
    r = own_row(req, db.query_one("SELECT * FROM review_items WHERE id=?", (req.path_params["id"],)))
    out = _review_row(req, r, full=True)
    nxt = db.query_one("SELECT id FROM review_items WHERE owner_id=? AND status='pending' AND id>? ORDER BY id LIMIT 1", (r["owner_id"], r["id"]))
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
    if r["owner_id"] != req.user["id"] and not is_admin(req):
        fail(404, "not_found")
    body = req.json()
    action = body.get("action")
    if action not in ("save", "skip", "keep"):
        fail(400, "bad_action")
    fields = validate_fields(body.get("fields") or {}) if action == "save" else {}
    date = fields.get("datetime_original") or fields.get("create_date")
    if action == "save" and body.get("date"):
        p = parse_dt(str(body["date"]).replace("T", " "))
        if not p:
            fail(400, "invalid_date", field="date")
        date = p[0]
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
    status = {"save": "saved", "skip": "skipped", "keep": "kept"}[action]
    decision = {"fields": fields, "date": date, "offset": fields.get("offset"), "scope": scope}
    with db.transaction():
        for t in targets:
            db.execute("UPDATE review_items SET status=?, decision=?, decided_by=?, decided_at=? WHERE id=?",
                       (status, json.dumps(decision, ensure_ascii=False), req.user["id"], utcnow(), t["id"]))
            if action == "save" and date and t["file_id"]:
                db.execute("UPDATE files SET chosen_date=?, chosen_date_source='review', date_flags='', updated_at=? WHERE id=?",
                           (date, utcnow(), t["file_id"]))
            if t["file_id"]:
                f = db.query_one("SELECT cur_path FROM files WHERE id=?", (t["file_id"],))
                from ..db.manager import log
                log(db, "REVIEW_DECIDED", owner_id=t["owner_id"], actor=req.user["username"], decision=status, path=(f or {}).get("cur_path"))
    nxt = db.query_one("SELECT id FROM review_items WHERE owner_id=? AND status='pending' AND id>? ORDER BY id LIMIT 1", (r["owner_id"], r["id"])) \
        or db.query_one("SELECT id FROM review_items WHERE owner_id=? AND status='pending' ORDER BY id LIMIT 1", (r["owner_id"],))
    return JSONResponse({"ok": True, "updated": len(targets), "next_id": nxt["id"] if nxt else None})


@router.post("/api/review/{id:int}/reopen")
def review_reopen(req: Request):
    db = need_db()
    r = own_row(req, db.query_one("SELECT * FROM review_items WHERE id=?", (req.path_params["id"],)))
    if r["applied"]:
        fail(409, "already_applied")
    db.execute("UPDATE review_items SET status='pending' WHERE id=?", (r["id"],))
    return JSONResponse({"ok": True})


# --- files (originals and previews, permission-checked) -------------------------------------
def _file(req: Request) -> dict:
    db = need_db()
    f = own_row(req, db.query_one("SELECT * FROM files WHERE id=?", (req.path_params["id"],)))
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
    core = common.app()
    pv = core.settings_section("previews")
    status, path = ensure_preview(core.settings.ffmpeg, core.settings.cache_dir, f, int(pv.get("transcode_height", 720)), int(pv.get("crf", 28)))
    if req.query.get("probe"):
        return JSONResponse({"status": status, "media_kind": f["media_kind"]})
    if status == "original":
        return file_response(f["cur_path"], req)
    if status == "ready":
        return file_response(path, req, "video/mp4" if f["media_kind"] == "video" else "image/jpeg")
    return JSONResponse({"status": "pending"}, 202)


# --- report, logs, manifest ---------------------------------------------------------------------
@router.get("/api/report")
def report(req: Request):
    db = need_db()
    owner = owner_filter(req)
    w, a = ("owner_id=?", [owner]) if owner else ("1=1", [])
    by_status = db.query(f"SELECT status, media_kind, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM files WHERE {w} AND present=1 GROUP BY status, media_kind", a)
    manifest = db.query(f"SELECT action, COUNT(*) AS n, SUM(CASE WHEN undone_at IS NULL THEN 0 ELSE 1 END) AS undone FROM manifest WHERE {w} GROUP BY action", a)
    review = db.query(f"SELECT status, COUNT(*) AS n FROM review_items WHERE {w} GROUP BY status", a)
    dups = db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(size*(members-1)),0) AS rec FROM dup_groups WHERE status='pending'" +
                        (" AND scope=?" if owner else ""), [f"user:{owner}"] if owner else [])
    missing = db.scalar(f"SELECT COUNT(*) AS n FROM files WHERE {w} AND present=0", a)
    return JSONResponse({"files": by_status, "manifest": manifest, "review": review, "missing": int(missing or 0),
                         "duplicates": {"groups": int(dups["n"] or 0), "recoverable": int(dups["rec"] or 0)}})


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
    rows = db.query(f"SELECT id, ts, level, event, message, params, job_id, owner_id, actor FROM run_log WHERE {w} ORDER BY id DESC LIMIT {lim} OFFSET {off}", args)
    if not is_admin(req):
        for r in rows:  # paths in English messages are made relative for base users
            p = json.loads(r["params"] or "{}")
            for k, v in p.items():
                if isinstance(v, str) and v.startswith("/"):
                    r["message"] = r["message"].replace(v, disp(req, v) or v)
            r["params"] = None
    return JSONResponse({"items": rows, "total": int(db.scalar(f"SELECT COUNT(*) AS n FROM run_log WHERE {w}", args) or 0)})


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
