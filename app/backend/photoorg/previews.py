"""Previews for the review screen.

Browser-friendly originals are streamed as they are.  Other formats get a
temporary preview in /state/cache/previews/<owner>/ (deletable at any time):
H.264/AAC MP4 for videos (HEVC, MOV, AVI...), JPEG for HEIC/RAW/TIFF.  The
original is never modified.
"""
from __future__ import annotations

import os
import subprocess
import threading

from .fsutil import BROWSER_IMAGE, ext_of

BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
_running: set[str] = set()
_lock = threading.Lock()


def cache_path(cache_dir, owner_id: str, sha: str, kind: str) -> str:
    d = os.path.join(str(cache_dir), "previews", owner_id)
    return os.path.join(d, f"{sha}.{'mp4' if kind == 'video' else 'jpg'}")


def probe_codec(ffmpeg: str, path: str) -> str | None:
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe") if os.path.dirname(ffmpeg) else "ffprobe"
    try:
        res = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, timeout=30, check=False,
                             env={"PATH": os.environ.get("PATH", "/usr/bin"), "TMPDIR": "/tmp", "HOME": "/tmp"})
        return res.stdout.decode().strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def needs_preview(f: dict, ffmpeg: str) -> bool:
    ext = ext_of(f["name"])
    if f["media_kind"] == "image":
        return ext not in BROWSER_IMAGE
    if f["media_kind"] == "video":
        if ext not in ("mp4", "m4v", "webm"):
            return True
        return probe_codec(ffmpeg, f["cur_path"]) not in BROWSER_VIDEO_CODECS
    return False


_ENCODER: list = []


def h264_encoder(ffmpeg: str) -> str:
    """libx264 (Alpine's ffmpeg) or another available H.264 encoder."""
    if not _ENCODER:
        found = "libx264"
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, timeout=30, check=False).stdout.decode()
            for name in ("libx264", "libopenh264"):
                if f" {name} " in out:
                    found = name
                    break
        except (OSError, subprocess.TimeoutExpired):
            pass
        _ENCODER.append(found)
    return _ENCODER[0]


def _generate(ffmpeg: str, src: str, dst: str, kind: str, height: int, crf: int, on_done) -> None:
    tmp = dst + ".part"
    os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
    if kind == "video":
        args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", f"scale=-2:'min({height},ih)'", "-c:v", h264_encoder(ffmpeg)] + (["-preset", "veryfast", "-crf", str(crf)] if h264_encoder(ffmpeg) == "libx264" else ["-b:v", "2M"]) + [
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-f", "mp4", tmp]
    else:
        args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-frames:v", "1",
                "-vf", "scale='min(1920,iw)':-2", "-f", "image2", "-c:v", "mjpeg", tmp]
    try:
        res = subprocess.run(args, capture_output=True, timeout=3600, check=False,
                             env={"PATH": os.environ.get("PATH", "/usr/bin"), "TMPDIR": "/tmp", "HOME": "/tmp"})
        if res.returncode == 0 and os.path.getsize(tmp) > 0:
            os.replace(tmp, dst)
            if on_done:
                on_done()
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
        with _lock:
            _running.discard(dst)


def ensure(ffmpeg: str, cache_dir, f: dict, height: int = 720, crf: int = 28, on_done=None) -> tuple[str, str | None]:
    """-> ("original", None) | ("ready", path) | ("pending", None)"""
    if not needs_preview(f, ffmpeg):
        return "original", None
    dst = cache_path(cache_dir, f["owner_id"], f["sha256"] or str(f["id"]), f["media_kind"])
    if os.path.exists(dst):
        return "ready", dst
    with _lock:
        if dst not in _running:
            _running.add(dst)
            threading.Thread(target=_generate, args=(ffmpeg, f["cur_path"], dst, f["media_kind"], height, crf, on_done),
                             daemon=True).start()
    return "pending", None


def prune(cache_dir, max_bytes: int) -> None:
    root = os.path.join(str(cache_dir), "previews")
    files = []
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
                files.append((st.st_atime, st.st_size, p))
            except OSError:
                pass
    total = sum(s for _, s, _ in files)
    for _, size, p in sorted(files):
        if total <= max_bytes:
            break
        try:
            os.unlink(p)
            total -= size
        except OSError:
            pass
