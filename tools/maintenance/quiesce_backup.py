"""Prepare a consistent voicemail backup and resume the SAME original container.

Linux/root only. This does not deploy a candidate. All configuration, stacks,
container inspection and file manifests stay in a private recovery generation.
Run through the capacity guard and a systemd unit with ExecStopPost=recover.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from configuration_files import (
    ConfigurationConflict,
    copy_verified,
    digest,
    read_regular,
    sync_directory,
    write_new,
)
from poll_boundary import idle_worker


def run(args, **kwargs):
    return subprocess.check_output(args, timeout=kwargs.pop("timeout", 30), **kwargs)


def inspect(cid):
    items = json.loads(run(["docker", "inspect", cid]))
    if len(items) != 1:
        raise RuntimeError("Ambiguous container")
    return items[0]


def config_fingerprint(state):
    host = dict(state["HostConfig"])
    return digest(
        json.dumps(
            {"Config": state["Config"], "HostConfig": host, "Mounts": state["Mounts"]},
            sort_keys=True,
        ).encode()
    )


def private_directory(path, create=False):
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise RuntimeError("Private directory must have an absolute real parent")
    if create:
        path.mkdir(mode=0o700)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
        raise RuntimeError("Unowned private directory")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Private directory permissions too broad")


def journal(directory, state):
    state["updated_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    temporary = directory / ("journal-new-" + str(time.time_ns()))
    write_new(temporary, (json.dumps(state, indent=2) + "\n").encode())
    temporary.replace(directory / "journal.json")
    sync_directory(directory)


def load(directory):
    private_directory(directory)
    return json.loads(read_regular(directory / "journal.json")[0])


def checkpoint(directory, state, phase):
    state["phase"] = phase
    journal(directory, state)


def owned_state(state):
    current = inspect(state["container_id"])
    if current["Id"] != state["container_id"] or current["Image"] != state["image"]:
        raise RuntimeError("Container identity changed")
    if config_fingerprint(current) != state["configuration_fingerprint"]:
        raise RuntimeError("Concurrent container configuration change")
    return current


def process_rows(cid):
    lines = run(["docker", "top", cid, "-eo", "pid,ppid,comm"], text=True).splitlines()[
        1:
    ]
    rows = []
    for line in lines:
        fields = line.split()
        if len(fields) != 3 or fields[2] != "gunicorn":
            raise RuntimeError("Unexpected process in voicemail container")
        rows.append((int(fields[0]), int(fields[1])))
    return rows


def executor_wait_site(cid):
    # Identify the exact blocking queue read in this image's standard library.
    # py-spy's active heuristic incorrectly marks SimpleQueue.get as active.
    program = """import concurrent.futures.thread as t,inspect,json,hashlib
from pathlib import Path
lines,first=inspect.getsourcelines(t._worker)
sites=[first+i for i,line in enumerate(lines) if line.strip()=='work_item = work_queue.get(block=True)']
assert len(sites)==1
p=Path(inspect.getsourcefile(t))
print(json.dumps({'filename':str(p),'line':sites[0],'module_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))
"""
    return json.loads(run(["docker", "exec", cid, "python", "-c", program]))


def frozen_idle_sample(directory, state):
    current = owned_state(state)
    if not current["State"]["Paused"]:
        raise RuntimeError("Final stack sample requires a frozen container")
    master = current["State"]["Pid"]
    rows = process_rows(state["container_id"])
    workers = [pid for pid, parent in rows if parent == master]
    if len(rows) != 2 or len(workers) != 1 or master not in {pid for pid, _ in rows}:
        raise RuntimeError("Expected one Gunicorn master and one worker")
    raw = run(
        [state["py_spy"], "dump", "--pid", str(workers[0]), "--json", "--nonblocking"],
        timeout=15,
    )
    threads = json.loads(raw)
    site = state["executor_wait_site"]
    module = Path("/proc") / str(workers[0]) / "root" / site["filename"].lstrip("/")
    if digest(module.read_bytes()) != site["module_sha256"]:
        raise RuntimeError("Executor module changed after preflight")
    result = idle_worker(threads, site)
    # Never request locals; reject a profiler that unexpectedly returns them.
    if any(f.get("locals") is not None for t in threads for f in t.get("frames", [])):
        raise RuntimeError("Unexpected local-variable payload")
    if process_rows(state["container_id"]) != rows:
        raise RuntimeError("Process identity changed during sample")
    state["idle_sample"] = {
        "accepted": result,
        "thread_count": len(threads),
        "stack_sha256": digest(raw),
        "master_pid": master,
        "worker_pid": workers[0],
    }
    # Frame names only, retained privately even though no application values occur.
    write_new(directory / ("stack-" + str(time.time_ns()) + ".json"), raw)
    return result


def tree_manifest(root):
    entries = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            raise RuntimeError("Backup refuses symlinks; explicit review required")
        if path.is_file():
            data, _ = read_regular(path)
            entries[relative] = {"sha256": digest(data), "bytes": len(data)}
        elif not path.is_dir():
            raise RuntimeError("Backup refuses special files")
    return entries


def tree_copy(source, destination):
    independent_paths([source.resolve(), destination.resolve()])
    before = tree_manifest(source)
    destination.mkdir(mode=0o700)
    for name in before:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        copy_verified(source / name, target)
    after = tree_manifest(destination)
    if before != after or before != tree_manifest(source):
        raise RuntimeError("Backup mismatch or source changed")
    return before


def independent_paths(paths):
    for i, first in enumerate(paths):
        for second in paths[i + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise RuntimeError("Backup and source paths must not overlap")


def http_ready(state):
    # Read only the response status inside this exact container. Do not export
    # the HTML, which includes mailbox configuration and application log text.
    program = (
        "import urllib.request; "
        'r=urllib.request.urlopen("http://127.0.0.1:8089/", timeout=3); '
        'assert r.status == 200; print("HTTP_200")'
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        owned_state(state)
        try:
            result = run(
                ["docker", "exec", state["container_id"], "python", "-c", program],
                timeout=5,
                stderr=subprocess.DEVNULL,
            )
            if result.strip() == b"HTTP_200":
                return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.5)
    raise RuntimeError("Original HTTP service did not become ready")


def database_check(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("pragma integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite integrity check failed")
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        if not {"seen", "seen_uidl", "attachments"}.issubset(tables):
            raise RuntimeError("Deduplication schema missing")
    finally:
        connection.close()


def resume_original(directory, state):
    # Recovery only releases this container's freeze. It never restarts an
    # externally stopped service, changes configuration, or restores old data.
    current = owned_state(state)
    if not current["State"]["Running"]:
        raise RuntimeError("Original was externally stopped; operator review required")
    if current["State"]["Paused"]:
        run(["docker", "unpause", state["container_id"]])
    current = owned_state(state)
    if not current["State"]["Running"] or current["State"]["Paused"]:
        raise RuntimeError("Original did not resume")
    http_ready(state)
    state["original_http_200"] = True
    checkpoint(directory, state, "resumed")


def sanitized(directory, state, error=None):
    result = {
        k: state.get(k)
        for k in [
            "phase",
            "started_utc",
            "updated_utc",
            "container_id",
            "image",
            "idle_sample",
            "backup",
            "fixture",
            "original_http_200",
            "failure_phase",
            "recovery_error_type",
        ]
    }
    result.update(
        deployed=False,
        original_resumed=state["phase"] == "resumed",
        application_values_exported=False,
        error=error,
    )
    p = directory / "result.sanitized.json"
    temporary = directory / ("result-new-" + str(time.time_ns()))
    write_new(temporary, (json.dumps(result, indent=2) + "\n").encode())
    temporary.replace(p)
    sync_directory(directory)
    print(json.dumps(result))


def prepare(args):
    os.umask(0o077)
    directory = args.generation
    private_directory(directory, create=True)
    current = inspect(args.container)
    name = current["Name"].lstrip("/")
    fixture = (
        name.startswith("codex-voicemail-")
        and current["Config"].get("Labels", {}).get("codex.voicemail.fixture") == "true"
    )
    if name != "voicemail-forwarder" and not fixture:
        raise RuntimeError("Unexpected service; a fixture requires its ownership label")
    if (
        current["Image"] != args.expected_image
        or not current["State"]["Running"]
        or current["State"]["Paused"]
    ):
        raise RuntimeError("Expected running original image not found")
    mounts = {x["Destination"]: x for x in current["Mounts"]}
    for path in ["/app/data", "/app/logs"]:
        if (
            path not in mounts
            or mounts[path]["Type"] != "bind"
            or not mounts[path]["RW"]
        ):
            raise RuntimeError("Expected independent writable data/log bind mounts")
    data = Path(mounts["/app/data"]["Source"])
    logs = Path(mounts["/app/logs"]["Source"])
    if data.resolve() != data or logs.resolve() != logs or data == logs:
        raise RuntimeError("Ambiguous data/log paths")
    if args.san_root.resolve() != args.san_root or not args.san_root.is_dir():
        raise RuntimeError("SAN parent must already exist and be a real directory")
    private_directory(args.san_root)
    independent_paths([data, logs, directory, args.san_root / directory.name])
    configuration = data / "config.json"
    original, metadata = read_regular(configuration)
    write_new(directory / "original-config.json", original)
    write_new(
        directory / "container.private.json",
        (json.dumps(current, indent=2) + "\n").encode(),
    )
    state = {
        "phase": "prepared",
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "container_id": current["Id"],
        "image": current["Image"],
        "fixture": fixture,
        "configuration_fingerprint": config_fingerprint(current),
        "configuration_path": str(configuration),
        "configuration_mode": metadata.st_mode & 0o777,
        "configuration_uid": metadata.st_uid,
        "configuration_gid": metadata.st_gid,
        "restart_policy": current["HostConfig"]["RestartPolicy"],
        "data": str(data),
        "logs": str(logs),
        "token": "backup-" + directory.name,
        "py_spy": str(args.py_spy),
        "san": str(args.san_root / directory.name),
    }
    state["executor_wait_site"] = executor_wait_site(state["container_id"])
    journal(directory, state)
    deadline = time.monotonic() + args.idle_timeout
    try:
        while time.monotonic() < deadline:
            checkpoint(directory, state, "pause_intent")
            run(["docker", "pause", state["container_id"]])
            checkpoint(directory, state, "paused")
            if frozen_idle_sample(directory, state):
                checkpoint(directory, state, "idle_verified")
                break
            run(["docker", "unpause", state["container_id"]])
            checkpoint(directory, state, "prepared")
            time.sleep(1)
        else:
            raise RuntimeError("No verified idle boundary before deadline")
        if read_regular(configuration)[0] != original:
            raise ConfigurationConflict(
                "Configuration changed before snapshot; edit preserved"
            )
        checkpoint(directory, state, "snapshot_intent")
        local = directory / "snapshot"
        local.mkdir(mode=0o700)
        manifest = {
            "data": tree_copy(data, local / "data"),
            "logs": tree_copy(logs, local / "logs"),
        }
        database_check(local / "data/state.db")
        checkpoint(directory, state, "local_complete")
        run(["docker", "unpause", state["container_id"]])
        checkpoint(directory, state, "snapshot_released")
        manifest_raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        write_new(directory / "manifest.private.json", manifest_raw)
        san = Path(state["san"])
        san.mkdir(mode=0o700)
        remote_manifest = {
            "data": tree_copy(local / "data", san / "data"),
            "logs": tree_copy(local / "logs", san / "logs"),
        }
        database_check(san / "data/state.db")
        checkpoint(directory, state, "san_complete")
        if manifest != remote_manifest:
            raise RuntimeError("SAN manifest mismatch")
        copy_verified(
            directory / "manifest.private.json", san / "manifest.private.json"
        )
        restored = directory / "independent-restore"
        restored.mkdir(mode=0o700)
        tree_copy(san / "data", restored / "data")
        tree_copy(san / "logs", restored / "logs")
        database_check(restored / "data/state.db")
        state["backup"] = {
            "local": str(local),
            "san": str(san),
            "independent_restore": str(restored),
            "manifest_sha256": digest(manifest_raw),
            "files": sum(len(v) for v in manifest.values()),
            "bytes": sum(x["bytes"] for v in manifest.values() for x in v.values()),
            "sqlite_integrity": True,
            "san_verified": True,
            "independent_restore_verified": True,
        }
        checkpoint(directory, state, "backup_complete")
        resume_original(directory, state)
        sanitized(directory, state)
    except BaseException as exc:
        state["failure_phase"] = state["phase"]
        state["failure_type"] = type(exc).__name__
        journal(directory, state)
        try:
            resume_original(directory, state)
        except BaseException as recovery_error:
            state["recovery_error_type"] = type(recovery_error).__name__
            journal(directory, state)
        sanitized(directory, state, type(exc).__name__)
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["prepare", "recover"])
    p.add_argument("--generation", type=Path, required=True)
    p.add_argument("--container", default="voicemail-forwarder")
    p.add_argument("--expected-image")
    p.add_argument("--san-root", type=Path)
    p.add_argument(
        "--py-spy", type=Path, default=Path("/home/dblagbro/.local/bin/py-spy")
    )
    p.add_argument("--idle-timeout", type=int, default=180)
    args = p.parse_args()
    if os.geteuid() != 0 or not __debug__:
        raise SystemExit("Requires root and unoptimized Python")
    if args.mode == "recover":
        # No mutation occurs before the first journal is durably written.
        if not (args.generation / "journal.json").exists():
            print(json.dumps({"recovery_required": False, "reason": "no_journal"}))
            return
        state = load(args.generation)
        if state["phase"] != "resumed":
            resume_original(args.generation, state)
        sanitized(args.generation, state)
    else:
        if not args.expected_image or args.san_root is None:
            p.error("prepare requires --expected-image and --san-root")
        prepare(args)


if __name__ == "__main__":
    main()
