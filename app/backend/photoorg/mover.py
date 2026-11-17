"""Journalled, never-overwriting file moves.

Protocol for every move:

1. the intent (src, dst, size, mtime, sha256) is committed to ``move_journal``;
2. the file is moved:
   * same volume: ``link(src, dst)`` (fails if dst exists -> never overwrites)
     followed by ``unlink(src)``; if hard links are not supported, an exclusive
     rename guarded by an existence check is used;
   * different volume: copy to a hidden temp file in the destination directory,
     fsync, verify SHA-256, copy timestamps, link into place, then remove the
     original;
3. the completion is committed.

If the process stops between 1 and 3, ``recover()`` inspects the disk and
completes or rolls back the operation; the outcome is never ambiguous.
"""
from __future__ import annotations

import contextlib
import errno
import os
import secrets
import shutil

from .db.manager import log
from .db.schema import utcnow
from .fsutil import sha256_file


class MoveError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CrossDevice(Exception):
    pass


def _fsync_dir(path: str) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _link_or_rename(src: str, dst: str) -> None:
    try:
        os.link(src, dst, follow_symlinks=False)
    except FileExistsError:
        raise MoveError("destination_exists") from None
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise CrossDevice() from None
        if exc.errno in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.ENOSYS):
            if os.path.lexists(dst):
                raise MoveError("destination_exists") from None
            os.rename(src, dst)
            return
        raise
    os.unlink(src)


def _copy_verify(src: str, dst: str, expected: str) -> None:
    d = os.path.dirname(dst)
    tmp = os.path.join(d, f".photoorg-{secrets.token_hex(6)}.part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(src, "rb") as fin, os.fdopen(fd, "wb", closefd=False) as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
            fout.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if sha256_file(tmp) != expected:
            raise MoveError("copy_verify_failed")
        shutil.copystat(src, tmp, follow_symlinks=False)
        try:
            os.link(tmp, dst)
        except FileExistsError:
            raise MoveError("destination_exists") from None
        except OSError:
            if os.path.lexists(dst):
                raise MoveError("destination_exists") from None
            os.rename(tmp, dst)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    _fsync_dir(d)
    os.unlink(src)


def move_file(db, *, job_id: int | None, owner_id: str, file_id: int | None, src: str, dst: str,
              sha256: str | None, op: str = "move", on_done=None) -> dict:
    """Move ``src`` to ``dst`` without ever overwriting. Returns journal info."""
    if os.path.lexists(dst):
        raise MoveError("destination_exists")
    st = os.lstat(src)
    if not os.path.isfile(src) or os.path.islink(src):
        raise MoveError("source_not_regular")
    dst_dir = os.path.dirname(dst)
    os.makedirs(dst_dir, exist_ok=True)
    # directories are compared (on overlayfs a file may report its lower layer's device);
    # link() returning EXDEV is the final authority
    cross = os.stat(dst_dir).st_dev != os.stat(os.path.dirname(src)).st_dev
    if not sha256:
        sha256 = sha256_file(src)
    kind = "copy_verify_delete" if cross else op
    with db.transaction():
        jid = db.insert(
            "INSERT INTO move_journal (job_id, owner_id, file_id, op, src, dst, sha256, size, mtime_ns, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, owner_id, file_id, kind, src, dst, sha256, st.st_size, st.st_mtime_ns, "intent", utcnow()),
        )
    try:
        if not cross:
            try:
                _link_or_rename(src, dst)
            except CrossDevice:
                cross = True
                db.execute("UPDATE move_journal SET op='copy_verify_delete' WHERE id=?", (jid,))
        if cross:
            _copy_verify(src, dst, sha256)
        _fsync_dir(dst_dir)
        _fsync_dir(os.path.dirname(src))
    except MoveError as exc:
        db.execute("UPDATE move_journal SET state='failed', error=?, completed_at=? WHERE id=?", (exc.code, utcnow(), jid))
        raise
    except OSError as exc:
        db.execute("UPDATE move_journal SET state='failed', error=?, completed_at=? WHERE id=?", (f"os_error_{exc.errno}", utcnow(), jid))
        raise MoveError("os_error") from None
    # completion, index update and log are committed atomically
    with db.transaction():
        db.execute("UPDATE move_journal SET state='done', completed_at=? WHERE id=?", (utcnow(), jid))
        if on_done:
            on_done()
        log(db, "FILE_COPIED_VERIFIED" if cross else "FILE_MOVED", job_id=job_id, owner_id=owner_id, src=src, dst=dst)
    return {"journal_id": jid, "cross_volume": cross}


def recover(db) -> list[dict]:
    """Resolve journal entries left in state 'intent' after a crash."""
    results = []
    rows = db.query("SELECT * FROM move_journal WHERE state='intent' ORDER BY id")
    for r in rows:
        src, dst = r["src"], r["dst"]
        s_ex, d_ex = os.path.lexists(src), os.path.lexists(dst)
        state = None
        if s_ex and not d_ex:
            state = "rolled_back"            # nothing happened
        elif d_ex and not s_ex:
            state = "done"                   # move completed, completion not recorded
        elif s_ex and d_ex:
            try:
                same_inode = os.stat(src).st_ino == os.stat(dst).st_ino and os.stat(src).st_dev == os.stat(dst).st_dev
            except OSError:
                same_inode = False
            if same_inode:
                os.unlink(src)               # link done, unlink pending
                state = "done"
            elif r["sha256"] and os.path.getsize(dst) == r["size"] and sha256_file(dst) == r["sha256"]:
                os.unlink(src)               # verified copy done, delete pending
                state = "done"
            else:
                state = "rolled_back"        # dst is an unrelated file: keep both, never delete
        else:
            state = "failed"                 # neither exists: report, never guess
        db.execute("UPDATE move_journal SET state=?, completed_at=?, error=? WHERE id=?",
                   (state, utcnow(), "recovered", r["id"]))
        if state == "done" and r["file_id"]:
            from .fsutil import path_hash
            db.execute("UPDATE files SET cur_path=?, cur_path_hash=?, updated_at=? WHERE id=?",
                       (dst, path_hash(dst), utcnow(), r["file_id"]))
        log(db, "MOVE_RECOVERED", job_id=r["job_id"], owner_id=r["owner_id"], state=state, src=src, dst=dst)
        results.append({"id": r["id"], "state": state})
    # temp files of interrupted cross-volume copies are left for the admin to inspect
    return results
