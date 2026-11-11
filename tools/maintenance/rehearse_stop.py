"""Synthetic, network-disabled mail fixtures plus a separate host firewall probe.

The INPUT fence is tested only on a dedicated loopback test listener. This is
not an end-to-end production cutover. No production container is changed.
"""

import argparse
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

import quiesce_backup as q
import quiescent_stop as stop
from ingress_fence import Fence, marked_http_status


def fence_for(state):
    f = state["fence"]
    return Fence(f["token"], f["port"], f["mark"])


def recover(directory):
    if not (directory / "journal.json").exists():
        return
    state = q.load(directory)
    if state["phase"] == "recovered":
        return

    def checkpoint(phase):
        if phase == "stopped":
            state["clean_stop_verified"] = True
        q.checkpoint(directory, state, phase)

    stop.recover_original(directory, state, checkpoint)
    q.http_ready(
        {
            "container_id": state["container_id"],
            "image": state["image"],
            "configuration_fingerprint": state["configuration_fingerprint"],
        }
    )
    fence_for(state).remove()
    q.checkpoint(directory, state, "recovered")


def worker(directory, container, port, fault):
    q.private_directory(directory, create=True)
    current = q.inspect(container)
    if (
        not current["Name"].startswith("/codex-voicemail-")
        or current["Config"].get("Labels", {}).get("codex.voicemail.fixture") != "true"
    ):
        raise RuntimeError("Rehearsal refuses a production or unowned container")
    if current["HostConfig"]["NetworkMode"] != "none":
        raise RuntimeError("Mail fixtures require network none")
    data = next(
        Path(x["Source"]) for x in current["Mounts"] if x["Destination"] == "/app/data"
    )
    cfg = data / "config.json"
    original, metadata = q.read_regular(cfg)
    q.write_new(directory / "original-config.json", original)
    state = {
        "phase": "prepared",
        "container_id": current["Id"],
        "image": current["Image"],
        "configuration_fingerprint": q.config_fingerprint(current),
        "configuration_path": str(cfg),
        "configuration_mode": metadata.st_mode & 0o777,
        "configuration_uid": metadata.st_uid,
        "configuration_gid": metadata.st_gid,
        "restart_policy": current["HostConfig"]["RestartPolicy"],
        "token": "test-" + secrets.token_hex(8),
        "py_spy": "/home/dblagbro/.local/bin/py-spy",
        "executor_wait_site": q.executor_wait_site(current["Id"]),
        "fence": {
            "token": secrets.token_hex(8),
            "port": port,
            "mark": secrets.randbelow(0xFFFFFFFE) + 1,
        },
        "clean_stop_verified": False,
    }
    q.journal(directory, state)

    def checkpoint(phase):
        if phase == "stopped":
            state["clean_stop_verified"] = True
        q.checkpoint(directory, state, phase)
        if phase == fault:
            q.write_new(
                directory / "fault-injected.json",
                (json.dumps({"phase": phase, "signal": "SIGKILL"}) + "\n").encode(),
            )
            os.kill(os.getpid(), 9)

    fence = fence_for(state)
    fence.preflight()
    try:
        for host in ["127.0.0.1", "::1"]:
            assert marked_http_status(port, 0, host) == 200
        checkpoint("fence_intent")
        fence.apply(checkpoint)
        for host in ["127.0.0.1", "::1"]:
            denied = False
            try:
                marked_http_status(port, 0, host)
            except (OSError, ConnectionError):
                denied = True
            assert denied, "Unmarked host probe bypassed fence"
            assert marked_http_status(port, state["fence"]["mark"], host) == 200
        checkpoint("fenced_verified")
        # Only connections on the dedicated synthetic host listener are reset.
        q.run(["ss", "-K", "sport = :" + str(port)])
        for attempt in range(20):
            checkpoint("pause_intent")
            q.run(["docker", "pause", current["Id"]])
            checkpoint("paused")
            if q.frozen_idle_sample(directory, state):
                break
            q.run(["docker", "unpause", current["Id"]])
            checkpoint("fenced_verified")
            time.sleep(0.5)
        else:
            raise RuntimeError("No idle fixture boundary")
        stop.stop_idle(directory, state, checkpoint)
        stop.restore_configuration(directory, state)
        q.database_check(data / "state.db")
        assert cfg.read_bytes() == original
        checkpoint("stopped_original_config_restored")
        recover(directory)
    except BaseException:
        recover(directory)
        raise


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"owned fixture")

    def log_message(self, *args):
        pass


class IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["rehearse", "worker", "recover"])
    p.add_argument("--generation", type=Path, required=True)
    p.add_argument("--image")
    p.add_argument("--container")
    p.add_argument("--port", type=int, default=18089)
    p.add_argument("--fault", default="none")
    a = p.parse_args()
    if os.geteuid() != 0 or not __debug__:
        p.error("root and unoptimized Python required")
    if not 18000 <= a.port <= 18999:
        p.error("rehearsal port must be in the dedicated test range")
    os.umask(0o077)
    if a.mode == "recover":
        recover(a.generation)
        return
    if a.mode == "worker":
        worker(a.generation, a.container, a.port, a.fault)
        return
    q.private_directory(a.generation, create=True)
    servers = [
        ThreadingHTTPServer(("127.0.0.1", a.port), Handler),
        IPv6Server(("::1", a.port), Handler),
    ]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    phases = [
        "none",
        "input-fenced-4",
        "rules-ready-6",
        "paused",
        "config_original_retained",
        "config_candidate_published",
        "term_intent",
        "worker_term_queued",
        "term_released",
        "stopped",
        "stopped_original_config_restored",
    ]
    results = []
    try:
        for i, phase in enumerate(phases):
            case = a.generation / ("case-" + str(i))
            case.mkdir(mode=0o700)
            for name in ["data", "logs"]:
                (case / name).mkdir(mode=0o700)
            cfg = {
                "system": {
                    "smtp": {},
                    "alert_email": "",
                    "alert_hours": 0,
                    "poll_interval_seconds": 60,
                    "attachment_retention_months": 24,
                },
                "mailboxes": [],
            }
            original = (json.dumps(cfg, indent=2) + "\n").encode()
            q.write_new(case / "data/config.json", original)
            name = "codex-voicemail-stop-" + str(os.getpid()) + "-" + str(i)
            cid = (
                q.run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        name,
                        "--label",
                        "codex.voicemail.fixture=true",
                        "--network",
                        "none",
                        "--restart",
                        "unless-stopped",
                        "--memory",
                        "512m",
                        "--cpus",
                        "1",
                        "-v",
                        str(case / "data") + ":/app/data",
                        "-v",
                        str(case / "logs") + ":/app/logs",
                        a.image,
                    ]
                )
                .decode()
                .strip()
            )
            q.write_new(case / "fixture-id", cid.encode())
            try:
                current = q.inspect(cid)
                fingerprint = q.config_fingerprint(current)
                q.http_ready(
                    {
                        "container_id": cid,
                        "image": a.image,
                        "configuration_fingerprint": fingerprint,
                    }
                )
                time.sleep(0.5)
                generation = case / "operation"
                helper = str(Path(__file__).resolve())
                recovery = " ".join(
                    [
                        sys.executable,
                        helper,
                        "recover",
                        "--generation",
                        str(generation),
                        "--port",
                        str(a.port),
                    ]
                )
                with (case / "unit.private.log").open("wb") as log:
                    result = subprocess.run(
                        [
                            "systemd-run",
                            "--unit=codex-vmail-stop-"
                            + str(os.getpid())
                            + "-"
                            + str(i),
                            "--wait",
                            "--collect",
                            "--property=Type=oneshot",
                            "--property=TimeoutStartSec=120",
                            "--property=TimeoutStopSec=100",
                            "--property=ExecStopPost=" + recovery,
                            sys.executable,
                            helper,
                            "worker",
                            "--generation",
                            str(generation),
                            "--container",
                            name,
                            "--port",
                            str(a.port),
                            "--fault",
                            phase,
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=240,
                    )
                state = q.load(generation)
                current = q.inspect(cid)
                assert state["phase"] == "recovered"
                assert current["State"]["Running"] and not current["State"]["Paused"]
                assert q.config_fingerprint(current) == fingerprint
                assert (case / "data/config.json").read_bytes() == original
                q.database_check(case / "data/state.db")
                for family in [4, 6]:
                    assert fence_for(state).inspect_owned(family) is None
                for host in ["127.0.0.1", "::1"]:
                    assert marked_http_status(a.port, 0, host) == 200
                assert (result.returncode == 0) == (phase == "none")
                marker = generation / "fault-injected.json"
                if phase == "none":
                    assert state["clean_stop_verified"]
                    assert not marker.exists()
                else:
                    assert json.loads(q.read_regular(marker)[0]) == {
                        "phase": phase,
                        "signal": "SIGKILL",
                    }
                results.append(
                    {
                        "fault_phase": phase,
                        "expected_failure": phase != "none",
                        "fault_injection_recorded": marker.exists(),
                        "original_resumed": True,
                        "original_configuration_and_restart_policy_restored": True,
                        "firewall_fence_removed": True,
                        "clean_stop_verified": state["clean_stop_verified"],
                        "container_id": cid,
                    }
                )
                print(json.dumps(results[-1]), flush=True)
            finally:
                operation = case / "operation"
                if (operation / "journal.json").exists():
                    # Retry only our journalled recovery before removing an
                    # exact owned fixture. A conflict leaves evidence intact.
                    recover(operation)
                current = q.inspect(cid)
                assert (
                    current["Name"] == "/" + name
                    and current["Config"]["Labels"]["codex.voicemail.fixture"] == "true"
                )
                if current["State"]["Paused"]:
                    q.run(["docker", "unpause", cid])
                q.run(["docker", "update", "--restart=no", cid])
                q.run(["docker", "stop", "--time", "45", cid], timeout=60)
                q.run(["docker", "rm", cid])
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    receipt = {
        "checked_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "image": a.image,
        "production_touched": False,
        "provider_traffic": False,
        "mail_fixture_network": "none",
        "host_firewall_probe_port": a.port,
        "firewall_probe_is_separate_from_mail_fixture": True,
        "cases": results,
        "owned_fixtures_removed": len(results),
        "end_to_end_production_cutover": False,
    }
    q.write_new(
        a.generation / "stop-rehearsal.sanitized.json",
        (json.dumps(receipt, indent=2) + "\n").encode(),
    )


if __name__ == "__main__":
    main()
