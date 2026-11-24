"""ASGI application: middleware chain, static files, setup wizard, login,
locales, personal settings and the header metrics endpoint."""
from __future__ import annotations

import asyncio
import os
import secrets
import time
import traceback

from .. import VERSION, events, i18n, sysinfo
from ..db import manager as dbm
from ..db.engine import DBError
from ..passwords import check_strength, hash_password, verify_password
from ..services import access
from ..vault import default_vault, utcnow
from . import common
from .common import fail, need_db, require
from .framework import POOL, HTTPError, JSONResponse, Request, Response, Router, file_response, read_body
from .security import (LANG_COOKIE, SECURITY_HEADERS, SESSION_COOKIE, network_allowed, real_client_ip)

router = Router()

# routes that stay usable during maintenance or before a password change
OPEN_PATHS = {"/api/login", "/api/logout", "/api/bootstrap", "/api/me", "/api/me/password", "/api/me/lang", "/api/me/theme", "/healthz"}


class ASGIApp:
    def __init__(self, core):
        self.core = core

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await asyncio.get_running_loop().run_in_executor(POOL, self.core.shutdown)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        try:
            resp = await self.dispatch(scope, receive)
        except HTTPError as exc:
            resp = JSONResponse({"error": exc.code, "params": exc.params}, exc.status)
            if exc.status == 429 and "retry_after" in exc.params:
                resp.headers["retry-after"] = str(exc.params["retry_after"])
        except Exception:
            events.console("JOB_FAILED", level=40, job_id=0, kind="http", error=traceback.format_exc(limit=2).replace("\n", " | ")[:500])
            resp = JSONResponse({"error": "internal_error", "params": {}}, 500)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        resp.headers.setdefault("cache-control", "no-store")
        await resp.send(send, head_only=scope["method"] == "HEAD")

    async def dispatch(self, scope, receive) -> Response:
        core = self.core
        s = core.settings
        req = Request(scope)
        # 1. network allow-list (every request, wizard and login included)
        if not network_allowed(req.peer_ip, s.allowed_networks):
            events.console("ACCESS_DENIED_NETWORK", level=30, ip=req.peer_ip)
            return Response("Forbidden", 403)
        req.client_ip = real_client_ip(req.peer_ip, req.headers.get("x-forwarded-for"), s.trusted_proxies)
        # 2. routing
        if not req.path.startswith("/api/") and req.path != "/healthz":
            return await self.static(req)
        route, params, other = router.resolve(req.method, req.path)
        if route is None:
            fail(405 if other else 404, "method_not_allowed" if other else "not_found")
        req.path_params = params
        # 3. body (bounded) unless the route streams it
        if not route.opts.get("raw"):
            req.body = await read_body(receive, s.max_request_bytes)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(POOL, self.authenticate, req, route)
        if route.opts.get("raw"):
            return await route.handler(req, receive)
        return await loop.run_in_executor(POOL, route.handler, req)

    def authenticate(self, req: Request, route) -> None:
        core = self.core
        if core.vault.data is not None:
            core.vault.refresh_if_changed()
        token = req.cookies.get(SESSION_COOKIE)
        sec = (core.vault.data or {}).get("security") or {}
        if core.setup_complete and token:
            sess = core.sessions.get(token, sec.get("session_hours", 8) * 3600, sec.get("idle_minutes", 30) * 60)
            if sess:
                user = core.user(sess["user_id"])
                groups = (core.vault.data or {}).get("groups", {})
                group_off = any(not groups.get(g, {}).get("active", True) for g in (user or {}).get("groups", []))
                if user and user.get("active", True) and not group_off and user.get("session_epoch", 0) == sess["epoch"]:
                    req.user, req.session = user, sess
                else:
                    core.sessions.drop(token)
        req.lang = (req.user or {}).get("lang") or req.cookies.get(LANG_COOKIE) or \
            ((core.vault.data or {}).get("settings") or {}).get("default_lang") or "en"
        # CSRF: state-changing requests must be same-origin and carry the token
        if req.method not in ("GET", "HEAD"):
            origin = req.headers.get("origin")
            host = req.headers.get("host", "")
            if origin and origin.split("://", 1)[-1] != host:
                fail(403, "csrf_origin")
            # Login replaces any current session, so it is protected like an anonymous
            # request (custom header, which a cross-site form cannot set) instead of
            # failing when the browser still holds a valid cookie but lost the token.
            if req.session and req.path != "/api/login":
                if not secrets.compare_digest(req.headers.get("x-csrf-token", ""), req.session["csrf"]):
                    fail(403, "csrf_token")
            elif req.headers.get("x-requested-with") != "twincheck":
                fail(403, "csrf_token")
        if route.opts.get("public"):
            return
        if not req.user:
            fail(401, "not_authenticated")
        if req.user.get("must_change_password") and req.path not in OPEN_PATHS:
            fail(403, "password_change_required")
        if core.maintenance and req.method not in ("GET", "HEAD") and not route.opts.get("maintenance_ok") \
                and req.path not in OPEN_PATHS:
            fail(503, "maintenance", reason=core.maintenance)

    async def static(self, req: Request) -> Response:
        if req.method not in ("GET", "HEAD"):
            fail(405, "method_not_allowed")
        root = os.path.realpath(str(self.core.settings.frontend_dir / "static"))
        path = req.path
        if path in ("/", "/index.html"):
            target = os.path.join(root, "index.html")
        elif path == "/favicon.ico" or path == "/favicon.svg":
            target = os.path.join(root, "img", "favicon.svg")
        elif path.startswith("/static/"):
            target = os.path.realpath(os.path.join(root, path[len("/static/"):]))
            if not target.startswith(root + "/"):
                fail(404, "not_found")
        else:
            fail(404, "not_found")
        if not os.path.isfile(target):          # never a directory listing
            fail(404, "not_found")
        ctype = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8", "svg": "image/svg+xml",
                 "html": "text/html; charset=utf-8", "json": "application/json"}.get(target.rsplit(".", 1)[-1])
        resp = file_response(target, req, ctype)
        resp.headers["cache-control"] = "no-cache"
        return resp


# ---------------------------------------------------------------------------------------
# health, bootstrap, locales
# ---------------------------------------------------------------------------------------
@router.get("/healthz", public=True)
def healthz(req: Request):
    return JSONResponse({"status": "ok"})


@router.get("/api/bootstrap", public=True)
def bootstrap(req: Request):
    core = common.app()
    data = core.vault.data or {}
    out = {"setup_required": not core.setup_complete, "authenticated": bool(req.user),
           "default_lang": ((data.get("settings") or {}).get("default_lang")) or core.settings.app_lang}
    if req.user:
        out.update(user=common.user_public(req.user), csrf=req.session["csrf"], version=VERSION,
                   maintenance=core.maintenance, db=core.db_status(),
                   permissions={p: access.can(req.user, p) for p in access.PERMISSIONS})
    return JSONResponse(out)


@router.get("/api/locales", public=True)
def locales(req: Request):
    s = common.app().settings
    return JSONResponse(i18n.listing(s.locales_dir, s.state_locales_dir))


@router.get("/api/locales/{code}", public=True)
def locale(req: Request):
    s = common.app().settings
    data = i18n.messages(req.path_params["code"], s.locales_dir, s.state_locales_dir)
    if not data:
        fail(404, "not_found")
    return JSONResponse(data, headers={"cache-control": "no-cache"})


# ---------------------------------------------------------------------------------------
# setup wizard (only until setup is complete, then permanently 410)
# ---------------------------------------------------------------------------------------
def _setup_guard(req: Request) -> dict:
    core = common.app()
    if core.setup_complete:
        fail(410, "setup_locked")
    body = req.json()
    wait = core.limiter.retry_after([f"ip:{req.client_ip}"])
    if wait:
        fail(429, "too_many_attempts", retry_after=wait)
    token = body.get("token") or ""
    if not core.setup_token or not secrets.compare_digest(str(token), core.setup_token):
        core.limiter.failure([f"ip:{req.client_ip}"], 3)
        fail(403, "bad_setup_token")
    return body


def db_params_from(body: dict, core) -> dict:
    p = body.get("database") or {}
    t = p.get("type", "sqlite")
    if t == "sqlite":
        name = (p.get("filename") or "twincheck.sqlite3").strip()
        if "/" in name or name.startswith(".") or not name.endswith((".sqlite3", ".sqlite", ".db")):
            fail(400, "bad_sqlite_name")
        return {"type": "sqlite", "path": str(core.settings.state_dir / name)}
    if t == "mariadb":
        try:
            port = int(p.get("port") or 3306)
        except ValueError:
            fail(400, "bad_port")
        out = {"type": "mariadb", "host": (p.get("host") or "").strip(), "port": port,
               "database": (p.get("database") or "").strip(), "user": (p.get("user") or "").strip(), "password": p.get("password") or ""}
        if not out["host"] or not out["database"] or not out["user"]:
            fail(400, "db_params_missing")
        if not out["database"].replace("_", "").replace("-", "").isalnum():
            fail(400, "bad_db_name")
        return out
    fail(400, "bad_db_type")


def inspect_params(core, params: dict) -> dict:
    db = core.make_db(params)
    try:
        info = dbm.inspect(db)
    except DBError as exc:
        return {"reachable": False, "error": exc.code, "type": params["type"]}
    except Exception as exc:
        return {"reachable": False, "error": "unreachable", "detail": type(exc).__name__, "type": params["type"]}
    finally:
        db.close()
    return info


@router.post("/api/setup/check-token", public=True)
def setup_check(req: Request):
    _setup_guard(req)
    return JSONResponse({"ok": True})


@router.post("/api/setup/test-db", public=True)
def setup_test_db(req: Request):
    body = _setup_guard(req)
    core = common.app()
    return JSONResponse(inspect_params(core, db_params_from(body, core)))


@router.post("/api/setup/complete", public=True)
def setup_complete(req: Request):
    body = _setup_guard(req)
    core = common.app()
    with core.lock:
        if core.setup_complete or core.vault.exists():
            fail(410, "setup_locked")
        pw, pw2 = body.get("password") or "", body.get("password_confirm") or ""
        if pw != pw2:
            fail(400, "password_mismatch")
        errs = check_strength(pw, "admin", 12)
        if errs:
            fail(400, "weak_password", reasons=errs)
        params = db_params_from(body, core)
        lang = body.get("lang") or core.settings.app_lang
        db = core.make_db(params)
        try:
            dbm.prepare(db, allow_migrate=bool(body.get("confirm_migration")))
        except DBError as exc:
            db.close()
            fail(409 if exc.code == "needs_migration" else 400, exc.code, **exc.params)
        except Exception:
            db.close()
            fail(400, "unreachable")
        data = default_vault(lang)
        uid = access.new_id("usr")
        data["users"][uid] = {"id": uid, "username": "admin", "role": "admin", "password_hash": hash_password(pw),
                              "active": True, "lang": lang, "groups": [], "sources": [], "session_epoch": 0,
                              "must_change_password": False, "created_at": utcnow()}
        data["database"] = params
        data["setup_complete"] = True
        data["settings"]["default_lang"] = lang
        from ..vault import audit
        audit(data, "admin", "SETUP_COMPLETED", db_type=params["type"])
        core.vault.create(data)            # written encrypted from the start
        core.setup_token = None
        try:
            core.attach(db)
        except DBError as exc:
            core.db_error = {"code": exc.code}
        events.console("SETUP_COMPLETED", db_type=params["type"])
        core.jobs.start()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------------------
# login / logout / personal settings
# ---------------------------------------------------------------------------------------
@router.post("/api/login", public=True)
def login(req: Request):
    core = common.app()
    if not core.setup_complete:
        fail(409, "setup_required")
    body = req.json()
    username = str(body.get("username") or "")[:100]
    password = str(body.get("password") or "")[:1024]
    sec = core.vault.data.get("security", {})
    core.limiter.sync_epoch(core.vault.data.get("auth_epoch", 0))
    keys = [f"ip:{req.client_ip}", f"user:{username.lower()}"]
    wait = core.limiter.retry_after(keys)
    if wait:
        events.console("LOGIN_THROTTLED", level=30, username=username, ip=req.client_ip)
        fail(429, "too_many_attempts", retry_after=wait)
    user = core.user_by_name(username)
    ok = verify_password(password, (user or {}).get("password_hash"))
    groups = core.vault.data.get("groups", {})
    if ok and user and (not user.get("active", True) or any(not groups.get(g, {}).get("active", True) for g in user.get("groups", []))):
        ok = False
    if not ok:
        core.limiter.failure(keys, int(sec.get("login_attempts_before_delay", 3)))
        events.console("LOGIN_FAILED", level=30, username=username, ip=req.client_ip)
        time.sleep(0.3)
        fail(401, "bad_credentials")
    core.limiter.success([f"user:{username.lower()}"])
    core.sessions.drop(req.cookies.get(SESSION_COOKIE))
    token, sess = core.sessions.create(user)
    events.console("LOGIN_OK", username=user["username"], ip=req.client_ip)
    resp = JSONResponse({"ok": True, "csrf": sess["csrf"], "user": common.user_public(user)})
    resp.set_cookie(SESSION_COOKIE, token, max_age=int(sec.get("session_hours", 8) * 3600))
    return resp


@router.post("/api/logout", public=True)
def logout(req: Request):
    core = common.app()
    core.sessions.drop(req.cookies.get(SESSION_COOKIE))
    if req.user:
        events.console("LOGOUT", username=req.user["username"])
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, "", max_age=0)
    return resp


@router.get("/api/me")
def me(req: Request):
    return JSONResponse(common.user_public(req.user))


@router.post("/api/me/password")
def change_password(req: Request):
    core = common.app()
    body = req.json()
    if not verify_password(body.get("current") or "", req.user.get("password_hash")):
        fail(400, "bad_current_password")
    new, new2 = body.get("password") or "", body.get("password_confirm") or ""
    if new != new2:
        fail(400, "password_mismatch")
    minlen = int(core.vault.data["security"].get("password_min_length", 12))
    errs = check_strength(new, req.user["username"], minlen)
    if errs:
        fail(400, "weak_password", reasons=errs, min_length=minlen)
    password_hash = hash_password(new)
    with core.vault.transaction() as data:
        u = data["users"][req.user["id"]]
        u["password_hash"] = password_hash
        u["must_change_password"] = False
        from ..vault import audit
        audit(data, u["username"], "PASSWORD_CHANGED", username=u["username"])
    events.console("PASSWORD_CHANGED", username=req.user["username"])
    return JSONResponse({"ok": True})


@router.post("/api/me/lang")
def set_lang(req: Request):
    core = common.app()
    code = (req.json().get("lang") or "").strip()
    if not i18n.CODE_RE.match(code) or not i18n.messages(code, core.settings.locales_dir, core.settings.state_locales_dir):
        fail(400, "unknown_language")
    with core.vault.transaction() as data:
        data["users"][req.user["id"]]["lang"] = code
    return JSONResponse({"ok": True, "lang": code})


@router.post("/api/me/theme")
def set_theme(req: Request):
    theme = req.json().get("theme")
    if theme not in ("auto", "light", "dark"):
        fail(400, "invalid_theme")
    with common.app().vault.transaction() as data:
        data["users"][req.user["id"]]["theme"] = theme
    return JSONResponse({"ok": True, "theme": theme})


# ---------------------------------------------------------------------------------------
# header metrics (authenticated; every role)
# ---------------------------------------------------------------------------------------
@router.get("/api/sysinfo")
def system_info(req: Request):
    core = common.app()
    info = sysinfo.system(core.settings, core.db_status(), core.jobs.running_count())
    data_dir = core.settings.data_dir.as_posix()
    if access.is_admin_role(req.user):
        areas = []
        for u in core.vault.data["users"].values():
            for a in access.areas(data_dir, u):
                a = dict(a)
                a["label"] = f"{u['username']}: {a['label']}"
                areas.append(a)
    else:
        areas = access.areas(data_dir, req.user)
    sizes = {}
    if core.db_ready:
        try:
            q = "SELECT source_id, SUM(size) AS total FROM files WHERE present=1"
            args: tuple = ()
            if not access.is_admin_role(req.user):
                q += " AND owner_id=?"
                args = (req.user["id"],)
            for r in core.db.query(q + " GROUP BY source_id", args):
                sizes[r["source_id"]] = int(r["total"] or 0)
        except Exception:
            pass
    info["disks"] = sysinfo.source_disks(core.settings, areas, sizes, lambda p: common.disp(req, p))
    return JSONResponse(info)


def create_app(core):
    common.APP = core
    from . import routes_admin, routes_tool  # noqa: F401  (register routes)
    return ASGIApp(core)


__all__ = ["create_app", "router", "need_db", "require", "dbm"]
