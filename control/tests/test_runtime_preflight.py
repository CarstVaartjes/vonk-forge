from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from vonk_agent_protocol.runtime_preflight import (
    RuntimePreflightFinding,
    RuntimePreflightRequest,
    RuntimePreflightResult,
)
from vonk_control.inventory_items_contract import PreflightInventoryItem
from vonk_control.runtime_preflight import (
    admission_blockers,
    mandatory_capabilities,
    request_digest,
)


def request(**changes):
    return RuntimePreflightRequest.model_validate(
        dict(
            source_build=True,
            minimum_free_bytes=4 * 1024**3,
            fabric_connectivity="none",
            fabric_minimum_mbps=0,
            **changes,
        )
    )


def result(req, **changes):
    fields = {
        "fingerprint": "a" * 64,
        "observed_at": 100,
        "findings": [
            {"capability": value, "status": "passed", "code": "available"}
            for value in mandatory_capabilities(req)
        ],
    }
    fields.update(changes)
    return RuntimePreflightResult.model_validate(fields)


def blockers(req, res, **changes):
    args = {"current_fingerprint": "a" * 64, "now": 101}
    args.update(changes)
    return admission_blockers(
        req,
        PreflightInventoryItem(
            requirements_sha256=request_digest(req),
            result=res,
            received_at=datetime.fromtimestamp(100, UTC),
        )
        if res is not None
        else None,
        **args,
    )


def test_exact_recipe_and_current_host_can_be_admitted():
    req = request()
    assert not blockers(req, result(req))


def test_changed_host_fingerprint_invalidates_passing_result():
    req = request()
    assert blockers(req, result(req), current_fingerprint="b" * 64)
    assert not blockers(req, result(req))


def test_unknown_optional_capability_does_not_block():
    req = request()
    res = result(req)
    res = res.model_copy(
        update={
            "findings": [
                *res.findings,
                RuntimePreflightFinding(
                    capability="optional_accelerator",
                    status="unknown",
                    code="not_observed",
                ),
            ]
        }
    )
    assert not blockers(req, res)


def test_missing_signed_helper_result_does_not_pass_after_successful_build():
    req = request()
    res = result(req)
    res = res.model_copy(
        update={
            "findings": [
                value
                for value in res.findings
                if value.capability != "signed_helper_run"
            ]
        }
    )
    assert blockers(req, res)
    assert not blockers(req, result(req))


def test_published_image_requires_serving_without_build_requirement():
    req = request().model_copy(update={"source_build": False})
    assert "podman_build" not in mandatory_capabilities(req)
    assert "signed_helper_run" in mandatory_capabilities(req)
    assert not blockers(req, result(req))


def test_fabric_requirement_missing_evidence_blocks():
    req = request().model_copy(
        update={"fabric_connectivity": "connected", "fabric_minimum_mbps": 200000}
    )
    res = result(req)
    res = res.model_copy(
        update={
            "findings": [
                value for value in res.findings if value.capability != "fabric"
            ]
        }
    )
    assert blockers(req, res)
    assert not blockers(req, result(req))


@pytest.mark.parametrize("now", [99, 401])
def test_stale_and_future_evidence_cannot_admit(now):
    req = request()
    assert blockers(req, result(req), now=now)
    assert not blockers(req, result(req))


def test_missing_current_fingerprint_and_missing_result_fail_clearly():
    req = request()
    assert blockers(req, None)
    assert not blockers(req, result(req))
    assert blockers(req, result(req), current_fingerprint=None)
    assert not blockers(req, result(req))


def test_wire_rejects_unknown_fields_duplicate_findings_and_fake_numeric_types():
    req = request()
    raw = result(req).model_dump(mode="json")
    for changes in [
        {"observed_at": True},
        {"findings": raw["findings"] * 2},
        {"shell": "bad"},
    ]:
        with pytest.raises(ValidationError):
            RuntimePreflightResult.model_validate({**raw, **changes})


def failed_with(code: str):
    req = request()
    findings = [
        {"capability": value, "status": "passed", "code": "available"}
        for value in mandatory_capabilities(req)
    ]
    findings[
        findings.index(next(f for f in findings if f["capability"] == "podman_build"))
    ] = {
        "capability": "podman_build",
        "status": "failed",
        "code": code,
    }
    return blockers(req, result(req, findings=findings))


@pytest.mark.parametrize(
    "reported",
    [
        "proc-mount-denied",
        "helper_operation_io",
        "preflight_finding.deadline_exceeded",
        "some_newer_code",
    ],
)
def test_failed_preflight_is_readable_and_repaired_evidence_admits(
    reported: str,
) -> None:
    assert failed_with(reported)
    req = request()
    assert not blockers(req, result(req))


def test_controller_receipt_clock_ignores_agent_clock(tmp_path):
    from vonk_control.inventory_items import record_preflight
    from vonk_control.models import AgentNode
    from vonk_control.runtime_preflight import latest_result

    from .test_lifecycle_preflight import _setup

    sessions, _, clock, node_id, _, _ = _setup(tmp_path)
    req = request()
    sample = result(req, observed_at=int(clock.now.timestamp()) + 600)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        record_preflight(node, request_digest(req), sample, clock.now)
    with sessions() as session:
        receipt = latest_result(
            session, node_id, requirements_sha256=request_digest(req)
        )
        assert not admission_blockers(
            req, receipt, current_fingerprint="a" * 64, now=int(clock.now.timestamp())
        )


def test_receipt_age_uses_controller_subsecond_precision():
    req = request()
    receipt = PreflightInventoryItem(
        requirements_sha256=request_digest(req),
        result=result(req, observed_at=900),
        received_at=datetime.fromtimestamp(100.25, UTC),
    )
    assert not admission_blockers(req, receipt, current_fingerprint="a" * 64, now=100.5)
    assert admission_blockers(req, receipt, current_fingerprint="a" * 64, now=400.5)
    assert admission_blockers(
        req,
        receipt.model_copy(update={"received_at": None}),
        current_fingerprint="a" * 64,
        now=100.5,
    )
