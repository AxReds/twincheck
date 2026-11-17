"""File-system helpers: confinement, overlap checks, media types, mounts."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

IMAGE_EXT = {"jpg", "jpeg", "jpe", "png", "gif", "webp", "heic", "heif", "avif", "tif", "tiff", "bmp", "dng", "cr2",
             "cr3", "nef", "arw", "orf", "rw2", "raf", "srw", "pef", "jxl"}
VIDEO_EXT = {"mp4", "m4v", "mov", "3gp", "3g2", "avi", "mkv", "mts", "m2ts", "wmv", "mpg", "mpeg", "webm", "hevc"}
SIDECAR_EXT = {"xmp", "aae", "json"}
BROWSER_IMAGE = {"jpg", "jpeg", "jpe", "png", "gif", "webp", "avif", "bmp"}


class PathError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def media_kind(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in SIDECAR_EXT:
        return "sidecar"
    return "other"


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def path_hash(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8", "surrogateescape")).hexdigest()


def is_within(child: str | Path, parent: str | Path) -> bool:
    child, parent = str(child), str(parent).rstrip("/") or "/"
    return child == parent or child.startswith(parent + "/") or parent == "/"


def overlap(a: str, b: str) -> bool:
    return is_within(a, b) or is_within(b, a)


def clean_relative(rel: str) -> str:
    """Normalise a user-supplied path relative to a root, refusing traversal."""
    if rel is None:
        raise PathError("invalid_path")
    rel = rel.replace("\\", "/").strip()
    if "\x00" in rel:
        raise PathError("invalid_path")
    parts = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise PathError("path_traversal")
        parts.append(part)
    return "/".join(parts)


def confine(root: str | Path, candidate: str | Path, must_exist: bool = True) -> Path:
    """Return the real absolute path of ``candidate`` if it lies inside ``root``
    after resolving every symbolic link; raise PathError otherwise."""
    root_real = os.path.realpath(str(root))
    cand = str(candidate)
    if not os.path.isabs(cand):
        cand = os.path.join(root_real, clean_relative(cand))
    else:
        norm = os.path.normpath(cand)
        if ".." in cand.split("/"):
            raise PathError("path_traversal")
        cand = norm
    real = os.path.realpath(cand)
    if not is_within(real, root_real):
        raise PathError("outside_source")
    if must_exist and not os.path.exists(real):
        raise PathError("not_found")
    return Path(real)


def safe_stat(path: str):
    """lstat that refuses symbolic links (they are never followed)."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise PathError("symlink")
    return st


def sha256_file(path: str, chunk: int = 1024 * 1024, stop=None) -> str:
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(fd, "rb", closefd=False) as fh:
            while True:
                if stop:
                    stop()
                b = fh.read(chunk)
                if not b:
                    break
                h.update(b)
    finally:
        os.close(fd)
    return h.hexdigest()


def same_bytes(a: str, b: str, chunk: int = 1024 * 1024) -> bool:
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(chunk), fb.read(chunk)
            if x != y:
                return False
            if not x:
                return True


# --- mounts ------------------------------------------------------------------------

def _unescape(s: str) -> str:
    return s.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def mounts(mountinfo: str = "/proc/self/mountinfo") -> list[dict]:
    out = []
    try:
        with open(mountinfo, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                f = left.split()
                r = right.split()
                if len(f) < 6 or len(r) < 1:
                    continue
                out.append({"mount_point": _unescape(f[4]), "options": f[5].split(","), "fstype": r[0],
                            "source": r[1] if len(r) > 1 else ""})
    except OSError:
        pass
    return out


def mount_for(path: str, table: list[dict] | None = None) -> dict | None:
    table = mounts() if table is None else table
    real = os.path.realpath(path)
    best = None
    for m in table:
        if is_within(real, m["mount_point"]) and (best is None or len(m["mount_point"]) > len(best["mount_point"])):
            best = m
    return best


def disk_usage(path: str) -> dict | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    return {"total": total, "used": used, "free": free,
            "used_pct": round(100.0 * used / total, 1) if total else None,
            "free_pct": round(100.0 * free / total, 1) if total else None,
            "read_only": bool(st.f_flag & os.ST_RDONLY)}


def same_device(a: str, b: str) -> bool | None:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return None
