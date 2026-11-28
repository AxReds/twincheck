"""Opening, inspecting and locking an operational database."""
from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from .. import events
from .engine import Database, DBError
from .schema import CURRENT_VERSION, create_structure, get_meta, migrate, structure_version, utcnow

LOCK_STALE_SECONDS = 90
HEARTBEAT_SECONDS = 20


def make_database(params: dict, password_file: str | None = None) -> Database:
    return Database(params, mariadb_password_file=password_file if params.get("type") == "mariadb" else None)


def inspect(db: Database) -> dict:
    """Read-only description used by "Test connection" (never modifies anything)."""
    info = {"type": db.kind, "label": db.label, "reachable": False, "structure": "absent", "db_id": None,
            "schema_version": None, "app_schema_version": CURRENT_VERSION, "files": 0, "open_jobs": 0,
            "last_update": None, "migration_in_progress": False, "compatible": True, "database_missing": False,
            "has_data": False, "provenance": None, "in_use_by": None}
    if db.kind == "sqlite":
        import os
        path = db.params.get("path") or ""
        if not os.path.exists(path):
            parent = os.path.dirname(path) or "."
            info.update(reachable=os.path.isdir(parent) and os.access(parent, os.W_OK), database_missing=True)
            return info
    try:
        tables = db.tables()
    except DBError as exc:
        if exc.code == "database_missing":
            info.update(reachable=True, database_missing=True)
            return info
        raise
    info["reachable"] = True
    if "schema_meta" not in tables:
        info["has_data"] = bool(tables)
        return info
    v = structure_version(db)
    info.update(structure="present", schema_version=v, db_id=get_meta(db, "db_id"),
                last_update=get_meta(db, "updated_at"), provenance=get_meta(db, "provenance"),
                migration_in_progress=get_meta(db, "migration_state") is not None)
    info["compatible"] = v is not None and v <= CURRENT_VERSION
    with_files = "files" in tables
    if with_files:
        info["files"] = int(db.scalar("SELECT COUNT(*) AS n FROM files") or 0)
        info["open_jobs"] = int(db.scalar("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running','paused','interrupted')") or 0)
        info["has_data"] = any(int(db.scalar(f"SELECT COUNT(*) AS n FROM {t}") or 0) for t in ("files", "jobs", "run_log"))
    if "instance_lock" in tables:
        row = db.query_one("SELECT instance_id, hostname, heartbeat_at FROM instance_lock WHERE id = 1")
        if row and not _stale(row["heartbeat_at"]):
            info["in_use_by"] = {"instance_id": row["instance_id"], "hostname": row["hostname"]}  # informative
    return info


def _stale(ts: str | None) -> bool:
    if not ts:
        return True
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - t > timedelta(seconds=LOCK_STALE_SECONDS)


def prepare(db: Database, allow_migrate: bool = False, create: bool = True) -> dict:
    """Make sure the structure exists and is at the current version.

    * absent  -> created (inside the schema lock)
    * present -> reused untouched
    * older   -> migrated only if ``allow_migrate`` (user confirmed), else DBError('needs_migration')
    * newer   -> DBError('schema_too_new')
    """
    if db.kind == "mariadb":
        db.create_database_if_missing()
    with db.schema_lock():
        v = structure_version(db)
        if v is None:
            if not create:
                raise DBError("structure_absent")
            if db.tables() - {"schema_meta"}:
                # foreign tables in the target: refuse instead of mixing
                if "files" not in db.tables():
                    raise DBError("foreign_tables")
            create_structure(db)
            events.console("DB_STRUCTURE_CREATED", schema=CURRENT_VERSION)
            created = True
        else:
            created = False
            if v > CURRENT_VERSION:
                raise DBError("schema_too_new", version=v, supported=CURRENT_VERSION)
            if v < CURRENT_VERSION:
                if not allow_migrate:
                    raise DBError("needs_migration", version=v, supported=CURRENT_VERSION)
                migrate(db)
                log(db, "DB_MIGRATED", echo=True, from_version=v, to_version=CURRENT_VERSION)
            else:
                events.console("DB_STRUCTURE_REUSED", schema=v)
        if get_meta(db, "migration_state") is not None:
            raise DBError("migration_in_progress")
    return {"created": created, "db_id": get_meta(db, "db_id"), "schema_version": structure_version(db)}


class InstanceLock:
    """Only one container may write to a given database.

    The lock dies with the process (no stale locks after a kill):
    * SQLite: ``flock`` on ``<database>.instance-lock`` next to the file;
    * MariaDB: ``GET_LOCK`` held by a dedicated connection (released by the
      server when the connection or the container disappears).
    The ``instance_lock`` row only records who holds it, for display."""

    def __init__(self, db: Database, instance_id: str):
        self.db = db
        self.instance_id = instance_id
        self.hostname = socket.gethostname()[:100]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd = None
        self._conn = None

    def _name(self) -> str:
        return f"twincheck_instance_{self.db.params.get('database')}"[:64]

    def acquire(self) -> None:
        if self.db.kind == "sqlite":
            import fcntl
            import os
            fd = os.open(str(self.db.params["path"]) + ".instance-lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                row = self._holder()
                events.console("DB_INSTANCE_LOCKED", instance=f"{(row or {}).get('hostname')}/{(row or {}).get('instance_id')}")
                raise DBError("in_use", instance=(row or {}).get("hostname")) from None
            self._fd = fd
        else:
            conn = self.db._connect_mariadb()
            with conn.cursor() as cur:
                cur.execute("SELECT GET_LOCK(%s, 0) AS l", (self._name(),))
                got = cur.fetchone()["l"]
            if got != 1:
                conn.close()
                row = self._holder()
                events.console("DB_INSTANCE_LOCKED", instance=f"{(row or {}).get('hostname')}/{(row or {}).get('instance_id')}")
                raise DBError("in_use", instance=(row or {}).get("hostname"))
            self._conn = conn
        now = utcnow()
        with self.db.transaction():
            if self.db.query_one("SELECT id FROM instance_lock WHERE id = 1"):
                self.db.execute("UPDATE instance_lock SET instance_id=?, hostname=?, acquired_at=?, heartbeat_at=? WHERE id=1",
                                (self.instance_id, self.hostname, now, now))
            else:
                self.db.execute("INSERT INTO instance_lock (id, instance_id, hostname, acquired_at, heartbeat_at) VALUES (1, ?, ?, ?, ?)",
                                (self.instance_id, self.hostname, now, now))
        self._stop.clear()
        self._thread = threading.Thread(target=self._beat, name="db-heartbeat", daemon=True)
        self._thread.start()

    def _holder(self):
        try:
            return self.db.query_one("SELECT instance_id, hostname FROM instance_lock WHERE id = 1")
        except Exception:
            return None

    def _beat(self):
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                if self._conn is not None:
                    self._conn.ping(reconnect=False)
                self.db.execute("UPDATE instance_lock SET heartbeat_at=? WHERE id=1 AND instance_id=?", (utcnow(), self.instance_id))
            except Exception:  # keep trying; connection may come back
                pass
        self.db.close_thread()

    def release(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self.db.execute("DELETE FROM instance_lock WHERE id=1 AND instance_id=?", (self.instance_id,))
        except Exception:
            pass
        if self._fd is not None:
            import fcntl
            import os
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# The operational database must never contain user names: the application sets
# this mapper (user name -> stable user id) so that "actor"/"username" values are
# stored as ids.  Container stdout keeps the readable names.
IDENTITY = None


def log(db: Database, event: str, level: str = "INFO", job_id: int | None = None, owner_id: str | None = None,
        actor: str | None = None, echo: bool = False, **params) -> None:
    """Execution log entry in the operational database (English text + code + params)."""
    if IDENTITY is not None:
        if actor:
            actor = IDENTITY(actor)
        for k in ("actor", "username"):
            if isinstance(params.get(k), str):
                params = {**params, k: IDENTITY(params[k])}
    if job_id is not None and "job_id" not in params:
        params = {"job_id": job_id, **params}
    msg = events.render(event, params)
    db.execute(
        "INSERT INTO run_log (ts, level, event, message, params, job_id, owner_id, actor) VALUES (?,?,?,?,?,?,?,?)",
        (utcnow(), level, event, msg, json.dumps(params, ensure_ascii=False, default=str), job_id, owner_id, actor),
    )
    if echo:
        events.console(event, **params)


def new_instance_id() -> str:
    return secrets.token_hex(8)


def wait(seconds: float) -> None:
    time.sleep(seconds)
