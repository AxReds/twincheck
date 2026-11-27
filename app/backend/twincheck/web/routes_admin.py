"""Administration endpoints (admin and administrator, with different rights)."""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from .. import events, i18n
from ..crypto import KeyError_, generate_key, parse_key
from ..db import manager as dbm
from ..db import transfer
from ..db.engine import DBError
from ..db.schema import CURRENT_VERSION, DATA_TABLES, get_meta
from ..fsutil import PathError
from ..passwords import check_strength, generate_password, hash_password
from ..services import access
from ..vault import audit, utcnow
from . import common
from .app import db_params_from, inspect_params, router
from .common import fail, need_db, require, user_public
from .framework import HTTPError, JSONResponse, Request, file_response

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
ARCHIVE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\.tcarc$")


def _target_user(req: Request, uid: str) -> dict:
    core = common.app()
    u = core.vault.data["users"].get(uid)
    if not u:
        fail(404, "not_found")
    if req.user["role"] == "administrator" and u["role"] != "user":
        fail(403, "forbidden")             # administrators manage base users only
    return u


# --- users --------------------------------------------------------------------------------
@router.get("/api/admin/users")
def users(req: Request):
    require(req, "manage_users")
    core = common.app()
    return JSONResponse({"users": [user_public(u) for u in sorted(core.vault.data["users"].values(), key=lambda x: x["username"])],
                         "groups": list(core.vault.data.get("groups", {}).values())})


@router.post("/api/admin/users")
def create_user(req: Request):
    require(req, "manage_users")
    core = common.app()
    body = req.json()
    username = (body.get("username") or "").strip()
    role = body.get("role", "user")
    if not USERNAME_RE.match(username) or username.lower() == "admin":
        fail(400, "invalid_username")
    if role not in ("user", "administrator"):
        fail(400, "invalid_role")
    if role == "administrator" and not access.can(req.user, "manage_administrators"):
        fail(403, "forbidden")
    if core.user_by_name(username):
        fail(409, "username_taken")
    minlen = int(core.vault.data["security"].get("password_min_length", 12))
    generated = None
    pw = body.get("password") or ""
    if body.get("generate") or not pw:
        pw = generated = generate_password()
    else:
        errs = check_strength(pw, username, minlen)
        if errs:
            fail(400, "weak_password", reasons=errs, min_length=minlen)
    password_hash = hash_password(pw)
    uid = access.new_id("usr")
    with core.vault.transaction() as data:
        try:
            srcs = access.validate_sources(data, uid, body.get("sources") or [], core.settings.data_dir.as_posix()) if role == "user" else []
        except PathError as exc:
            raise HTTPError(400, exc.code) from None
        groups = [g for g in body.get("groups", []) if g in data.get("groups", {})]
        data["users"][uid] = {"id": uid, "username": username, "role": role, "password_hash": password_hash, "active": True,
                              "lang": body.get("lang") or None, "groups": groups, "sources": srcs, "session_epoch": 0,
                              "must_change_password": True, "created_at": utcnow()}
        audit(data, req.user["username"], "USER_CREATED", username=username, role=role)
    events.console("USER_CREATED", username=username, role=role, actor=req.user["username"])
    return JSONResponse({"id": uid, "generated_password": generated}, 201)


@router.patch("/api/admin/users/{uid}")
def update_user(req: Request):
    require(req, "manage_users")
    core = common.app()
    uid = req.path_params["uid"]
    target = _target_user(req, uid)
    body = req.json()
    with core.vault.transaction() as data:
        u = data["users"][uid]
        if "active" in body:
            if u["username"] == "admin" and not body["active"]:
                fail(400, "admin_fixed")
            u["active"] = bool(body["active"])
            if not u["active"]:
                u["session_epoch"] = u.get("session_epoch", 0) + 1
        if "role" in body and body["role"] != u["role"]:
            if u["username"] == "admin" or not access.can(req.user, "manage_administrators") or body["role"] not in ("user", "administrator"):
                fail(403, "forbidden")
            audit(data, req.user["username"], "USER_ROLE_CHANGED", user_id=uid, username=u["username"],
                  old_role=u["role"], role=body["role"])
            u["role"] = body["role"]
            u["session_epoch"] = u.get("session_epoch", 0) + 1
        if "groups" in body:
            groups = [g for g in body["groups"] if g in data.get("groups", {})]
            if groups != u["groups"]:
                audit(data, req.user["username"], "USER_GROUPS_CHANGED", user_id=uid, username=u["username"])
            u["groups"] = groups
        if "sources" in body:
            try:
                u["sources"] = access.validate_sources(data, uid, body["sources"] or [], core.settings.data_dir.as_posix())
            except PathError as exc:
                raise HTTPError(400, exc.code) from None
            audit(data, req.user["username"], "SOURCES_UPDATED", username=u["username"])
        if "lang" in body:
            u["lang"] = body["lang"] or None
        audit(data, req.user["username"], "USER_UPDATED", username=target["username"])
    return JSONResponse({"ok": True})


@router.delete("/api/admin/users/{uid}")
def delete_user(req: Request):
    require(req, "manage_users")
    core = common.app()
    uid = req.path_params["uid"]
    target = _target_user(req, uid)
    if target["username"] == "admin":
        fail(400, "admin_fixed")
    if uid == req.user["id"]:
        fail(400, "cannot_delete_self")
    with core.vault.transaction() as data:
        data["users"].pop(uid, None)
        audit(data, req.user["username"], "USER_DELETED", username=target["username"])
    core.sessions.drop_user(uid)
    return JSONResponse({"ok": True, "note": "operational_records_kept"})


@router.post("/api/admin/users/{uid}/password")
def reset_user_password(req: Request):
    require(req, "manage_users")
    core = common.app()
    uid = req.path_params["uid"]
    target = _target_user(req, uid)
    if target["username"] == "admin" and req.user["username"] != "admin":
        fail(403, "forbidden")
    pw = generate_password()
    password_hash = hash_password(pw)
    with core.vault.transaction() as data:
        u = data["users"][uid]
        u["password_hash"] = password_hash
        u["must_change_password"] = True
        u["session_epoch"] = u.get("session_epoch", 0) + 1
        data["auth_epoch"] = data.get("auth_epoch", 0) + 1
        auth_epoch = data["auth_epoch"]
        audit(data, req.user["username"], "PASSWORD_RESET", user_id=uid, username=u["username"])
    core.limiter.sync_epoch(auth_epoch)
    core.sessions.drop_user(uid)
    events.console("PASSWORD_RESET", username=target["username"], actor=req.user["username"])
    return JSONResponse({"generated_password": pw})


# --- groups -------------------------------------------------------------------------------
@router.post("/api/admin/groups")
def create_group(req: Request):
    require(req, "manage_users")
    core = common.app()
    name = (req.json().get("name") or "").strip()
    if not (1 <= len(name) <= 60):
        fail(400, "invalid_name")
    gid = access.new_id("grp")
    with core.vault.transaction() as data:
        if any(g["name"].lower() == name.lower() for g in data.get("groups", {}).values()):
            fail(409, "name_taken")
        data.setdefault("groups", {})[gid] = {"id": gid, "name": name, "active": True, "created_at": utcnow()}
        audit(data, req.user["username"], "GROUP_CREATED", name=name)
    return JSONResponse({"id": gid}, 201)


@router.patch("/api/admin/groups/{gid}")
def update_group(req: Request):
    require(req, "manage_users")
    core = common.app()
    gid = req.path_params["gid"]
    body = req.json()
    with core.vault.transaction() as data:
        g = data.get("groups", {}).get(gid)
        if not g:
            fail(404, "not_found")
        if "name" in body and (body["name"] or "").strip():
            g["name"] = body["name"].strip()[:60]
        if "active" in body:
            g["active"] = bool(body["active"])
        if "members" in body:
            for u in data["users"].values():
                if u["role"] != "user":
                    continue
                if u["id"] in body["members"] and gid not in u.get("groups", []):
                    u.setdefault("groups", []).append(gid)
                    audit(data, req.user["username"], "USER_GROUPS_CHANGED", user_id=u["id"], username=u["username"])
                elif u["id"] not in body["members"] and gid in u.get("groups", []):
                    u["groups"].remove(gid)
                    audit(data, req.user["username"], "USER_GROUPS_CHANGED", user_id=u["id"], username=u["username"])
        audit(data, req.user["username"], "GROUP_UPDATED", name=g["name"])
    return JSONResponse({"ok": True})


@router.delete("/api/admin/groups/{gid}")
def delete_group(req: Request):
    require(req, "manage_users")
    core = common.app()
    gid = req.path_params["gid"]
    with core.vault.transaction() as data:
        g = data.get("groups", {}).pop(gid, None)
        if not g:
            fail(404, "not_found")
        for u in data["users"].values():
            if gid in u.get("groups", []):
                u["groups"].remove(gid)
                audit(data, req.user["username"], "USER_GROUPS_CHANGED", user_id=u["id"], username=u["username"])
        audit(data, req.user["username"], "GROUP_DELETED", name=g["name"])
    return JSONResponse({"ok": True})


# --- settings -------------------------------------------------------------------------------
@router.get("/api/admin/settings")
def get_settings(req: Request):
    require(req, "tool_settings")
    core = common.app()
    d = core.vault.data
    settings = {k: v for k, v in d["settings"].items() if k not in ("library_timezone", "show_version_on_login")}
    if req.user["role"] == "admin":
        settings["library_timezone"] = core.library_timezone
        settings["show_version_on_login"] = d["settings"].get("show_version_on_login", True)
    out = {"settings": settings, "library_timezone_notice": req.user["role"] == "admin" and
           "library_timezone" not in d["settings"] and core.settings.library_timezone_notice,
           "security": d["security"] if access.can(req.user, "security_settings") else None,
           "locales": i18n.listing(core.settings.locales_dir, core.settings.state_locales_dir)}
    if req.user["role"] == "admin":
        now = datetime.now(timezone.utc)
        zones = []
        for name in sorted(available_timezones()):
            offset = now.astimezone(ZoneInfo(name)).utcoffset()
            minutes = int(offset.total_seconds() / 60)
            sign = "-" if minutes < 0 else "+"
            zones.append({"name": name, "offset": f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"})
        out["timezones"] = zones
    return JSONResponse(out)


@router.patch("/api/admin/settings")
def set_settings(req: Request):
    require(req, "tool_settings")
    core = common.app()
    body = req.json()
    if "show_version_on_login" in body:
        if req.user["role"] != "admin":
            fail(403, "forbidden")
        if not isinstance(body["show_version_on_login"], bool):
            fail(400, "invalid_value", field="show_version_on_login")
    if "library_timezone" in body:
        if req.user["role"] != "admin":
            fail(403, "forbidden")
        try:
            if not isinstance(body["library_timezone"], str):
                fail(400, "invalid_value", field="library_timezone")
            ZoneInfo(body["library_timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            fail(400, "invalid_timezone")
    with core.vault.transaction() as data:
        s = data["settings"]
        if "show_version_on_login" in body and body["show_version_on_login"] != s.get("show_version_on_login", True):
            s["show_version_on_login"] = body["show_version_on_login"]
            audit(data, req.user["username"], "SETTING_CHANGED", setting="show_version_on_login")
        if "library_timezone" in body and body["library_timezone"] != core.library_timezone:
            s["library_timezone"] = body["library_timezone"]
            audit(data, req.user["username"], "SETTING_CHANGED", setting="library_timezone")
        elif "library_timezone" in body:
            s["library_timezone"] = body["library_timezone"]
        org = body.get("organize")
        if org:
            for k in ("no_date_folder", "duplicates_folder"):
                v = (org.get(k) or "").strip()
                if v:
                    if "/" in v or v.startswith(".") or len(v) > 60:
                        fail(400, "invalid_folder_name")
                    s["organize"][k] = v
        sc = body.get("scan")
        if sc and isinstance(sc.get("exclude"), list):
            s["scan"]["exclude"] = [str(x)[:100] for x in sc["exclude"] if str(x).strip()][:100]
        md = body.get("metadata")
        if md and isinstance(md.get("fields"), list):
            from ..services.metadata import WRITABLE
            s["metadata"]["fields"] = [f for f in md["fields"] if f in WRITABLE]
        pv = body.get("previews")
        if pv:
            s["previews"]["transcode_height"] = max(240, min(2160, int(pv.get("transcode_height", 720))))
            s["previews"]["crf"] = max(18, min(40, int(pv.get("crf", 28))))
        if "default_lang" in body:
            require(req, "security_settings")
            if not i18n.messages(body["default_lang"], core.settings.locales_dir, core.settings.state_locales_dir):
                fail(400, "unknown_language")
            s["default_lang"] = body["default_lang"]
        audit(data, req.user["username"], "SETTINGS_UPDATED", section="tool")
    if "library_timezone" in body and core.db_ready:
        dbm.log(core.db, "SETTING_CHANGED", actor=req.user["id"], setting="library_timezone")
    if "show_version_on_login" in body and core.db_ready:
        dbm.log(core.db, "SETTING_CHANGED", actor=req.user["id"], setting="show_version_on_login")
    return JSONResponse({"ok": True})


@router.patch("/api/admin/security")
def set_security(req: Request):
    require(req, "security_settings")
    core = common.app()
    body = req.json()
    limits = {"password_min_length": (8, 128), "session_hours": (1, 72), "idle_minutes": (5, 720), "login_attempts_before_delay": (1, 20)}
    with core.vault.transaction() as data:
        for k, (lo, hi) in limits.items():
            if k in body:
                try:
                    data["security"][k] = max(lo, min(hi, int(body[k])))
                except (TypeError, ValueError):
                    fail(400, "invalid_value", field=k)
        audit(data, req.user["username"], "SETTINGS_UPDATED", section="security")
    return JSONResponse({"ok": True})


@router.get("/api/admin/audit")
def audit_log(req: Request):
    from .app import security_activity_response
    return security_activity_response(req, admin=True)


@router.get("/api/admin/audit.csv", maintenance_ok=True)
def audit_csv(req: Request):
    from .app import security_csv_response
    return security_csv_response(req, admin=True)


# --- database: status, test, switch, schema migration -----------------------------------------
def _mask(params: dict | None) -> dict | None:
    if not params:
        return None
    out = {k: v for k, v in params.items() if k != "password"}
    if params.get("type") == "mariadb":
        out["password"] = "********" if params.get("password") else ""
    return out


@router.get("/api/admin/db")
def db_status(req: Request):
    require(req, "switch_database")
    core = common.app()
    info = None
    if core.db is not None:
        try:
            info = dbm.inspect(core.db)
        except Exception:
            info = None
    return JSONResponse({"status": core.db_status(), "current": _mask(core.vault.data.get("database")), "info": info,
                         "error": core.db_error, "previous": [_mask(p) for p in core.vault.data.get("previous_databases", [])],
                         "maintenance": core.maintenance, "app_schema_version": CURRENT_VERSION})


def _params(req: Request, body: dict) -> dict:
    core = common.app()
    if body.get("use_previous") is not None:
        prev = core.vault.data.get("previous_databases", [])
        i = int(body["use_previous"])
        if not 0 <= i < len(prev):
            fail(400, "not_found")
        return dict(prev[i])
    if body.get("from_transfer"):
        p = core.vault.data.get("pending_transfers", {}).get(str(body["from_transfer"]))
        if not p:
            fail(404, "not_found")
        return dict(p)
    p = db_params_from(body, core)
    if p["type"] == "mariadb" and not p["password"] and body.get("keep_password"):
        cur = core.vault.data.get("database") or {}
        if cur.get("type") == "mariadb" and cur.get("host") == p["host"] and cur.get("user") == p["user"]:
            p["password"] = cur.get("password", "")
    return p


@router.post("/api/admin/db/test", maintenance_ok=True)
def db_test(req: Request):
    require(req, "switch_database")
    core = common.app()
    p = _params(req, req.json())
    try:
        core.check_sqlite_path(p)
    except DBError as exc:
        fail(400, exc.code)
    info = inspect_params(core, p)
    info["is_current"] = core.db is not None and core.db.label == core.make_db(p).label
    return JSONResponse(info)


@router.post("/api/admin/db/switch", maintenance_ok=True)
def db_switch(req: Request):
    """Change of the operational database (database switch procedure, steps 3-8)."""
    require(req, "switch_database")
    core = common.app()
    body = req.json()
    if not body.get("confirm"):
        fail(400, "confirmation_required")
    new_params = _params(req, body)
    try:
        core.check_sqlite_path(new_params)
    except DBError as exc:
        fail(400, exc.code)
    new_db = core.make_db(new_params)
    if core.db is not None and new_db.label == core.db.label:
        fail(400, "same_database")
    old_params = core.vault.data.get("database")
    old_label = core.db.label if core.db else "none"
    with core.lock:
        if core.maintenance:
            fail(503, "maintenance", reason=core.maintenance)
        core.maintenance = "db_switch"
    paused = list(core.jobs.running)
    try:
        if not core.jobs.pause_all(120):
            raise DBError("jobs_did_not_pause")
        events.console("DB_SWITCH_STARTED", actor=req.user["username"], old_db=old_label, new_db=new_db.label)
        try:
            dbm.prepare(new_db, allow_migrate=bool(body.get("confirm_migration")))
        except DBError:
            raise
        except Exception as exc:
            raise DBError("unreachable", type(exc).__name__) from None
        new_lock = dbm.InstanceLock(new_db, core.instance_id)
        new_lock.acquire()
        # --- point of no return: swap; the old database is left untouched -------------
        old_db, old_lock = core.db, core.instance_lock
        if old_db is not None and core.db_ready:
            try:
                dbm.log(old_db, "DB_SWITCH_STARTED", actor=req.user["username"], old_db=old_label, new_db=new_db.label)
            except Exception:
                pass
        core.db, core.instance_lock = new_db, new_lock
        from ..mover import recover
        recover(new_db)
        core.db_ready, core.db_error = True, None
        try:
            with core.vault.transaction() as data:
                if old_params:
                    prev = [p for p in data.get("previous_databases", []) if core.make_db(p).label != new_db.label]
                    if core.make_db(old_params).label not in [core.make_db(p).label for p in prev]:
                        prev.insert(0, old_params)
                    data["previous_databases"] = prev[:10]
                data["database"] = new_params
                audit(data, req.user["username"], "DB_SWITCHED", old_db=old_label, new_db=new_db.label)
        except Exception:
            # rollback: back to the previous database, nothing lost
            new_lock.release()
            new_db.close()
            core.db, core.instance_lock, core.db_ready = old_db, old_lock, old_db is not None
            raise DBError("vault_write_failed") from None
        if old_lock:
            old_lock.release()
        if old_db:
            old_db.close()
        dbm.log(new_db, "DB_SWITCHED", actor=req.user["username"], old_db=old_label, new_db=new_db.label)
        events.console("DB_SWITCHED", actor=req.user["username"], old_db=old_label, new_db=new_db.label)
        core.jobs.recover_after_start()
        core.maintenance = None
        core.jobs.unblock()
        rec = core.jobs.submit("reconcile", req.user["id"], {"hash_mode": body.get("hash_mode", "none")})
        return JSONResponse({"ok": True, "reconcile_job_id": rec, "db_id": get_meta(new_db, "db_id"),
                             "paused_in_previous": paused})
    except DBError as exc:
        events.console("DB_SWITCH_FAILED", level=30, old_db=old_label, reason=exc.code)
        new_db.close()
        core.maintenance = None
        core.jobs.unblock()
        for jid in paused:          # nothing changed: resume what was running
            try:
                core.jobs.resume(jid)
            except Exception:
                pass
        if core.db_ready:
            dbm.log(core.db, "DB_SWITCH_FAILED", old_db=old_label, reason=exc.code)
        fail(409 if exc.code in ("needs_migration", "in_use", "schema_too_new", "migration_in_progress") else 400, exc.code, **exc.params)


@router.post("/api/admin/db/migrate-schema", maintenance_ok=True)
def db_migrate_schema(req: Request):
    """Upgrade the configured database after explicit confirmation (backup first!)."""
    require(req, "switch_database")
    core = common.app()
    if not req.json().get("confirm_backup"):
        fail(400, "confirmation_required")
    if core.db_ready:
        fail(409, "not_needed")
    from_version = (core.db_error or {}).get("version")
    last_log = int(core.db.scalar("SELECT MAX(id) FROM run_log") or 0) if core.db is not None else 0
    if core.db is not None:
        core.db.close()
    core.db = None
    core.connect_configured(allow_migrate=True)
    if not core.db_ready:
        fail(400, (core.db_error or {}).get("code", "unreachable"))
    if from_version is not None and from_version < CURRENT_VERSION:
        with core.vault.transaction() as data:
            audit(data, req.user["username"], "DB_MIGRATED", from_version=from_version, to_version=CURRENT_VERSION)
            for entry in core.db.query("SELECT event, params FROM run_log WHERE id>? "
                                       "AND event IN ('DECISIONS_FLAGGED_FOR_RECHECK','REVIEW_SKIPS_RETURNED_TO_PENDING')", (last_log,)):
                audit(data, req.user["username"], entry["event"], **json.loads(entry["params"]))
    core.jobs.start()
    return JSONResponse({"ok": True})


# --- orphans (owners not in the vault, e.g. after a lost key) -----------------------------------
@router.get("/api/admin/owners")
def owners(req: Request):
    require(req, "reassign_owner")
    db = need_db()
    core = common.app()
    known = set(core.vault.data["users"])
    rows = db.query("SELECT owner_id, COUNT(*) AS n FROM files GROUP BY owner_id")
    return JSONResponse({"owners": [{"owner_id": r["owner_id"], "files": r["n"], "known": r["owner_id"] in known,
                                     "username": (core.vault.data["users"].get(r["owner_id"]) or {}).get("username")} for r in rows]})


@router.post("/api/admin/owners/reassign")
def reassign(req: Request):
    require(req, "reassign_owner")
    db = need_db()
    core = common.app()
    body = req.json()
    old, new = body.get("from_owner"), body.get("to_user")
    if new not in core.vault.data["users"] or old in core.vault.data["users"]:
        fail(400, "invalid_owner")
    with db.transaction():
        for t in DATA_TABLES:
            db.execute(f"UPDATE {t} SET owner_id=? WHERE owner_id=?", (new, old))
    core.audit(req.user["username"], "OWNER_REASSIGNED", old_owner=old, username=core.vault.data["users"][new]["username"])
    return JSONResponse({"ok": True})


# --- master key rotation (admin) -------------------------------------------------------------
@router.post("/api/admin/key/prepare")
def key_prepare(req: Request):
    require(req, "rotate_key")
    core = common.app()
    new = generate_key()
    core.pending_rotation = {"key": new, "by": req.user["id"]}
    return JSONResponse({"new_key": new})


@router.post("/api/admin/key/commit")
def key_commit(req: Request):
    require(req, "rotate_key")
    core = common.app()
    pend = core.pending_rotation
    given = (req.json().get("new_key") or "").strip()
    if not pend or pend["by"] != req.user["id"] or not secrets.compare_digest(given, pend["key"]):
        fail(400, "key_confirmation_mismatch")
    try:
        new_key = parse_key(given)
        core.vault.rotate(new_key)
        core.key = new_key
    except (KeyError_, Exception) as exc:
        core.audit(req.user["username"], "KEY_ROTATION_FAILED", reason=type(exc).__name__)
        fail(500, "key_rotation_failed")
    core.pending_rotation = None
    core.audit(req.user["username"], "KEY_ROTATED")
    return JSONResponse({"ok": True})


# --- export / import / migration (admin only) ---------------------------------------------------
def _passphrase(body: dict) -> str:
    p = body.get("passphrase") or ""
    if len(p) < 12:
        fail(400, "weak_passphrase")
    return p


def _archive_path(core, body: dict) -> str:
    name = body.get("archive") or ""
    where = body.get("location", "imports")
    if not ARCHIVE_RE.match(name) or where not in ("imports", "exports"):
        fail(400, "invalid_archive_name")
    base = core.settings.imports_dir if where == "imports" else core.settings.exports_dir
    path = os.path.join(str(base), name)
    if not os.path.isfile(path):
        fail(404, "not_found")
    return path


def _dest_params(core, body: dict) -> dict:
    p = db_params_from(body, core)
    core.check_sqlite_path(p)
    if core.db is not None and core.make_db(p).label == core.db.label:
        fail(400, "same_database")
    return p


@router.post("/api/admin/transfer/preview", maintenance_ok=True)
def transfer_preview(req: Request):
    require(req, "transfer_database")
    core = common.app()
    body = req.json()
    mode = body.get("mode")
    out: dict = {"mode": mode}
    try:
        if mode in ("export", "migrate"):
            db = need_db()
            out["source"] = transfer.preview_source(db)
            out["warnings"] = transfer.compat_warnings(out["source"]["schema_version"], body.get("database", {}).get("type") if mode == "migrate" else None)
        if mode in ("import", "migrate"):
            p = _dest_params(core, body)
            dest = inspect_params(core, p)
            out["destination"] = dest
            if dest.get("has_data") or dest.get("structure") == "present" and dest.get("files"):
                out.setdefault("errors", []).append("destination_not_empty")
        if mode == "import":
            path = _archive_path(core, body)
            hdr = transfer.read_header(path, _passphrase(body))
            out["archive"] = {"name": os.path.basename(path), "bytes": os.path.getsize(path), "format": hdr["format"],
                              "app_version": hdr["app_version"], "schema_version": hdr["schema_version"],
                              "source_db_id": hdr["source_db_id"], "source_type": hdr["source_type"], "created_at": hdr["created_at"],
                              "tables": [t["name"] for t in hdr["tables"]]}
            out["warnings"] = transfer.compat_warnings(hdr["schema_version"], body.get("database", {}).get("type"))
            if hdr["schema_version"] > CURRENT_VERSION:
                out.setdefault("errors", []).append("archive_too_new")
            out["est_seconds"] = int(os.path.getsize(path) / (8 * 1024 * 1024)) + 1
    except transfer.ArchiveError as exc:
        fail(400, exc.code)
    except DBError as exc:
        fail(400, exc.code)
    if mode not in ("export", "import", "migrate"):
        fail(400, "bad_mode")
    return JSONResponse(out)


@router.post("/api/admin/transfer/start", maintenance_ok=True)
def transfer_start(req: Request):
    require(req, "transfer_database")
    core = common.app()
    db = need_db()
    body = req.json()
    if not body.get("confirm"):
        fail(400, "confirmation_required")
    mode = body.get("mode")
    passphrase = _passphrase(body) if mode in ("export", "import") else None
    actor = req.user["username"]          # readable name: vault audit and container log only
    actor_id = req.user["id"]             # stable id: stored in the operational database
    try:
        if mode == "export":
            name = transfer.export_name(get_meta(db, "db_id"))
            core.begin_maintenance("export")
            jid = core.jobs.submit("export", req.user["id"], {"name": name, "actor": actor_id}, {"passphrase": passphrase}, force=True)
            core.maintenance = f"job:{jid}"
            dbm.log(db, "EXPORT_STARTED", actor=actor, db_id=get_meta(db, "db_id"))
            core.audit(actor, "EXPORT_STARTED", db_id=get_meta(db, "db_id"))
            core.jobs.start_now(jid)
            return JSONResponse({"job_id": jid, "name": name}, 202)
        p = _dest_params(core, body)
        token = secrets.token_hex(8)
        with core.vault.transaction() as data:
            data.setdefault("pending_transfers", {})[token] = p     # destination credentials stay encrypted
        if mode == "import":
            path = _archive_path(core, body)
            jid = core.jobs.submit("import", req.user["id"], {"archive": path, "token": token, "actor": actor_id, "dest": core.make_db(p).label},
                                   {"passphrase": passphrase})
            core.audit(actor, "IMPORT_STARTED", name=os.path.basename(path), db_type=p["type"])
            with core.vault.transaction() as data:
                data["pending_transfers"][str(jid)] = p
            return JSONResponse({"job_id": jid}, 202)
        if mode == "migrate":
            core.begin_maintenance("migrate")
            jid = core.jobs.submit("migrate", req.user["id"], {"token": token, "actor": actor_id, "dest": core.make_db(p).label}, {"dest": p},
                                   force=True)
            core.maintenance = f"job:{jid}"
            with core.vault.transaction() as data:
                data["pending_transfers"][str(jid)] = p
            core.audit(actor, "MIGRATE_STARTED", src_db=get_meta(db, "db_id"), db_type=p["type"])
            core.jobs.start_now(jid)
            return JSONResponse({"job_id": jid}, 202)
    except DBError as exc:
        if core.maintenance in ("export", "migrate"):
            core.end_maintenance()
        fail(400, exc.code)
    fail(400, "bad_mode")


@router.post("/api/admin/transfer/{id:int}/resume", maintenance_ok=True)
def transfer_resume(req: Request):
    require(req, "transfer_database")
    core = common.app()
    db = need_db()
    j = db.query_one("SELECT * FROM jobs WHERE id=?", (req.path_params["id"],))
    if not j or j["kind"] not in ("export", "import", "migrate") or j["status"] not in ("paused", "interrupted", "failed"):
        fail(409, "not_resumable")
    body = req.json()
    secret = {"passphrase": _passphrase(body)} if j["kind"] in ("export", "import") else {}
    params = json.loads(j["params"])
    if j["kind"] == "migrate":
        secret["dest"] = core.vault.data.get("pending_transfers", {}).get(params["token"])
    if j["kind"] in ("export", "migrate"):
        core.begin_maintenance(j["kind"])
        core.maintenance = f"job:{j['id']}"
        core.jobs.secrets[j["id"]] = secret
        db.execute("UPDATE jobs SET status='queued', error=NULL WHERE id=?", (j["id"],))
        core.jobs.start_now(j["id"])
    else:
        core.jobs.resume(j["id"], secret)
    return JSONResponse({"ok": True})


@router.post("/api/admin/transfer/{id:int}/purge", maintenance_ok=True)
def transfer_purge(req: Request):
    """Empty a destination left half-way, only after explicit confirmation."""
    require(req, "transfer_database")
    core = common.app()
    db = need_db()
    if not req.json().get("confirm"):
        fail(400, "confirmation_required")
    j = db.query_one("SELECT * FROM jobs WHERE id=?", (req.path_params["id"],))
    if not j or j["kind"] not in ("import", "migrate") or j["status"] in ("running", "queued", "completed"):
        fail(409, "not_purgeable")
    params = json.loads(j["params"])
    p = core.vault.data.get("pending_transfers", {}).get(params["token"])
    if not p:
        fail(404, "not_found")
    dest = core.make_db(p)
    try:
        transfer.purge_destination(dest)
    except DBError as exc:
        fail(400, exc.code)
    finally:
        dest.close()
    core.audit(req.user["username"], "DESTINATION_PURGED")
    return JSONResponse({"ok": True})


@router.get("/api/admin/archives")
def archives(req: Request):
    require(req, "transfer_database")
    core = common.app()
    out = {}
    for where, d in (("exports", core.settings.exports_dir), ("imports", core.settings.imports_dir)):
        items = []
        try:
            for e in sorted(os.scandir(d), key=lambda e: e.name):
                if e.is_file() and ARCHIVE_RE.match(e.name):
                    st = e.stat()
                    items.append({"name": e.name, "bytes": st.st_size, "modified": st.st_mtime})
        except OSError:
            pass
        out[where] = items
    return JSONResponse(out)


@router.get("/api/admin/archives/exports/{name}")
def download_export(req: Request):
    require(req, "transfer_database")
    core = common.app()
    name = req.path_params["name"]
    if not ARCHIVE_RE.match(name):
        fail(400, "invalid_archive_name")
    return file_response(os.path.join(str(core.settings.exports_dir), name), req, "application/octet-stream", download_name=name)


@router.put("/api/admin/archives/imports/{name}", raw=True, maintenance_ok=True)
async def upload_import(req: Request, receive):
    """Streamed upload of a large archive straight to /state/imports (never overwrites)."""
    require(req, "transfer_database")
    core = common.app()
    name = req.path_params["name"]
    if not ARCHIVE_RE.match(name):
        fail(400, "invalid_archive_name")
    final = os.path.join(str(core.settings.imports_dir), name)
    if os.path.exists(final):
        fail(409, "file_exists")
    tmp = os.path.join(str(core.settings.imports_dir), f".{name}.{secrets.token_hex(4)}.part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    size = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    raise HTTPError(400, "client_disconnected")
                b = msg.get("body", b"")
                size += len(b)
                if size > core.settings.max_upload_bytes:
                    raise HTTPError(413, "request_too_large")
                fh.write(b)
                if not msg.get("more_body"):
                    break
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, final)
        except FileExistsError:
            raise HTTPError(409, "file_exists") from None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return JSONResponse({"ok": True, "name": name, "bytes": size}, 201)


@router.delete("/api/admin/archives/{where}/{name}")
def delete_archive(req: Request):
    require(req, "transfer_database")
    core = common.app()
    where, name = req.path_params["where"], req.path_params["name"]
    if where not in ("exports", "imports") or not ARCHIVE_RE.match(name):
        fail(400, "invalid_archive_name")
    if not req.query.get("confirm"):
        fail(400, "confirmation_required")
    path = os.path.join(str(core.settings.exports_dir if where == "exports" else core.settings.imports_dir), name)
    if not os.path.isfile(path):
        fail(404, "not_found")
    os.unlink(path)
    return JSONResponse({"ok": True})


# --- global deduplication (admin) ------------------------------------------------------------
@router.post("/api/admin/global-dedupe/analyze")
def global_analyze(req: Request):
    require(req, "global_dedupe")
    from .routes_tool import _submit
    return _submit(req, "global_dedupe_analyze", {})


@router.post("/api/admin/global-dedupe/plan")
def global_plan(req: Request):
    require(req, "global_dedupe")
    from .routes_tool import _submit
    return _submit(req, "dedupe_plan", {"scope": "global"})
