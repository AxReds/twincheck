"""Synthetic rc.9 regressions; no owner library or credentials."""
import json
import tempfile
import unittest
from unittest import mock

from helpers import Instance
from test_database import backends, drop
from twincheck import events
from twincheck.db import manager as dbm
from twincheck.db.schema import CURRENT_VERSION, create_structure, set_meta


class MigrationEvents(unittest.TestCase):
    def test_confirmed_migration_logs_once_with_real_versions_and_audits_flags(self):
        inst = Instance().start()
        try:
            inst.setup()
            inst.core.detach()
            db = inst.core.make_db(inst.core.vault.data["database"])
            fid = db.insert("INSERT INTO files (owner_id,cur_path_hash,mtime_ns,phase_org) VALUES (?,?,?,?)",
                            ("synthetic", "fixture", 0, "pending"))
            db.execute("INSERT INTO review_items (owner_id,file_id,status,decision,decision_source,applied) VALUES (?,?,?,?,?,?)",
                       ("synthetic", fid, "saved", json.dumps({"date": "1970-01-01T00:00:00"}), "unknown", 0))
            set_meta(db, "schema_version", 3)
            db.close()
            inst.core.connect_configured()
            admin = inst.admin()
            with mock.patch.object(events, "console", wraps=events.console) as console:
                response = admin.post("/api/admin/db/migrate-schema", {"confirm_backup": True})
            self.assertEqual(response.status, 200, response.text)
            migrated = [c for c in console.call_args_list if c.args[0] == "DB_MIGRATED"]
            self.assertEqual(len(migrated), 1)
            self.assertEqual((migrated[0].kwargs["from_version"], migrated[0].kwargs["to_version"]),
                             (3, CURRENT_VERSION))
            db = inst.core.db
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM run_log WHERE event='DB_MIGRATED'"), 1)
            audit = inst.core.vault.snapshot()["audit"]
            flagged = [e for e in audit if e["event"] == "DECISIONS_FLAGGED_FOR_RECHECK"]
            self.assertEqual(len(flagged), 1)
            self.assertEqual(flagged[0]["params"]["count"], 1)
        finally:
            inst.cleanup()
