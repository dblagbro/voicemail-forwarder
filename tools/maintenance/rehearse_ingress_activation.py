"""Synthetic TLS proxy -> host-network mail rehearsal, including fresh-data rollback.

This is deliberately NOT a production operator. Every mutating target must carry
this generation's fixture label, use its private mounts and test-only ports.
No real mail configuration, credentials, application data or provider is used.
"""

import argparse
import base64
import datetime
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import time

import quiesce_backup as q
import quiescent_stop as stop
from ingress_fence import Fence, marked_http_status
from cutover_files import move_absent


def check_ports(backend, proxy):
    if backend == proxy or any(not 18000 <= p <= 18999 for p in [backend, proxy]):
        raise ValueError("Distinct test ports in 18000..18999 required")


def safe_config(raw):
    cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        raise RuntimeError("Unexpected fixture configuration")
    system = cfg.get("system", {})
    if not isinstance(system, dict) or not isinstance(system.get("smtp", {}), dict):
        raise RuntimeError("Unexpected fixture system configuration")
    if cfg.get("mailboxes") != [] or system.get("alert_email") or system.get("alert_hours"):
        raise RuntimeError("Rehearsal refuses mailbox or alert configuration")
    if any(system.get("smtp", {}).values()):
        raise RuntimeError("Rehearsal refuses SMTP configuration")
    return cfg


def find(name):
    ids = q.run(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"], text=True).split()
    if not ids:
        return None
    if len(ids) != 1:
        raise RuntimeError("Ambiguous fixture name")
    return q.inspect(ids[0])


def owned(state, role):
    current = find(state[role + "_name"])
    if current is None:
        return None
    labels = current["Config"].get("Labels") or {}
    if (labels.get("codex.voicemail.fixture") != "true"
            or labels.get("codex.voicemail.generation") != state["token"]
            or current["Image"] != state[role + "_image"]):
        raise RuntimeError("Fixture ownership or image conflict")
    if role != "proxy":
        mounts = {m["Destination"]: m for m in current["Mounts"]}
        expected = {"/app/data": str(Path(state["runtime"]) / "data"),
                    "/app/logs": str(Path(state["runtime"]) / "logs")}
        if (current["HostConfig"]["NetworkMode"] != "host"
                or set(mounts) != set(expected)
                or any(mounts[k]["Type"] != "bind" or mounts[k]["Source"] != v
                       for k, v in expected.items())):
            raise RuntimeError("Fixture mounts or network changed")
        expected_cmd = mail_command(state)
        if current["Config"]["Cmd"] != expected_cmd:
            raise RuntimeError("Fixture command changed")
    return current


def mail_command(state):
    port = str(state["backend_port"])
    return ["gunicorn", "-b", "127.0.0.1:" + port, "-b", state["gateway"] + ":" + port,
            "-w", "1", "--threads", "8", "app.app:app"]


def persist(directory, state, stage=None):
    if stage:
        state["stage"] = stage
    q.journal(directory, state)


def step(directory, state, stage, fault=None):
    persist(directory, state, stage)
    if stage == fault:
        q.write_new(directory / "fault-injected.json",
                    (json.dumps({"stage": stage, "signal": "SIGKILL"}) + "\n").encode())
        os.kill(os.getpid(), signal.SIGKILL)


def fence(state):
    return Fence(state["token"], state["backend_port"], state["mark"])


def proxy_status(state, authenticated):
    proxy = owned(state, "proxy")
    ip = proxy["NetworkSettings"]["Networks"][state["network"]]["IPAddress"]
    context = ssl.create_default_context(cafile=str(Path(state["case"]) / "proxy/cert.pem"))
    with socket.create_connection((ip, state["proxy_port"]), timeout=4) as raw:
        with context.wrap_socket(raw, server_hostname="voicemail-fixture.invalid") as sock:
            auth = "Authorization: Basic " + state["fixture_basic"] + "\r\n" if authenticated else ""
            sock.sendall(("GET /voicemail-forwarder/ HTTP/1.0\r\nHost: voicemail-fixture.invalid\r\n"
                          + auth + "Connection: close\r\n\r\n").encode())
            response = http.client.HTTPResponse(sock)
            response.begin()
            return response.status


def ready(state, mark=0):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            if marked_http_status(state["backend_port"], mark) == 200:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.2)
    raise RuntimeError("Fixture did not become ready")


def seed(case, suffix):
    """Synthetic commits made after activation must survive image rollback."""
    connection = sqlite3.connect(case / "data/state.db")
    try:
        connection.execute("insert or ignore into seen values (?,?)", (suffix, "fixture"))
        connection.execute("insert or ignore into seen_uidl values (?,?,?)",
                           ("fixture", suffix, datetime.datetime.now().isoformat()))
        connection.execute("insert into attachments(filename,mailbox,subject,ts) values (?,?,?,?)",
                           (suffix + ".txt", "fixture", "synthetic", datetime.datetime.now().isoformat()))
        connection.commit()
    finally:
        connection.close()
    q.write_new(case / "logs/attachments" / (suffix + ".txt"), ("synthetic " + suffix).encode())


def data_identity(case):
    q.database_check(case / "data/state.db")
    connection = sqlite3.connect((case / "data/state.db").as_uri() + "?mode=ro", uri=True)
    try:
        rows = {table: sorted(connection.execute("select * from " + table).fetchall())
                for table in ["seen", "seen_uidl", "attachments"]}
    finally:
        connection.close()
    blobs = q.tree_manifest(case / "logs/attachments")
    return q.digest(json.dumps({"rows": rows, "blobs": blobs}, sort_keys=True).encode())


def make_stop_state(current, state, directory):
    config = Path(state["runtime"]) / "data/config.json"
    raw, metadata = q.read_regular(config)
    safe_config(raw)
    directory.mkdir(mode=0o700)
    q.write_new(directory / "original-config.json", raw)
    return {"phase": "prepared", "maintenance_version": 1,
            "container_id": current["Id"], "image": current["Image"],
            "configuration_fingerprint": q.config_fingerprint(current),
            "configuration_path": str(config), "configuration_mode": metadata.st_mode & 0o777,
            "configuration_uid": metadata.st_uid, "configuration_gid": metadata.st_gid,
            "restart_policy": current["HostConfig"]["RestartPolicy"],
            "token": "integrated-" + secrets.token_hex(8), "py_spy": state["py_spy"],
            "executor_wait_site": q.executor_wait_site(current["Id"])}


def freeze_idle(directory, state, child, role):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        owned(state, role)
        current = stop.owned_state(child)
        if not current["State"]["Paused"]:
            q.run(["docker", "pause", child["container_id"]])
        child["phase"] = "paused"
        persist(directory, state)
        # The backup sampler expects the original restart policy. Candidate is
        # always restart=no; original has not changed its policy yet.
        if q.frozen_idle_sample(directory / role, child):
            persist(directory, state)
            return
        q.run(["docker", "unpause", child["container_id"]])
        time.sleep(0.2)
    raise RuntimeError("No complete idle fixture boundary")


def stop_candidate(directory, state):
    current = owned(state, "candidate")
    if current is None or not current["State"]["Running"]:
        return
    safe_config(q.read_regular(Path(state["runtime"]) / "data/config.json")[0])
    child = state.get("candidate")
    if child is None:
        child = make_stop_state(current, state, directory / "candidate")
        state["candidate"] = child
        persist(directory, state)
    if child["phase"] not in ["term_intent", "term_released", "stopped"]:
        freeze_idle(directory, state, child, "candidate")
        child["phase"] = "term_intent"
        persist(directory, state)
    current = stop.owned_state(child)
    if current["State"]["Paused"]:
        idle = child["idle_sample"]
        if current["State"]["Pid"] != idle["master_pid"]:
            raise RuntimeError("Candidate process changed")
        for pid in [idle["worker_pid"], idle["master_pid"]]:
            os.kill(pid, signal.SIGTERM)
        q.run(["docker", "unpause", child["container_id"]])
        child["phase"] = "term_released"
        persist(directory, state)
    def checkpoint(phase):
        child["phase"] = phase
        persist(directory, state)
    stop.await_stopped(directory / "candidate", child, checkpoint)


def recover(directory):
    state = q.load(directory)
    if state["stage"] == "recovered":
        return
    owned(state, "original")
    owned(state, "proxy")
    fence(state).apply()
    stop_candidate(directory, state)
    child = state["original"]
    def checkpoint(phase):
        child["phase"] = phase
        persist(directory, state)
    stop.recover_original(directory / "original", child, checkpoint)
    ready(state, state["mark"])
    expected = state.get("after_activation_data", state["before_data"])
    if data_identity(Path(state["runtime"])) != expected:
        raise RuntimeError("Fresh synthetic data changed during image rollback")
    fence(state).remove()
    if proxy_status(state, True) != 200 or proxy_status(state, False) != 401:
        raise RuntimeError("TLS/auth ingress did not recover")
    persist(directory, state, "recovered")


def create_mail(state, role):
    if find(state[role + "_name"]) is not None:
        raise RuntimeError("Fixture name already exists")
    args = ["docker", "create", "--name", state[role + "_name"],
            "--label", "codex.voicemail.fixture=true", "--label",
            "codex.voicemail.generation=" + state["token"], "--network", "host",
            "--restart", "unless-stopped" if role == "original" else "no",
            "--memory", "512m", "--cpus", "1"]
    for name in ["data", "logs"]:
        args += ["-v", str(Path(state["runtime"]) / name) + ":/app/" + name]
    args += [state[role + "_image"], *mail_command(state)]
    return q.run(args).decode().strip()


def worker(directory, fault):
    state = q.load(directory)
    f = fence(state)
    f.preflight()
    if proxy_status(state, True) != 200 or proxy_status(state, False) != 401:
        raise RuntimeError("Initial TLS/basic-auth fixture failed")
    step(directory, state, "fence_intent", fault)
    f.apply()
    if proxy_status(state, True) != 502 or proxy_status(state, False) != 401:
        raise RuntimeError("Authenticated proxy bypassed the backend fence")
    if marked_http_status(state["backend_port"], state["mark"]) != 200:
        raise RuntimeError("Marked maintenance probe failed")
    step(directory, state, "ingress_fenced", fault)
    q.run(["ss", "-K", "sport = :" + str(state["backend_port"])])
    child = state["original"]
    freeze_idle(directory, state, child, "original")
    def checkpoint(phase):
        child["phase"] = phase
        persist(directory, state)
    stop.stop_idle(directory / "original", child, checkpoint)
    step(directory, state, "original_stopped", fault)
    step(directory, state, "candidate_create_intent", fault)
    create_mail(state, "candidate")
    step(directory, state, "candidate_created", fault)
    q.run(["docker", "start", state["candidate_name"]])
    ready(state, state["mark"])
    if data_identity(Path(state["runtime"])) != state["before_data"]:
        raise RuntimeError("Warmup changed synthetic deduplication or attachments")
    step(directory, state, "candidate_warm", fault)
    stop.restore_configuration(directory / "original", child)
    step(directory, state, "configuration_activated", fault)
    f.remove()
    if proxy_status(state, True) != 200 or proxy_status(state, False) != 401:
        raise RuntimeError("Candidate TLS/auth ingress failed")
    step(directory, state, "candidate_active", fault)
    seed(Path(state["runtime"]), "after-activation")
    state["after_activation_data"] = data_identity(Path(state["runtime"]))
    if state["after_activation_data"] == state["before_data"]:
        raise RuntimeError("Synthetic fresh-data witness missing")
    step(directory, state, "fresh_data_committed", fault)
    recover(directory)


def setup_case(case, args):
    case.mkdir(mode=0o700)
    runtime = args.runtime_root / case.name
    runtime.mkdir(mode=0o700)
    for name in ["data", "logs"]:
        (runtime / name).mkdir(mode=0o700)
    for name in ["proxy", "operation"]:
        (case / name).mkdir(mode=0o700)
    cfg = {"system": {"smtp": {}, "alert_email": "", "alert_hours": 0,
                      "poll_interval_seconds": 60, "attachment_retention_months": 24}, "mailboxes": []}
    q.write_new(runtime / "data/config.json", (json.dumps(cfg, indent=2) + "\n").encode())
    token = secrets.token_hex(8)
    state = {"stage": "setup", "case": str(case), "runtime": str(runtime), "token": token,
             "mark": secrets.randbelow(0xFFFFFFFE) + 1, "backend_port": args.backend_port,
             "proxy_port": args.proxy_port, "network": args.network, "gateway": args.gateway,
             "py_spy": args.py_spy, "original_image": args.original_image,
             "candidate_image": args.candidate_image, "proxy_image": args.nginx_image}
    for role in ["original", "candidate", "proxy"]:
        state[role + "_name"] = "codex-voicemail-integrated-" + token + "-" + role
    persist(case / "operation", state)
    password = secrets.token_urlsafe(24)
    state["fixture_basic"] = base64.b64encode(("fixture:" + password).encode()).decode()
    openssl_argv = "openssl passwd -apr1 -stdin".split()
    hashed = subprocess.check_output(openssl_argv,
                                     input=password.encode(), stderr=subprocess.DEVNULL).strip()
    q.write_new(case / "proxy/htpasswd", b"fixture:" + hashed + b"\n")
    q.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
           "-subj", "/CN=voicemail-fixture.invalid", "-addext",
           "subjectAltName=DNS:voicemail-fixture.invalid", "-keyout", str(case / "proxy/key.pem"),
           "-out", str(case / "proxy/cert.pem")], stderr=subprocess.DEVNULL)
    nginx = """user root;
events {}
http { access_log off; error_log /dev/null; server {
 listen PROXY ssl; server_name voicemail-fixture.invalid;
 ssl_certificate /fixture/cert.pem; ssl_certificate_key /fixture/key.pem;
 location /voicemail-forwarder {
  auth_basic "Synthetic voicemail fixture"; auth_basic_user_file /fixture/htpasswd;
  rewrite ^/voicemail-forwarder/(.*) /$1 break;
  rewrite ^/voicemail-forwarder$ / break;
  proxy_pass http://GATEWAY:BACKEND;
  proxy_connect_timeout 2s; proxy_read_timeout 3s;
 }
} }
""".replace("PROXY", str(args.proxy_port)).replace("GATEWAY", args.gateway).replace("BACKEND", str(args.backend_port))
    q.write_new(case / "proxy/nginx.conf", nginx.encode())
    create_mail(state, "original")
    q.run(["docker", "start", state["original_name"]])
    ready(state)
    seed(runtime, "before-activation")
    state["before_data"] = data_identity(runtime)
    q.run(["docker", "run", "-d", "--name", state["proxy_name"], "--label",
           "codex.voicemail.fixture=true", "--label", "codex.voicemail.generation=" + token,
           "--network", args.network, "--restart", "no", "--memory", "128m", "--cpus", "0.5",
           "-v", str(case / "proxy") + ":/fixture:ro", "--entrypoint", "nginx",
           args.nginx_image, "-c", "/fixture/nginx.conf", "-g", "daemon off;"])
    state["original"] = make_stop_state(owned(state, "original"), state, case / "operation/original")
    persist(case / "operation", state, "prepared")
    return state


def cleanup(directory):
    state = q.load(directory)
    if "original" in state:
        recover(directory)
    elif owned(state, "candidate") is not None:
        raise RuntimeError("Unexpected candidate during setup; preserve for review")
    for role in ["candidate", "original", "proxy"]:
        current = owned(state, role)
        if current is None:
            continue
        if current["State"]["Running"]:
            # These newly created, empty-mailbox fixtures have no provider work.
            # Use TERM plus Docker wait; never use a forced stop/remove.
            q.run(["docker", "update", "--restart=no", current["Id"]])
            q.run(["docker", "kill", "--signal=TERM", current["Id"]])
            q.run(["docker", "wait", current["Id"]], timeout=60)
        if q.inspect(current["Id"])["State"]["Running"]:
            raise RuntimeError("Fixture still running; preserve it")
        q.run(["docker", "rm", current["Id"]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["rehearse", "worker", "recover"])
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path,
                        help="New private local runtime directory; evidence generation may be on NFS")
    parser.add_argument("--original-image")
    parser.add_argument("--candidate-image")
    parser.add_argument("--nginx-image")
    parser.add_argument("--network", default="docker_default")
    parser.add_argument("--gateway", default="172.18.0.1")
    parser.add_argument("--backend-port", type=int, default=18089)
    parser.add_argument("--proxy-port", type=int, default=18090)
    parser.add_argument("--py-spy", default="/home/dblagbro/.local/bin/py-spy")
    parser.add_argument("--fault", default="none")
    args = parser.parse_args()
    if os.geteuid() != 0 or not __debug__:
        parser.error("Root and unoptimized Python required")
    os.umask(0o077)
    check_ports(args.backend_port, args.proxy_port)
    if args.mode == "recover":
        if (args.generation / "journal.json").exists():
            recover(args.generation)
        return
    if args.mode == "worker":
        worker(args.generation, args.fault)
        return
    for value in [args.original_image, args.candidate_image, args.nginx_image]:
        if not value or len(value) != 71 or not value.startswith("sha256:"):
            parser.error("Exact image IDs required")
    if args.runtime_root is None or not args.runtime_root.is_absolute():
        parser.error("A new absolute --runtime-root is required for synthetic mutable data")
    # No production API action is issued; compare its exact identity/config
    # before and after all cases, and refuse occupied test listener ports.
    production = q.inspect("voicemail-forwarder")
    production_key = (production["Id"], q.config_fingerprint(production), production["State"]["StartedAt"])
    network = json.loads(q.run(["docker", "network", "inspect", args.network]))[0]
    if args.gateway not in [x.get("Gateway") for x in network["IPAM"]["Config"]]:
        raise RuntimeError("Fixture bridge gateway mismatch")
    for address in ["127.0.0.1", args.gateway]:
        with socket.socket() as sock:
            sock.bind((address, args.backend_port))
    q.private_directory(args.generation, create=True)
    q.private_directory(args.runtime_root, create=True)
    # Probe the required primitive before creating or pausing any container.
    # Production mutable data is local; NFS evidence storage need not implement
    # RENAME_NOREPLACE. Do not weaken the non-overwrite guarantee as a fallback.
    q.write_new(args.runtime_root / "rename-probe-source", b"synthetic filesystem probe")
    move_absent(args.runtime_root / "rename-probe-source", args.runtime_root / "rename-probe-target")
    phases = ["none", "ingress_fenced", "original_stopped", "candidate_created", "candidate_warm",
              "configuration_activated", "candidate_active", "fresh_data_committed"]
    results = []
    for index, fault in enumerate(phases):
        case = args.generation / ("case-" + str(index))
        directory = case / "operation"
        try:
            state = setup_case(case, args)
        except BaseException:
            if (directory / "journal.json").exists():
                cleanup(directory)
            raise
        helper = str(Path(__file__).resolve())
        recovery = " ".join([sys.executable, helper, "recover", "--generation", str(directory)])
        try:
            with (case / "unit.private.log").open("wb") as log:
                result = subprocess.run(["systemd-run", "--wait", "--collect",
                    "--unit=codex-vmail-integrated-" + state["token"], "--property=Type=oneshot",
                    "--property=TimeoutStartSec=180", "--property=TimeoutStopSec=120",
                    "--property=ExecStopPost=" + recovery, sys.executable, helper, "worker",
                    "--generation", str(directory), "--fault", fault],
                    stdout=log, stderr=subprocess.STDOUT, timeout=320)
            final = q.load(directory)
            if final["stage"] != "recovered":
                raise RuntimeError("Systemd backstop did not restore service")
            injected = (directory / "fault-injected.json").exists()
            if (result.returncode == 0) != (fault == "none") or injected != (fault != "none"):
                raise RuntimeError("Intended interruption was not exercised")
            current = owned(final, "original")
            if q.config_fingerprint(current) != final["original"]["configuration_fingerprint"]:
                raise RuntimeError("Original runtime configuration changed")
            if any(fence(final).inspect_owned(family) is not None for family in [4, 6]):
                raise RuntimeError("Owned fence remains")
            results.append({"fault_stage": fault, "fault_injection_recorded": injected,
                            "original_resumed": True, "original_configuration_restored": True,
                            "tls_authenticated_200": proxy_status(final, True) == 200,
                            "tls_unauthenticated_401": proxy_status(final, False) == 401,
                            "fresh_post_activation_data_preserved": bool(final.get("after_activation_data")),
                            "candidate_id": (owned(final, "candidate") or {}).get("Id"),
                            "original_id": current["Id"], "firewall_fence_removed": True})
            print(json.dumps(results[-1]), flush=True)
        finally:
            cleanup(directory)
        actual = q.inspect("voicemail-forwarder")
        if (actual["Id"], q.config_fingerprint(actual), actual["State"]["StartedAt"]) != production_key:
            raise RuntimeError("Concurrent production change; stop rehearsals")
    receipt = {"checked_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "production_modified": False, "deployed": False, "synthetic_data_only": True,
               "real_provider_acceptance": False, "real_public_route_changed": False,
               "proxy_image": args.nginx_image, "original_image": args.original_image,
               "candidate_image": args.candidate_image, "cases": results,
               "new_owned_fixtures_removed": True, "retained_backup_directories_removed": False,
               "limitations": "Test-only host port and TLS proxy; not production operator, canonical Compose publication, live provider acceptance or reboot recovery."}
    q.write_new(args.generation / "ingress-activation.sanitized.json",
                (json.dumps(receipt, indent=2) + "\n").encode())


if __name__ == "__main__":
    main()
