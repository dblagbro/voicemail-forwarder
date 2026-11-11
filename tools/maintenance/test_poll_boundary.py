import copy
import unittest
from poll_boundary import idle_worker


def frame(name, filename):
    return {"name": name, "filename": filename, "locals": None}


def idle():
    return [
        {
            "thread_name": "poller",
            "active": False,
            "owns_gil": False,
            "frames": [
                frame("wait", "/usr/local/lib/python3.12/threading.py"),
                frame("poll_loop", "/app/app/core.py"),
            ],
        },
        {
            "thread_name": "MainThread",
            "active": False,
            "owns_gil": False,
            "frames": [
                frame("select", "/usr/local/lib/python3.12/selectors.py"),
                frame(
                    "run",
                    "/usr/local/lib/python3.12/site-packages/gunicorn/workers/gthread.py",
                ),
            ],
        },
    ]


class BoundaryTests(unittest.TestCase):
    def test_idle_poller_and_listener(self):
        self.assertTrue(idle_worker(idle()))

    def test_idle_executor_allowed(self):
        data = idle()
        data.append(
            {
                "thread_name": "ThreadPoolExecutor-0_0",
                "active": False,
                "owns_gil": False,
                "frames": [
                    frame(
                        "_worker",
                        "/usr/local/lib/python3.12/concurrent/futures/thread.py",
                    )
                ],
            }
        )
        site = {
            "filename": "/usr/local/lib/python3.12/concurrent/futures/thread.py",
            "line": 90,
        }
        data[-1]["frames"][0]["line"] = 90
        self.assertFalse(idle_worker(data))
        self.assertTrue(idle_worker(data, site))
        data[-1]["active"] = True
        self.assertTrue(idle_worker(data, site))
        data[-1]["frames"][0]["line"] = 91
        self.assertFalse(idle_worker(data, site))

    def test_automatic_mail_send_is_never_idle(self):
        for name in [
            "poll_once",
            "smtp_send",
            "archive_attachments",
            "seen_add",
            "prune_old_attachments",
        ]:
            with self.subTest(name=name):
                data = idle()
                data[0]["frames"].insert(0, frame(name, "/app/app/core.py"))
                self.assertFalse(idle_worker(data))

    def test_manual_poll_and_config_write_are_never_idle(self):
        for name in [
            "manual_poll_now",
            "save_cfg",
            "settings",
            "toggle_mailbox",
            "smtp_test",
        ]:
            with self.subTest(name=name):
                data = idle()
                data.append(
                    {
                        "thread_name": "ThreadPoolExecutor-0_0",
                        "active": False,
                        "owns_gil": False,
                        "frames": [
                            frame("wait", "/threading.py"),
                            frame(name, "/app/app/core.py"),
                        ],
                    }
                )
                self.assertFalse(idle_worker(data))

    def test_unknown_or_partial_stack_rejected(self):
        for value in [None, {}, [], [{}], idle()[:1], idle() + [{}]]:
            self.assertFalse(idle_worker(value))
        data = idle()
        data[0]["thread_name"] = "unknown"
        self.assertFalse(idle_worker(data))
        data = idle()
        data[0]["frames"] = []
        self.assertFalse(idle_worker(data))
        data = idle()
        del data[0]["active"]
        self.assertFalse(idle_worker(data))

    def test_active_or_gil_owning_thread_rejected(self):
        for key in ["active", "owns_gil"]:
            data = idle()
            data[0][key] = True
            self.assertFalse(idle_worker(data))

    def test_locals_and_extra_pollers_rejected(self):
        data = idle()
        data[0]["frames"][0]["locals"] = {}
        self.assertFalse(idle_worker(data))
        data = idle()
        data.append(copy.deepcopy(data[0]))
        self.assertFalse(idle_worker(data))


if __name__ == "__main__":
    unittest.main()
