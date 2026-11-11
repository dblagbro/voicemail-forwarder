"""Idle-boundary stop/recovery primitives; caller owns journaling and ingress.

No candidate deployment is performed here. Never force-kill a mail worker.
"""

import json
import os
from pathlib import Path
import signal
import time

import quiesce_backup as backup
from cutover_files import (
    ConfigurationConflict,
    Publication,
    read_regular,
    move_absent,
    sync_directory,
)


def maintenance_config(original):
    result = json.loads(original)
    if not isinstance(result.get("mailboxes"), list) or not isinstance(
        result.get("system"), dict
    ):
        raise ValueError("Unexpected mail configuration shape")
    for mailbox in result["mailboxes"]:
        mailbox["enabled"] = False
    system = result["system"]
    system.update(alert_email="", alert_hours=0, poll_interval_seconds=3600)
    # Fence queued test-send actions too, even though ingress is already blocked.
    system["smtp"] = {
        **system.get("smtp", {}),
        "host": "",
        "user": "",
        "pass": "",
        "from": "",
    }
    return (json.dumps(result, indent=2) + "\n").encode()


def publication(directory, state, reverse=False):
    original = read_regular(directory / "original-config.json")[0]
    fenced = maintenance_config(original)
    before, after = (fenced, original) if reverse else (original, fenced)
    return Publication(
        Path(state["configuration_path"]),
        before,
        after,
        state["token"] + ("-restore" if reverse else "-fence"),
        state["configuration_mode"],
        state["configuration_uid"],
        state["configuration_gid"],
    )


def restore_configuration(directory, state):
    fence = publication(directory, state)
    restore = publication(directory, state, True)
    current = fence._read(fence.canonical)
    if current == fence.original:
        return
    if (
        current is None
        and fence._read(fence.previous) == fence.original
        and restore._read(restore.previous) is None
    ):
        move_absent(fence.previous, fence.canonical)
        os.chmod(fence.canonical, state["configuration_mode"])
        sync_directory(fence.canonical.parent)
        return
    restore.apply()


def owned_state(state):
    current = backup.inspect(state["container_id"])
    if current["Id"] != state["container_id"] or current["Image"] != state["image"]:
        raise RuntimeError("Original container identity changed")
    # Only our temporary restart=no change is allowed.
    actual = current["HostConfig"]["RestartPolicy"]
    if actual not in [state["restart_policy"], {"Name": "no", "MaximumRetryCount": 0}]:
        raise RuntimeError("Concurrent restart policy change")
    current["HostConfig"]["RestartPolicy"] = state["restart_policy"]
    if backup.config_fingerprint(current) != state["configuration_fingerprint"]:
        raise RuntimeError("Concurrent original runtime change")
    current["HostConfig"]["RestartPolicy"] = actual
    return current


def restart_policy(cid, policy):
    value = policy["Name"] or "no"
    if value == "on-failure" and policy.get("MaximumRetryCount"):
        value += ":" + str(policy["MaximumRetryCount"])
    backup.run(["docker", "update", "--restart=" + value, cid])


def stop_idle(directory, state, checkpoint):
    current = owned_state(state)
    if not current["State"]["Running"]:
        if state["phase"] not in [
            "term_intent",
            "worker_term_queued",
            "term_released",
            "stopped",
            "stopped_original_config_restored",
        ]:
            raise RuntimeError("Original was stopped outside this operation")
        if current["State"]["ExitCode"] != 0 or current["State"].get("OOMKilled"):
            raise RuntimeError("Original did not stop cleanly")
        checkpoint("stopped")
        return
    if not current["State"]["Paused"]:
        raise RuntimeError("Stopping requires the frozen verified idle boundary")
    idle = state["idle_sample"]
    if not idle["accepted"] or current["State"]["Pid"] != idle["master_pid"]:
        raise RuntimeError("Idle worker identity changed")
    rows = backup.process_rows(state["container_id"])
    if len(rows) != 2 or (idle["worker_pid"], idle["master_pid"]) not in rows:
        raise RuntimeError("Unexpected workers after idle sample")
    checkpoint("configuration_fence_intent")
    publication(directory, state).apply(lambda phase: checkpoint("config_" + phase))
    checkpoint("configuration_fenced")
    restart_policy(state["container_id"], {"Name": "no"})
    checkpoint("term_intent")
    # Queue TERM for BOTH the verified worker and master before releasing the
    # freeze; the worker cannot race the master's signal-forwarding delay.
    os.kill(idle["worker_pid"], signal.SIGTERM)
    checkpoint("worker_term_queued")
    os.kill(idle["master_pid"], signal.SIGTERM)
    backup.run(["docker", "unpause", state["container_id"]])
    checkpoint("term_released")
    await_stopped(directory, state, checkpoint)


def await_stopped(directory, state, checkpoint):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = owned_state(state)
        if not current["State"]["Running"]:
            if current["State"]["ExitCode"] != 0 or current["State"].get("OOMKilled"):
                raise RuntimeError("Original did not stop cleanly")
            checkpoint("stopped")
            return
        time.sleep(0.2)
    raise RuntimeError("Graceful exit timed out; no force-kill performed")


def recover_original(directory, state, checkpoint):
    current = owned_state(state)
    if (
        state["phase"] in ["term_intent", "worker_term_queued", "term_released"]
        and current["State"]["Running"]
    ):
        if current["State"]["Paused"]:
            # A crash can occur after recording intent but before either signal.
            # The private fence is required before repeating the owned signals.
            p = publication(directory, state)
            if p._read(p.canonical) != p.desired:
                raise ConfigurationConflict("Configuration fence changed")
            idle = state["idle_sample"]
            if current["State"]["Pid"] != idle["master_pid"]:
                raise RuntimeError("Original master changed during recovery")
            for pid in [idle["worker_pid"], idle["master_pid"]]:
                os.kill(pid, signal.SIGTERM)
            backup.run(["docker", "unpause", state["container_id"]])
        await_stopped(directory, state, checkpoint)
        current = owned_state(state)
    if not current["State"]["Running"] and state["phase"] not in [
        "stopped",
        "stopped_original_config_restored",
        "term_intent",
        "worker_term_queued",
        "term_released",
    ]:
        raise RuntimeError("Original was stopped outside this operation")
    restore_configuration(directory, state)
    restart_policy(state["container_id"], state["restart_policy"])
    if current["State"]["Paused"]:
        backup.run(["docker", "unpause", state["container_id"]])
    elif not current["State"]["Running"]:
        backup.run(["docker", "start", state["container_id"]])
    current = owned_state(state)
    if not current["State"]["Running"] or current["State"]["Paused"]:
        raise RuntimeError("Original service did not resume")
