#!/usr/bin/env python3
"""Build the Rust probes and exercise every Controller/Spark wire boundary."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROBES = {
    "VONK_INSTALLER_RELEASE_WIRE_PROBE": (
        "vonk-spark-setup",
        "installer_release_wire_probe",
    ),
    "VONK_SOURCE_BUNDLE_WIRE_PROBE": ("vonk-agent", "source_bundle_wire_probe"),
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
    "VONK_RECIPE_OBSERVATION_WIRE_PROBE": (
        "vonk-agent",
        "recipe_observation_wire_probe",
    ),
    "VONK_COMPILED_PLAN_WIRE_PROBE": ("vonk-agent", "compiled_plan_wire_probe"),
    "VONK_RESTART_RECOVERY_PROBE": ("vonk-agent", "restart_recovery_probe"),
    "VONK_CANONICAL_WIRE_PROBE": ("vonk-agent-protocol", "canonical_wire_probe"),
    "VONK_PROGRESS_WIRE_PROBE": ("vonk-agent-protocol", "progress_wire_probe"),
    "VONK_RUNTIME_PREFLIGHT_WIRE_PROBE": (
        "vonk-agent-protocol",
        "runtime_preflight_wire_probe",
    ),
    "VONK_RUNTIME_PREFLIGHT_PROBE": ("vonk-agent", "runtime_preflight_probe"),
}
# Whole binaries the boundary tests run, built in the same Cargo invocation.
BINARIES = {
    "VONK_AGENT_BINARY": ("vonk-agent", "vonk-agent"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--probe-directory",
        type=Path,
        help="directory containing already-built Rust probe executables",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="build the probes and exit, so CI can overlap the build with other checks",
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
        binaries = [
            (package, name)
            for key, (package, name) in BINARIES.items()
            if key not in environment
        ]
        if packages or binaries:
            # Resolve one Cargo feature graph for every probe. Separate builds
            # compiled the shared protocol/jsonschema dependencies repeatedly
            # with different feature sets, even after restoring the CI cache.
            subprocess.run(
                [
                    "cargo",
                    "build",
                    "--locked",
                    *[
                        argument
                        for package in packages
                        for argument in ("--package", package)
                    ],
                    *[
                        argument
                        for names in packages.values()
                        for name in names
                        for argument in ("--example", name)
                    ],
                    *[
                        argument
                        for package, name in binaries
                        for argument in ("--package", package, "--bin", name)
                    ],
                ],
                cwd=repository,
                check=True,
            )
    if args.build_only:
        return 0
    for key, (_, name) in BINARIES.items():
        binary = Path(environment.get(key, target_root / "debug" / name))
        if not binary.is_absolute():
            binary = repository / binary
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise SystemExit(f"{name} is not executable: {binary}")
        environment[key] = str(binary.resolve())
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
    # Root and Controller tests use the same Python package name. Keep their
    # collection isolated while sharing the exact native probe environment.
    selections = [
        [
            "agent_protocol/tests",
            *[str(path.relative_to(repository)) for path in bridges],
            "control/tests/test_agent_jobs.py::test_queue_stores_and_claims_the_canonical_payload_hash",
        ],
        [
            "tests/scripts/test_install_release_publication.py::test_actual_publisher_manifest_is_complete_at_the_signed_rust_boundary",
            "tests/acceptance/test_rust_agent_parity.py::test_rust_claim_capabilities_cover_current_controller_contract",
        ],
    ]
    for selection in selections:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *selection, *pytest_args],
            cwd=repository,
            env=environment,
            check=False,
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
