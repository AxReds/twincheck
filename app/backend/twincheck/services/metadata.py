"""Metadata normalisation WITHOUT inventing values.

* A missing field is completed only by copying it from a reliable source of
  the same file or its linked files (equivalent EXIF/XMP/QuickTime tags, .xmp
  sidecar, Google Takeout .json).
* Deduced values (date from the file name or modification time) are only
  *proposals* and go to review.  Position, device and time zone are never
  guessed.
* An existing value is never overwritten (ExifTool ``-wm cg``); conflicts
  between sources go to review.
* QuickTime dates are UTC: a local time is never written into them (and vice
  versa) unless the offset is known; ambiguity is flagged.
* Before every change the original values are stored in the database.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from ..db.schema import utcnow
from ..exif import ExifError, ExifTool
from ..fsutil import path_hash, sha256_file
from ..jobs import JobContext, register
from .companions import group_directory

FIELDS = ["datetime_original", "create_date", "modify_date", "offset", "gps", "make", "model", "orientation",
          "dimensions", "duration", "title", "description"]
WRITABLE = {"datetime_original", "create_date", "offset", "gps", "make", "model", "orientation", "title", "description"}
EXIF_WRITABLE_EXT = {"jpg", "jpeg", "jpe", "tif", "tiff", "heic", "heif", "dng", "webp", "avif", "jxl"}
XMP_ONLY_EXT = {"png", "gif"}
QT_WRITABLE_EXT = {"mov", "mp4", "m4v", "3gp", "3g2"}

# field -> [(tag, semantic)]; semantic: local | utc | tz (has offset) | plain
READ_TAGS = {
    "datetime_original": [("ExifIFD:DateTimeOriginal", "local"), ("XMP-exif:DateTimeOriginal", "local"),
                          ("XMP-photoshop:DateCreated", "local"), ("Keys:CreationDate", "tz"),
                          ("UserData:DateTimeOriginal", "local")],
    "create_date": [("ExifIFD:CreateDate", "local"), ("XMP-xmp:CreateDate", "local"), ("QuickTime:CreateDate", "utc"),
                    ("Track*:MediaCreateDate", "utc"), ("Track*:TrackCreateDate", "utc")],
    "modify_date": [("IFD0:ModifyDate", "local"), ("XMP-xmp:ModifyDate", "local"), ("QuickTime:ModifyDate", "utc")],
    "offset": [("ExifIFD:OffsetTimeOriginal", "plain"), ("ExifIFD:OffsetTime", "plain")],
    "gps": [("Composite:GPSPosition", "plain"), ("Keys:GPSCoordinates", "plain"), ("UserData:GPSCoordinates", "plain"),
            ("XMP-exif:GPSPosition", "plain")],
    "make": [("IFD0:Make", "plain"), ("XMP-tiff:Make", "plain"), ("Keys:Make", "plain"), ("UserData:Make", "plain")],
    "model": [("IFD0:Model", "plain"), ("XMP-tiff:Model", "plain"), ("Keys:Model", "plain"), ("UserData:Model", "plain")],
    "orientation": [("IFD0:Orientation", "plain"), ("XMP-tiff:Orientation", "plain")],
    "dimensions": [("Composite:ImageSize", "plain")],
    "duration": [("QuickTime:Duration", "plain"), ("Composite:Duration", "plain")],
    "title": [("XMP-dc:Title", "plain"), ("IPTC:ObjectName", "plain"), ("Keys:Title", "plain")],
    "description": [("XMP-dc:Description", "plain"), ("IFD0:ImageDescription", "plain"), ("IPTC:Caption-Abstract", "plain"),
                    ("Keys:Description", "plain")],
}


# --- value normalisation ------------------------------------------------------------
DT_RE = re.compile(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+\-]\d{2}:?\d{2})?$")


def parse_dt(value) -> tuple[str, str | None] | None:
    """-> ("YYYY-MM-DDTHH:MM:SS", offset or None). Rejects 0000:00:00 placeholders."""
    if value is None:
        return None
    m = DT_RE.match(str(value).strip())
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    if y == "0000" or mo == "00" or d == "00":
        return None
    try:
        datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    except ValueError:
        return None
    off = None
    if tz:
        off = "+00:00" if tz == "Z" else (tz if ":" in tz else tz[:3] + ":" + tz[3:])
    return f"{y}-{mo}-{d}T{h}:{mi}:{s or '00'}", off


def to_utc(local: str, offset: str) -> str:
    sign = 1 if offset[0] == "+" else -1
    hh, mm = int(offset[1:3]), int(offset[4:6])
    dt = datetime.strptime(local, "%Y-%m-%dT%H:%M:%S") - sign * timedelta(hours=hh, minutes=mm)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def exif_dt(iso: str) -> str:
    return iso.replace("-", ":", 2).replace("T", " ")


def parse_gps(value) -> tuple[float, float] | None:
    if value is None:
        return None
    nums = re.findall(r"-?\d+(?:\.\d+)?", str(value))
    if len(nums) < 2:
        return None
    lat, lon = float(nums[0]), float(nums[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return round(lat, 6), round(lon, 6)


def _get(tags: dict, pattern: str):
    if "*" not in pattern:
        return tags.get(pattern)
    group, name = pattern.split(":", 1)
    prefix = group.rstrip("*")
    for k, v in tags.items():
        g, _, n = k.partition(":")
        if n == name and g.startswith(prefix):
            return v
    return None


def collect(tags: dict, source: str) -> dict[str, list[dict]]:
    """field -> candidates [{value, tag, source, semantic, offset}]"""
    out: dict[str, list[dict]] = {f: [] for f in FIELDS}
    for field, specs in READ_TAGS.items():
        for tag, sem in specs:
            raw = _get(tags, tag)
            if raw in (None, ""):
                continue
            if field in ("datetime_original", "create_date", "modify_date"):
                p = parse_dt(raw)
                if not p:
                    continue
                local, off = p
                if sem == "tz" and not off:
                    sem = "local"
                out[field].append({"value": local, "offset": off, "tag": tag, "source": source, "semantic": sem})
            elif field == "gps":
                g = parse_gps(raw)
                if g:
                    out[field].append({"value": f"{g[0]:.6f},{g[1]:.6f}", "tag": tag, "source": source, "semantic": "plain"})
            else:
                out[field].append({"value": str(raw).strip(), "tag": tag, "source": source, "semantic": "plain"})
    return out


def takeout_candidates(path: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {f: [] for f in FIELDS}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return out
    src = "sidecar:" + os.path.basename(path)
    ts = (data.get("photoTakenTime") or {}).get("timestamp")
    if ts and str(ts).isdigit() and int(ts) > 0:
        utc = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        out["datetime_original"].append({"value": utc, "offset": None, "tag": "Takeout:photoTakenTime", "source": src, "semantic": "utc"})
    geo = data.get("geoDataExif") or data.get("geoData") or {}
    g = parse_gps(f"{geo.get('latitude')} {geo.get('longitude')}") if geo else None
    if g:
        out["gps"].append({"value": f"{g[0]:.6f},{g[1]:.6f}", "tag": "Takeout:geoData", "source": src, "semantic": "plain"})
    if (data.get("description") or "").strip():
        out["description"].append({"value": data["description"].strip(), "tag": "Takeout:description", "source": src, "semantic": "plain"})
    return out


# --- decisions ------------------------------------------------------------------------
def _distinct(cands: list[dict], key=lambda c: c["value"]) -> list[str]:
    seen = []
    for c in cands:
        k = key(c)
        if k not in seen:
            seen.append(k)
    return seen


def _dt_consistent(cands: list[dict]) -> bool:
    """Same instant for all candidates (UTC vs local tolerated only if the
    difference is a whole time-zone offset - then it is *not* a conflict)."""
    locals_ = _distinct([c for c in cands if c["semantic"] in ("local", "tz")])
    utcs = _distinct([c for c in cands if c["semantic"] == "utc"])
    if len(locals_) > 1 or len(utcs) > 1:
        return False
    if locals_ and utcs:
        a = datetime.strptime(locals_[0], "%Y-%m-%dT%H:%M:%S")
        b = datetime.strptime(utcs[0], "%Y-%m-%dT%H:%M:%S")
        diff = abs((a - b).total_seconds())
        return diff <= 14 * 3600 and diff % 900 == 0
    return True


def canonical_tags(field: str, kind: str, ext: str) -> list[str]:
    if kind == "image":
        exif = ext in EXIF_WRITABLE_EXT
        xmp = exif or ext in XMP_ONLY_EXT
        table = {
            "datetime_original": ["ExifIFD:DateTimeOriginal"] if exif else (["XMP-exif:DateTimeOriginal"] if xmp else []),
            "create_date": ["ExifIFD:CreateDate"] if exif else (["XMP-xmp:CreateDate"] if xmp else []),
            "offset": ["ExifIFD:OffsetTimeOriginal"] if exif else [],
            "gps": ["GPS:GPSLatitude", "GPS:GPSLongitude"] if exif else [],
            "make": ["IFD0:Make"] if exif else (["XMP-tiff:Make"] if xmp else []),
            "model": ["IFD0:Model"] if exif else (["XMP-tiff:Model"] if xmp else []),
            "orientation": ["IFD0:Orientation"] if exif else [],
            "title": ["XMP-dc:Title"] if xmp else [],
            "description": ["XMP-dc:Description"] if xmp else [],
        }
        return table.get(field, [])
    if kind == "video" and ext in QT_WRITABLE_EXT:
        table = {"create_date": ["QuickTime:CreateDate"], "datetime_original": ["Keys:CreationDate"],
                 "gps": ["Keys:GPSCoordinates"], "make": ["Keys:Make"], "model": ["Keys:Model"],
                 "title": ["XMP-dc:Title"], "description": ["XMP-dc:Description"]}
        return table.get(field, [])
    return []


def write_values(field: str, tag_list: list[str], cand: dict, offset_known: str | None) -> dict | None:
    """Translate a reliable candidate into tag=value pairs for the canonical tags,
    or None when that would require an assumption (e.g. time zone)."""
    v = cand["value"]
    if field in ("datetime_original", "create_date"):
        tag = tag_list[0]
        target_sem = "utc" if tag == "QuickTime:CreateDate" else ("tz" if tag == "Keys:CreationDate" else "local")
        sem, off = cand["semantic"], cand.get("offset") or offset_known
        if target_sem == "local":
            if sem in ("local", "tz"):
                return {tag: exif_dt(v)}
            return None                                   # UTC -> local needs a time zone: review
        if target_sem == "utc":
            if sem == "utc":
                return {tag: exif_dt(v)}
            if sem == "tz" and off:
                return {tag: exif_dt(to_utc(v, off))}    # exact conversion, offset known
            return None
        if target_sem == "tz":
            if sem == "tz" and off:
                return {tag: exif_dt(v) + off}
            return None
    if field == "gps":
        lat, lon = (float(x) for x in v.split(","))
        if tag_list[0].startswith("GPS:"):
            return {"GPS:GPSLatitude": abs(lat), "GPS:GPSLatitudeRef": "N" if lat >= 0 else "S",
                    "GPS:GPSLongitude": abs(lon), "GPS:GPSLongitudeRef": "E" if lon >= 0 else "W"}
        return {tag_list[0]: f"{lat:.6f}, {lon:.6f}"}
    if field == "orientation":
        try:
            return {tag_list[0]: int(float(v))}
        except ValueError:
            return None
    if field == "offset":
        return {tag_list[0]: v}
    return {tag_list[0]: v}


FILENAME_PATTERNS = [
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[ _T\-.]?(\d{2})[-_.:h]?(\d{2})[-_.:m]?(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)"),
]


def date_from_filename(name: str) -> str | None:
    for pat in FILENAME_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        parts = [int(x) for x in m.groups()] + [0] * (6 - len(m.groups()))
        try:
            return datetime(*parts).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return None


def analyse_file(f: dict, own: dict, sidecars: list[dict], enabled: set[str]) -> dict:
    """Pure function: decide fills, conflicts, proposals and the chosen date."""
    kind, ext = f["media_kind"], f["ext"]
    fields: dict[str, dict] = {}
    fills: dict[str, dict] = {}
    conflicts: dict[str, list] = {}
    for field in FIELDS:
        cands = list(own.get(field, []))
        side = [c for s in sidecars for c in s.get(field, [])]
        allc = cands + side
        present = bool(cands)
        canon = canonical_tags(field, kind, ext)
        canon_present = any(c["tag"] in canon for c in cands) or (field == "gps" and canon and any(c["tag"] == "Composite:GPSPosition" for c in cands))
        info = {"present": present, "candidates": allc, "canonical": canon, "canonical_present": canon_present}
        if field in ("datetime_original", "create_date", "modify_date"):
            consistent = _dt_consistent(allc)
        else:
            consistent = len(_distinct(allc)) <= 1
        if allc and not consistent:
            conflicts[field] = allc
        elif field in WRITABLE and field in enabled and canon and not canon_present and allc:
            off_known = next((c["value"] for c in own.get("offset", [])), None)
            for c in allc:
                if c["tag"] in canon:
                    continue
                vals = write_values(field, canon, c, off_known)
                if vals:
                    fills[field] = {"tags": vals, "from": c["tag"], "source": c["source"], "value": c["value"]}
                    break
        fields[field] = info
    # chosen date for organisation
    chosen, src, flags, chosen_offset = None, None, "", None
    dt_cands = fields["datetime_original"]["candidates"] + fields["create_date"]["candidates"]
    local = [c for c in dt_cands if c["semantic"] in ("local", "tz")]
    utc = [c for c in dt_cands if c["semantic"] == "utc"]
    if "datetime_original" not in conflicts and "create_date" not in conflicts:
        if local:
            chosen, src, chosen_offset = local[0]["value"], local[0]["tag"], local[0].get("offset")
        elif utc:
            off = next((c["value"] for c in own.get("offset", [])), None)
            if off and re.match(r"^[+\-]\d{2}:\d{2}$", off):
                sign = 1 if off[0] == "+" else -1
                dt = datetime.strptime(utc[0]["value"], "%Y-%m-%dT%H:%M:%S") + sign * timedelta(hours=int(off[1:3]), minutes=int(off[4:6]))
                chosen, src = dt.strftime("%Y-%m-%dT%H:%M:%S"), utc[0]["tag"] + "+offset"
            else:
                chosen, src, flags = utc[0]["value"], utc[0]["tag"], "utc_ambiguous"
    proposals = []
    if not chosen and not conflicts.get("datetime_original") and not conflicts.get("create_date"):
        fn = date_from_filename(f["name"])
        if fn:
            proposals.append({"field": "datetime_original", "value": fn, "origin": "filename"})
        mt = datetime.fromtimestamp((f.get("mtime_ns") or 0) / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        proposals.append({"field": "datetime_original", "value": mt, "origin": "mtime_utc"})
    return {"fields": fields, "fills": fills, "conflicts": conflicts, "chosen": chosen, "chosen_source": src,
            "chosen_offset": chosen_offset, "flags": flags, "proposals": proposals}


def _add_review(db, f: dict, kind: str, details: dict, proposals: list, job_id: int) -> None:
    db.execute(
        "INSERT INTO review_items (owner_id, file_id, kind, details, proposals, status, created_at, job_id, group_key, applied) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f["owner_id"], f["id"], kind, json.dumps(details, ensure_ascii=False), json.dumps(proposals, ensure_ascii=False),
         "pending", utcnow(), job_id, "dir:" + path_hash(os.path.dirname(f["cur_path"]))[:32], 0),
    )


def ensure_capture_dates(ctx: JobContext, rows: list[dict]) -> int:
    """Read reliable date metadata without creating review items or write plans."""
    pending = [r for r in rows if not r.get("meta_read") or r.get("phase_meta") not in ("analysed", "done")]
    if not pending:
        return 0
    et = ctx.app.exiftool
    dirs: dict[str, tuple] = {}
    side_paths: dict[int, list[str]] = {}
    for row in pending:
        directory = os.path.dirname(row["cur_path"])
        if directory not in dirs:
            try:
                dirs[directory] = group_directory(os.listdir(directory))
            except OSError:
                dirs[directory] = ({}, set())
        companions = dirs[directory][0].get(row["name"], [])
        side_paths[row["id"]] = [os.path.join(directory, name) for name in companions
                                 if name.lower().endswith((".xmp", ".json"))]
    to_read = [row["cur_path"] for row in pending]
    to_read += [path for paths in side_paths.values() for path in paths if path.lower().endswith(".xmp")]
    try:
        metadata = et.read(to_read)
    except ExifError as exc:
        raise RuntimeError(exc.args[0]) from None
    for row in pending:
        own = collect(metadata.get(row["cur_path"], {}), "file")
        sidecars = []
        for path in side_paths[row["id"]]:
            if path.lower().endswith(".xmp"):
                sidecars.append(collect(metadata.get(path, {}), "sidecar:" + os.path.basename(path)))
            else:
                sidecars.append(takeout_candidates(path))
        result = analyse_file(row, own, sidecars, set())
        stored = {"fields": result["fields"], "fills": {}, "proposals": result["proposals"]}
        ctx.db.execute("UPDATE files SET meta_read=?, chosen_date=?, chosen_date_source=?, date_flags=?, "
                       "phase_meta='analysed', updated_at=? WHERE id=?",
                       (json.dumps(stored, ensure_ascii=False), result["chosen"], result["chosen_source"],
                        result["flags"], utcnow(), row["id"]))
        row.update(meta_read=stored and json.dumps(stored, ensure_ascii=False), chosen_date=result["chosen"],
                   chosen_date_source=result["chosen_source"], date_flags=result["flags"], phase_meta="analysed")
    return len(pending)


@register("metadata_analyze", auto_resume=True)
def metadata_analyze(ctx: JobContext) -> dict:
    """Read metadata, choose dates, queue doubtful files for review and build the
    plan of automatic completions (written only after confirmation)."""
    db = ctx.db
    et = ctx.app.exiftool
    enabled = set(ctx.app.settings_section("metadata").get("fields", WRITABLE))
    source_id = ctx.params.get("source_id")
    where = "owner_id=? AND present=1 AND media_kind IN ('image','video') AND status<>'dup_moved'"
    args: list = [ctx.owner_id]
    if source_id:
        where += " AND source_id=?"
        args.append(source_id)
    from . import access
    user = ctx.app.user(ctx.owner_id)
    in_scope = (lambda row: access.file_in_scope(ctx.app.settings.data_dir.as_posix(), user, row)) if user else (lambda row: True)
    last_id = ctx.checkpoint.get("last_id", 0)
    stats = ctx.checkpoint.get("stats") or {"analysed": 0, "fills": 0, "review": 0}
    total = int(db.scalar(f"SELECT COUNT(*) AS n FROM files WHERE {where}", args) or 0)
    seq = ctx.checkpoint.get("seq", 0)
    while True:
        ctx.check()
        rows = [row for row in db.query(f"SELECT * FROM files WHERE {where} AND id>? ORDER BY id LIMIT 50", (*args, last_id))
                if in_scope(row)]
        if not rows:
            max_row = db.query_one(f"SELECT MAX(id) AS id FROM files WHERE {where} AND id>?", (*args, last_id))
            if not max_row or max_row["id"] is None:
                break
            last_id = max_row["id"]
            ctx.save_checkpoint(last_id=last_id, stats=stats, seq=seq)
            continue
        if not rows:
            break
        # read the batch (+ XMP sidecars) with one ExifTool call
        dirs: dict[str, tuple] = {}
        side_paths: dict[int, list[str]] = {}
        for r in rows:
            d = os.path.dirname(r["cur_path"])
            if d not in dirs:
                try:
                    dirs[d] = group_directory(os.listdir(d))
                except OSError:
                    dirs[d] = ({}, set())
            comps = dirs[d][0].get(r["name"], [])
            side_paths[r["id"]] = [os.path.join(d, c) for c in comps if c.lower().endswith((".xmp", ".json"))]
        to_read = [r["cur_path"] for r in rows] + [p for ps in side_paths.values() for p in ps if p.lower().endswith(".xmp")]
        try:
            meta = et.read(to_read)
        except ExifError as exc:
            raise RuntimeError(exc.args[0]) from None
        for r in rows:
            ctx.check()
            last_id = r["id"]
            own = collect(meta.get(r["cur_path"], {}), "file")
            sidecars = []
            for sp in side_paths[r["id"]]:
                if sp.lower().endswith(".xmp"):
                    sidecars.append(collect(meta.get(sp, {}), "sidecar:" + os.path.basename(sp)))
                else:
                    sidecars.append(takeout_candidates(sp))
            res = analyse_file(r, own, sidecars, enabled)
            raw_subset = {k: v for k, v in meta.get(r["cur_path"], {}).items()
                          if any(k == t or (t.startswith("Track*") and k.endswith(t.split(":")[1])) for specs in READ_TAGS.values() for t, _ in specs)}
            duration = None
            for c in res["fields"]["duration"]["candidates"]:
                try:
                    duration = float(c["value"])
                    break
                except ValueError:
                    pass
            with db.transaction():
                db.execute("DELETE FROM review_items WHERE file_id=? AND status='pending' AND kind IN ('meta_conflict','no_date','date_utc')",
                           (r["id"],))
                db.execute("DELETE FROM plan_items WHERE plan_job_id=? AND file_id=?", (ctx.id, r["id"]))
                status = r["status"]
                if res["conflicts"]:
                    _add_review(db, r, "meta_conflict", {"fields": res["conflicts"]}, res["proposals"], ctx.id)
                    stats["review"] += 1
                    status = "review" if status in ("indexed", "unique") else status
                elif not res["chosen"]:
                    _add_review(db, r, "no_date", {}, res["proposals"], ctx.id)
                    stats["review"] += 1
                    status = "review" if status in ("indexed", "unique") else status
                elif res["flags"] == "utc_ambiguous":
                    _add_review(db, r, "date_utc", {"value": res["chosen"], "tag": res["chosen_source"]}, [], ctx.id)
                    stats["review"] += 1
                if res["fills"]:
                    seq += 1
                    db.execute("INSERT INTO plan_items (plan_job_id, owner_id, kind, seq, file_id, src, dst, payload, status) "
                               "VALUES (?,?,?,?,?,?,?,?,?)",
                               (ctx.id, r["owner_id"], "meta_fill", seq, r["id"], r["cur_path"], None,
                                json.dumps({"fills": res["fills"]}, ensure_ascii=False), "planned"))
                    stats["fills"] += 1
                decided = db.query_one("SELECT id FROM review_items WHERE file_id=? AND status='saved' AND decision LIKE ?",
                                       (r["id"], '%"date"%'))
                db.execute("UPDATE files SET meta_read=?, chosen_date=CASE WHEN ? THEN chosen_date ELSE ? END, "
                           "chosen_date_source=CASE WHEN ? THEN chosen_date_source ELSE ? END, date_flags=?, phase_meta='analysed', "
                           "status=?, duration_s=?, meta_before=COALESCE(meta_before, ?), updated_at=? WHERE id=?",
                           (json.dumps({"fields": res["fields"], "fills": res["fills"], "proposals": res["proposals"]}, ensure_ascii=False),
                            1 if decided else 0, res["chosen"], 1 if decided else 0, res["chosen_source"], res["flags"],
                            status, duration, json.dumps(raw_subset, ensure_ascii=False), utcnow(), r["id"]))
            stats["analysed"] += 1
            ctx.progress(done=stats["analysed"], total=total, phase="metadata")
        ctx.save_checkpoint(last_id=last_id, stats=stats, seq=seq)
    return stats


def apply_fill(ctx: JobContext, item: dict, et: ExifTool) -> str:
    """Write one planned completion. Returns 'done' or a skip reason."""
    db = ctx.db
    f = db.query_one("SELECT * FROM files WHERE id=?", (item["file_id"],))
    if not f or not f["present"] or f["cur_path"] != item["src"]:
        return "file_changed"
    user = ctx.app.user(item["owner_id"])
    from . import access
    if f["owner_id"] != item["owner_id"] or not access.file_in_scope(ctx.app.settings.data_dir.as_posix(), user, f):
        return "outside_source"
    try:
        st = os.stat(f["cur_path"])
    except OSError:
        return "file_missing"
    if st.st_size != f["size"] or st.st_mtime_ns != f["mtime_ns"]:
        return "file_changed"
    payload = json.loads(item["payload"])
    tags: dict = {}
    for fill in payload.get("fills", {}).values():
        tags.update(fill["tags"])
    if not tags:
        return "nothing_to_do"
    current = et.read([f["cur_path"]]).get(f["cur_path"], {})
    before = {t: current.get(t) for t in tags}
    if any(v not in (None, "") for k, v in before.items() if not k.endswith("Ref")):
        return "value_present"                    # never overwrite
    sha_before = f["sha256"]
    with db.transaction():
        mid = db.insert("INSERT INTO manifest (job_id, owner_id, file_id, action, orig_path, new_path, sha256_before, meta_before, meta_after, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ctx.id, f["owner_id"], f["id"], "metadata", f["cur_path"], f["cur_path"], sha_before,
                         json.dumps(before, ensure_ascii=False, default=str), json.dumps(tags, ensure_ascii=False, default=str), utcnow()))
        for t, v in tags.items():
            db.execute("INSERT INTO meta_changes (job_id, owner_id, file_id, manifest_id, tag, before_value, after_value, source, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (ctx.id, f["owner_id"], f["id"], mid, t, json.dumps(before.get(t)), json.dumps(v), item["kind"], utcnow()))
    try:
        et.write(f["cur_path"], tags, create_only=True)
    except ExifError as exc:
        with db.transaction():
            db.execute("DELETE FROM meta_changes WHERE manifest_id=?", (mid,))
            db.execute("UPDATE manifest SET undone_at=?, meta_after=? WHERE id=?", (utcnow(), json.dumps({"error": exc.args[0]}), mid))
        ctx.log("META_SKIPPED", level="WARNING", path=f["cur_path"], reason=exc.args[0])
        return exc.args[0]
    st2 = os.stat(f["cur_path"])
    sha_after = sha256_file(f["cur_path"])
    with db.transaction():
        db.execute("UPDATE manifest SET sha256_after=? WHERE id=?", (sha_after, mid))
        db.execute("UPDATE files SET sha256=?, size=?, mtime_ns=?, meta_after=?, status=CASE WHEN status IN ('indexed','unique') "
                   "THEN 'normalized' ELSE status END, phase_meta='done', updated_at=? WHERE id=?",
                   (sha_after, st2.st_size, st2.st_mtime_ns, json.dumps(tags, ensure_ascii=False, default=str), utcnow(), f["id"]))
        ctx.log("META_WRITTEN", path=f["cur_path"], tags=", ".join(sorted(tags)))
    return "done"


def run_meta_plan(ctx: JobContext) -> dict:
    db = ctx.db
    et = ctx.app.exiftool
    plan_id = ctx.params["plan_job_id"]
    total = int(db.scalar("SELECT COUNT(*) AS n FROM plan_items WHERE plan_job_id=? AND owner_id=?", (plan_id, ctx.owner_id)) or 0)
    done = int(db.scalar("SELECT COUNT(*) AS n FROM plan_items WHERE plan_job_id=? AND owner_id=? AND status<>'planned'", (plan_id, ctx.owner_id)) or 0)
    counts = {"done": 0, "skipped": 0}
    for item in db.query("SELECT * FROM plan_items WHERE plan_job_id=? AND owner_id=? AND status='planned' ORDER BY seq", (plan_id, ctx.owner_id)):
        ctx.check()
        if item["kind"] == "meta_restore":
            from .undo import restore_meta_item
            outcome = restore_meta_item(ctx, item, et)
        else:
            outcome = apply_fill(ctx, item, et)
        db.execute("UPDATE plan_items SET status=?, note=? WHERE id=?",
                   ("done" if outcome == "done" else "skipped", None if outcome == "done" else outcome, item["id"]))
        if item["kind"] == "meta_review" and outcome == "done":
            db.execute("UPDATE review_items SET applied=1 WHERE file_id=? AND status='saved'", (item["file_id"],))
        counts["done" if outcome == "done" else "skipped"] += 1
        done += 1
        ctx.progress(done=done, total=total, phase="write")
    return counts


@register("metadata_apply", writes_files=True)
def metadata_apply(ctx: JobContext) -> dict:
    return run_meta_plan(ctx)


@register("review_plan", auto_resume=True)
def review_plan(ctx: JobContext) -> dict:
    """Plan writing of review decisions (only fields missing in the file)."""
    db = ctx.db
    seq, n = 0, 0
    db.execute("DELETE FROM plan_items WHERE plan_job_id=?", (ctx.id,))
    from . import access
    user = ctx.app.user(ctx.owner_id)
    for it in db.query("SELECT r.*, f.media_kind, f.ext, f.cur_path, f.meta_read, f.owner_id, f.size, f.mtime_ns, f.present FROM review_items r JOIN files f ON f.id=r.file_id "
                       "WHERE r.owner_id=? AND r.status='saved' AND r.applied=0 AND f.present=1 ORDER BY r.id", (ctx.owner_id,)):
        ctx.check()
        if not access.file_in_scope(ctx.app.settings.data_dir.as_posix(), user, it):
            continue
        decision = json.loads(it["decision"] or "{}")
        values = decision.get("fields") or {}
        meta = json.loads(it["meta_read"] or "{}").get("fields", {})
        fills = {}
        for field, value in values.items():
            if field not in WRITABLE or value in (None, ""):
                continue
            if (meta.get(field) or {}).get("present"):
                continue                               # present in the file: never overwritten
            canon = canonical_tags(field, it["media_kind"], it["ext"])
            if not canon:
                continue
            cand = {"value": value, "semantic": "local", "offset": None}
            if field in ("datetime_original", "create_date") and decision.get("offset"):
                cand.update(semantic="tz", offset=decision["offset"])
            vals = write_values(field, canon, cand, decision.get("offset"))
            if vals:
                fills[field] = {"tags": vals, "from": "review", "source": "review", "value": value}
        if fills:
            seq += 1
            n += 1
            db.execute("INSERT INTO plan_items (plan_job_id, owner_id, kind, seq, file_id, src, payload, status) VALUES (?,?,?,?,?,?,?,?)",
                       (ctx.id, ctx.owner_id, "meta_review", seq, it["file_id"], it["cur_path"], json.dumps({"fills": fills}), "planned"))
    return {"planned": n}


def field_table(f: dict) -> list[dict]:
    """Rows for the review screen: every target field, present or missing."""
    meta = json.loads(f.get("meta_read") or "{}")
    fields = meta.get("fields", {})
    fills = meta.get("fills", {})
    rows = []
    for field in FIELDS:
        info = fields.get(field) or {"present": False, "candidates": []}
        rows.append({"field": field, "present": info.get("present", False), "candidates": info.get("candidates", []),
                     "fill": fills.get(field), "writable": field in WRITABLE and bool(canonical_tags(field, f["media_kind"], f["ext"]))})
    return rows

