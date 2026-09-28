"""Controller runtime-preflight requests cross the Rust protocol and agent."""

from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path

import pytest
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.runtime_preflight import (
    RuntimePreflightRequest,
    RuntimePreflightResult,
)
from vonk_control.runtime_preflight import (
    RuntimePreflightBlocker,
    admission_blockers,
    request_digest,
)

FINGERPRINT = "a" * 64


def _probe(variable: str) -> str:
    probe = os.environ.get(variable)
    if not probe:
        pytest.skip(f"set {variable} to the compiled probe")
    return probe


def _request(**changes: object) -> RuntimePreflightRequest:
    architecture = (
        "linux-arm64" if platform.machine() in {"aarch64", "arm64"} else "linux-amd64"
    )
    return RuntimePreflightRequest.model_validate(
        {
            "schema_version": 1,
            "architecture": architecture,
            "source_build": False,
            "minimum_free_bytes": 0,
            "fabric_connectivity": "none",
            "fabric_minimum_mbps": 0,
            "mandatory_capabilities": ["gpu.cuda"],
            **changes,
        }
    )


def _run(
    command: list[str], request: RuntimePreflightRequest
) -> RuntimePreflightResult:
    completed = subprocess.run(
        command,
        input=canonical_message(request),
        capture_output=True,
        check=True,
    )
    return RuntimePreflightResult.model_validate_json(completed.stdout)


def test_rust_protocol_digests_the_controller_request_identically() -> None:
    probe = _probe("VONK_RUNTIME_PREFLIGHT_WIRE_PROBE")
    request = _request(
        source_build=True,
        minimum_free_bytes=2**40,
        fabric_connectivity="full_mesh",
        fabric_minimum_mbps=200_000,
    )

    result = _run([probe], request)

    assert result.request_sha256 == request_digest(request)


def test_rust_protocol_rejects_duplicate_mandatory_capabilities() -> None:
    probe = _probe("VONK_RUNTIME_PREFLIGHT_WIRE_PROBE")
    document = _request().model_dump(mode="json")
    document["mandatory_capabilities"] = ["gpu.cuda", "gpu.cuda"]

    completed = subprocess.run(
        [probe], input=canonical_message(document), capture_output=True, check=False
    )

    assert completed.returncode != 0


def test_agent_preflight_result_is_admitted_by_the_controller(tmp_path: Path) -> None:
    probe = _probe("VONK_RUNTIME_PREFLIGHT_PROBE")
    request = _request()

    result = _run([probe, str(tmp_path)], request)

    # The agent proves every local capability it owns; the signed helper and
    # an undeclared recipe capability stay unknown and still block admission.
    assert admission_blockers(
        request, result, current_fingerprint=FINGERPRINT, now=int(time.time())
    ) == (
        RuntimePreflightBlocker(
            "runtime_preflight.requirement_unknown",
            "Mandatory runtime capability signed_helper_run is unknown.",
        ),
        RuntimePreflightBlocker(
            "runtime_preflight.requirement_unknown",
            "Mandatory runtime capability gpu.cuda is unknown.",
        ),
    )
