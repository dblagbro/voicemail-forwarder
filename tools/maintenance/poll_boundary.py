"""Fail-closed interpretation of frame-only py-spy dumps for an idle worker.

This is not a live polling lock. The caller must freeze the exact container
before the final sample and keep it frozen throughout the snapshot.
"""


def idle_worker(threads, executor_wait_site=None):
    if not isinstance(threads, list) or not threads:
        return False
    pollers = mains = 0
    for thread in threads:
        frames = thread.get("frames") or []
        if (
            not frames
            or type(thread.get("active")) is not bool
            or thread.get("owns_gil") is not False
        ):
            return False
        if any(frame.get("locals") is not None for frame in frames):
            return False
        top = frames[0]
        name = thread.get("thread_name")
        app_frames = [
            f for f in frames if f.get("filename", "").startswith("/app/app/")
        ]
        if name == "poller":
            if thread["active"]:
                return False
            pollers += 1
            if top.get("name") != "wait" or not top.get("filename", "").endswith(
                "/threading.py"
            ):
                return False
            if len(app_frames) != 1 or app_frames[0].get("name") != "poll_loop":
                return False
        elif name == "MainThread":
            if thread["active"]:
                return False
            mains += 1
            if (
                app_frames
                or top.get("name") != "select"
                or not top.get("filename", "").endswith("/selectors.py")
            ):
                return False
            if not any(
                f.get("filename", "").endswith("/gunicorn/workers/gthread.py")
                and f.get("name") == "run"
                for f in frames
            ):
                return False
        elif name and name.startswith("ThreadPoolExecutor-"):
            if (
                app_frames
                or top.get("name") != "_worker"
                or not top.get("filename", "").endswith("/concurrent/futures/thread.py")
                or not executor_wait_site
                or top.get("filename") != executor_wait_site["filename"]
                or top.get("line") != executor_wait_site["line"]
            ):
                return False
        else:
            return False
    return pollers == mains == 1
