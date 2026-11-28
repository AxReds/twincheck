"""Shared review state for queues, item badges, counts and organization."""
import json

METADATA_KINDS = ("meta_conflict", "no_date", "date_utc")


def active(rows: list[dict]) -> list[dict]:
    """Keep historical rows intact while hiding superseded metadata queue entries."""
    selected = {}
    rank = {"pending": 0, "skipped": 1, "decided": 2, "needs_recheck": 3}
    for row in rows:
        key = (row["owner_id"], row["file_id"], "metadata") if row["kind"] in METADATA_KINDS else ("item", row["id"])
        previous = selected.get(key)
        if previous is None or (rank.get(status(row), 0), row["id"]) > (rank.get(status(previous), 0), previous["id"]):
            selected[key] = row
    return sorted(selected.values(), key=lambda row: row["id"])


def canonical(db, row: dict) -> dict:
    if row["kind"] not in METADATA_KINDS:
        return row
    return active(db.query("SELECT * FROM review_items WHERE owner_id=? AND file_id=? "
                           "AND kind IN ('meta_conflict','no_date','date_utc')",
                           (row["owner_id"], row["file_id"])))[0]


def status(row: dict) -> str:
    if row.get("recheck_reason") or row["status"] == "needs_recheck":
        return "needs_recheck"
    if row["status"] in ("saved", "kept"):
        return "decided"
    if row["status"] == "skipped":
        decision = json.loads(row.get("decision") or "{}")
        if decision.get("date") or any((decision.get("fields") or {}).values()) or row.get("applied"):
            return "decided"
        return "skipped"
    if row["status"] == "pending" and row.get("skipped_at"):
        return "skipped"
    return row["status"]


def preserves_decision(db, file_id: int) -> bool:
    return any(status(row) in ("decided", "needs_recheck")
               for row in active(db.query("SELECT * FROM review_items WHERE file_id=?", (file_id,))))


def organize_block(db, file_id: int) -> str | None:
    states = {status(row) for row in active(db.query("SELECT * FROM review_items WHERE file_id=?", (file_id,)))}
    if "needs_recheck" in states:
        return "needs_recheck"
    if "skipped" in states:
        return "review_skipped"
    return None
