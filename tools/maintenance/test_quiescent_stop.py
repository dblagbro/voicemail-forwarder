import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import quiescent_stop as s


class Interrupted(BaseException):
    pass


class StopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.original = (
            json.dumps(
                {
                    "system": {
                        "smtp": {"host": "smtp.invalid", "pass": "synthetic"},
                        "alert_hours": 1,
                        "alert_email": "fixture@example.invalid",
                    },
                    "mailboxes": [{"enabled": True}],
                }
            )
            + "\n"
        ).encode()
        (self.root / "original-config.json").write_bytes(self.original)
        self.cfg = self.root / "config.json"
        self.cfg.write_bytes(self.original)
        self.state = {
            "configuration_path": str(self.cfg),
            "configuration_mode": 0o600,
            "configuration_uid": os.geteuid(),
            "configuration_gid": os.getegid(),
            "token": "fixture",
            "container_id": "owned",
            "image": "original",
            "phase": "paused",
            "restart_policy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_fence_cannot_send_poll_or_alert_or_smtp_test(self):
        cfg = json.loads(s.maintenance_config(self.original))
        self.assertFalse(any(x["enabled"] for x in cfg["mailboxes"]))
        self.assertEqual(cfg["system"]["alert_email"], "")
        self.assertEqual(cfg["system"]["smtp"]["host"], "")
        self.assertEqual(
            json.loads(self.original)["system"]["smtp"]["host"], "smtp.invalid"
        )

    def test_recover_each_file_publication_boundary(self):
        for phase in [
            "candidate_file_ready",
            "original_retained",
            "candidate_published",
        ]:
            self.state["token"] = phase.replace("_", "-")

            def die(at):
                if at == phase:
                    raise Interrupted()

            with self.assertRaises(Interrupted):
                s.publication(self.root, self.state).apply(die)
            s.restore_configuration(self.root, self.state)
            self.assertEqual(self.cfg.read_bytes(), self.original)
            s.restore_configuration(self.root, self.state)

    def test_explicit_maintenance_and_legacy_journals_restore_exact_bytes(self):
        legacy = s.maintenance_config(self.original)
        self.assertNotIn("maintenance", json.loads(legacy)["system"])
        for version in (0, 1):
            self.state.update(maintenance_version=version, token="version-" + str(version))
            publication = s.publication(self.root, self.state)
            publication.apply()
            active = json.loads(self.cfg.read_bytes())["system"].get("maintenance", False)
            self.assertIs(active, version == 1)
            s.restore_configuration(self.root, self.state)
            self.assertEqual(self.cfg.read_bytes(), self.original)
        self.state["maintenance_version"] = 99
        with self.assertRaises(ValueError):
            s.publication(self.root, self.state)

    def test_reverse_file_publication_is_recoverable(self):
        for phase in [
            "candidate_file_ready",
            "original_retained",
            "candidate_published",
        ]:
            self.state["token"] = phase.replace("_", "-")
            s.publication(self.root, self.state).apply()

            def die(at):
                if at == phase:
                    raise Interrupted()

            with self.assertRaises(Interrupted):
                s.publication(self.root, self.state, True).apply(die)
            s.restore_configuration(self.root, self.state)
            self.assertEqual(self.cfg.read_bytes(), self.original)

    def test_concurrent_mail_configuration_edit_preserved(self):
        s.publication(self.root, self.state).apply()
        self.cfg.write_bytes(b"new private edit")
        with self.assertRaises(s.ConfigurationConflict):
            s.restore_configuration(self.root, self.state)
        self.assertEqual(self.cfg.read_bytes(), b"new private edit")

    def test_stop_rejects_unfrozen_or_changed_worker(self):
        current = {"State": {"Running": True, "Paused": False}}
        with (
            patch.object(s, "owned_state", return_value=current),
            patch.object(s.os, "kill") as kill,
        ):
            with self.assertRaises(RuntimeError):
                s.stop_idle(self.root, self.state, lambda p: None)
            kill.assert_not_called()

    def test_recovery_never_restarts_foreign_stop(self):
        with (
            patch.object(s, "owned_state", return_value={"State": {"Running": False}}),
            patch.object(s.backup, "run") as run,
        ):
            with self.assertRaises(RuntimeError):
                s.recover_original(self.root, self.state, lambda p: None)
            run.assert_not_called()

    def test_stop_does_not_claim_an_external_shutdown(self):
        current = {"State": {"Running": False, "ExitCode": 0, "OOMKilled": False}}
        with patch.object(s, "owned_state", return_value=current):
            with self.assertRaises(RuntimeError):
                s.stop_idle(self.root, self.state, lambda p: self.fail(p))


if __name__ == "__main__":
    unittest.main()
