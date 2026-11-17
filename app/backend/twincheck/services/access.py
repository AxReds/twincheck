"""Roles, permissions and per-user source areas."""
from __future__ import annotations

import os
import secrets

from ..fsutil import PathError, clean_relative, confine, is_within, overlap, path_hash

ROLES = ("admin", "administrator", "user")
ADMIN_ROLES = ("admin", "administrator")

# permission -> roles allowed (checked server-side on every request)
PERMISSIONS = {
    "use_tool": ROLES,
    "see_all_sources": ADMIN_ROLES,
    "manage_users": ADMIN_ROLES,          # base users and groups (administrator: base users only)
    "manage_administrators": ("admin",),
    "tool_settings": ADMIN_ROLES,
    "security_settings": ("admin",),
    "switch_database": ADMIN_ROLES,
    "transfer_database": ("admin",),
    "rotate_key": ("admin",),
    "view_audit": ADMIN_ROLES,
    "global_dedupe": ("admin",),
    "reassign_owner": ("admin",),
}


def can(user: dict | None, perm: str) -> bool:
    return bool(user) and user.get("role") in PERMISSIONS[perm]


def is_admin_role(user: dict | None) -> bool:
    return bool(user) and user.get("role") in ADMIN_ROLES


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def areas(data_dir: str, user: dict) -> list[dict]:
    """Assigned sources of a user, with absolute source and destination paths."""
    out = []
    for s in user.get("sources", []):
        src = os.path.join(data_dir, s["path"]) if s["path"] else data_dir
        dst = os.path.join(data_dir, s["dest"]) if s.get("dest") else src
        out.append({"id": s["id"], "label": s.get("label") or s["path"], "path_rel": s["path"],
                    "dest_rel": s.get("dest") or s["path"], "path": os.path.normpath(src), "dest": os.path.normpath(dst)})
    return out


def adhoc_source(data_dir: str, rel: str) -> dict:
    rel = clean_relative(rel)
    p = os.path.normpath(os.path.join(data_dir, rel)) if rel else os.path.normpath(data_dir)
    return {"id": "adhoc_" + path_hash(p)[:16], "label": "/" + rel, "path_rel": rel, "dest_rel": rel, "path": p, "dest": p}


def find_area(data_dir: str, user: dict, source_id: str, allow_adhoc_rel: str | None = None) -> dict:
    for a in areas(data_dir, user):
        if a["id"] == source_id:
            return a
    if allow_adhoc_rel is not None and is_admin_role(user):
        a = adhoc_source(data_dir, allow_adhoc_rel)
        if a["id"] == source_id or source_id in ("", None, "adhoc"):
            return a
    raise PathError("source_not_assigned")


def check_path_allowed(data_dir: str, user: dict, path: str, must_exist: bool = True) -> str:
    """Return the real path if ``user`` may access it (symlinks resolved)."""
    real = str(confine(data_dir, path, must_exist=must_exist))
    if is_admin_role(user):
        return real
    for a in areas(data_dir, user):
        if is_within(real, os.path.realpath(a["path"])) or is_within(real, os.path.realpath(a["dest"])):
            return real
    raise PathError("outside_source")


def display_path(data_dir: str, user: dict, path: str | None) -> str | None:
    """Base users see paths relative to their own source label; admins see /data paths."""
    if path is None:
        return None
    if is_admin_role(user):
        return path
    for a in areas(data_dir, user):
        for root in (a["path"], a["dest"]):
            if is_within(path, root):
                rel = os.path.relpath(path, root)
                return a["label"] + ("" if rel == "." else "/" + rel)
    return "…/" + os.path.basename(path)


def validate_sources(vault_data: dict, user_id: str, sources: list[dict], data_dir: str) -> list[dict]:
    """Normalise and validate a user's source list.

    Rules: every path exists under /data; no path traversal or symlink escape;
    the areas (source + destination) of different base users never overlap or
    nest; the areas of the same user do not nest into each other (a destination
    may equal its own source)."""
    clean = []
    for s in sources:
        rel = clean_relative(s.get("path", ""))
        if not rel:
            raise PathError("source_is_data_root")
        real = confine(data_dir, rel, must_exist=True)
        if not os.path.isdir(real):
            raise PathError("not_a_directory")
        dest_rel = clean_relative(s.get("dest") or "") or rel
        dreal = confine(data_dir, dest_rel, must_exist=False)
        if os.path.exists(dreal) and not os.path.isdir(dreal):
            raise PathError("not_a_directory")
        clean.append({"id": s.get("id") or new_id("src"), "path": os.path.relpath(real, os.path.realpath(data_dir)),
                      "dest": os.path.relpath(dreal, os.path.realpath(data_dir)), "label": (s.get("label") or "").strip()[:80]})
    mine = []
    for s in clean:
        for p in {s["path"], s["dest"]}:
            mine.append((s["id"], p))
    for i, (sid_a, a) in enumerate(mine):
        for sid_b, b in mine[i + 1:]:
            if sid_a != sid_b and overlap(a, b):
                raise PathError("source_nested_own")
    for uid, other in vault_data.get("users", {}).items():
        if uid == user_id or other.get("role") != "user":
            continue
        for o in other.get("sources", []):
            for op_ in {o["path"], o.get("dest") or o["path"]}:
                for _, p in mine:
                    if overlap(p, op_):
                        raise PathError("source_overlap_other_user")
    return clean
