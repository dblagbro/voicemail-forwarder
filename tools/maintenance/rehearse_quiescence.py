"""Offline original-image rehearsal, including real controller SIGKILL recovery.

Run as root through capacity_guard.py. Creates only labelled network=none
fixtures with fresh synthetic data and no published ports. No production data
is read. Every container ID is retained in the private receipt before use.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import quiesce_backup as q


def fault_driver():
    """Invoked only inside an owned fixture's transient systemd unit."""
    phase, arguments = sys.argv[2], sys.argv[3:]
    name = arguments[arguments.index("--container") + 1]
    current = q.inspect(name)
    if (
        not current["Name"].startswith("/codex-voicemail-")
        or current["Config"]["Labels"].get("codex.voicemail.fixture") != "true"
    ):
        raise RuntimeError("Fault injection is restricted to labelled fixtures")
    checkpoint = q.checkpoint

    def die_at(directory, state, at):
        checkpoint(directory, state, at)
        if at == phase:
            os.kill(os.getpid(), 9)

    q.checkpoint = die_at
    sys.argv = [str(Path(q.__file__))] + arguments
    q.main()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--generation", type=Path, required=True)
    p.add_argument("--san-root", type=Path, required=True)
    p.add_argument(
        "--phases",
        nargs="+",
        default=[
            "normal",
            "pause_intent",
            "paused",
            "idle_verified",
            "snapshot_intent",
            "local_complete",
            "san_complete",
            "backup_complete",
        ],
    )
    a = p.parse_args()
    if os.geteuid() != 0:
        p.error("root required")
    os.umask(0o077)
    q.private_directory(a.generation, create=True)
    q.private_directory(a.san_root)
    results = []
    for index, phase in enumerate(a.phases):
        case = a.generation / f"case-{index}-{phase.replace('_', '-')}"
        case.mkdir(mode=0o700)
        for child in ["data", "logs"]:
            (case / child).mkdir(mode=0o700)
        configuration = {
            "system": {
                "poll_interval_seconds": 60,
                "attachment_retention_months": 24,
                "smtp": {},
                "alert_hours": 0,
                "alert_email": "",
            },
            "mailboxes": [],
        }
        original = (json.dumps(configuration, indent=2) + "\n").encode()
        q.write_new(case / "data/config.json", original)
        name = "codex-voicemail-quiesce-" + str(os.getpid()) + "-" + str(index)
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
                    "no",
                    "--memory",
                    "512m",
                    "--cpus",
                    "1",
                    "-v",
                    str(case / "data") + ":/app/data",
                    "-v",
                    str(case / "logs") + ":/app/logs",
                    a.image,
                    "gunicorn",
                    "-b",
                    "127.0.0.1:8089",
                    "-w",
                    "1",
                    "--threads",
                    "8",
                    "app.app:app",
                ]
            )
            .decode()
            .strip()
        )
        q.write_new(case / "fixture-id", cid.encode())
        try:
            state = q.inspect(cid)
            guard = {
                "container_id": cid,
                "image": a.image,
                "configuration_fingerprint": q.config_fingerprint(state),
            }
            q.http_ready(guard)
            # Let the first fake/empty poll finish and the HTTP worker become idle.
            time.sleep(1)
            generation = case / ("backup-" + str(index) + "-" + phase.replace("_", "-"))
            prepare = [
                "prepare",
                "--generation",
                str(generation),
                "--container",
                name,
                "--expected-image",
                a.image,
                "--san-root",
                str(a.san_root),
                "--idle-timeout",
                "20",
            ]
            helper = str(Path(q.__file__).resolve())
            command = (
                [sys.executable, helper] + prepare
                if phase == "normal"
                else [sys.executable, str(Path(__file__).resolve()), "fault", phase]
                + prepare
            )
            recovery = " ".join(
                [sys.executable, helper, "recover", "--generation", str(generation)]
            )
            # Every path is generated here and cannot contain whitespace or shell syntax.
            if any(
                any(c.isspace() for c in x)
                for x in [sys.executable, helper, str(generation)]
            ):
                raise RuntimeError("Unsupported systemd executable path")
            unit = "codex-voicemail-fixture-" + str(os.getpid()) + "-" + str(index)
            with (case / "unit.private.log").open("wb") as log:
                completed = subprocess.run(
                    [
                        "systemd-run",
                        "--unit=" + unit,
                        "--wait",
                        "--collect",
                        "--property=Type=oneshot",
                        "--property=TimeoutStartSec=100",
                        "--property=TimeoutStopSec=60",
                        "--property=ExecStopPost=" + recovery,
                    ]
                    + command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=180,
                )
            final = q.load(generation)
            current = q.inspect(cid)
            assert final["phase"] == "resumed", final["phase"]
            assert current["State"]["Running"] and not current["State"]["Paused"]
            assert (
                current["Image"] == a.image
                and current["HostConfig"]["RestartPolicy"]["Name"] == "no"
            )
            assert (case / "data/config.json").read_bytes() == original
            q.database_check(case / "data/state.db")
            if phase == "normal":
                assert completed.returncode == 0
                assert (
                    final["backup"]["san_verified"]
                    and final["backup"]["independent_restore_verified"]
                )
            else:
                assert completed.returncode != 0, "Fault injection was not reached"
            results.append(
                {
                    "phase": phase,
                    "expected_controller_failure": phase != "normal",
                    "unit_returncode": completed.returncode,
                    "original_resumed": True,
                    "configuration_restored": True,
                    "sqlite_integrity": True,
                    "http_200": final["original_http_200"],
                    "container_id": cid,
                }
            )
            print(json.dumps(results[-1]), flush=True)
        finally:
            current = q.inspect(cid)
            assert (
                current["Name"] == "/" + name
                and current["Config"]["Labels"].get("codex.voicemail.fixture") == "true"
            )
            if current["State"]["Paused"]:
                q.run(["docker", "unpause", cid])
            q.run(["docker", "stop", "--time", "45", cid], timeout=60)
            q.run(["docker", "rm", cid])
    receipt = {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "image": a.image,
        "network": "none",
        "published_ports": False,
        "production_touched": False,
        "synthetic_data_only": True,
        "cases": results,
        "owned_fixtures_removed": len(results),
    }
    q.write_new(
        a.generation / "rehearsal.sanitized.json",
        (json.dumps(receipt, indent=2) + "\n").encode(),
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fault":
        fault_driver()
    else:
        main()
