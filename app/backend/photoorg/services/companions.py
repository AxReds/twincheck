"""Linked files that travel with a photo or video: XMP/AAE/Google Takeout JSON
sidecars and the video half of a Live Photo."""
from __future__ import annotations

from ..fsutil import ext_of, media_kind

RAW_FIRST = ("dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "srw", "pef", "heic", "heif")


def stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def group_directory(names: list[str]) -> tuple[dict[str, list[str]], set[str]]:
    """Return (primary name -> companion names, set of names that are companions)."""
    lower = {n.lower(): n for n in names}
    media = sorted([n for n in names if media_kind(n) in ("image", "video")],
                   key=lambda n: (ext_of(n) not in RAW_FIRST, media_kind(n) != "image", n))
    groups: dict[str, list[str]] = {n: [] for n in media}
    taken: set[str] = set()
    # Live Photos: image + same-stem MOV/MP4
    for n in media:
        if media_kind(n) != "image":
            continue
        for vext in ("mov", "mp4"):
            v = lower.get(f"{stem(n)}.{vext}".lower())
            if v and v not in taken and v in groups and v != n:
                groups[n].append(v)
                taken.add(v)
    for n in media:
        if n in taken:
            continue
        s = stem(n)
        for cand in (f"{n}.xmp", f"{s}.xmp", f"{n}.json", f"{n}.supplemental-metadata.json", f"{s}.json", f"{s}.aae", f"{n}.aae"):
            real = lower.get(cand.lower())
            if real and real not in taken and media_kind(real) == "sidecar":
                groups[n].append(real)
                taken.add(real)
    for t in taken:
        groups.pop(t, None)
    return groups, taken


def renamed_companion(companion: str, old_primary: str, new_primary: str) -> str:
    """Keep the companion's naming pattern when the primary gets a suffix."""
    if companion.lower().startswith(old_primary.lower()):
        return new_primary + companion[len(old_primary):]
    old_s, new_s = stem(old_primary), stem(new_primary)
    if companion.lower().startswith(old_s.lower()):
        return new_s + companion[len(old_s):]
    return companion
