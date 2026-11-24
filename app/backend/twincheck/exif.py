"""Thin ExifTool wrapper (EXIF, IPTC, XMP, QuickTime).

* reading: ``-j -G1 -a -n`` (numeric values, family-1 groups), file list on stdin
  (``-@ -``) so names are never interpreted by a shell;
* writing: ``-wm cg`` (create new tags only: an existing value is never
  overwritten), ``-P`` (keep file-system modification time),
  ``-overwrite_original`` (no ``_original`` copies left behind). Pixels and
  encoding are never touched: ExifTool only rewrites metadata blocks.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time

COMMON = ["-charset", "filename=utf8", "-api", "LargeFileSupport=1"]


class ExifError(Exception):
    pass


class ExifTool:
    def __init__(self, path: str = "exiftool", persistent: bool = False):
        self.path = path
        self.persistent = persistent
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._execute_id = 0

    def _run(self, args: list[str], stdin: str | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": "/tmp", "HOME": "/tmp", "LANG": "C.UTF-8"}
        try:
            return subprocess.run([self.path, *args], input=stdin.encode("utf-8") if stdin is not None else None,
                                  capture_output=True, timeout=timeout, env=env, check=False)
        except FileNotFoundError:
            raise ExifError("exiftool_missing") from None
        except subprocess.TimeoutExpired:
            raise ExifError("exiftool_timeout") from None

    def _start(self) -> subprocess.Popen:
        try:
            self._process = subprocess.Popen(
                [self.path, "-stay_open", "True", "-@", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": "/tmp",
                     "HOME": "/tmp", "LANG": "C.UTF-8"},
            )
            return self._process
        except FileNotFoundError:
            self._process = None
            raise ExifError("exiftool_missing") from None

    def _persistent_run(self, args: list[str], timeout: int = 600) -> bytes:
        with self._lock:
            for attempt in range(2):
                if self._process and self._process.poll() is not None:
                    self.close_locked()
                process = self._process or self._start()
                self._execute_id += 1
                marker = str(self._execute_id)
                try:
                    assert process.stdin and process.stdout
                    payload = "".join(arg + "\n" for arg in args) + f"-execute{marker}\n"
                    process.stdin.write(payload.encode("utf-8"))
                    process.stdin.flush()
                    deadline_at = time.monotonic() + timeout
                    output = bytearray()
                    ready_marker = ("{ready" + marker + "}\n").encode()
                    while True:
                        remaining = deadline_at - time.monotonic()
                        if remaining <= 0:
                            self.close_locked()
                            raise ExifError("exiftool_timeout")
                        ready, _, _ = select.select([process.stdout], [], [], min(1, remaining))
                        if not ready:
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise BrokenPipeError("exiftool closed")
                        output.extend(chunk)
                        pos = output.find(ready_marker)
                        if pos >= 0:
                            return bytes(output[:pos])
                except (BrokenPipeError, OSError, ValueError):
                    self.close_locked()
                    if attempt:
                        raise ExifError("exiftool_failed") from None
            raise ExifError("exiftool_timeout")

    def process_id(self) -> int | None:
        return self._process.pid if self._process and self._process.poll() is None else None

    def close_locked(self) -> None:
        process = self._process
        self._process = None
        if not process:
            return
        try:
            if process.poll() is None:
                if process.stdin:
                    process.stdin.write(b"-stay_open\nFalse\n")
                    process.stdin.flush()
                process.wait(timeout=5)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()

    def close(self) -> None:
        with self._lock:
            self.close_locked()

    def available(self) -> bool:
        try:
            return self._run(["-ver"], timeout=20).returncode == 0
        except ExifError:
            return False

    def read(self, paths: list[str]) -> dict[str, dict]:
        if not paths:
            return {}
        args = ["-j", "-G1", "-a", "-s", "-n", "-q", "-q", "-m", *COMMON, *paths]
        if self.persistent:
            stdout = self._persistent_run(args)
        else:
            args += ["-@", "-"]
            stdout = self._run(args, stdin="\n".join(paths) + "\n").stdout
        out: dict[str, dict] = {}
        if stdout:
            try:
                for item in json.loads(stdout.decode("utf-8", "replace")):
                    out[item.get("SourceFile")] = item
            except ValueError:
                raise ExifError("exiftool_bad_output") from None
        return out

    def write(self, path: str, tags: dict[str, object], create_only: bool = True) -> None:
        """``tags`` value None deletes the tag (used only by undo)."""
        args = ["-P", "-overwrite_original", "-m", "-q", *COMMON]
        if create_only:
            args += ["-wm", "cg"]
        for tag, value in tags.items():
            if value is None:
                args.append(f"-{tag}=")
            else:
                args.append(f"-{tag}#={value}" if isinstance(value, (int, float)) else f"-{tag}={value}")
        if not os.path.isabs(path):
            raise ExifError("relative_path")  # absolute paths can never be mistaken for options
        args.append(path)
        if self.persistent:
            self._persistent_run(args)
        else:
            res = self._run(args)
            if res.returncode != 0:
                raise ExifError("exiftool_write_failed")
