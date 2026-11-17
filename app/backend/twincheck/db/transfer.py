"""Export, import and direct migration between operational databases.

Archive format (``.tcarc``), technology independent::

    "TCARC\\0\\1\\n" | scrypt N | salt | nonce prefix | AES-256-GCM chunks (last chunk flagged)
      plaintext = gzip( NDJSON records )
        {"t":"header", format, app_version, schema_version, source_db_id, source_type, created_at, tables:[{name, columns}]}
        {"t":"table", "name": ...}
        {"t":"rows", "d": [[v, v, ...], ...]}          (batches of up to 1000 rows, ordered by id)
        {"t":"end", "name": ..., "rows": N, "sha256": ...}
        ...
        {"t":"manifest", format, app_version, schema_version, source_db_id, tables:[{name, rows, sha256}], total_sha256}

Row checksum: SHA-256 over canonical JSON arrays (one per line).  Dates are
UTC ISO 8601 text, hashes lower-case hex, text is full UTF-8; NULL is null.
The archive never contains credentials or vault data.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import random
import secrets
import time

from .. import VERSION
from ..crypto import StreamDecryptor, StreamEncryptor
from ..jobs import JobContext, register
from .engine import Database, DBError
from .manager import inspect, log, make_database
from .schema import (CURRENT_VERSION, DATA_TABLES, REFERENCES, columns_at, create_structure, del_meta, get_meta, migrate,
                     set_meta, structure_version, utcnow)

FORMAT = 1
BATCH = 1000
SAMPLE = 50


def canon_value(col_type: str, v):
    if v is None:
        return None
    if col_type in ("pk", "int", "bool", "intpk"):
        return int(v)
    if col_type == "float":
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8")
    return str(v)


def canon_line(values: list) -> bytes:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _filter(table: str, flt: dict | None) -> tuple[str, list]:
    """Optional row filter used by direct migrations: rows created after the
    migration started and the migration job's own rows are not copied."""
    if not flt:
        return "", []
    sql, args = " AND id<=?", [int(flt["max_id"].get(table, 0))]
    if table == "jobs":
        sql += " AND id<>?"
        args.append(flt["own_job"])
    elif table == "run_log":
        sql += " AND (job_id IS NULL OR job_id<>?)"
        args.append(flt["own_job"])
    return sql, args


def iter_rows(db: Database, table: str, cols: list, after_id: int = 0, batch: int = BATCH, flt: dict | None = None):
    names = ",".join(c.name for c in cols)
    last = after_id
    extra, extra_args = _filter(table, flt)
    while True:
        rows = db.query(f"SELECT {names} FROM {table} WHERE id>?{extra} ORDER BY id LIMIT {int(batch)}", (last, *extra_args))
        if not rows:
            return
        out = [[canon_value(c.type, r[c.name]) for c in cols] for r in rows]
        last = out[-1][0]
        yield out


def table_checksum(db: Database, table: str, cols: list, flt: dict | None = None) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    for chunk in iter_rows(db, table, cols, flt=flt):
        for row in chunk:
            h.update(canon_line(row))
            n += 1
    return n, h.hexdigest()


def total_checksum(tables: list[dict]) -> str:
    h = hashlib.sha256()
    for t in tables:
        h.update(f"{t['name']}:{t['rows']}:{t['sha256']}\n".encode())
    return h.hexdigest()


def preview_source(db: Database) -> dict:
    v = structure_version(db)
    cols = columns_at(v)
    tables, total = [], 0
    for t in DATA_TABLES:
        n = int(db.scalar(f"SELECT COUNT(*) AS n FROM {t}") or 0)
        sample = db.query(f"SELECT * FROM {t} ORDER BY id LIMIT 50")
        avg = (sum(len(json.dumps(r, default=str)) for r in sample) / len(sample)) if sample else 0
        tables.append({"name": t, "rows": n, "est_bytes": int(n * avg)})
        total += n
    est = sum(t["est_bytes"] for t in tables)
    return {"schema_version": v, "db_id": get_meta(db, "db_id"), "tables": tables, "rows": total,
            "est_bytes": est, "est_archive_bytes": int(est * 0.25), "est_seconds": int(total / 20000) + 1,
            "columns": {t: [c.name for c in cols[t]] for t in DATA_TABLES}}


def compat_warnings(src_version: int | None, dest_kind: str | None) -> list[str]:
    w = []
    if src_version is not None and src_version < CURRENT_VERSION:
        w.append("schema_upgrade")
    if src_version is not None and src_version > CURRENT_VERSION:
        w.append("schema_too_new")
    if dest_kind == "mariadb":
        w.append("mariadb_binary_collation")
        w.append("mariadb_packet_size")
    return w


# --- export ------------------------------------------------------------------------------
class _Writer:
    def __init__(self, enc):
        self.enc = enc

    def write(self, b):
        self.enc.write(b)
        return len(b)

    def flush(self):
        pass


def export_to(main_db: Database, fh, passphrase: str, ctx: JobContext | None = None) -> dict:
    """Write an archive of the database (consistent read-only snapshot) to ``fh``.
    A dedicated read-only connection is used, so job progress can still be
    recorded through the normal connection."""
    db = Database(main_db.params, mariadb_password_file=main_db._pw_file)
    try:
        return _export(db, fh, passphrase, ctx)
    finally:
        db.close()


def _export(db: Database, fh, passphrase: str, ctx: JobContext | None) -> dict:
    enc = StreamEncryptor(fh, passphrase)
    gz = gzip.GzipFile(fileobj=_Writer(enc), mode="wb", mtime=0, compresslevel=6)
    with db.snapshot():
        v = structure_version(db)
        cols = columns_at(v)
        header = {"t": "header", "format": FORMAT, "app_version": VERSION, "schema_version": v,
                  "source_db_id": get_meta(db, "db_id"), "source_type": db.kind, "created_at": utcnow(),
                  "tables": [{"name": t, "columns": [[c.name, c.type] for c in cols[t]]} for t in DATA_TABLES]}
        gz.write(canon_line(header))
        total_rows = sum(int(db.scalar(f"SELECT COUNT(*) AS n FROM {t}") or 0) for t in DATA_TABLES)
        done = 0
        summary = []
        for t in DATA_TABLES:
            if ctx:
                ctx.check()
            gz.write(canon_line({"t": "table", "name": t}))
            h = hashlib.sha256()
            n = 0
            for chunk in iter_rows(db, t, cols[t]):
                for row in chunk:
                    h.update(canon_line(row))
                n += len(chunk)
                gz.write(json.dumps({"t": "rows", "d": chunk}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                done += len(chunk)
                if ctx:
                    ctx.check()
                    ctx.progress(done=done, total=total_rows, phase=f"export:{t}")
            summary.append({"name": t, "rows": n, "sha256": h.hexdigest()})
            gz.write(canon_line({"t": "end", "name": t, "rows": n, "sha256": h.hexdigest()}))
        manifest = {"t": "manifest", "format": FORMAT, "app_version": VERSION, "schema_version": v,
                    "source_db_id": header["source_db_id"], "tables": summary, "total_sha256": total_checksum(summary)}
        gz.write(canon_line(manifest))
    gz.close()
    enc.close()
    return manifest


# --- archive reading ----------------------------------------------------------------------
class ArchiveError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def read_records(path: str, passphrase: str):
    fh = open(path, "rb")
    try:
        dec = StreamDecryptor(fh, passphrase)
        gz = gzip.GzipFile(fileobj=dec, mode="rb")
        for line in io.BufferedReader(gz, buffer_size=1024 * 1024):
            yield json.loads(line)
    finally:
        fh.close()


def read_header(path: str, passphrase: str) -> dict:
    from ..crypto import DecryptError
    try:
        for rec in read_records(path, passphrase):
            if rec.get("t") != "header":
                raise ArchiveError("bad_archive")
            return rec
    except DecryptError:
        raise ArchiveError("bad_passphrase_or_tampered") from None
    except (OSError, ValueError, EOFError, gzip.BadGzipFile):
        raise ArchiveError("bad_archive") from None
    raise ArchiveError("bad_archive")


def verify_archive(path: str, passphrase: str, ctx: JobContext | None = None) -> dict:
    """Full pass: authenticity (GCM), structure, row counts and checksums."""
    from ..crypto import DecryptError
    header, manifest, current, h, n = None, None, None, None, 0
    computed, samples = [], {}
    rnd = random.Random(7)
    try:
        for rec in read_records(path, passphrase):
            t = rec.get("t")
            if t == "header":
                header = rec
                if rec.get("format") != FORMAT:
                    raise ArchiveError("unsupported_format")
                if int(rec.get("schema_version") or 0) > CURRENT_VERSION:
                    raise ArchiveError("archive_too_new")
            elif t == "table":
                current, h, n = rec["name"], hashlib.sha256(), 0
                samples[current] = []
            elif t == "rows":
                for row in rec["d"]:
                    h.update(canon_line(row))
                    n += 1
                    s = samples[current]
                    if len(s) < SAMPLE:
                        s.append(row)
                    elif rnd.random() < SAMPLE / n:
                        s[rnd.randrange(SAMPLE)] = row
                if ctx:
                    ctx.check()
            elif t == "end":
                if rec["name"] != current or rec["rows"] != n or rec["sha256"] != h.hexdigest():
                    raise ArchiveError("checksum_mismatch")
                computed.append({"name": current, "rows": n, "sha256": h.hexdigest()})
            elif t == "manifest":
                manifest = rec
    except DecryptError:
        raise ArchiveError("bad_passphrase_or_tampered") from None
    except (ValueError, EOFError, gzip.BadGzipFile, KeyError, TypeError):
        raise ArchiveError("bad_archive") from None
    if not header or not manifest:
        raise ArchiveError("bad_archive")
    if manifest["tables"] != computed or manifest["total_sha256"] != total_checksum(computed):
        raise ArchiveError("checksum_mismatch")
    return {"header": header, "manifest": manifest, "samples": samples}


# --- destination handling -------------------------------------------------------------------
def prepare_destination(dest: Database, version: int, token: str, source_db_id: str) -> dict:
    """Create the structure at ``version`` in an EMPTY destination and mark it
    as "migration in progress".  Resumes if the marker carries our token."""
    if dest.kind == "mariadb":
        dest.create_database_if_missing()
    with dest.schema_lock():
        tables = dest.tables()
        if "schema_meta" in tables:
            state = get_meta(dest, "migration_state")
            if state and state.get("token") == token:
                return state
            if state:
                raise DBError("destination_busy")
            info = inspect(dest)
            if info["has_data"]:
                raise DBError("destination_not_empty")
            if structure_version(dest) != version:
                raise DBError("destination_not_empty")
        elif tables:
            raise DBError("destination_not_empty")
        else:
            create_structure(dest, version)
        state = {"token": token, "source_db_id": source_db_id, "table": None, "rows": 0, "done_tables": [], "started_at": utcnow()}
        set_meta(dest, "migration_state", state)
    return state


def insert_rows(dest: Database, table: str, cols: list[str], rows: list[list], state: dict) -> None:
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
    with dest.transaction():
        dest.executemany(sql, [tuple(r) for r in rows])
        state["table"] = table
        state["rows"] += len(rows)
        set_meta(dest, "migration_state", state)


def finish_table(dest: Database, table: str, state: dict) -> None:
    state["done_tables"].append(table)
    state["table"], state["rows"] = None, 0
    set_meta(dest, "migration_state", state)


def verify_destination(dest: Database, version: int, expected: list[dict], samples: dict) -> dict:
    cols = columns_at(version)
    report = {"tables": [], "orphans": {}, "samples_ok": True}
    for t in expected:
        n, sha = table_checksum(dest, t["name"], cols[t["name"]])
        report["tables"].append({"name": t["name"], "rows_source": t["rows"], "rows_dest": n,
                                 "sha256_source": t["sha256"], "sha256_dest": sha, "ok": n == t["rows"] and sha == t["sha256"]})
    for table, col, ref in REFERENCES:
        if col not in [c.name for c in cols[table]]:
            continue
        cnt = int(dest.scalar(f"SELECT COUNT(*) AS n FROM {table} a LEFT JOIN {ref} b ON b.id=a.{col} "
                              f"WHERE a.{col} IS NOT NULL AND b.id IS NULL") or 0)
        if cnt:
            report["orphans"][f"{table}.{col}"] = cnt
    for t, rows in (samples or {}).items():
        names = [c.name for c in cols[t]]
        for row in rows:
            got = dest.query_one(f"SELECT {','.join(names)} FROM {t} WHERE id=?", (row[0],))
            if not got or [canon_value(c.type, got[c.name]) for c in cols[t]] != row:
                report["samples_ok"] = False
                break
    report["ok"] = all(t["ok"] for t in report["tables"]) and report["samples_ok"]
    # orphans are reported (they may legitimately exist, e.g. log entries of purged jobs) but do not fail
    return report


def finalize_destination(dest: Database, version: int, source: dict, actor: str, entries: list | None = None) -> str:
    """Interrupt 'running' jobs, record provenance, new db_id, upgrade schema.
    Log entries of the transfer are written to the destination only now, after
    the copy has been verified (writing them earlier would collide with the
    copied rows and alter the checksums)."""
    with dest.transaction():
        for event, params in entries or []:
            log(dest, event, actor=actor, **params)
        dest.execute("UPDATE jobs SET status='interrupted', updated_at=? WHERE status='running'", (utcnow(),))
        new_id = secrets.token_hex(8)
        set_meta(dest, "db_id", new_id)
        set_meta(dest, "created_at", utcnow())
        set_meta(dest, "created_by_app", VERSION)
        set_meta(dest, "provenance", {"source_db_id": source.get("source_db_id"), "source_app_version": source.get("app_version"),
                                      "source_schema_version": version, "date": utcnow(), "app_version": VERSION, "by": actor})
        log(dest, "TRANSFER_PROVENANCE", actor=actor, source_db_id=source.get("source_db_id"), app_version=source.get("app_version"), date=utcnow())
    with dest.schema_lock():
        migrate(dest, CURRENT_VERSION)
        del_meta(dest, "migration_state")
    return new_id


def purge_destination(dest: Database) -> None:
    """Drop the tables of a destination left half-way (only if marked by us)."""
    if get_meta(dest, "migration_state") is None:
        raise DBError("not_a_partial_destination")
    for t in DATA_TABLES + ["instance_lock", "schema_meta"]:
        if t in dest.tables():
            dest.execute(f"DROP TABLE {t}")
    if dest.kind == "sqlite":
        dest.close()


# --- jobs --------------------------------------------------------------------------------
def _dest(ctx: JobContext) -> Database:
    params = ctx.secret.get("dest") or ctx.app.vault.snapshot().get("pending_transfers", {}).get(ctx.params["token"])
    if not params:
        raise DBError("destination_params_missing")
    return make_database(params, ctx.app.settings_password_file())


@register("export", needs_secret=True)
def export_job(ctx: JobContext) -> dict:
    """Read-only. A restart re-runs the export from the beginning (a snapshot cannot
    span two processes); the source is never modified."""
    exports = ctx.app.settings.exports_dir
    name = ctx.params["name"]
    tmp = exports / f".{name}.part"
    final = exports / name
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        manifest = export_to(ctx.db, fh, ctx.secret["passphrase"], ctx)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    rows = sum(t["rows"] for t in manifest["tables"])
    ctx.log("EXPORT_DONE", name=name, rows=rows)
    return {"name": name, "bytes": final.stat().st_size, "manifest": manifest}


def _copy_tables(ctx, dest, version, record_iter, state, total):
    cols = columns_at(version)
    done_rows = sum(0 for _ in [])
    progress = state.get("progress", 0)
    current = None
    skip = 0
    for rec in record_iter:
        ctx.check()
        t = rec.get("t")
        if t == "table":
            current = rec["name"]
            skip = state["rows"] if state.get("table") == current else 0
        elif t == "rows":
            if current in state["done_tables"]:
                continue
            rows = rec["d"]
            if skip >= len(rows):
                skip -= len(rows)
                continue
            if skip:
                rows, skip = rows[skip:], 0
            insert_rows(dest, current, [c.name for c in cols[current]], rows, state)
            progress += len(rows)
            ctx.progress(done=progress, total=total, phase=f"copy:{current}")
        elif t == "end":
            if current not in state["done_tables"]:
                finish_table(dest, current, state)
                ctx.log("TRANSFER_TABLE", table=current, rows=rec["rows"])
    return done_rows


def _db_records(src: Database, version: int, ctx: JobContext, flt: dict):
    cols = columns_at(version)
    for t in DATA_TABLES:
        yield {"t": "table", "name": t}
        h, n = hashlib.sha256(), 0
        for chunk in iter_rows(src, t, cols[t], flt=flt):
            for row in chunk:
                h.update(canon_line(row))
            n += len(chunk)
            yield {"t": "rows", "d": chunk}
        yield {"t": "end", "name": t, "rows": n, "sha256": h.hexdigest()}


@register("import", writes_files=False, needs_secret=True)
def import_job(ctx: JobContext) -> dict:
    path = ctx.params["archive"]
    token = ctx.params["token"]
    passphrase = ctx.secret["passphrase"]
    ctx.progress(phase="verify_archive", force=True)
    checked = verify_archive(path, passphrase, ctx)
    header, manifest = checked["header"], checked["manifest"]
    version = int(header["schema_version"])
    total = sum(t["rows"] for t in manifest["tables"])
    dest = _dest(ctx)
    try:
        state = prepare_destination(dest, version, token, header["source_db_id"])
        _copy_tables(ctx, dest, version, read_records(path, passphrase), state, total)
        ctx.progress(phase="verify_destination", force=True)
        report = verify_destination(dest, version, manifest["tables"], checked["samples"])
        if not report["ok"]:
            raise DBError("verification_failed")
        ctx.log("TRANSFER_VERIFIED", tables=len(report["tables"]))
        entries = [("IMPORT_STARTED", {"name": os.path.basename(path), "db_type": dest.kind})] + \
            [("TRANSFER_TABLE", {"table": t["name"], "rows": t["rows_dest"]}) for t in report["tables"]] + \
            [("TRANSFER_VERIFIED", {"tables": len(report["tables"])})]
        new_id = finalize_destination(dest, version, header, ctx.params.get("actor", "admin"), entries)
        return {"report": report, "db_id": new_id, "source_db_id": header["source_db_id"], "from_schema": version,
                "to_schema": CURRENT_VERSION, "dest": dest.public_params()}
    finally:
        dest.close()


@register("migrate", needs_secret=True)
def migrate_job(ctx: JobContext) -> dict:
    """Direct database -> database copy (source = current database, read only)."""
    src = ctx.db
    token = ctx.params["token"]
    version = structure_version(src)
    source = {"source_db_id": get_meta(src, "db_id"), "app_version": VERSION}
    dest = _dest(ctx)
    try:
        state = prepare_destination(dest, version, token, source["source_db_id"])
        if "filter" not in state:
            state["filter"] = {"own_job": ctx.id,
                               "max_id": {t: int(src.scalar(f"SELECT MAX(id) AS m FROM {t}") or 0) for t in DATA_TABLES}}
            set_meta(dest, "migration_state", state)
        flt = state["filter"]
        total = 0
        for t in DATA_TABLES:
            extra, args = _filter(t, flt)
            total += int(src.scalar(f"SELECT COUNT(*) AS n FROM {t} WHERE 1=1{extra}", args) or 0)
        _copy_tables(ctx, dest, version, _db_records(src, version, ctx, flt), state, total)
        ctx.progress(phase="verify_destination", force=True)
        cols = columns_at(version)
        expected, samples = [], {}
        rnd = random.Random(11)
        for t in DATA_TABLES:
            n, sha = table_checksum(src, t, cols[t], flt)
            expected.append({"name": t, "rows": n, "sha256": sha})
            extra, args = _filter(t, flt)
            ids = [r["id"] for r in src.query(f"SELECT id FROM {t} WHERE 1=1{extra} ORDER BY id LIMIT 5000", args)]
            pick = rnd.sample(ids, min(SAMPLE, len(ids))) if ids else []
            samples[t] = []
            for i in pick:
                r = src.query_one(f"SELECT {','.join(c.name for c in cols[t])} FROM {t} WHERE id=?", (i,))
                samples[t].append([canon_value(c.type, r[c.name]) for c in cols[t]])
        report = verify_destination(dest, version, expected, samples)
        if not report["ok"]:
            raise DBError("verification_failed")
        ctx.log("TRANSFER_VERIFIED", tables=len(report["tables"]))
        entries = [("MIGRATE_STARTED", {"src_db": source["source_db_id"], "db_type": dest.kind})] + \
            [("TRANSFER_TABLE", {"table": t["name"], "rows": t["rows_dest"]}) for t in report["tables"]] + \
            [("TRANSFER_VERIFIED", {"tables": len(report["tables"])})]
        new_id = finalize_destination(dest, version, source, ctx.params.get("actor", "admin"), entries)
        return {"report": report, "db_id": new_id, "source_db_id": source["source_db_id"], "from_schema": version,
                "to_schema": CURRENT_VERSION, "dest": dest.public_params()}
    finally:
        dest.close()


def export_name(db_id: str) -> str:
    return f"twincheck-export-{db_id}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.tcarc"
