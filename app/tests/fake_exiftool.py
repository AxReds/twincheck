#!/usr/bin/env python3
"""Stand-in for ExifTool used ONLY by the unit tests when the real binary is not
installed (the container test-suite uses the real ExifTool).

Tags live in a JSON database (env FAKE_EXIF_DB): {inode: {"Group:Tag": value}}.
Supported: ``-ver``; reading with ``-j ... -@ -`` (paths on stdin); writing
``-TAG=VALUE`` / ``-TAG#=VALUE`` / ``-TAG=`` with ``-wm cg`` (create only) and
``-P``.  A write appends a marker to the file so that its SHA-256 changes like
with a real metadata rewrite, and keeps mtime when -P is given.
"""
import json
import os
import sys


def key(path):
    """Tags follow the file (inode), like real embedded metadata does when a file is moved."""
    return str(os.stat(path).st_ino)


def db_path():
    # ExifTool is started with a minimal environment: the database sits next to this script
    return os.environ.get("FAKE_EXIF_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_exif.json")


def load():
    p = db_path()
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}


def save(db):
    with open(db_path(), "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False)


def main(argv):
    if argv == ["-ver"]:
        print("13.00")
        return 0
    db = load()
    if "-j" in argv:
        paths = [line.strip("\n") for line in sys.stdin.read().splitlines() if line.strip()]
        out = []
        for p in paths:
            if os.path.exists(p):
                out.append({"SourceFile": p, **db.get(key(p), {})})
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0
    path = argv[-1]
    create_only = "-wm" in argv
    keep = "-P" in argv
    tags = db.setdefault(key(path), {})
    for a in argv[:-1]:
        if not a.startswith("-") or "=" not in a:
            continue
        k, _, v = a[1:].partition("=")
        k = k.rstrip("#")
        if v == "":
            tags.pop(k, None)
            continue
        if create_only and k in tags:
            continue
        try:
            v = float(v) if k.startswith("GPS:GPSL") and "Ref" not in k else v
        except ValueError:
            pass
        tags[k] = v
        if k == "GPS:GPSLatitude" or k == "GPS:GPSLongitude":
            lat = tags.get("GPS:GPSLatitude")
            lon = tags.get("GPS:GPSLongitude")
            if lat is not None and lon is not None:
                tags["Composite:GPSPosition"] = f"{lat} {lon}"
    st = os.stat(path)
    with open(path, "ab") as fh:
        fh.write(b"\x00fake-exif-write")
    if keep:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    save(db)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
