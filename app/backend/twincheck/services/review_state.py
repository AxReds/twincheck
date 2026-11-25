"""Shared review state for queues, item badges, counts and organization."""
import json


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
               for row in db.query("SELECT * FROM review_items WHERE file_id=?", (file_id,)))


def organize_block(db, file_id: int) -> str | None:
    states = {status(row) for row in db.query("SELECT * FROM review_items WHERE file_id=?", (file_id,))}
    if "needs_recheck" in states:
        return "needs_recheck"
    if "skipped" in states:
        return "review_skipped"
    return None
