"""Versioned schema of the operational database.

The schema is defined as an ordered list of migrations.  Version 1 creates the
baseline tables; later versions add columns/indexes.  Building "the schema at
version N" means applying migrations 1..N, so the same definitions serve fresh
installs, upgrades and imports of archives produced by older versions.

Portable column types (identical behaviour on SQLite and MariaDB):

=======  =================================  ==========================================
type     SQLite                             MariaDB (utf8mb4, InnoDB)
=======  =================================  ==========================================
pk       INTEGER PRIMARY KEY AUTOINCREMENT  BIGINT AUTO_INCREMENT PRIMARY KEY
int      INTEGER                            BIGINT
bool     INTEGER                            TINYINT
float    REAL                               DOUBLE
key      TEXT                               VARCHAR(191) utf8mb4_bin (indexable)
hash     TEXT                               VARCHAR(128) ascii_bin (lower-case hex)
ts       TEXT                               VARCHAR(40) (UTC ISO 8601, ...Z)
path     TEXT                               TEXT utf8mb4_bin (case-sensitive, any length)
text     TEXT                               MEDIUMTEXT utf8mb4_bin (JSON documents)
=======  =================================  ==========================================

Paths are never indexed directly: each indexed path has a companion
``*_path_hash`` column (SHA-256 of the UTF-8 path) so arbitrarily long and
case-differing paths are supported on both technologies.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass

from .. import VERSION
from .engine import Database, DBError

CURRENT_VERSION = 4

SQLITE_TYPES = {
    "pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "int": "INTEGER", "bool": "INTEGER", "float": "REAL",
    "key": "TEXT", "hash": "TEXT", "ts": "TEXT", "path": "TEXT", "text": "TEXT", "keypk": "TEXT PRIMARY KEY",
    "intpk": "INTEGER PRIMARY KEY",
}
MARIADB_TYPES = {
    "pk": "BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY", "int": "BIGINT", "bool": "TINYINT", "float": "DOUBLE",
    "key": "VARCHAR(191) COLLATE utf8mb4_bin", "hash": "VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin",
    "ts": "VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin", "path": "TEXT COLLATE utf8mb4_bin",
    "text": "MEDIUMTEXT COLLATE utf8mb4_bin", "keypk": "VARCHAR(191) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY",
    "intpk": "BIGINT NOT NULL PRIMARY KEY",
}
INDEXABLE = {"pk", "int", "bool", "float", "key", "hash", "ts", "keypk", "intpk"}


@dataclass
class Col:
    name: str
    type: str
    default: object = None


def C(name, type_, default=None):
    return Col(name, type_, default)


# ------------------------------------------------------------------------------------
# migrations: version -> list of operations
# ("table", name, [cols]) | ("column", table, Col) | ("index", table, name, [cols], unique)
# ------------------------------------------------------------------------------------
MIGRATIONS: dict[int, list[tuple]] = {
    1: [
        ("table", "schema_meta", [C("name", "keypk"), C("value", "text")]),
        ("table", "instance_lock", [C("id", "intpk"), C("instance_id", "key"), C("hostname", "key"),
                                    C("acquired_at", "ts"), C("heartbeat_at", "ts")]),
        ("table", "jobs", [
            C("id", "pk"), C("kind", "key"), C("owner_id", "key"), C("status", "key"), C("params", "text"),
            C("progress_done", "int", 0), C("progress_total", "int", 0), C("phase", "key"), C("checkpoint", "text"),
            C("message", "text"), C("error", "text"), C("result", "text"), C("parent_job_id", "int"),
            C("created_at", "ts"), C("started_at", "ts"), C("updated_at", "ts"), C("finished_at", "ts"),
        ]),
        ("index", "jobs", "ix_jobs_owner", ["owner_id", "status"], False),
        ("table", "run_log", [
            C("id", "pk"), C("ts", "ts"), C("level", "key"), C("event", "key"), C("message", "text"),
            C("params", "text"), C("job_id", "int"), C("owner_id", "key"), C("actor", "key"),
        ]),
        ("index", "run_log", "ix_log_owner", ["owner_id", "id"], False),
        ("index", "run_log", "ix_log_job", ["job_id", "id"], False),
        ("table", "files", [
            C("id", "pk"), C("owner_id", "key"), C("source_id", "key"),
            C("orig_path", "path"), C("orig_path_hash", "hash"), C("cur_path", "path"), C("cur_path_hash", "hash"),
            C("name", "path"), C("ext", "key"), C("size", "int"), C("mtime_ns", "int"), C("sha256", "hash"),
            C("hashed_at", "ts"), C("media_kind", "key"), C("status", "key"), C("dup_group_id", "int"),
            C("dup_of", "int"), C("primary_file_id", "int"), C("chosen_date", "key"), C("chosen_date_source", "key"),
            C("date_flags", "key"), C("meta_before", "text"), C("meta_after", "text"), C("meta_read", "text"),
            C("phase_scan", "key"), C("phase_hash", "key"), C("phase_meta", "key"), C("phase_org", "key"),
            C("present", "bool", 1), C("last_scan_job", "int"), C("first_seen", "ts"), C("updated_at", "ts"),
        ]),
        ("index", "files", "ux_files_path", ["owner_id", "cur_path_hash"], True),
        ("index", "files", "ix_files_hash", ["owner_id", "size", "sha256"], False),
        ("index", "files", "ix_files_source", ["owner_id", "source_id", "id"], False),
        ("index", "files", "ix_files_status", ["owner_id", "status"], False),
        ("table", "dup_groups", [
            C("id", "pk"), C("owner_id", "key"), C("scope", "key"), C("sha256", "hash"), C("size", "int"),
            C("keeper_file_id", "int"), C("members", "int"), C("verified", "bool", 0), C("status", "key"),
            C("job_id", "int"), C("created_at", "ts"),
        ]),
        ("index", "dup_groups", "ix_dup_scope", ["scope", "status"], False),
        ("table", "plan_items", [
            C("id", "pk"), C("plan_job_id", "int"), C("owner_id", "key"), C("kind", "key"), C("seq", "int"),
            C("file_id", "int"), C("src", "path"), C("dst", "path"), C("payload", "text"), C("status", "key"),
            C("note", "key"),
        ]),
        ("index", "plan_items", "ix_plan", ["plan_job_id", "seq"], False),
        ("table", "move_journal", [
            C("id", "pk"), C("job_id", "int"), C("owner_id", "key"), C("file_id", "int"), C("op", "key"),
            C("src", "path"), C("dst", "path"), C("sha256", "hash"), C("size", "int"), C("mtime_ns", "int"),
            C("state", "key"), C("error", "text"), C("created_at", "ts"), C("completed_at", "ts"),
        ]),
        ("index", "move_journal", "ix_journal_state", ["state"], False),
        ("table", "manifest", [
            C("id", "pk"), C("job_id", "int"), C("owner_id", "key"), C("file_id", "int"), C("action", "key"),
            C("orig_path", "path"), C("new_path", "path"), C("sha256_before", "hash"), C("sha256_after", "hash"),
            C("meta_before", "text"), C("meta_after", "text"), C("created_at", "ts"), C("undone_at", "ts"),
            C("undo_job_id", "int"),
        ]),
        ("index", "manifest", "ix_manifest_owner", ["owner_id", "id"], False),
        ("table", "meta_changes", [
            C("id", "pk"), C("job_id", "int"), C("owner_id", "key"), C("file_id", "int"), C("manifest_id", "int"),
            C("tag", "key"), C("before_value", "text"), C("after_value", "text"), C("source", "key"),
            C("created_at", "ts"), C("undone_at", "ts"),
        ]),
        ("index", "meta_changes", "ix_meta_file", ["owner_id", "file_id"], False),
        ("table", "review_items", [
            C("id", "pk"), C("owner_id", "key"), C("file_id", "int"), C("kind", "key"), C("details", "text"),
            C("proposals", "text"), C("status", "key"), C("decision", "text"), C("decided_by", "key"),
            C("decided_at", "ts"), C("created_at", "ts"), C("job_id", "int"), C("applied", "bool", 0),
        ]),
        ("index", "review_items", "ix_review_owner", ["owner_id", "status", "id"], False),
    ],
    # v2: video duration and grouping key for "apply to whole group/folder"
    2: [
        ("column", "files", C("duration_s", "float")),
        ("column", "review_items", C("group_key", "key")),
        ("index", "review_items", "ix_review_group", ["owner_id", "group_key"], False),
    ],
    # v3: automatic resume flag and event index for log filters
    3: [
        ("column", "jobs", C("auto_resume", "bool", 0)),
        ("index", "run_log", "ix_log_event", ["event"], False),
    ],
    4: [
        ("column", "review_items", C("decision_source", "key", "unknown")),
        ("column", "review_items", C("decision_timezone", "key", "unknown")),
        ("column", "review_items", C("decision_provenance", "text")),
        ("column", "review_items", C("recheck_reason", "key")),
        ("recheck",),
    ],
}

# tables that make up the operational state (exported / migrated, in this order)
DATA_TABLES = ["jobs", "run_log", "files", "dup_groups", "plan_items", "move_journal", "manifest", "meta_changes", "review_items"]
SYSTEM_TABLES = ["schema_meta", "instance_lock"]

# referential checks performed after a transfer: (table, column, referenced table)
REFERENCES = [
    ("files", "dup_of", "files"), ("files", "dup_group_id", "dup_groups"), ("files", "primary_file_id", "files"),
    ("dup_groups", "keeper_file_id", "files"), ("plan_items", "plan_job_id", "jobs"),
    ("move_journal", "file_id", "files"), ("manifest", "file_id", "files"), ("meta_changes", "file_id", "files"),
    ("meta_changes", "manifest_id", "manifest"), ("review_items", "file_id", "files"), ("run_log", "job_id", "jobs"),
]


def columns_at(version: int) -> dict[str, list[Col]]:
    """Table -> ordered column list at a given schema version."""
    out: dict[str, list[Col]] = {}
    for v in range(1, version + 1):
        for op in MIGRATIONS[v]:
            if op[0] == "table":
                out[op[1]] = list(op[2])
            elif op[0] == "column":
                out[op[1]].append(op[2])
    return out


def _coldef(db: Database, col: Col) -> str:
    types = SQLITE_TYPES if db.kind == "sqlite" else MARIADB_TYPES
    sql = f"{col.name} {types[col.type]}"
    if col.default is not None:
        sql += f" DEFAULT {int(col.default) if isinstance(col.default, (bool, int)) else repr(col.default)}"
    return sql


def _apply_op(db: Database, op: tuple) -> None:
    kind = op[0]
    if kind == "table":
        name, cols = op[1], op[2]
        if name in db.tables():
            return
        body = ", ".join(_coldef(db, c) for c in cols)
        suffix = "" if db.kind == "sqlite" else " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
        db.execute(f"CREATE TABLE {name} ({body}){suffix}")
    elif kind == "column":
        table, col = op[1], op[2]
        if col.name in db.columns(table):
            return
        db.execute(f"ALTER TABLE {table} ADD COLUMN {_coldef(db, col)}")
    elif kind == "index":
        table, name, cols, unique = op[1], op[2], op[3], op[4]
        if name in db.indexes(table):
            return
        db.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {table} ({', '.join(cols)})")
    elif kind == "recheck":
        _flag_historical_decisions(db)


def _flag_historical_decisions(db: Database) -> None:
    from datetime import datetime, timezone
    from ..services.metadata import parse_dt
    from ..db.manager import log

    count = 0
    last = 0
    while True:
        rows = db.query(
            "SELECT r.*, f.mtime_ns, f.meta_read, f.phase_org FROM review_items r "
            "JOIN files f ON f.id=r.file_id WHERE r.id>? AND r.applied=0 "
            "AND r.decision_source='unknown' AND r.status IN ('saved','kept','skipped') ORDER BY r.id LIMIT 500", (last,))
        if not rows:
            break
        for r in rows:
            last = r["id"]
            if r["phase_org"] == "done" or db.query_one(
                    "SELECT id FROM manifest WHERE file_id=? AND action='move' AND undone_at IS NULL LIMIT 1", (r["file_id"],)):
                continue
            decision = json.loads(r["decision"] or "{}")
            fields = decision.get("fields") or {}
            parsed = parse_dt(decision.get("date") or fields.get("datetime_original") or fields.get("create_date"))
            if not parsed:
                continue
            value = parsed[0]
            mt = datetime.fromtimestamp((r["mtime_ns"] or 0) / 1e9, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            metadata = json.loads(r["meta_read"] or "{}").get("fields", {})
            candidates = [c for name in ("datetime_original", "create_date")
                          for c in metadata.get(name, {}).get("candidates", []) if c.get("value") == value]
            sources = {c.get("source", "file") + ":" + c.get("semantic", "local") for c in candidates}
            if value != mt and len(sources) == 1:
                continue
            reason = "mtime_utc_match" if value == mt else "unknown_provenance"
            db.execute("UPDATE review_items SET status='needs_recheck', recheck_reason=? WHERE id=?", (reason, r["id"]))
            count += 1
    if count:
        log(db, "DECISIONS_FLAGGED_FOR_RECHECK", count=count)


def utcnow() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1e6):06d}Z"


def get_meta(db: Database, key: str, default=None):
    try:
        row = db.query_one("SELECT value FROM schema_meta WHERE name = ?", (key,))
    except Exception:
        return default
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def set_meta(db: Database, key: str, value) -> None:
    db.upsert("schema_meta", ["name"], {"name": key, "value": json.dumps(value, ensure_ascii=False)})


def del_meta(db: Database, key: str) -> None:
    db.execute("DELETE FROM schema_meta WHERE name = ?", (key,))


def structure_version(db: Database) -> int | None:
    if "schema_meta" not in db.tables():
        return None
    v = get_meta(db, "schema_version")
    return int(v) if v is not None else None


def create_structure(db: Database, version: int = CURRENT_VERSION) -> None:
    """Create the structure at ``version`` (idempotent, caller holds schema_lock)."""
    for v in range(1, version + 1):
        for op in MIGRATIONS[v]:
            _apply_op(db, op)
    set_meta(db, "schema_version", version)
    if get_meta(db, "db_id") is None:
        set_meta(db, "db_id", secrets.token_hex(8))
        set_meta(db, "created_at", utcnow())
        set_meta(db, "created_by_app", VERSION)
    set_meta(db, "updated_at", utcnow())


def migrate(db: Database, target: int = CURRENT_VERSION) -> tuple[int, int]:
    """Apply migrations from the stored version up to ``target`` (idempotent)."""
    current = structure_version(db) or 0
    if current > target:
        raise DBError("schema_too_new", version=current, supported=target)
    for v in range(current + 1, target + 1):
        for op in MIGRATIONS[v]:
            _apply_op(db, op)
        set_meta(db, "schema_version", v)
    set_meta(db, "updated_at", utcnow())
    return current, target
