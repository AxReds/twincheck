"""Scan job: index files (walk) and compute SHA-256 (hash).

Read-only on disk; resumes automatically after a restart.  Hashes are reused
when path, size and modification time are unchanged.
"""
from __future__ import annotations

import fnmatch
import json
import os
import stat

from ..db.schema import utcnow
from ..fsutil import ext_of, is_within, media_kind, path_hash, sha256_file
from ..jobs import JobContext, register

BATCH = 200


def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns) or name.startswith(".twincheck-")


def iter_dirs(root: str, patterns: list[str], ctx: JobContext | None = None):
    """Sorted depth-first walk without following symbolic links.
    Yields (relative component tuple, absolute dir, [file entries])."""
    stack = [()]
    while stack:
        rel = stack.pop()
        d = os.path.join(root, *rel)
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as exc:
            if ctx:
                ctx.log("FILE_UNREADABLE", level="WARNING", path=d, error=exc.strerror or str(exc.errno))
            continue
        subdirs, files = [], []
        for e in entries:
            if _excluded(e.name, patterns):
                continue
            try:
                if e.is_symlink():
                    if ctx:
                        ctx.log("SYMLINK_SKIPPED", path=e.path)
                    continue
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(e.name)
                elif e.is_file(follow_symlinks=False):
                    files.append(e)
            except OSError:
                continue
        yield rel, d, files
        for name in reversed(subdirs):
            stack.append(rel + (name,))


def index_dir(ctx: JobContext, source_id: str, files: list, scan_job: int, counts: dict | None = None) -> int:
    db = ctx.db
    n = 0
    now = utcnow()
    with db.transaction():
        for e in files:
            kind = media_kind(e.name)
            if kind == "other":
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            p = e.path
            ph = path_hash(p)
            row = db.query_one("SELECT id, size, mtime_ns, inode, device, present FROM files WHERE owner_id=? AND cur_path_hash=?", (ctx.owner_id, ph))
            if row:
                if (row["size"] != st.st_size or row["mtime_ns"] != st.st_mtime_ns or not row["present"]
                        or row["inode"] is not None and (row["inode"], row["device"]) != (st.st_ino, st.st_dev)):
                    if counts is not None:
                        counts["changed"] += 1
                    db.execute("UPDATE files SET size=?, mtime_ns=?, sha256=NULL, phase_hash='pending', phase_meta='pending', "
                               "present=1, last_scan_job=?, source_id=?, inode=?, device=?, file_changed_at=?, updated_at=? WHERE id=?",
                               (st.st_size, st.st_mtime_ns, scan_job, source_id, st.st_ino, st.st_dev, now, now, row["id"]))
                else:
                    if counts is not None:
                        counts["unchanged"] += 1
                    db.execute("UPDATE files SET present=1, last_scan_job=?, source_id=?, inode=?, device=? WHERE id=?",
                               (scan_job, source_id, st.st_ino, st.st_dev, row["id"]))
            else:
                if counts is not None:
                    counts["new"] += 1
                db.execute(
                    "INSERT INTO files (owner_id, source_id, orig_path, orig_path_hash, cur_path, cur_path_hash, name, ext, size, "
                    "mtime_ns, media_kind, status, phase_scan, phase_hash, phase_meta, phase_org, present, last_scan_job, first_seen, updated_at, inode, device, file_changed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ctx.owner_id, source_id, p, ph, p, ph, e.name, ext_of(e.name), st.st_size, st.st_mtime_ns, kind, "indexed",
                     "done", "pending", "pending", "pending", 1, scan_job, now, now, st.st_ino, st.st_dev, now),
                )
            n += 1
    return n


@register("scan", auto_resume=True)
def scan_job(ctx: JobContext) -> dict:
    roots: list[str] = ctx.params["roots"]
    source_id: str = ctx.params["source_id"]
    partial = ctx.params.get("partial_scan", False)
    patterns = ctx.app.settings_section("scan").get("exclude", [])
    cp = ctx.checkpoint
    phase = cp.get("phase", "walk")
    indexed = cp.get("indexed", 0)
    counts = dict(cp.get("counts") or {"new": 0, "changed": 0, "unchanged": 0})

    if phase == "walk":
        for ri, root in enumerate(roots):
            if ri < cp.get("root_index", 0) or not os.path.isdir(root):
                continue
            last = tuple(cp.get("last_dir") or ()) if ri == cp.get("root_index", 0) else ()
            for rel, d, files in iter_dirs(root, patterns, ctx):
                ctx.check()
                if last and rel <= last:
                    continue
                if files:
                    indexed += index_dir(ctx, source_id, files, ctx.id, counts)
                ctx.progress(done=indexed, total=0, phase="walk")
                ctx.save_checkpoint(phase="walk", root_index=ri, last_dir=list(rel), indexed=indexed, counts=counts)
            cp = ctx.checkpoint
            ctx.save_checkpoint(phase="walk", root_index=ri + 1, last_dir=[], indexed=indexed)
        ctx.save_checkpoint(phase="missing")
        phase = "missing"

    missing = cp.get("missing", 0)
    if phase == "missing":
        db = ctx.db
        rows = db.query("SELECT id, cur_path FROM files WHERE owner_id=? AND source_id=? AND present=1 AND "
                        "(last_scan_job IS NULL OR last_scan_job <> ?)", (ctx.owner_id, source_id, ctx.id))
        with db.transaction():
            for r in rows:
                if partial and not any(is_within(r["cur_path"], root) for root in roots):
                    continue
                if not os.path.lexists(r["cur_path"]):
                    db.execute("UPDATE files SET present=0, file_changed_at=?, updated_at=? WHERE id=?", (utcnow(), utcnow(), r["id"]))
                    ctx.log("FILE_MISSING", level="WARNING", path=r["cur_path"])
                    missing += 1
                else:
                    # still present but outside the scanned roots (e.g. excluded pattern)
                    db.execute("UPDATE files SET last_scan_job=? WHERE id=?", (ctx.id, r["id"]))
        ctx.save_checkpoint(phase="hash", missing=missing, last_id=0)
        phase = "hash"

    hashed = cp.get("hashed", 0)
    unreadable = int(ctx.checkpoint.get("unreadable") or 0)
    hash_scope = "owner_id=? AND source_id=? AND present=1" + (" AND last_scan_job=?" if partial else "")
    hash_args = (ctx.owner_id, source_id) + ((ctx.id,) if partial else ())
    if phase == "hash":
        db = ctx.db
        if isinstance(ctx, PipelineContext):
            ctx.phase_event("scan", False, indexed, found=indexed, removed=missing, **counts)
        total = int(db.scalar(f"SELECT COUNT(*) AS n FROM files WHERE {hash_scope} AND phase_hash<>'done'",
                              hash_args) or 0) + hashed
        candidates = int(ctx.checkpoint.get("candidates") or total)
        ctx.save_checkpoint(candidates=candidates)
        if isinstance(ctx, PipelineContext):
            ctx.phase_event("hash", True, candidates, candidates=candidates)
        ctx.progress(done=hashed, total=total, phase="hash", force=True)
        last_id = ctx.checkpoint.get("last_id", 0)
        while True:
            ctx.check()
            rows = db.query(f"SELECT id, cur_path, size, mtime_ns FROM files WHERE {hash_scope} "
                            "AND phase_hash<>'done' AND id>? ORDER BY id LIMIT ?", hash_args + (last_id, BATCH))
            if not rows:
                break
            for r in rows:
                ctx.check()
                last_id = r["id"]
                try:
                    st = os.lstat(r["cur_path"])
                    if st.st_size != r["size"] or st.st_mtime_ns != r["mtime_ns"]:
                        db.execute("UPDATE files SET size=?, mtime_ns=? WHERE id=?", (st.st_size, st.st_mtime_ns, r["id"]))
                    digest = sha256_file(r["cur_path"], stop=ctx.check)
                    st2 = os.lstat(r["cur_path"])
                    if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns:
                        continue  # changed while hashing: retried at next scan
                except OSError as exc:
                    ctx.log("FILE_UNREADABLE", level="WARNING", path=r["cur_path"], error=exc.strerror or str(exc.errno))
                    unreadable += 1
                    continue
                db.execute("UPDATE files SET sha256=?, phase_hash='done', hashed_at=?, updated_at=? WHERE id=?",
                           (digest, utcnow(), utcnow(), r["id"]))
                hashed += 1
                ctx.progress(done=hashed, total=total, phase="hash")
            ctx.save_checkpoint(phase="hash", last_id=last_id, hashed=hashed, unreadable=unreadable)
    ready = int(ctx.db.scalar(f"SELECT COUNT(*) AS n FROM files WHERE {hash_scope} "
                              "AND phase_hash='done' AND sha256 IS NOT NULL", hash_args) or 0)
    unhashed = int(ctx.db.scalar(f"SELECT COUNT(*) AS n FROM files WHERE {hash_scope} "
                                 "AND (phase_hash<>'done' OR sha256 IS NULL)", hash_args) or 0)
    result = {"indexed": indexed, "found": indexed, "missing": missing, "removed": missing, "hashed": hashed,
              "reused": max(0, ready - hashed), "unhashed": unhashed, "unreadable": unreadable,
              "candidates": ctx.checkpoint.get("candidates", 0), **counts}
    if isinstance(ctx, PipelineContext):
        ctx.phase_event("hash", False, hashed, candidates=result["candidates"], hashed=hashed,
                        reused=result["reused"], unreadable=unreadable)
        return result
    return {key: result[key] for key in ("indexed", "missing", "hashed", "reused", "unhashed")}


def analysis_phase_event(ctx: JobContext, phase: str, started: bool, count: int, **counts) -> None:
    event = "ANALYSIS_PHASE_STARTED" if started else "ANALYSIS_PHASE_COMPLETED"
    with ctx.db.transaction():
        previous = ctx.db.query("SELECT params FROM run_log WHERE job_id=? AND event=?", (ctx.id, event))
        if any(json.loads(row["params"] or "{}").get("phase") == phase for row in previous):
            return
        summary = f"{count} item{'s' if count != 1 else ''}"
        if not started:
            if phase == "hash":
                summary = f"{counts['hashed']} hashed, {counts['reused']} reused, {counts['unreadable']} unreadable"
            elif phase == "capture_dates":
                summary = f"{counts['read']} extracted, {counts['reused']} reused, {counts['unknown']} unknown"
            elif phase == "scan":
                summary = ", ".join(f"{counts[key]} {key}" for key in ("found", "new", "changed", "unchanged", "removed"))
            elif phase == "duplicates":
                summary = ", ".join(f"{value} {key[:-1] if value == 1 and key.endswith('s') else key}"
                                    for key, value in counts.items())
            elif phase == "metadata":
                from . import access, review_state
                rows = ctx.db.query("SELECT r.*,f.cur_path FROM review_items r JOIN files f ON f.id=r.file_id "
                                    "AND f.owner_id=r.owner_id WHERE r.owner_id=? AND f.present=1 "
                                    "AND f.media_kind IN ('image','video') AND f.status<>'dup_moved'", (ctx.owner_id,))
                user = ctx.app.user(ctx.owner_id)
                reviews = review_state.active([row for row in rows if access.file_in_scope(
                    ctx.app.settings.data_dir.as_posix(), user, row)])
                counts["review"] = len(reviews)
                counts["pending"] = sum(review_state.status(row) != "decided" for row in reviews)
                counts["decided"] = len(reviews) - counts["pending"]
                fills = counts["fills"]
                summary = (f"{counts['analysed']} analysed, {fills} metadata completion{'s' if fills != 1 else ''} proposed, "
                           f"{counts['review']} flagged, {counts['pending']} to review, {counts['decided']} decided")
        ctx.log(event, phase=phase, phase_label=phase.replace("_", " "), count=count, summary=summary, **counts)


class PipelineContext:
    """A staged view of one persistent job checkpoint."""

    def __init__(self, parent: JobContext, stage: str, index: int, label: str):
        self.parent = parent
        self.stage = stage
        self.index = index
        self.label = label
        self.app = parent.app
        self.db = parent.db
        self.id = parent.id
        self.kind = parent.kind
        self.owner_id = parent.owner_id
        self.params = dict(parent.params)
        self.checkpoint = dict(parent.checkpoint.get("stages", {}).get(stage) or {})
        self.done = 0
        self.total = 0
        self.last_phase = None

    def check(self) -> None:
        self.parent.check()

    def progress(self, done=None, total=None, phase=None, force=False) -> None:
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total
        phase_name = phase or self.label
        if self.stage == "scan":
            if phase == "hash":
                phase_name, index = "hash", 2
            else:
                phase_name, index = "scan", 1
        else:
            index = self.index
        self.parent.progress(done=max(0, index - 1), total=5,
                            phase=f"{index}/5 {phase_name}", force=force)

    def phase_event(self, phase: str, started: bool, count: int, **counts) -> None:
        analysis_phase_event(self.parent, phase, started, count, **counts)

    def save_checkpoint(self, cp: dict | None = None, **updates) -> None:
        if cp is not None:
            self.checkpoint = cp
        self.checkpoint.update(updates)
        stages = dict(self.parent.checkpoint.get("stages") or {})
        stages[self.stage] = dict(self.checkpoint)
        self.parent.save_checkpoint(stages=stages, current=self.stage)

    def log(self, event: str, level: str = "INFO", **params):
        return self.parent.log(event, level=level, **params)


@register("analyze_library", auto_resume=True)
def analyze_library(ctx: JobContext) -> dict:
    """Read-only scan/hash/date/dedupe/metadata chain with resumable stages."""
    from . import access
    from .dedupe import dedupe_analyze
    from .metadata import ensure_capture_dates, metadata_analyze

    results = dict(ctx.checkpoint.get("results") or {})
    stages = dict(ctx.checkpoint.get("stages") or {})
    completed = set(ctx.checkpoint.get("completed") or [])
    resumed_phase = ctx.checkpoint.get("current")
    if resumed_phase:
        resume_stage = ctx.checkpoint.get("stages", {}).get(resumed_phase, {})
        ctx.log("ANALYSIS_RESUMED", phase=resumed_phase,
                position=json.dumps(resume_stage, ensure_ascii=False)[:200])

    def start_phase(name: str, count: int = 0):
        analysis_phase_event(ctx, name, True, count)

    def finish_phase(name: str, count: int = 0, **counts):
        analysis_phase_event(ctx, name, False, count, **counts)

    if "scan" not in completed:
        found = sum(1 for root in ctx.params["roots"] for _, _, entries in iter_dirs(
            root, ctx.app.settings_section("scan").get("exclude", [])) for e in entries if media_kind(e.name) != "other")
        start_phase("scan", found)
        scan_ctx = PipelineContext(ctx, "scan", 1, "scan")
        results["scan"] = scan_job(scan_ctx)
        completed.add("scan")
        ctx.save_checkpoint(stages=dict(ctx.checkpoint.get("stages") or {}), current="capture_dates",
                            completed=sorted(completed), results=results)
        stages = dict(ctx.checkpoint.get("stages") or {})

    if "capture_dates" not in completed:
        db = ctx.db
        user = ctx.app.user(ctx.owner_id)
        source_id = ctx.params.get("source_id")
        media = [row for row in db.query("SELECT * FROM files WHERE owner_id=? AND source_id=? AND present=1 "
                                        "AND media_kind IN ('image','video')", (ctx.owner_id, source_id))
                 if access.file_in_scope(ctx.app.settings.data_dir.as_posix(), user, row)]
        start_phase("capture_dates", len(media))
        cp = dict(stages.get("capture_dates") or {})
        last_id = int(cp.get("last_id") or 0)
        read = int(cp.get("read") or 0)
        while True:
            ctx.check()
            rows = db.query("SELECT * FROM files WHERE owner_id=? AND source_id=? AND present=1 "
                            "AND media_kind IN ('image','video') AND id>? ORDER BY id LIMIT 50",
                            (ctx.owner_id, source_id, last_id))
            rows = [row for row in rows if access.file_in_scope(ctx.app.settings.data_dir.as_posix(), user, row)]
            if not rows:
                maximum = db.query_one("SELECT MAX(id) AS id FROM files WHERE owner_id=? AND source_id=? AND id>?",
                                       (ctx.owner_id, source_id, last_id))
                if maximum and maximum["id"] is not None:
                    last_id = maximum["id"]
                    stages["capture_dates"] = {"last_id": last_id, "read": read}
                    ctx.save_checkpoint(stages=stages, current="capture_dates", completed=sorted(completed), results=results)
                    continue
                break
            read += ensure_capture_dates(ctx, rows)
            last_id = rows[-1]["id"]
            ctx.progress(done=read, total=0, phase="3/5 capture_dates")
            stages["capture_dates"] = {"last_id": last_id, "read": read}
            ctx.save_checkpoint(stages=stages, current="capture_dates", completed=sorted(completed), results=results)
        unknown = sum(1 for row in media if not db.scalar("SELECT chosen_date FROM files WHERE id=?", (row["id"],)))
        reused = max(0, len(media) - read)
        results["capture_dates"] = {"read": read, "reused": reused, "unknown": unknown}
        finish_phase("capture_dates", read, read=read, reused=reused, unknown=unknown)
        completed.add("capture_dates")
        ctx.save_checkpoint(stages=stages, current="duplicates", completed=sorted(completed), results=results)

    if "duplicates" not in completed:
        start_phase("duplicates", int(ctx.db.scalar("SELECT COUNT(*) FROM files WHERE owner_id=? AND source_id=? "
                                                   "AND present=1 AND media_kind IN ('image','video')", (ctx.owner_id, ctx.params["source_id"])) or 0))
        duplicate_ctx = PipelineContext(ctx, "duplicates", 4, "duplicates")
        results["duplicates"] = dedupe_analyze(duplicate_ctx)
        finish_phase("duplicates", results["duplicates"].get("groups", 0), **results["duplicates"])
        completed.add("duplicates")
        ctx.save_checkpoint(stages=dict(ctx.checkpoint.get("stages") or {}), current="metadata",
                            completed=sorted(completed), results=results)

    if "metadata" not in completed:
        start_phase("metadata", int(ctx.db.scalar("SELECT COUNT(*) FROM files WHERE owner_id=? AND source_id=? "
                                                 "AND present=1 AND media_kind IN ('image','video')", (ctx.owner_id, ctx.params["source_id"])) or 0))
        metadata_ctx = PipelineContext(ctx, "metadata", 5, "metadata")
        results["metadata"] = metadata_analyze(metadata_ctx)
        finish_phase("metadata", results["metadata"].get("analysed", 0), **results["metadata"])
        completed.add("metadata")
        ctx.save_checkpoint(stages=dict(ctx.checkpoint.get("stages") or {}), current="complete",
                            completed=sorted(completed), results=results)
    ctx.progress(done=5, total=5, phase="5/5 complete", force=True)
    return results
