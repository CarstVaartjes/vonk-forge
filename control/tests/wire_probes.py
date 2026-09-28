"""Locate the prebuilt Rust wire probes the ``*_wire_bridge.py`` tests drive.

``scripts/tests/run_agent_wire_contracts.py`` builds every probe in one Cargo
invocation with a single feature graph and exports each path. Building inside a
test fixture instead compiled the shared protocol crates once per probe with
different features, which cost minutes and hid build time inside test time.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_REPOSITORY = Path(__file__).resolve().parents[2]


def prebuilt_probe(environment_name: str) -> Path:
    configured = os.environ.get(environment_name)
    if not configured:
        message = (
            f"{environment_name} is unset; run "
            "scripts/tests/run_agent_wire_contracts.py to build the wire probes"
        )
        if os.getenv("CI", "").lower() == "true":
            pytest.fail(f"CI prerequisite missing: {message}", pytrace=False)
        pytest.skip(message)
    probe = Path(configured).expanduser()
    if not probe.is_absolute():
        probe = _REPOSITORY / probe
    probe = probe.resolve()
    if not probe.is_file() or not os.access(probe, os.X_OK):
        raise AssertionError(f"configured wire probe is not executable: {probe}")
    return probe
