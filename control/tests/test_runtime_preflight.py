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
    node_fingerprint,
    request_digest,
)


def request(**changes):
    return RuntimePreflightRequest.model_validate(
        dict(
            schema_version=1,
            architecture="linux-arm64",
            source_build=True,
            minimum_free_bytes=4 * 1024**3,
            fabric_connectivity="none",
            fabric_minimum_mbps=0,
            mandatory_capabilities=[],
            **changes,
        )
    )


def result(req, **changes):
    fields = {
        "schema_version": 1,
        "fingerprint": "a" * 64,
        "request_sha256": request_digest(req),
        "observed_at": 100,
        "duration_ms": 500,
        "cached": False,
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


def test_unknown_mandatory_requirement_blocks_without_optional_capability_gate():
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
    req = req.model_copy(update={"mandatory_capabilities": ["future_engine_feature"]})
    res = res.model_copy(update={"request_sha256": request_digest(req)})
    assert blockers(req, res)[0].code == "runtime_preflight.requirement_unknown"


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


def test_fabric_and_disk_requirements_are_exact_and_missing_evidence_blocks():
    req = request().model_copy(
        update={"fabric_connectivity": "full_mesh", "fabric_minimum_mbps": 100000}
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
    changed = req.model_copy(update={"minimum_free_bytes": req.minimum_free_bytes + 1})
    assert (
        blockers(changed, result(req))[0].code
        == "runtime_preflight.requirements_changed"
    )


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
    assert node_fingerprint(["runtime.preflight.fingerprint." + "a" * 64]) == "a" * 64
    assert node_fingerprint(["runtime.preflight.fingerprint.invalid"]) is None
    assert node_fingerprint(["runtime.preflight.fingerprint." + "a" * 64] * 2) is None


def test_wire_rejects_unknown_fields_duplicate_findings_and_fake_numeric_types():
    req = request()
    raw = result(req).model_dump(mode="json")
    for changes in [
        {"duration_ms": True},
        {"duration_ms": 60000},
        {"findings": raw["findings"] * 2},
        {"shell": "bad"},
    ]:
        with pytest.raises(ValidationError):
            RuntimePreflightResult.model_validate({**raw, **changes})
