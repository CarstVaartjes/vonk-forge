#!/usr/bin/env python3
"""Build the Rust install/start wire probe once, then run its Controller lane."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--probe-path",
        type=Path,
        help="use an already-built install_start_wire_probe executable",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    repository = Path(__file__).resolve().parents[2]
    probe = args.probe_path
    if probe is not None and not probe.is_absolute():
        probe = repository / probe
    if probe is not None:
        probe = probe.resolve()
    if probe is None:
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--package",
                "vonk-agent",
                "--example",
                "install_start_wire_probe",
            ],
            cwd=repository,
            check=True,
        )
        target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
        if not target_root.is_absolute():
            target_root = repository / target_root
        probe = target_root / "debug" / "examples" / "install_start_wire_probe"
    if not probe.is_file() or not os.access(probe, os.X_OK):
        raise SystemExit(f"install/start wire probe is not executable: {probe}")

    environment = os.environ.copy()
    environment["VONK_INSTALL_START_WIRE_PROBE"] = str(probe)
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "control/tests/test_install_start_wire_bridge.py",
            *pytest_args,
        ],
        cwd=repository,
        env=environment,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
