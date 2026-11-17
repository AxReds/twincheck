"""Background jobs with persistent state, checkpoints and safe pause/resume.

States: queued, running, paused, interrupted, completed, failed, cancelled.
A job's parameters, owner, progress and checkpoint live in the operational
database, so after a restart (or a database switch) work resumes from the last
checkpoint.  Only read-only kinds resume automatically; kinds that write or
move files wait for an explicit confirmation in the UI.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from typing import Any, Callable

from . import events
from .db.manager import log
from .db.schema import utcnow

ACTIVE = ("queued", "running")
OPEN = ("queued", "running", "paused", "interrupted")


class JobPaused(Exception):
    pass


class JobCancelled(Exception):
    pass


class JobSpec:
    def __init__(self, func: Callable, writes_files: bool, auto_resume: bool, needs_secret: bool = False):
        self.func = func
        self.writes_files = writes_files
        self.auto_resume = auto_resume
        self.needs_secret = needs_secret


REGISTRY: dict[str, JobSpec] = {}


def register(kind: str, writes_files: bool = False, auto_resume: bool = False, needs_secret: bool = False):
    def deco(func):
        REGISTRY[kind] = JobSpec(func, writes_files, auto_resume, needs_secret)
        return func
    return deco


class JobContext:
    def __init__(self, manager: "JobManager", row: dict):
        self.manager = manager
        self.app = manager.app
        self.db = manager.app.db
        self.id = row["id"]
        self.kind = row["kind"]
        self.owner_id = row["owner_id"]
        self.params = json.loads(row["params"] or "{}")
        self.checkpoint: dict = json.loads(row["checkpoint"] or "{}")
        self.secret = manager.secrets.get(row["id"], {})
        self._last = 0.0
        self.done = row.get("progress_done") or 0
        self.total = row.get("progress_total") or 0

    def check(self) -> None:
        flag = self.manager.flags.get(self.id)
        if flag == "pause":
            raise JobPaused()
        if flag == "cancel":
            raise JobCancelled()

    def progress(self, done: int | None = None, total: int | None = None, phase: str | None = None,
                 force: bool = False) -> None:
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total
        now = time.monotonic()
        if not force and now - self._last < 0.5:
            return
        self._last = now
        sets, vals = ["progress_done=?", "progress_total=?", "updated_at=?"], [self.done, self.total, utcnow()]
        if phase is not None:
            sets.append("phase=?")
            vals.append(phase)
        self.db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*vals, self.id))

    def save_checkpoint(self, cp: dict | None = None, **updates: Any) -> None:
        if cp is not None:
            self.checkpoint = cp
        self.checkpoint.update(updates)
        self.db.execute("UPDATE jobs SET checkpoint=?, progress_done=?, progress_total=?, updated_at=? WHERE id=?",
                        (json.dumps(self.checkpoint, ensure_ascii=False), self.done, self.total, utcnow(), self.id))

    def log(self, event: str, level: str = "INFO", **params: Any) -> None:
        params.pop("job_id", None)
        log(self.db, event, level=level, job_id=self.id, owner_id=self.owner_id, **params)


class JobManager:
    def __init__(self, app, workers: int = 2):
        self.app = app
        self.workers = workers
        self.flags: dict[int, str] = {}
        self.running: dict[int, threading.Thread] = {}
        self.secrets: dict[int, dict] = {}
        self.blocked = False          # maintenance / database switch
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30) -> None:
        self.pause_all(timeout)
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def recover_after_start(self) -> None:
        """Mark jobs left 'running' as interrupted; requeue read-only ones."""
        db = self.app.db
        rows = db.query("SELECT id, kind FROM jobs WHERE status='running'")
        for r in rows:
            db.execute("UPDATE jobs SET status='interrupted', updated_at=? WHERE id=?", (utcnow(), r["id"]))
            log(db, "JOB_INTERRUPTED", job_id=r["id"], kind=r["kind"], echo=True)
        for r in db.query("SELECT id, kind FROM jobs WHERE status='interrupted' ORDER BY id"):
            spec = REGISTRY.get(r["kind"])
            if spec and spec.auto_resume and not spec.writes_files and not spec.needs_secret:
                db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE id=?", (utcnow(), r["id"]))
                log(db, "JOB_RESUMED", job_id=r["id"], kind=r["kind"])
        self._wake.set()

    # -- API ---------------------------------------------------------------------------
    def submit(self, kind: str, owner_id: str, params: dict, secret: dict | None = None,
               parent_job_id: int | None = None, force: bool = False) -> int:
        if kind not in REGISTRY:
            raise ValueError(kind)
        if self.blocked and not force:
            raise RuntimeError("maintenance")
        db = self.app.db
        now = utcnow()
        jid = db.insert(
            "INSERT INTO jobs (kind, owner_id, status, params, checkpoint, progress_done, progress_total, parent_job_id, "
            "created_at, updated_at, auto_resume) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kind, owner_id, "queued", json.dumps(params, ensure_ascii=False), "{}", 0, 0, parent_job_id, now, now,
             1 if REGISTRY[kind].auto_resume else 0),
        )
        if secret:
            self.secrets[jid] = secret
        log(db, "JOB_QUEUED", job_id=jid, owner_id=owner_id, kind=kind)
        self._wake.set()
        return jid

    def pause(self, job_id: int) -> None:
        db = self.app.db
        row = db.query_one("SELECT status FROM jobs WHERE id=?", (job_id,))
        if not row:
            return
        if row["status"] == "queued":
            db.execute("UPDATE jobs SET status='paused', updated_at=? WHERE id=?", (utcnow(), job_id))
        elif row["status"] == "running":
            self.flags[job_id] = "pause"

    def cancel(self, job_id: int) -> None:
        db = self.app.db
        row = db.query_one("SELECT status, kind FROM jobs WHERE id=?", (job_id,))
        if not row:
            return
        if row["status"] == "running":
            self.flags[job_id] = "cancel"
        elif row["status"] in ("queued", "paused", "interrupted"):
            db.execute("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?", (utcnow(), utcnow(), job_id))
            log(db, "JOB_CANCELLED", job_id=job_id, kind=row["kind"])

    def resume(self, job_id: int, secret: dict | None = None) -> None:
        if self.blocked:
            raise RuntimeError("maintenance")
        db = self.app.db
        row = db.query_one("SELECT status, kind FROM jobs WHERE id=?", (job_id,))
        if row and row["status"] in ("paused", "interrupted", "failed"):
            if secret:
                self.secrets[job_id] = secret
            db.execute("UPDATE jobs SET status='queued', error=NULL, updated_at=? WHERE id=?", (utcnow(), job_id))
            log(db, "JOB_RESUMED", job_id=job_id, kind=row["kind"])
            self._wake.set()

    def pause_all(self, timeout: float = 60) -> bool:
        """Block new jobs and bring running ones to a checkpoint."""
        self.blocked = True
        with self._lock:
            ids = list(self.running)
        for jid in ids:
            self.flags[jid] = "pause"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self.running:
                    return True
            time.sleep(0.1)
        return False

    def unblock(self) -> None:
        self.blocked = False
        self._wake.set()

    def start_now(self, job_id: int) -> None:
        """Run a job immediately even while new jobs are blocked (maintenance
        jobs: export / migration)."""
        db = self.app.db
        row = db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            return
        db.execute("UPDATE jobs SET status='running', started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?",
                   (utcnow(), utcnow(), job_id))
        with self._lock:
            t = threading.Thread(target=self._run, args=(row,), name=f"job-{job_id}", daemon=True)
            t.job_kind, t.owner_id = row["kind"], row["owner_id"]
            self.running[job_id] = t
        t.start()

    def running_count(self) -> int:
        with self._lock:
            return len(self.running)

    # -- scheduler -----------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if self.blocked or self.app.db is None or not self.app.db_ready:
                continue
            try:
                self._schedule()
            except Exception:  # never let the scheduler die
                events.console("JOB_FAILED", job_id=0, kind="scheduler", error=traceback.format_exc(limit=1).strip())
                time.sleep(1)

    def _schedule(self) -> None:
        db = self.app.db
        with self._lock:
            free = self.workers - len(self.running)
            if free <= 0:
                return
            busy_writers = set()
            for jid, t in self.running.items():
                spec = REGISTRY.get(getattr(t, "job_kind", ""))
                if spec and spec.writes_files:
                    busy_writers.add(getattr(t, "owner_id", None))
            rows = db.query("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 50")
            for row in rows:
                if free <= 0:
                    break
                spec = REGISTRY.get(row["kind"])
                if spec is None:
                    continue
                if spec.needs_secret and row["id"] not in self.secrets:
                    db.execute("UPDATE jobs SET status='interrupted', message=?, updated_at=? WHERE id=?",
                               ("secret_required", utcnow(), row["id"]))
                    continue
                if spec.writes_files and row["owner_id"] in busy_writers:
                    continue
                db.execute("UPDATE jobs SET status='running', started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?",
                           (utcnow(), utcnow(), row["id"]))
                t = threading.Thread(target=self._run, args=(row,), name=f"job-{row['id']}", daemon=True)
                t.job_kind = row["kind"]
                t.owner_id = row["owner_id"]
                self.running[row["id"]] = t
                if spec.writes_files:
                    busy_writers.add(row["owner_id"])
                free -= 1
                t.start()

    def _run(self, row: dict) -> None:
        jid, kind = row["id"], row["kind"]
        db = self.app.db
        ctx = JobContext(self, row)
        resumed = bool(ctx.checkpoint)
        try:
            ctx.log("JOB_RESUMED" if resumed else "JOB_STARTED", kind=kind)
            result = REGISTRY[kind].func(ctx)
            ctx.progress(force=True)
            db.execute("UPDATE jobs SET status='completed', result=?, finished_at=?, updated_at=? WHERE id=?",
                       (json.dumps(result or {}, ensure_ascii=False, default=str), utcnow(), utcnow(), jid))
            ctx.log("JOB_COMPLETED", job_id=jid, kind=kind)
            self.secrets.pop(jid, None)
        except JobPaused:
            ctx.progress(force=True)
            db.execute("UPDATE jobs SET status='paused', updated_at=? WHERE id=?", (utcnow(), jid))
            ctx.log("JOB_PAUSED", job_id=jid, kind=kind)
        except JobCancelled:
            db.execute("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?", (utcnow(), utcnow(), jid))
            ctx.log("JOB_CANCELLED", job_id=jid, kind=kind)
            self.secrets.pop(jid, None)
        except Exception as exc:  # job failure is recorded in English, without secrets
            err = f"{type(exc).__name__}: {getattr(exc, 'code', '') or str(exc)[:300]}"
            events.console("JOB_FAILED", job_id=jid, kind=kind, error=err)
            try:
                db.execute("UPDATE jobs SET status='failed', error=?, finished_at=?, updated_at=? WHERE id=?",
                           (err, utcnow(), utcnow(), jid))
                ctx.log("JOB_FAILED", level="ERROR", job_id=jid, kind=kind, error=err)
            except Exception:
                pass
        finally:
            self.flags.pop(jid, None)
            with self._lock:
                self.running.pop(jid, None)
            db.close_thread()
            cb = getattr(self.app, "on_job_finished", None)
            if cb:
                try:
                    cb(jid, kind)
                except Exception:
                    pass
            self._wake.set()
