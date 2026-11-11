"""Recovery must preserve concurrent edits and the latest deduplication state."""

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import quiesce_backup as q


class Interrupted(BaseException):
    pass


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.generation = self.root / "generation"
        self.generation.mkdir(mode=0o700)
        self.cfg = self.root / "config.json"
        self.original = b'{"system":{},"mailboxes":[]}\n'
        self.maintenance = b'{"system":{"poll_interval_seconds":3600},"mailboxes":[]}\n'
        self.cfg.write_bytes(self.original)
        (self.generation / "original-config.json").write_bytes(self.original)
        (self.generation / "maintenance-config.json").write_bytes(self.maintenance)
        self.state = {
            "configuration_path": str(self.cfg),
            "configuration_mode": 0o640,
            "configuration_uid": os.geteuid(),
            "configuration_gid": os.getegid(),
            "token": "test",
            "container_id": "exact-id",
            "image": "exact-image",
            "restart_policy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "phase": "paused",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_path_overlap_refused(self):
        for paths in [[self.root, self.root / "child"], [self.root, self.root]]:
            with self.assertRaises(RuntimeError):
                q.independent_paths(paths)
        q.independent_paths([self.root / "a", self.root / "b"])

    def test_snapshot_independent_and_no_data_reversion(self):
        data = self.root / "data"
        data.mkdir()
        db = data / "state.db"
        with sqlite3.connect(db) as c:
            for table in ["seen", "seen_uidl", "attachments"]:
                c.execute(f"create table {table} (id text)")
            c.execute("insert into seen_uidl values (?)", ("before",))
        snapshot = self.root / "snapshot"
        q.tree_copy(data, snapshot)
        q.database_check(snapshot / "state.db")
        with sqlite3.connect(db) as c:
            c.execute("insert into seen_uidl values (?)", ("latest",))
        with sqlite3.connect(snapshot / "state.db") as c:
            self.assertEqual(
                c.execute("select count(*) from seen_uidl").fetchone(), (1,)
            )
        with sqlite3.connect(db) as c:
            self.assertEqual(
                c.execute("select count(*) from seen_uidl").fetchone(), (2,)
            )
        with self.assertRaises(FileExistsError):
            q.tree_copy(data, snapshot)

    def test_invalid_database_and_symlink_refused(self):
        db = self.root / "state.db"
        with sqlite3.connect(db):
            pass
        with self.assertRaises(RuntimeError):
            q.database_check(db)
        (self.root / "link").symlink_to(db)
        with self.assertRaises(RuntimeError):
            q.tree_manifest(self.root)

    def test_container_config_fingerprint_detects_restart_and_network_edits(self):
        a = {
            "Config": {"Cmd": ["original"]},
            "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"}},
            "Mounts": [],
        }
        b = json.loads(json.dumps(a))
        b["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"
        self.assertNotEqual(q.config_fingerprint(a), q.config_fingerprint(b))
        b["HostConfig"]["NetworkMode"] = "foreign"
        self.assertNotEqual(q.config_fingerprint(a), q.config_fingerprint(b))

    def test_resume_only_unfreezes_and_checks_http(self):
        current = {"State": {"Running": True, "Paused": True}}
        running = {"State": {"Running": True, "Paused": False}}
        with (
            patch.object(q, "owned_state", side_effect=[current, running]),
            patch.object(q, "run") as command,
            patch.object(q, "http_ready"),
            patch.object(q, "checkpoint"),
        ):
            q.resume_original(self.generation, self.state)
            command.assert_called_once_with(["docker", "unpause", "exact-id"])
        self.cfg.write_bytes(b"concurrent edit")
        with (
            patch.object(q, "owned_state", return_value=running),
            patch.object(q, "run") as command,
            patch.object(q, "http_ready"),
            patch.object(q, "checkpoint"),
        ):
            q.resume_original(self.generation, self.state)
            command.assert_not_called()
        self.assertEqual(self.cfg.read_bytes(), b"concurrent edit")

    def test_resume_never_restarts_externally_stopped_service(self):
        with (
            patch.object(q, "owned_state", return_value={"State": {"Running": False}}),
            patch.object(q, "run") as command,
        ):
            with self.assertRaises(RuntimeError):
                q.resume_original(self.generation, self.state)
            command.assert_not_called()

    def test_wrong_container_identity_refused(self):
        with patch.object(
            q, "inspect", return_value={"Id": "different", "Image": "exact-image"}
        ):
            with self.assertRaises(RuntimeError):
                q.owned_state(self.state)


if __name__ == "__main__":
    unittest.main()
