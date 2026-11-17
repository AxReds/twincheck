"""Application state: vault, current database, jobs, sessions, maintenance."""
from __future__ import annotations

import os
import secrets
import threading

from . import VERSION, events
from .config import Settings, ensure_state_dirs
from .crypto import DecryptError, MasterKey
from .db import manager as dbm
from .db.engine import Database, DBError
from .db.schema import CURRENT_VERSION, get_meta
from .jobs import JobManager
from .mover import recover as recover_moves
from .vault import Vault, audit, utcnow
from .web.security import LoginLimiter, SessionStore

# importing the services registers the job kinds
from .services import dedupe, metadata, moves, scan, undo, verify  # noqa: F401,E402
from .db import transfer  # noqa: F401,E402


class StartupError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class App:
    def __init__(self, settings: Settings, key: MasterKey):
        self.settings = settings
        self.key = key
        self.vault = Vault(settings.vault_path, key)
        self.db: Database | None = None
        self.db_ready = False
        self.db_error: dict | None = None
        self.instance_lock: dbm.InstanceLock | None = None
        self.instance_id = dbm.new_instance_id()
        self.jobs = JobManager(self, settings.job_workers)
        self.sessions = SessionStore()
        self.limiter = LoginLimiter()
        self.setup_token: str | None = None
        self.maintenance: str | None = None
        self.maintenance_paused: list[int] = []
        self.pending_rotation: dict | None = None
        self.lock = threading.RLock()

    # -- startup -------------------------------------------------------------------------
    def identity(self, name: str) -> str:
        if name in ("console", "system") or name.startswith("usr_"):
            return name
        u = self.user_by_name(name) if self.vault.data else None
        return u["id"] if u else "unknown"

    def startup(self) -> None:
        dbm.IDENTITY = self.identity
        ensure_state_dirs(self.settings)
        if self.vault.exists():
            try:
                self.vault.load()
            except DecryptError:
                events.console("VAULT_DECRYPT_FAILED", level=40)
                raise StartupError("vault_decrypt_failed") from None
            events.console("VAULT_LOADED")
        if not self.setup_complete:
            self.setup_token = secrets.token_urlsafe(18)
            events.console("SETUP_REQUIRED")
            events.console("SETUP_TOKEN", token=self.setup_token)   # the only secret ever written to the logs
            return
        self.connect_configured()
        self.jobs.start()

    @property
    def setup_complete(self) -> bool:
        return bool(self.vault.data and self.vault.data.get("setup_complete"))

    def settings_section(self, name: str) -> dict:
        data = self.vault.data or {}
        return (data.get("settings") or {}).get(name, {})

    def settings_password_file(self) -> str | None:
        return os.environ.get("MARIADB_PASSWORD_FILE") or None

    def make_db(self, params: dict) -> Database:
        p = dict(params)
        if p.get("type") == "sqlite" and not p.get("path"):
            p["path"] = str(self.settings.default_sqlite_path)
        return dbm.make_database(p, self.settings_password_file())

    def check_sqlite_path(self, params: dict) -> None:
        """SQLite databases must live in /state (the only persistent writable volume besides /data)."""
        if params.get("type") == "sqlite":
            path = os.path.realpath(params.get("path") or str(self.settings.default_sqlite_path))
            if not path.startswith(os.path.realpath(str(self.settings.state_dir)) + "/"):
                raise DBError("sqlite_outside_state")

    def connect_configured(self, allow_migrate: bool = False) -> None:
        params = (self.vault.data or {}).get("database")
        if not params:
            return
        db = self.make_db(params)
        try:
            dbm.prepare(db, allow_migrate=allow_migrate)
            self.attach(db)
        except DBError as exc:
            self.db_error = {"code": exc.code, **exc.params}
            self.db = db if exc.code in ("needs_migration",) else None
            self.db_ready = False
            events.console("DB_CONNECT_FAILED", level=30, reason=exc.code)
        except Exception as exc:  # unreachable server etc.
            self.db_error = {"code": "unreachable"}
            self.db_ready = False
            events.console("DB_CONNECT_FAILED", level=30, reason=type(exc).__name__)

    def attach(self, db: Database) -> None:
        lock = dbm.InstanceLock(db, self.instance_id)
        lock.acquire()
        self.db, self.instance_lock = db, lock
        recover_moves(db)
        self.db_ready, self.db_error = True, None
        dbm.log(db, "DB_CONNECTED", echo=True, db_type=db.kind, db_id=get_meta(db, "db_id"), schema=CURRENT_VERSION)
        self.jobs.recover_after_start()

    def detach(self) -> None:
        if self.instance_lock:
            self.instance_lock.release()
        if self.db:
            self.db.close()
        self.db, self.instance_lock, self.db_ready = None, None, False

    def shutdown(self) -> None:
        self.jobs.stop()
        if self.db_ready:
            try:
                dbm.log(self.db, "APP_STOPPED")
            except Exception:
                pass
        self.detach()
        events.console("APP_STOPPED")

    def db_status(self) -> dict:
        if not self.db:
            return {"type": ((self.vault.data or {}).get("database") or {}).get("type"), "connected": False,
                    "schema_version": None, "error": (self.db_error or {}).get("code")}
        try:
            v = get_meta(self.db, "schema_version")
        except Exception:
            v = None
        return {"type": self.db.kind, "connected": self.db_ready, "schema_version": v, "app_schema_version": CURRENT_VERSION,
                "error": (self.db_error or {}).get("code")}

    # -- maintenance -----------------------------------------------------------------------
    def begin_maintenance(self, reason: str) -> None:
        with self.lock:
            if self.maintenance:
                raise DBError("maintenance")
            self.maintenance = reason
            running = list(self.jobs.running)
        self.maintenance_paused = running
        if not self.jobs.pause_all(120):
            self.end_maintenance(resume=True)
            raise DBError("jobs_did_not_pause")

    def end_maintenance(self, resume: bool = True) -> None:
        with self.lock:
            self.maintenance = None
        self.jobs.unblock()
        if resume and self.db_ready:
            for jid in self.maintenance_paused:
                try:
                    self.jobs.resume(jid)
                except Exception:
                    pass
        self.maintenance_paused = []

    def on_job_finished(self, job_id: int, kind: str) -> None:
        if self.maintenance == f"job:{job_id}":
            self.end_maintenance(resume=True)

    # -- audit ---------------------------------------------------------------------------
    def audit(self, actor: str, event: str, **params) -> None:
        with self.vault.transaction() as data:
            audit(data, actor, event, **params)
        events.console(event, actor=actor, **params)
        if self.db_ready:
            try:
                dbm.log(self.db, event, actor=actor, **params)
            except Exception:
                pass

    def user(self, user_id: str) -> dict | None:
        return (self.vault.snapshot().get("users") or {}).get(user_id)

    def user_by_name(self, username: str) -> dict | None:
        for u in (self.vault.snapshot().get("users") or {}).values():
            if u["username"].lower() == (username or "").lower():
                return u
        return None


def started_message() -> None:
    events.console("APP_STARTED", version=VERSION)


def now() -> str:
    return utcnow()
