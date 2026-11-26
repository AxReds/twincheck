"""Log event catalogue.

Logs are ALWAYS and ONLY in English: every entry has a stable event code, an
English template and parameters.  These texts are never translated and must
never appear in the locale files (``app/tools/check_locales.py`` enforces it).
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

EVENTS: dict[str, str] = {
    # system
    "APP_STARTED": "Application {version} started",
    "APP_STOPPED": "Application stopped",
    "SETUP_TOKEN": "Setup token: {token}",
    "SETUP_REQUIRED": "Setup is required: open the web interface and enter the setup token",
    "SETUP_COMPLETED": "Setup completed, database {db_type}",
    "VAULT_LOADED": "Encrypted vault loaded",
    "VAULT_RELOADED": "Encrypted vault changed on disk and was reloaded",
    "VAULT_SAVED": "Encrypted vault saved",
    "KEY_MISSING": "Master key missing: set APP_MASTER_KEY or APP_MASTER_KEY_FILE",
    "KEY_INVALID": "Master key rejected: {reason}",
    "VAULT_DECRYPT_FAILED": "Cannot decrypt the vault: wrong key or file tampered/corrupted; nothing was modified",
    "KEY_ROTATED": "Master key rotated by {actor}",
    "KEY_ROTATION_FAILED": "Master key rotation failed and was rolled back: {reason}",
    # auth / admin audit
    "LOGIN_OK": "User {username} logged in from {ip}",
    "LOGIN_FAILED": "Failed login for {username} from {ip}",
    "LOGIN_THROTTLED": "Login throttled for {username} from {ip}",
    "LOGOUT": "User {username} logged out",
    "PASSWORD_CHANGED": "User {username} changed the password",
    "PASSWORD_RESET": "Password of {username} reset by {actor}",
    "PASSWORD_RESET_CLI": "Password of {username} reset from the container console",
    "USER_CREATED": "User {username} created with role {role} by {actor}",
    "USER_UPDATED": "User {username} updated by {actor}",
    "USER_DELETED": "User {username} deleted by {actor}",
    "GROUP_CREATED": "Group {name} created by {actor}",
    "GROUP_UPDATED": "Group {name} updated by {actor}",
    "GROUP_DELETED": "Group {name} deleted by {actor}",
    "SOURCES_UPDATED": "Sources of {username} updated by {actor}",
    "SETTINGS_UPDATED": "Settings {section} updated by {actor}",
    "SETTING_CHANGED": "Setting {setting} changed by {actor}",
    "DECISIONS_FLAGGED_FOR_RECHECK": "{count} review decision{count_suffix} flagged for re-check; stored values unchanged",
    "OWNER_REASSIGNED": "Records of owner {old_owner} reassigned to {username} by {actor}",
    "ACCESS_DENIED_NETWORK": "Request from {ip} rejected by ALLOWED_NETWORKS",
    # database
    "DB_CONNECTED": "Connected to {db_type} database {db_id} (schema {schema})",
    "DB_CONNECT_FAILED": "Database connection failed: {reason}",
    "DB_STRUCTURE_CREATED": "Database structure created (schema {schema})",
    "DB_STRUCTURE_REUSED": "Existing database structure reused (schema {schema})",
    "DB_MIGRATED": "Database schema migrated from {from_version} to {to_version}",
    "DB_SWITCH_STARTED": "Database switch started by {actor}: {old_db} -> {new_db}",
    "DB_SWITCHED": "Database switched by {actor} from {old_db} to {new_db}",
    "DB_SWITCH_FAILED": "Database switch failed, staying on {old_db}: {reason}",
    "DB_INSTANCE_LOCKED": "Database is in use by another instance {instance}",
    # jobs
    "JOB_QUEUED": "Job {job_id} ({kind}) queued",
    "JOB_STARTED": "Job {job_id} ({kind}) started",
    "JOB_RESUMED": "Job {job_id} ({kind}) resumed from checkpoint",
    "JOB_PAUSED": "Job {job_id} ({kind}) paused at checkpoint",
    "JOB_INTERRUPTED": "Job {job_id} ({kind}) marked interrupted after restart",
    "JOB_COMPLETED": "Job {job_id} ({kind}) completed",
    "JOB_FAILED": "Job {job_id} ({kind}) failed: {error}",
    "JOB_CANCELLED": "Job {job_id} ({kind}) cancelled",
    "ANALYSIS_PHASE_STARTED": "Analysis phase {phase_label} started ({count} item{count_suffix})",
    "ANALYSIS_PHASE_COMPLETED": "Analysis phase {phase_label} completed: {summary}",
    "ANALYSIS_RESUMED": "Analysis resumed from phase {phase_label} at {position}",
    # files
    "SCAN_DIR": "Scanning {path}",
    "FILE_INDEXED": "File indexed {path}",
    "FILE_MISSING": "File no longer present {path}",
    "FILE_HASHED": "File hashed {path} sha256={sha256}",
    "FILE_UNREADABLE": "File cannot be read {path}: {error}",
    "SYMLINK_SKIPPED": "Symbolic link skipped {path}",
    "DUP_GROUP": "Duplicate group sha256={sha256} with {count} file{count_suffix}, keeper {keeper}",
    "DUP_MISMATCH": "Byte comparison mismatch despite equal hash: {a} {b}",
    "FILE_MOVED": "File moved from {src} to {dst}",
    "FILE_COPIED_VERIFIED": "File copied across volumes and verified from {src} to {dst}",
    "MOVE_INTENT": "Move intent recorded {src} -> {dst}",
    "MOVE_RECOVERED": "Interrupted move recovered as {state}: {src} -> {dst}",
    "MOVE_SKIPPED": "Move skipped {src}: {reason}",
    "META_WRITTEN": "Metadata written to {path}: {tags}",
    "META_RESTORED": "Metadata restored on {path}: {tags}",
    "META_SKIPPED": "Metadata not written to {path}: {reason}",
    "REVIEW_ADDED": "File queued for review {path}: {reason}",
    "REVIEW_DECIDED": "Review decision {decision} for {path}",
    "REVIEW_SKIPPED": "Review postponed for {path}",
    "REVIEW_SKIPS_RETURNED_TO_PENDING": "{count} skip-only review record{count_suffix} returned to pending; skip timestamps preserved",
    "UNDO_MOVE": "Move undone {src} -> {dst}",
    "VERIFY_MISMATCH": "Verification mismatch {path}: {reason}",
    "VERIFY_OK": "Verification passed for {count} file{count_suffix}",
    "RECONCILE_DIFF": "Reconciliation difference {kind}: {path}",
    "RECONCILE_DONE": "Reconciliation finished: {missing} missing, {modified} modified, {new} new, {moved} moved",
    "PREVIEW_TRANSCODED": "Preview transcoded for file {file_id}",
    # transfer
    "EXPORT_STARTED": "Export of database {db_id} started by {actor}",
    "EXPORT_DONE": "Export written {name} ({rows} row{rows_suffix})",
    "IMPORT_STARTED": "Import of {name} into {db_type} started by {actor}",
    "MIGRATE_STARTED": "Direct migration {src_db} -> {db_type} started by {actor}",
    "TRANSFER_TABLE": "Table {table}: {rows} row{rows_suffix} copied",
    "TRANSFER_VERIFIED": "Transfer verified: {tables} table{tables_suffix}, checksums identical",
    "TRANSFER_FAILED": "Transfer failed: {error}",
    "TRANSFER_PROVENANCE": "Database created from {source_db_id} (app {app_version}) on {date}",
    "DESTINATION_PURGED": "Destination database purged by {actor} after failed transfer",
}

_logger = logging.getLogger("twincheck")


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
    root = logging.getLogger("twincheck")
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False


def render(event: str, params: dict[str, Any] | None = None) -> str:
    template = EVENTS[event]
    values = dict(params or {})
    for key in ("count", "rows", "tables"):
        values[key + "_suffix"] = "" if values.get(key) in (1, "1") else "s"
    if "phase" in values:
        values["phase_label"] = str(values["phase"]).replace("_", " ")
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return template + " " + json.dumps(params or {}, ensure_ascii=False, default=str)


def console(event: str, level: int = logging.INFO, **params: Any) -> str:
    """Write an English log line to stdout and return the rendered text."""
    text = render(event, params)
    _logger.log(level, "[%s] %s", event, text)
    return text
