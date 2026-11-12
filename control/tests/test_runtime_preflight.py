from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import RuntimePreflightCode
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
    args = {"current_fingerprint": "a" * 64}
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


def test_a_receipt_is_valid_by_identity_not_age():
    """Catches a wall-clock window: an old receipt on the same host and the
    same requirements still admits; a new host fingerprint does not."""
    req = request()
    old = PreflightInventoryItem(
        requirements_sha256=request_digest(req),
        result=result(req),
        received_at=datetime.fromtimestamp(100, UTC) - timedelta(days=30),
    )
    assert not admission_blockers(req, old, current_fingerprint="a" * 64)
    [changed] = admission_blockers(req, old, current_fingerprint="b" * 64)
    assert changed.code is RuntimePreflightCode.HOST_CHANGED


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


def test_receipts_are_retained_per_digest_for_the_current_host(tmp_path):
    """Catches retention by age (a receipt for another digest pruned after
    300 s) and retention without a bound (receipts from an old host kept)."""
    from vonk_control.inventory_items import record_preflight
    from vonk_control.models import AgentNode
    from vonk_control.runtime_preflight import latest_result

    from .test_lifecycle_preflight import _setup

    sessions, _, clock, node_id, _, _ = _setup(tmp_path)
    a = request()
    b = a.model_copy(update={"source_build": False})
    later = clock.now + timedelta(days=1)

    def record(req, fingerprint, now):
        with sessions.begin() as session:
            node = session.get(AgentNode, node_id)
            assert node is not None
            record_preflight(
                node, request_digest(req), result(req, fingerprint=fingerprint), now
            )

    def kept(req):
        with sessions() as session:
            return latest_result(
                session, node_id, requirements_sha256=request_digest(req)
            )

    record(a, "a" * 64, clock.now)
    record(b, "a" * 64, later)
    assert kept(a) is not None and kept(b) is not None
    record(b, "c" * 64, later)
    assert kept(a) is None
    current = kept(b)
    assert current is not None and current.result.fingerprint == "c" * 64
