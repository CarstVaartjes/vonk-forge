#!/usr/bin/env python3
"""Build the Rust probes and exercise every Controller/Spark wire boundary."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROBES = {
    "VONK_INSTALL_START_WIRE_PROBE": "install_start_wire_probe",
    "VONK_HEARTBEAT_WIRE_PROBE": "heartbeat_wire_probe",
    "VONK_ENROLLMENT_WIRE_PROBE": "enrollment_wire_probe",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--probe-directory",
        type=Path,
        help="directory containing already-built Rust probe executables",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    repository = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    target_root = Path(environment.get("CARGO_TARGET_DIR", repository / "target"))
    probe_directory = args.probe_directory or target_root / "debug" / "examples"
    if not probe_directory.is_absolute():
        probe_directory = repository / probe_directory
    missing = [name for key, name in PROBES.items() if key not in environment]
    if missing and args.probe_directory is None:
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--package",
                "vonk-agent",
                *[argument for name in missing for argument in ("--example", name)],
            ],
            cwd=repository,
            check=True,
        )
    for key, name in PROBES.items():
        probe = Path(environment.get(key, probe_directory / name))
        if not probe.is_absolute():
            probe = repository / probe
        probe = probe.resolve()
        if not probe.is_file() or not os.access(probe, os.X_OK):
            raise SystemExit(f"{name} is not executable: {probe}")
        environment[key] = str(probe)
    bridges = sorted((repository / "control/tests").glob("test_*_wire_bridge.py"))
    if not bridges:
        raise SystemExit("no Controller/Spark wire bridge tests found")
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "agent_protocol/tests",
            *[str(path.relative_to(repository)) for path in bridges],
            *pytest_args,
        ],
        cwd=repository,
        env=environment,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
