"""Boot original and candidate from separate private snapshot copies offline."""

import argparse
import datetime
import json
import os
from pathlib import Path
import sqlite3

import quiesce_backup as q


def records(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as c:
        return {
            table: sorted(c.execute("select * from " + table).fetchall(), key=repr)
            for table in ["seen", "seen_uidl", "attachments"]
        }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backup-generation", type=Path, required=True)
    p.add_argument("--generation", type=Path, required=True)
    p.add_argument("--original-image", required=True)
    p.add_argument("--candidate-image", required=True)
    a = p.parse_args()
    if os.geteuid() != 0 or not __debug__:
        p.error("root and unoptimized Python required")
    os.umask(0o077)
    state = q.load(a.backup_generation)
    assert state["phase"] == "resumed" and state["backup"]["san_verified"]
    assert state["image"] == a.original_image
    q.private_directory(a.generation, create=True)
    snapshot = Path(state["backup"]["san"])
    manifest_raw = q.read_regular(snapshot / "manifest.private.json")[0]
    assert q.digest(manifest_raw) == state["backup"]["manifest_sha256"]
    manifest = json.loads(manifest_raw)
    for part in ["data", "logs"]:
        assert q.tree_manifest(snapshot / part) == manifest[part]
    live = q.inspect(state["container_id"])
    assert (
        live["Image"] == a.original_image
        and live["State"]["Running"]
        and not live["State"]["Paused"]
    )
    before_live = (
        live["Id"],
        live["Image"],
        live["State"]["StartedAt"],
        q.config_fingerprint(live),
    )
    results = []
    for index, (role, image) in enumerate(
        [("original", a.original_image), ("candidate", a.candidate_image)]
    ):
        case = a.generation / role
        case.mkdir(mode=0o700)
        q.tree_copy(snapshot / "data", case / "data")
        q.tree_copy(snapshot / "logs", case / "logs")
        cfg = case / "data/config.json"
        original = q.read_regular(cfg)[0]
        q.write_new(case / "original-config.private.json", original)
        isolated = json.loads(original)
        for mailbox in isolated["mailboxes"]:
            mailbox["enabled"] = False
        isolated["system"].update(
            alert_email="",
            alert_hours=0,
            poll_interval_seconds=3600,
            attachment_retention_months=1200,
        )
        cfg.write_text(json.dumps(isolated, indent=2) + "\n")
        before = records(case / "data/state.db")
        attachments = q.tree_manifest(case / "logs/attachments")
        name = "codex-voicemail-restore-" + str(os.getpid()) + "-" + str(index)
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
                    image,
                ]
            )
            .decode()
            .strip()
        )
        q.write_new(case / "container-id", cid.encode())
        try:
            runtime = q.inspect(cid)
            guard = {
                "container_id": cid,
                "image": image,
                "configuration_fingerprint": q.config_fingerprint(runtime),
            }
            q.http_ready(guard)
            q.database_check(case / "data/state.db")
            assert records(case / "data/state.db") == before
            assert q.tree_manifest(case / "logs/attachments") == attachments
            q.run(["docker", "stop", "--time", "45", cid], timeout=60)
            final = q.inspect(cid)
            assert final["State"]["ExitCode"] == 0 and not final["State"]["OOMKilled"]
            assert records(case / "data/state.db") == before
            results.append(
                {
                    "role": role,
                    "image": image,
                    "http_200": True,
                    "sqlite_integrity": True,
                    "deduplication_and_attachment_records_unchanged": True,
                    "attachment_bytes_unchanged": True,
                    "clean_stop": True,
                    "container_id": cid,
                }
            )
        finally:
            current = q.inspect(cid)
            assert (
                current["Name"] == "/" + name
                and current["Config"]["Labels"].get("codex.voicemail.fixture") == "true"
            )
            if current["State"]["Running"]:
                q.run(["docker", "stop", "--time", "45", cid], timeout=60)
            q.run(["docker", "rm", cid])
    live = q.inspect(state["container_id"])
    after_live = (
        live["Id"],
        live["Image"],
        live["State"]["StartedAt"],
        q.config_fingerprint(live),
    )
    assert (
        before_live == after_live
        and live["State"]["Running"]
        and not live["State"]["Paused"]
    )
    for part in ["data", "logs"]:
        assert q.tree_manifest(snapshot / part) == manifest[part]
    receipt = {
        "checked_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "production_unchanged": True,
        "deployed": False,
        "network": "none",
        "published_ports": False,
        "polling_and_alerts_disabled_in_separate_copies": True,
        "application_values_exported": False,
        "provider_acceptance": False,
        "source_snapshot_unchanged": True,
        "cases": results,
        "owned_fixtures_removed": len(results),
    }
    q.write_new(
        a.generation / "restore.sanitized.json",
        (json.dumps(receipt, indent=2) + "\n").encode(),
    )
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
