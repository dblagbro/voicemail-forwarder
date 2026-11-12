import copy
import json
import unittest
from unittest.mock import patch

import rehearse_ingress_activation as r


class IntegratedSafetyTests(unittest.TestCase):
    def test_production_ports_and_port_alias_are_rejected(self):
        for pair in [(8089, 18090), (18089, 443), (18089, 18089), (19000, 18090)]:
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                r.check_ports(*pair)

    def test_mailboxes_alerts_and_provider_smtp_are_rejected(self):
        for cfg in [
            {"mailboxes": [{"enabled": False}], "system": {}},
            {"mailboxes": [], "system": {"alert_email": "fixture@example.invalid"}},
            {"mailboxes": [], "system": {"alert_hours": 1}},
            {"mailboxes": [], "system": {"smtp": {"host": "smtp.invalid"}}},
            {"mailboxes": [], "system": {"smtp": "invalid"}},
            [],
        ]:
            with self.subTest(cfg=cfg), self.assertRaises(RuntimeError):
                r.safe_config(json.dumps(cfg))

    def test_only_empty_synthetic_configuration_is_accepted(self):
        cfg = {"mailboxes": [], "system": {"smtp": {}, "alert_hours": 0}}
        self.assertEqual(r.safe_config(json.dumps(cfg)), cfg)
        self.assertEqual(r.safe_config(r.stop.maintenance_config(json.dumps(cfg))),
                         json.loads(r.stop.maintenance_config(json.dumps(cfg))))

    def fixture(self):
        state = {"case": "/private/evidence", "runtime": "/private/fixture", "token": "owned", "gateway": "172.18.0.1",
                 "backend_port": 18089, "original_name": "fixture", "original_image": "image"}
        current = {"Image": "image", "Config": {
            "Labels": {"codex.voicemail.fixture": "true", "codex.voicemail.generation": "owned"},
            "Cmd": r.mail_command(state)}, "HostConfig": {"NetworkMode": "host"},
            "Mounts": [{"Destination": "/app/" + n, "Source": "/private/fixture/" + n,
                        "Type": "bind"} for n in ["data", "logs"]]}
        return state, current

    def test_foreign_generation_or_image_never_passes_ownership_gate(self):
        state, original = self.fixture()
        for change in ["generation", "image", "label"]:
            current = copy.deepcopy(original)
            if change == "generation":
                current["Config"]["Labels"]["codex.voicemail.generation"] = "foreign"
            elif change == "label":
                current["Config"]["Labels"] = {}
            else:
                current["Image"] = "foreign"
            with patch.object(r, "find", return_value=current), self.assertRaises(RuntimeError):
                r.owned(state, "original")

    def test_live_mount_or_production_listener_never_passes_gate(self):
        state, original = self.fixture()
        for change in ["mount", "command", "network"]:
            current = copy.deepcopy(original)
            if change == "mount":
                current["Mounts"][0]["Source"] = "/production/data"
            elif change == "command":
                current["Config"]["Cmd"][2] = "127.0.0.1:8089"
            else:
                current["HostConfig"]["NetworkMode"] = "bridge"
            with patch.object(r, "find", return_value=current), self.assertRaises(RuntimeError):
                r.owned(state, "original")


if __name__ == "__main__":
    unittest.main()
