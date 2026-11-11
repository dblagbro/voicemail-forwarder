"""Run a bounded, capacity-guarded backup with automatic unfreeze recovery."""

import argparse
import datetime
import os
from pathlib import Path
import subprocess
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--generation", type=Path, required=True)
    p.add_argument("--san-root", type=Path, required=True)
    p.add_argument("--expected-image", required=True)
    p.add_argument("--guard", type=Path, required=True)
    args = p.parse_args()
    if os.geteuid() != 0 or not __debug__:
        p.error("root and unoptimized Python required")
    helper = Path(__file__).resolve().with_name("quiesce_backup.py")
    paths = [
        str(x)
        for x in [args.generation, args.san_root, args.guard, helper, sys.executable]
    ]
    if any(
        not x.startswith("/") or any(c.isspace() or c in "%$;\"'\\" for c in x)
        for x in paths
    ):
        p.error("paths must be absolute and safe for systemd arguments")
    if args.generation.exists():
        p.error("generation must be new; recover an existing journal separately")
    unit = "codex-voicemail-backup-" + datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y%m%d%H%M%S")
    recovery = " ".join(
        [sys.executable, str(helper), "recover", "--generation", str(args.generation)]
    )
    command = [
        "systemd-run",
        "--unit=" + unit,
        "--wait",
        "--collect",
        "--property=Type=oneshot",
        "--property=TimeoutStartSec=300",
        "--property=TimeoutStopSec=60",
        "--property=ExecStopPost=" + recovery,
        sys.executable,
        str(args.guard),
        "--peak-gib",
        "1",
        "--",
        sys.executable,
        str(helper),
        "prepare",
        "--generation",
        str(args.generation),
        "--san-root",
        str(args.san_root),
        "--expected-image",
        args.expected_image,
    ]
    raise SystemExit(subprocess.call(command))


if __name__ == "__main__":
    main()
