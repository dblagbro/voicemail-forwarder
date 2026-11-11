"""Build and verify only a disposable candidate; never deploy or use real mail."""

import argparse
import datetime
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--trivy", type=Path)
    parser.add_argument("--trivy-cache", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "production_modified": False,
        "deployed": False,
        "checks": {},
    }

    def save():
        p = args.output / "candidate.json"
        p.write_text(json.dumps(report, indent=2) + "\n")

    def run(name, command, timeout=900):
        log = args.output / (name + ".log")
        with log.open("w") as stream:
            result = subprocess.run(
                command,
                stdout=stream,
                stderr=subprocess.STDOUT,
                cwd=args.source,
                timeout=timeout,
            )
        report["checks"][name] = {
            "exit": result.returncode,
            "sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        }
        save()
        if result.returncode:
            raise RuntimeError(name + " failed; inspect the retained log")

    try:
        report["source_sha256"] = {
            str(p.relative_to(args.source)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in args.source.rglob("*")
            if p.is_file()
            and not set(p.relative_to(args.source).parts).intersection(
                {".git", "security-evidence", "__pycache__", "data", "logs", ".venv"}
            )
        }
        save()
        run("build", ["docker", "build", "--progress=plain", "-t", args.image, "."])
        image = subprocess.check_output(
            ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True
        ).strip()
        report["image_id"] = image
        common = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "-e",
            "PYTHONPATH=/app",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            str(args.source / "tests") + ":/tests:ro",
            "--entrypoint",
            "python",
            image,
        ]
        run("tests", common + ["-m", "unittest", "discover", "-s", "/tests", "-v"])
        expected = dict(line.split("==") for line in (args.source / "requirements.lock").read_text().splitlines() if line and not line.startswith("#"))
        run("runtime-dependencies", common + ["-c", "import importlib.metadata as m,importlib.util; expected=" + repr(expected) + "; assert all(m.version(k)==v for k,v in expected.items()); assert importlib.util.find_spec('pip') is None; print('runtime lock matches; unused pip absent')"])
        raw = subprocess.check_output(
            common
            + [
                "-c",
                "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()}))",
            ],
            text=True,
        )
        report["packages"] = json.loads(raw)
        if args.trivy:
            command = [
                str(args.trivy),
                "image",
                "--scanners",
                "vuln",
                "--format",
                "json",
                "--output",
                str(args.output / "candidate-trivy.json"),
            ]
            if args.trivy_cache:
                command += ["--cache-dir", str(args.trivy_cache)]
            run("scan", command + [image], 1200)
            scan = json.loads((args.output / "candidate-trivy.json").read_text())
            matches = [
                v
                for target in scan.get("Results", [])
                for v in target.get("Vulnerabilities", [])
            ]
            report["scan"] = {
                severity: sum(v["Severity"] == severity for v in matches)
                for severity in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
            }
            report["scan_findings"] = [
                {
                    k: v.get(k)
                    for k in [
                        "VulnerabilityID",
                        "PkgName",
                        "InstalledVersion",
                        "FixedVersion",
                        "Severity",
                    ]
                }
                for v in matches
            ]
            report["scan_sha256"] = hashlib.sha256(
                (args.output / "candidate-trivy.json").read_bytes()
            ).hexdigest()
        report["phase"] = "candidate_verified_not_deployed"
    except Exception as exc:
        report["phase"] = "failed"
        report["failure"] = str(exc)
        raise
    finally:
        report["updated_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save()
        print(
            json.dumps(
                {
                    k: v
                    for k, v in report.items()
                    if k not in {"source_sha256", "scan_findings"}
                }
            )
        )


if __name__ == "__main__":
    main()
