from __future__ import annotations

import pytest
from pydantic import ValidationError
from vonk_agent_protocol.runtime_preflight import (
    RuntimePreflightFinding,
    RuntimePreflightRequest,
    RuntimePreflightResult,
)
from vonk_control.runtime_preflight import (
    admission_blockers,
    mandatory_capabilities,
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
    return admission_blockers(req, res, **args)


def test_exact_recipe_and_current_host_can_be_admitted():
    req = request()
    assert not blockers(req, result(req))


def test_changed_host_fingerprint_invalidates_passing_result():
    req = request()
    assert (
        blockers(req, result(req), current_fingerprint="b" * 64)[0].code
        == "runtime_preflight.host_changed"
    )


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
    assert "signed_helper_run" in blockers(req, res)[0].detail


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
    assert "fabric" in blockers(req, res)[0].detail


@pytest.mark.parametrize("now", [99, 401])
def test_stale_and_future_evidence_cannot_admit(now):
    req = request()
    assert blockers(req, result(req), now=now)[0].code == "runtime_preflight.stale"


def test_missing_current_fingerprint_and_missing_result_fail_clearly():
    req = request()
    assert blockers(req, None)[0].code == "runtime_preflight.required"
    assert (
        blockers(req, result(req), current_fingerprint=None)[0].code
        == "runtime_preflight.host_changed"
    )


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
    ("reported", "shown"),
    [
        # An older agent's free text is read through the one legacy adapter.
        ("proc-mount-denied", "preflight_finding.proc_mount_denied"),
        ("helper_operation_io", "preflight_finding.helper_operation_io"),
        # A current spelling reads as itself.
        ("preflight_finding.deadline_exceeded", "preflight_finding.deadline_exceeded"),
        # A word no member spells is kept visible, never refused.
        (
            "some_newer_code",
            "preflight_finding.unclassified (some_newer_code)",
        ),
    ],
)
def test_a_failed_capability_names_its_finding_code_from_any_agent_vintage(
    reported: str, shown: str
) -> None:
    (blocker,) = failed_with(reported)
    assert blocker.code == "runtime_preflight.capability_failed"
    assert blocker.detail == f"Runtime capability podman_build failed: {shown}."
