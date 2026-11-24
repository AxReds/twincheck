"""Helpers shared by the route modules."""
from __future__ import annotations

from typing import Any

from ..fsutil import PathError
from ..services import access
from .framework import HTTPError, Request

APP = None  # set by web.app.create_app


def app():
    return APP


def fail(status: int, code: str, **params: Any):
    raise HTTPError(status, code, **params)


def require(req: Request, perm: str) -> dict:
    if not req.user:
        fail(401, "not_authenticated")
    if not access.can(req.user, perm):
        fail(403, "forbidden")
    return req.user


def need_db():
    a = app()
    if a.db is None or not a.db_ready:
        fail(503, "db_unavailable", reason=(a.db_error or {}).get("code"))
    return a.db


def is_admin(req: Request) -> bool:
    return access.is_admin_role(req.user)


def owner_filter(req: Request) -> str | None:
    """Base users: always themselves.  Admin roles: everything, or ?owner=."""
    if is_admin(req):
        return req.query.get("owner") or None
    return req.user["id"]


def own_row(req: Request, row: dict | None) -> dict:
    """404 (not 403) for rows of other users: no information leak."""
    if not row:
        fail(404, "not_found")
    if not is_admin(req) and row.get("owner_id") != req.user["id"]:
        fail(404, "not_found")
    return row


def scoped_file(req: Request, row: dict | None) -> dict:
    row = own_row(req, row)
    if not access.file_in_scope(app().settings.data_dir.as_posix(), req.user, row):
        fail(404, "not_found")
    return row


def scoped_rows(req: Request, rows: list[dict]) -> list[dict]:
    data_dir = app().settings.data_dir.as_posix()
    return [row for row in rows if access.file_in_scope(data_dir, req.user, row)]


def disp(req: Request, path: str | None) -> str | None:
    return access.display_path(app().settings.data_dir.as_posix(), req.user, path)


def paging(req: Request, default: int = 50, maximum: int = 500) -> tuple[int, int]:
    try:
        off = max(0, int(req.query.get("offset", 0)))
        lim = max(1, min(maximum, int(req.query.get("limit", default))))
    except ValueError:
        fail(400, "bad_paging")
    return off, lim


def path_error(exc: PathError):
    fail(400, exc.code)


def user_public(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"], "active": u.get("active", True),
            "groups": u.get("groups", []), "lang": u.get("lang"), "theme": u.get("theme", "auto"), "must_change_password": u.get("must_change_password", False),
            "sources": u.get("sources", []), "created_at": u.get("created_at"), "fixed": u["username"] == "admin"}
