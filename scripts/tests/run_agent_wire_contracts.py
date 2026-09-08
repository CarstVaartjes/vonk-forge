#!/usr/bin/env python3
"""Build the Rust probes and exercise every Controller/Spark wire boundary."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROBES = {
    "VONK_JOB_INVOCATION_WIRE_PROBE": ("vonk-agent", "job_invocation_wire_probe"),
    "VONK_INSTALL_START_WIRE_PROBE": ("vonk-agent", "install_start_wire_probe"),
    "VONK_HEARTBEAT_WIRE_PROBE": ("vonk-agent", "heartbeat_wire_probe"),
    "VONK_ENROLLMENT_WIRE_PROBE": ("vonk-agent", "enrollment_wire_probe"),
    "VONK_BOOTSTRAP_WIRE_PROBE": ("vonk-spark-setup", "bootstrap_wire_probe"),
    "VONK_RECIPE_JOB_WIRE_PROBE": ("vonk-agent-protocol", "recipe_job_wire_probe"),
    "VONK_BUILD_IMPORT_WIRE_PROBE": ("vonk-agent-protocol", "build_import_wire_probe"),
    "VONK_INVENTORY_WIRE_PROBE": ("vonk-agent-protocol", "inventory_wire_probe"),
    "VONK_TELEMETRY_WIRE_PROBE": ("vonk-agent", "telemetry_wire_probe"),
    "VONK_HOST_HELPER_WIRE_PROBE": ("vonk-agent-helper", "host_helper_wire_probe"),
    "VONK_RECIPE_OBSERVATION_WIRE_PROBE": ("vonk-agent", "recipe_observation_wire_probe"),
    "VONK_COMPILED_PLAN_WIRE_PROBE": ("vonk-agent", "compiled_plan_wire_probe"),
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
    if args.probe_directory is None:
        packages: dict[str, list[str]] = {}
        for key, (package, name) in PROBES.items():
            if key not in environment:
                packages.setdefault(package, []).append(name)
        for package, names in packages.items():
            subprocess.run(
                [
                    "cargo",
                    "build",
                    "--locked",
                    "--package",
                    package,
                    *[argument for name in names for argument in ("--example", name)],
                ],
                cwd=repository,
                check=True,
            )
    for key, (_, name) in PROBES.items():
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
