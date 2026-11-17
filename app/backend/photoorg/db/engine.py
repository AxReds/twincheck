"""Minimal data-access layer shared by SQLite and MariaDB.

All SQL in the application is written once with ``?`` placeholders and portable
syntax; this module adapts placeholders, connection handling, transactions and
the few dialect-specific statements (upsert, insert-ignore, locks).
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator


class DBError(Exception):
    """English ``code`` + optional detail; the UI translates the code."""

    def __init__(self, code: str, detail: str = "", **params):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.params = params


def _mariadb():
    try:
        import pymysql  # noqa: F401
        return pymysql
    except ImportError as exc:  # pragma: no cover - present in the image
        raise DBError("driver_missing", "PyMySQL is not installed") from exc


class Database:
    """One logical database (SQLite file or MariaDB schema)."""

    def __init__(self, params: dict, mariadb_password_file: str | None = None):
        self.params = dict(params)
        self.kind = params.get("type", "sqlite")
        if self.kind not in ("sqlite", "mariadb"):
            raise DBError("bad_type")
        self._local = threading.local()
        self._all: list = []
        self._lock = threading.Lock()
        self._pw_file = mariadb_password_file
        self.read_only = False

    # -- description (never contains secrets) -----------------------------------------
    @property
    def label(self) -> str:
        if self.kind == "sqlite":
            return f"sqlite:{self.params.get('path')}"
        p = self.params
        return f"mariadb:{p.get('user')}@{p.get('host')}:{p.get('port', 3306)}/{p.get('database')}"

    def public_params(self) -> dict:
        out = {k: v for k, v in self.params.items() if k != "password"}
        if self.kind == "mariadb":
            out["password_set"] = bool(self.params.get("password") or self._pw_file)
        return out

    # -- connections -------------------------------------------------------------------
    def _password(self) -> str:
        if self._pw_file:
            try:
                with open(self._pw_file, "r", encoding="utf-8") as fh:
                    return fh.read().strip()
            except OSError:
                pass
        return self.params.get("password") or ""

    def _connect_sqlite(self):
        path = self.params.get("path")
        if not path:
            raise DBError("bad_params", "path")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=60, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=OFF")
        if self.read_only:
            conn.execute("PRAGMA query_only=1")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return conn

    def _connect_mariadb(self, with_db: bool = True):
        pymysql = _mariadb()
        p = self.params
        kwargs = dict(
            host=p.get("host"), port=int(p.get("port") or 3306), user=p.get("user"), password=self._password(),
            charset="utf8mb4", autocommit=True, connect_timeout=10, read_timeout=600, write_timeout=600,
            cursorclass=pymysql.cursors.DictCursor,
        )
        if with_db:
            kwargs["database"] = p.get("database")
        try:
            conn = pymysql.connect(**kwargs)
        except pymysql.err.OperationalError as exc:
            code = exc.args[0] if exc.args else 0
            if code == 1049:
                raise DBError("database_missing", str(code)) from None
            if code in (1044, 1045):
                raise DBError("access_denied", str(code)) from None
            raise DBError("unreachable", str(code)) from None
        with conn.cursor() as cur:
            cur.execute("SET SESSION time_zone = '+00:00'")
            cur.execute("SET SESSION sql_mode = 'STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_BACKSLASH_ESCAPES'")
            cur.execute("SET NAMES utf8mb4 COLLATE utf8mb4_bin")
        return conn

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect_sqlite() if self.kind == "sqlite" else self._connect_mariadb()
            self._local.conn = c
            self._local.depth = 0
            with self._lock:
                self._all.append(c)
        elif self.kind == "mariadb" and getattr(self._local, "depth", 0) == 0:
            c.ping(reconnect=True)
        return c

    def close(self):
        with self._lock:
            for c in self._all:
                with contextlib.suppress(Exception):
                    c.close()
            self._all.clear()
        self._local = threading.local()

    def close_thread(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            with self._lock:
                if c in self._all:
                    self._all.remove(c)
            with contextlib.suppress(Exception):
                c.close()
            self._local.conn = None

    # -- statements --------------------------------------------------------------------
    def _sql(self, sql: str) -> str:
        if self.kind == "mariadb":
            return sql.replace("%", "%%").replace("?", "%s")
        return sql

    def execute(self, sql: str, params: Iterable[Any] = ()):
        c = self.conn()
        if self.kind == "sqlite":
            return c.execute(sql, tuple(params))
        cur = c.cursor()
        cur.execute(self._sql(sql), tuple(params))
        return cur

    def executemany(self, sql: str, rows: list[tuple]):
        if not rows:
            return
        c = self.conn()
        if self.kind == "sqlite":
            c.executemany(sql, rows)
        else:
            with c.cursor() as cur:
                cur.executemany(self._sql(sql), rows)

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = self.execute(sql, params)
        return int(cur.lastrowid)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        if self.kind == "sqlite":
            return [dict(r) for r in rows]
        cur.close()
        return list(rows)

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = ()):
        row = self.query_one(sql, params)
        if not row:
            return None
        return next(iter(row.values()))

    @contextlib.contextmanager
    def transaction(self, immediate: bool = False) -> Iterator["Database"]:
        c = self.conn()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            if self.kind == "sqlite":
                c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            else:
                with c.cursor() as cur:
                    cur.execute("START TRANSACTION")
        self._local.depth = depth + 1
        try:
            yield self
        except BaseException:
            self._local.depth = depth
            if depth == 0:
                with contextlib.suppress(Exception):
                    c.rollback() if self.kind == "mariadb" else c.execute("ROLLBACK")
            raise
        else:
            self._local.depth = depth
            if depth == 0:
                if self.kind == "sqlite":
                    c.execute("COMMIT")
                else:
                    c.commit()

    @contextlib.contextmanager
    def snapshot(self) -> Iterator["Database"]:
        """Consistent read-only snapshot for exports (never writes)."""
        c = self.conn()
        if self.kind == "sqlite":
            c.execute("PRAGMA query_only=1")
            c.execute("BEGIN")
            try:
                self._local.depth = 1
                yield self
            finally:
                self._local.depth = 0
                c.execute("ROLLBACK")
                c.execute("PRAGMA query_only=0")
        else:
            with c.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            try:
                self._local.depth = 1
                yield self
            finally:
                self._local.depth = 0
                c.rollback()

    # -- dialect helpers ------------------------------------------------------------------
    def upsert(self, table: str, keys: list[str], data: dict) -> None:
        cols = list(data)
        ph = ",".join("?" for _ in cols)
        updates = [c for c in cols if c not in keys]
        if self.kind == "sqlite":
            upd = ",".join(f"{c}=excluded.{c}" for c in updates) or f"{keys[0]}={keys[0]}"
            sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) ON CONFLICT({','.join(keys)}) DO UPDATE SET {upd}"
        else:
            upd = ",".join(f"{c}=VALUES({c})" for c in updates) or f"{keys[0]}={keys[0]}"
            sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) ON DUPLICATE KEY UPDATE {upd}"
        self.execute(sql, [data[c] for c in cols])

    def insert_ignore_sql(self, table: str, cols: list[str]) -> str:
        ph = ",".join("?" for _ in cols)
        verb = "INSERT OR IGNORE" if self.kind == "sqlite" else "INSERT IGNORE"
        return f"{verb} INTO {table} ({','.join(cols)}) VALUES ({ph})"

    def tables(self) -> set[str]:
        if self.kind == "sqlite":
            rows = self.query("SELECT name FROM sqlite_master WHERE type='table'")
            return {r["name"] for r in rows}
        rows = self.query("SELECT table_name AS name FROM information_schema.tables WHERE table_schema = DATABASE()")
        return {r["name"] for r in rows}

    def columns(self, table: str) -> list[str]:
        if self.kind == "sqlite":
            return [r["name"] for r in self.query(f"PRAGMA table_info({table})")]
        rows = self.query(
            "SELECT column_name AS name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = ? ORDER BY ordinal_position",
            (table,),
        )
        return [r["name"] for r in rows]

    def indexes(self, table: str) -> set[str]:
        if self.kind == "sqlite":
            return {r["name"] for r in self.query(f"PRAGMA index_list({table})")}
        rows = self.query(
            "SELECT DISTINCT index_name AS name FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = ?",
            (table,),
        )
        return {r["name"] for r in rows}

    @contextlib.contextmanager
    def schema_lock(self, timeout: int = 120) -> Iterator[None]:
        """Serialise structure creation / migrations across processes."""
        if self.kind == "sqlite":
            # BEGIN IMMEDIATE takes the RESERVED lock: other writers wait (busy_timeout)
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            self._local.depth = 1
            try:
                yield
            except BaseException:
                self._local.depth = 0
                c.execute("ROLLBACK")
                raise
            else:
                self._local.depth = 0
                c.execute("COMMIT")
        else:
            name = f"photoorg_schema_{self.params.get('database')}"[:64]
            got = self.scalar("SELECT GET_LOCK(?, ?) AS l", (name, timeout))
            if got != 1:
                raise DBError("lock_timeout")
            try:
                yield
            finally:
                self.scalar("SELECT RELEASE_LOCK(?) AS l", (name,))

    def create_database_if_missing(self) -> bool:
        """MariaDB only: create the schema if the user has the privilege."""
        if self.kind != "mariadb":
            return False
        try:
            c = self._connect_mariadb(with_db=True)
            c.close()
            return False
        except DBError as exc:
            if exc.code != "database_missing":
                raise
        c = self._connect_mariadb(with_db=False)
        try:
            name = self.params.get("database") or ""
            if not name.replace("_", "").replace("-", "").isalnum():
                raise DBError("bad_params", "database name")
            pymysql = _mariadb()
            try:
                with c.cursor() as cur:
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
            except pymysql.err.OperationalError as exc:
                raise DBError("create_denied", str(exc.args[0] if exc.args else "")) from None
        finally:
            c.close()
        return True
