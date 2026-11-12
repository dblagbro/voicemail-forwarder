"""Offline regression and real Gunicorn HTTP tests using disposable fixtures."""

import gzip
import importlib
import json
import os
import poplib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

IMPORT_ROOT = tempfile.TemporaryDirectory(prefix="voicemail-import-")
os.environ["APP_BASE"] = IMPORT_ROOT.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
core = importlib.import_module("app.core")

with patch.object(core, "start_poller"):
    web = importlib.import_module("app.app")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="voicemail-test-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for key, suffix in {
            "BASE_DIR": "",
            "DATA_DIR": "data",
            "LOGS_DIR": "logs",
            "ATT_DIR": "logs/attachments",
            "CFG_PATH": "data/config.json",
            "DB_PATH": "data/state.db",
            "LOG_FILE": "logs/forwarder.log",
            "LAST_POLL_FILE": "data/last_poll.txt",
        }.items():
            setattr(core, key, str(base / suffix))
        Path(core.ATT_DIR).mkdir(parents=True)
        core._initialized = False
        core._uidl_warned.clear()
        core.ensure_initialized()
        self.client = web.app.test_client()
        self.mailbox = dict(
            name="fixture",
            pop3_host="pop.invalid",
            pop3_port=110,
            pop3_user="fixture",
            pop3_pass="synthetic-only",
            dest_to="recipient@example.invalid",
            use_stls=True,
        )
        cfg = core.default_config()
        cfg["mailboxes"] = [self.mailbox]
        core.save_cfg(cfg)
        self.pop = Mock()
        self.pop.getwelcome.return_value = b"+OK fixture"
        self.pop.user.return_value = self.pop.pass_.return_value = b"+OK"
        self.pop.uidl.return_value = (b"+OK", [b"1 fixture-uidl"], 1)
        self.raw = b"Subject: Synthetic\r\nMessage-ID: <fixture@example.invalid>\r\n\r\nFixture\r\n"
        self.pop.retr.return_value = (
            b"+OK",
            self.raw.split(b"\r\n")[:-1],
            len(self.raw),
        )
        self.pop.stat.return_value = (1, len(self.raw))
        self.factory = patch.object(core.poplib, "POP3", return_value=self.pop)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.send = patch.object(core, "smtp_send")
        self.smtp = self.send.start()
        self.addCleanup(self.send.stop)

    def maintenance_config(self):
        cfg = core.load_cfg()
        cfg["system"]["maintenance"] = True
        cfg["system"]["poll_interval_seconds"] = 3600
        core.save_cfg(cfg)
        return cfg

    def test_maintenance_preserves_expired_attachment_and_poll_timestamp(self):
        self.maintenance_config()
        attachment = Path(core.ATT_DIR) / "expired.txt"
        attachment.write_bytes(b"synthetic old attachment")
        with sqlite3.connect(core.DB_PATH) as conn:
            conn.execute("INSERT INTO attachments(filename,mailbox,subject,ts) VALUES (?,?,?,?)",
                         (attachment.name, "fixture", "synthetic", "1900-01-01T00:00:00"))
        Path(core.LAST_POLL_FILE).write_text("unchanged")
        before = Path(core.DB_PATH).read_bytes()
        core.poll_once()
        self.assertTrue(attachment.exists())
        self.assertEqual(Path(core.DB_PATH).read_bytes(), before)
        self.assertEqual(Path(core.LAST_POLL_FILE).read_text(), "unchanged")
        self.pop.retr.assert_not_called()
        self.smtp.assert_not_called()

    def test_maintenance_rejects_all_http_mutations(self):
        self.maintenance_config()
        before = Path(core.CFG_PATH).read_bytes()
        for path in ["/settings", "/poll-now", "/smtp-test", "/mailboxes/add",
                     "/mailboxes/delete/fixture", "/attachments/delete/fixture.txt"]:
            with self.subTest(path=path):
                response = self.client.post(path)
                self.assertEqual(response.status_code, 503)
        self.assertEqual(Path(core.CFG_PATH).read_bytes(), before)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.smtp.assert_not_called()

    def test_maintenance_rejects_direct_manual_poll_and_provider_tests(self):
        self.maintenance_config()
        self.assertFalse(core.manual_poll_now())
        result = core.pop3_test(self.mailbox)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "maintenance")
        self.pop.user.assert_not_called()
        self.send.stop()
        with patch.object(core.smtplib, "SMTP") as connect:
            with self.assertRaisesRegex(RuntimeError, "maintenance"):
                core.smtp_test({"host": "smtp.invalid"}, "fixture@example.invalid")
            connect.assert_not_called()

    def test_maintenance_loop_skips_poll_and_stall_alert(self):
        cfg = self.maintenance_config()
        cfg["system"].update(alert_email="fixture@example.invalid", alert_hours=1)
        core.save_cfg(cfg)
        Path(core.LAST_POLL_FILE).write_text("1900-01-01T00:00:00")
        with patch.object(core, "_poll_stop") as event, patch.object(core, "poll_once") as poll:
            event.is_set.side_effect = [False, True]
            core.poll_loop()
            event.wait.assert_called_once_with(timeout=1)
            poll.assert_not_called()
        self.smtp.assert_not_called()

    def test_maintenance_loop_rechecks_before_long_configured_interval(self):
        cfg = self.maintenance_config()
        def resume(**kwargs):
            cfg["system"]["maintenance"] = False
            core.save_cfg(cfg)
        with patch.object(core, "_poll_stop") as event, patch.object(core, "poll_once") as poll:
            event.is_set.side_effect = [False, False, True]
            event.wait.side_effect = resume
            core.poll_loop()
            self.assertEqual(event.wait.call_args_list[0].kwargs, {"timeout": 1})
            poll.assert_called_once()

    def test_invalid_maintenance_configuration_fails_closed(self):
        cfg = core.load_cfg()
        for value in ["false", None, 0, [], {}]:
            with self.subTest(value=value):
                cfg["system"]["maintenance"] = value
                core.save_cfg(cfg)
                self.assertTrue(core.maintenance_active())
                core.poll_once()
                self.assertEqual(self.client.post("/poll-now").status_code, 503)
        for raw in ["{", "[]", '{"system": []}']:
            Path(core.CFG_PATH).write_text(raw)
            self.assertTrue(core.maintenance_active())
            core.poll_once()
        self.pop.retr.assert_not_called()
        self.smtp.assert_not_called()

    def test_seen_uidl_never_fetches_or_forwards(self):
        core.seen_uidl_add("fixture", "fixture-uidl")
        core.poll_once()
        self.pop.retr.assert_not_called()
        self.smtp.assert_not_called()
        self.pop.dele.assert_not_called()

    def test_new_message_forwards_once_across_repeated_polls(self):
        core.poll_once()
        core.poll_once()
        self.assertEqual(self.pop.retr.call_count, 1)
        self.assertEqual(self.smtp.call_count, 1)
        self.pop.dele.assert_not_called()

    def test_legacy_seen_message_indexes_uidl_without_resend(self):
        core.seen_add(
            core.make_key(
                "fixture-uidl", "Synthetic", "fixture", "pop.invalid", "fixture"
            ),
            "fixture",
        )
        core.poll_once()
        self.smtp.assert_not_called()
        self.assertTrue(core.seen_uidl_has("fixture", "fixture-uidl"))

    def test_uidl_unsupported_falls_back_without_duplicate_send(self):
        self.pop.uidl.side_effect = poplib.error_proto("-ERR unsupported")
        self.pop.list.return_value = (b"+OK", [b"1 80"], 1)
        core.poll_once()
        core.poll_once()
        self.assertEqual(self.pop.retr.call_count, 2)
        self.assertEqual(self.smtp.call_count, 1)

    def test_uidl_timeout_does_not_trigger_fallback(self):
        self.pop.uidl.side_effect = TimeoutError("fixture")
        core.poll_once()
        self.pop.list.assert_not_called()
        self.pop.retr.assert_not_called()
        self.smtp.assert_not_called()

    def test_stls_failure_never_sends_credentials(self):
        for error in [poplib.error_proto("-ERR TLS"), OSError("TLS unavailable")]:
            with self.subTest(error=type(error).__name__):
                self.pop.reset_mock()
                self.pop.stls.side_effect = error
                core.poll_once()
                self.pop.user.assert_not_called()
                self.pop.pass_.assert_not_called()
                self.pop.close.assert_called()
                self.smtp.assert_not_called()

    def test_pop3_test_fails_at_stls_before_auth(self):
        self.pop.stls.side_effect = poplib.error_proto("-ERR TLS")
        result = core.pop3_test(self.mailbox)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "stls")
        self.pop.user.assert_not_called()
        self.pop.pass_.assert_not_called()

    def test_missing_connection_fields_never_connect(self):
        with patch.object(core.poplib, "POP3") as connect:
            result = core.pop3_test({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "config")
        for key in ["pop3_host", "pop3_user", "pop3_pass", "dest_to"]:
            self.assertIn(key, result["error"])
        connect.assert_not_called()

    def test_pop3_test_success_never_fetches_or_deletes(self):
        self.assertTrue(core.pop3_test(self.mailbox)["ok"])
        self.pop.retr.assert_not_called()
        self.pop.dele.assert_not_called()
        self.smtp.assert_not_called()

    def test_path_traversal_and_symlink_do_not_read_or_delete_config(self):
        config = Path(core.CFG_PATH)
        original = config.read_bytes()
        (Path(core.ATT_DIR) / "escape.wav").symlink_to(config)
        for path in ["../../data/config.json", "escape.wav"]:
            self.assertIsNone(core.safe_attachment_path(path))
            self.assertEqual(self.client.get("/attachments/" + path).status_code, 404)
            self.client.post("/attachments/delete/" + path)
            self.assertEqual(config.read_bytes(), original)

    def test_regular_attachment_serves_and_deletes(self):
        p = Path(core.ATT_DIR) / "fixture.wav"
        p.write_bytes(b"RIFFsynthetic")
        with self.client.get("/attachments/fixture.wav") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"RIFFsynthetic")
        self.assertEqual(
            self.client.post("/attachments/delete/fixture.wav").status_code, 302
        )
        self.assertFalse(p.exists())

    def test_rotated_gzip_log_search_and_pages(self):
        Path(core.LOG_FILE).write_text("[2026-01-02 00:00:00] INFO | new fixture\n")
        with gzip.open(core.LOG_FILE + ".1.gz", "wt") as stream:
            stream.write("[2026-01-01 00:00:00] INFO | old fixture\n")
        text, _ = core.search_log(q="old fixture")
        self.assertIn("old fixture", text)
        for path in ["/", "/settings", "/mailboxes", "/logs", "/attachments"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_sqlite_backup_preserves_deduplication(self):
        core.seen_uidl_add("fixture", "fixture-uidl")
        backup = Path(self.tmp.name) / "restore.db"
        with sqlite3.connect(core.DB_PATH) as source, sqlite3.connect(backup) as dest:
            source.backup(dest)
        with sqlite3.connect(backup) as restored:
            self.assertEqual(
                restored.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                restored.execute("SELECT uidl FROM seen_uidl").fetchone()[0],
                "fixture-uidl",
            )


class GunicornTests(unittest.TestCase):
    @classmethod
    def prepare_fixture(cls):
        pass

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="voicemail-http-")
        cls.prepare_fixture()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        env = dict(os.environ, APP_BASE=cls.tmp.name)
        cls.log = tempfile.TemporaryFile()
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gunicorn",
                "-b",
                f"127.0.0.1:{cls.port}",
                "-w",
                "1",
                "--threads",
                "8",
                "app.app:app",
            ],
            env=env,
            stdout=cls.log,
            stderr=cls.log,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if b"200 OK" in cls.exchange(
                    b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                ):
                    return
            except OSError:
                pass
            if cls.proc.poll() is not None:
                break
            time.sleep(0.1)
        cls.tearDownClass()
        raise RuntimeError("Isolated Gunicorn did not become ready")

    @classmethod
    def tearDownClass(cls):
        if cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait()
        cls.log.close()
        cls.tmp.cleanup()

    @classmethod
    def exchange(cls, payload):
        with socket.create_connection(("127.0.0.1", cls.port), timeout=5) as conn:
            conn.sendall(payload)
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

    def test_regular_http_pages_work(self):
        for path in ["/", "/settings", "/mailboxes", "/logs", "/attachments"]:
            with self.subTest(path=path):
                response = self.exchange(
                    f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
                )
                self.assertIn(b"200 OK", response.split(b"\r\n", 1)[0])

    def test_invalid_transfer_encoding_rejected(self):
        for encoding in [b"identity", b"chunked, identity", b"gzip"]:
            with self.subTest(encoding=encoding):
                response = self.exchange(
                    b"POST /poll-now HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nTransfer-Encoding: "
                    + encoding
                    + b"\r\nContent-Length: 0\r\n\r\n"
                )
                self.assertIn(b"400", response.split(b"\r\n", 1)[0])


class MaintenanceGunicornTests(GunicornTests):
    @classmethod
    def prepare_fixture(cls):
        base = Path(cls.tmp.name)
        (base / "data").mkdir()
        (base / "logs/attachments").mkdir(parents=True)
        cls.config = base / "data/config.json"
        cls.timestamp = base / "data/last_poll.txt"
        cls.attachment = base / "logs/attachments/expired.txt"
        cfg = core.default_config()
        cfg["system"].update(maintenance=True, poll_interval_seconds=3600,
                             attachment_retention_months=1)
        cls.config.write_text(json.dumps(cfg))
        cls.timestamp.write_text("unchanged")
        cls.attachment.write_bytes(b"synthetic old attachment")
        with sqlite3.connect(base / "data/state.db") as conn:
            conn.execute("CREATE TABLE attachments(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "filename TEXT NOT NULL, mailbox TEXT NOT NULL, subject TEXT, ts TEXT NOT NULL)")
            conn.execute("INSERT INTO attachments(filename,mailbox,subject,ts) VALUES (?,?,?,?)",
                         ("expired.txt", "fixture", "synthetic", "1900-01-01T00:00:00"))

    def test_maintenance_warmup_preserves_then_activation_resumes_promptly(self):
        time.sleep(1.2)  # Exercise a real maintenance wait cycle in Gunicorn.
        self.assertTrue(self.attachment.exists())
        self.assertEqual(self.timestamp.read_text(), "unchanged")
        response = self.exchange(b"POST /poll-now HTTP/1.1\r\nHost: localhost\r\n"
                                 b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        self.assertIn(b"503", response.split(b"\r\n", 1)[0])
        cfg = json.loads(self.config.read_text())
        cfg["system"]["maintenance"] = False
        pending = self.config.with_suffix(".pending")
        pending.write_text(json.dumps(cfg))
        os.replace(pending, self.config)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.timestamp.read_text() == "unchanged":
            time.sleep(0.1)
        self.assertNotEqual(self.timestamp.read_text(), "unchanged")
        self.assertFalse(self.attachment.exists())  # Normal retention resumes only after activation.


if __name__ == "__main__":
    unittest.main()
