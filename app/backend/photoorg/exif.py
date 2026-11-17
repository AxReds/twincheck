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
import subprocess

COMMON = ["-charset", "filename=utf8", "-api", "LargeFileSupport=1"]


class ExifError(Exception):
    pass


class ExifTool:
    def __init__(self, path: str = "exiftool"):
        self.path = path

    def _run(self, args: list[str], stdin: str | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": "/tmp", "HOME": "/tmp", "LANG": "C.UTF-8"}
        try:
            return subprocess.run([self.path, *args], input=stdin.encode("utf-8") if stdin is not None else None,
                                  capture_output=True, timeout=timeout, env=env, check=False)
        except FileNotFoundError:
            raise ExifError("exiftool_missing") from None
        except subprocess.TimeoutExpired:
            raise ExifError("exiftool_timeout") from None

    def available(self) -> bool:
        try:
            return self._run(["-ver"], timeout=20).returncode == 0
        except ExifError:
            return False

    def read(self, paths: list[str]) -> dict[str, dict]:
        if not paths:
            return {}
        args = ["-j", "-G1", "-a", "-s", "-n", "-q", "-q", "-m", *COMMON, "-@", "-"]
        res = self._run(args, stdin="\n".join(paths) + "\n")
        out: dict[str, dict] = {}
        if res.stdout:
            try:
                for item in json.loads(res.stdout.decode("utf-8", "replace")):
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
        res = self._run(args)
        if res.returncode != 0:
            raise ExifError("exiftool_write_failed")
