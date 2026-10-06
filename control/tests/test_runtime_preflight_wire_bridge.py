"""Controller runtime-preflight requests cross the Rust protocol and agent."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from vonk_agent_protocol import RuntimePreflightFindingCode, canonical_message
from vonk_agent_protocol.runtime_preflight import (
    RuntimePreflightRequest,
    RuntimePreflightResult,
)
from vonk_control.runtime_preflight import (
    RuntimePreflightBlocker,
    admission_blockers,
)

FINGERPRINT = "a" * 64


def _probe(variable: str) -> str:
    probe = os.environ.get(variable)
    if not probe:
        pytest.skip(f"set {variable} to the compiled probe")
    return probe


def _request(**changes: object) -> RuntimePreflightRequest:
    return RuntimePreflightRequest.model_validate(
        {
            "source_build": False,
            "minimum_free_bytes": 0,
            "fabric_connectivity": "none",
            "fabric_minimum_mbps": 0,
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


def test_rust_protocol_accepts_the_controller_request() -> None:
    probe = _probe("VONK_RUNTIME_PREFLIGHT_WIRE_PROBE")
    request = _request(
        source_build=True,
        minimum_free_bytes=2**40,
        fabric_connectivity="connected",
        fabric_minimum_mbps=200_000,
    )

    result = _run([probe], request)

    assert result.findings == []


def test_agent_preflight_result_is_admitted_by_the_controller(tmp_path: Path) -> None:
    probe = _probe("VONK_RUNTIME_PREFLIGHT_PROBE")
    request = _request()

    result = _run([probe, str(tmp_path)], request)

    # The agent proves every local capability it owns; the signed helper stays
    # unknown and still blocks admission.
    assert admission_blockers(
        request, result, current_fingerprint=FINGERPRINT, now=int(time.time())
    ) == (
        RuntimePreflightBlocker(
            "runtime_preflight.requirement_unknown",
            "Mandatory runtime capability signed_helper_run is unknown.",
        ),
    )


def test_agent_preflight_codes_are_contract_members_not_free_text(
    tmp_path: Path,
) -> None:
    probe = _probe("VONK_RUNTIME_PREFLIGHT_PROBE")
    request = _request()

    result = _run([probe, str(tmp_path)], request)

    assert result.findings
    for finding in result.findings:
        # The agent writes a member's own word; it never needs the adapter.
        assert RuntimePreflightFindingCode(finding.code) is finding.finding_code
        assert finding.finding_code is not RuntimePreflightFindingCode.UNCLASSIFIED
