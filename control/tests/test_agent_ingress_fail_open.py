"""Optional agent evidence never costs the mandatory part of an ingress report.

The class: an agent report is a mandatory core plus optional evidence, and a
damaged optional part must not make the Controller refuse the core (an
inventory that stops updating, a heartbeat that loses its lease, a failure
result that is lost). Every agent ingress body is classified below; the checks
inject invalid optional fields into each evidence-bearing model and assert the
core is still accepted, and fail when a new ingress body or a new optional field
is added without a decision.
"""

from __future__ import annotations

import json
import typing
import uuid
from collections.abc import Mapping
from pathlib import Path

import pytest
import vonk_agent_protocol
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError
from vonk_agent_protocol import (
    AgentEvidenceCode,
    AgentProgress,
    AgentResult,
    InventoryRequest,
)
from vonk_agent_protocol.claims import ClaimRequest
from vonk_agent_protocol.optional_evidence import EvidenceGroup, OptionalEvidenceModel
from vonk_agent_protocol.telemetry import TelemetryRequest, TelemetrySample
from vonk_control.agent_api import install_agent_routes

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY

VECTORS = Path(vonk_agent_protocol.__file__).parent / "vectors"

#: Ingress bodies that are authority, identity or per-item judged, with the reason
#: they stay strict. Everything else must be an ``OptionalEvidenceModel``.
STRICT_BODIES = {
    "ActivateRequest": "certificate activation identity",
    "AgentUpgradeGrantRequest": "authorization request for a signed grant",
    "HostRuntimeGrantRequest": "authorization request for a signed grant",
    "PackageActivationGrantRequest": "authorization request for a signed grant",
    "RecipeRunObservationsWire": "each run is judged on its own at the endpoint",
    "RenewRequest": "certificate renewal identity",
}

#: Optional fields no evidence group covers, each reviewed as part of the
#: mandatory core (failure identity, plan binding, receipts), not as evidence.
REVIEWED_CORE_DEFAULTS = {
    "AgentRuntimeIdentity.package_activation",
    "RecipeStartResult.endpoint",
    "RecipeJobRunResult.reason",
    # Reached through OutcomeDone.result, the receipt of a success: its
    # diagnostics describe a completed job, not a failure the Controller acts on.
    "RecipeJobRunResult.diagnostics",
    "FailureDiagnostics.schema_version",
    "AgentFailureResult.error_code",
    "AgentFailureResult.failure_kind",
    "AgentFailureResult.operation",
    "AgentFailureResult.package_activation",
    "AgentFailureResult.reason",
    "AgentFailureResult.recovery",
    "AgentFailureResult.retry_after_seconds",
    "AgentFailureResult.status",
    "AgentFailureResult.summary",
    "AgentFailureResult.uncertain",
    "AgentFailureResult.wait_reason",
    "OutcomeFailed.evidence",
    "OutcomeFailed.failure_kind",
    "OutcomeFailed.receipt",
    "OutcomeFailed.retry_after_seconds",
    "OutcomeEvidence.package_activation",
    "OutcomeUnknown.evidence",
    "OutcomeUnknown.receipt",
    "OutcomeUnknown.retry_after_seconds",
}


def _ingress_bodies() -> list[type[BaseModel]]:
    app = FastAPI()
    install_agent_routes(app, services=None)
    included = app.routes[-1]
    bodies: dict[type[BaseModel], None] = {}
    for route in included.original_router.routes:  # type: ignore[attr-defined]
        for parameter in route.dependant.body_params:
            bodies[parameter.field_info.annotation] = None
    return list(bodies)


def _models_in(annotation: object) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    for argument in typing.get_args(annotation):
        found += _models_in(argument)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    return found


def _uncovered_optional_fields(
    model: type[BaseModel],
    covered: frozenset[tuple[str, ...]],
    prefix: tuple[str, ...] = (),
    seen: set[tuple[type[BaseModel], tuple[str, ...]]] | None = None,
) -> set[str]:
    seen = set() if seen is None else seen
    if (model, prefix) in seen:
        return set()
    seen.add((model, prefix))
    found: set[str] = set()
    for name, field in model.model_fields.items():
        path = (*prefix, name)
        if path in covered:
            continue
        if not field.is_required():
            found.add(f"{model.__name__}.{name}")
        for nested in _models_in(field.annotation):
            found |= _uncovered_optional_fields(nested, covered, path, seen)
    return found


def test_every_ingress_body_is_evidence_tolerant_or_reviewed_strict() -> None:
    """Catches a new agent endpoint whose optional evidence can fail its core."""

    bodies = _ingress_bodies()
    assert bodies
    for body in bodies:
        if body.__name__ in STRICT_BODIES:
            continue
        # Evidence lives on the body itself or on a nested element (telemetry
        # samples); a body with neither must carry no optional field at all.
        carriers = (
            [body, *_models_in(body.model_fields["samples"].annotation)]
            if ("samples" in body.model_fields)
            else [body]
        )
        covered = frozenset(
            path
            for carrier in carriers
            if issubclass(carrier, OptionalEvidenceModel)
            for group in carrier.EVIDENCE_GROUPS
            for path in group.paths
        )
        assert any(
            issubclass(carrier, OptionalEvidenceModel) and carrier.EVIDENCE_GROUPS
            for carrier in carriers
        ) or not _uncovered_optional_fields(body, covered), (
            f"{body.__name__} is an agent ingress body with optional fields "
            "but no OptionalEvidenceModel groups; classify it"
        )
    assert {body.__name__ for body in bodies} >= set(STRICT_BODIES)


def test_every_optional_field_is_evidence_or_a_reviewed_core_default() -> None:
    """Catches a new optional field on a report that nobody decided about."""

    uncovered: set[str] = set()
    for body in _ingress_bodies():
        if body.__name__ in STRICT_BODIES:
            continue
        carriers = (
            _models_in(body.model_fields["samples"].annotation)
            if "samples" in body.model_fields
            else [body]
        )
        for carrier in carriers:
            covered = frozenset(
                path
                for group in getattr(carrier, "EVIDENCE_GROUPS", ())
                for path in group.paths
            )
            uncovered |= _uncovered_optional_fields(carrier, covered)
    assert uncovered == REVIEWED_CORE_DEFAULTS


# ---------------------------------------------------------------- injection

_INVENTORY = {
    "schema_version": 1,
    "observed_at": "2026-10-06T10:00:00+00:00",
    "disk_total_bytes": 10,
    "disk_free_bytes": 5,
    "host_memory_total_bytes": 10,
    "host_memory_free_bytes": 5,
    "gpu_memory_total_bytes": 10,
    "gpu_memory_free_bytes": 5,
    "gpu_count": 1,
    "memory_pool": "separate",
    "artifact_store_read_only": False,
    "capabilities": ["runtime.vonk.v1"],
    "nvidia_driver_version": "580.1",
    "container_runtime_version": "28.3",
}
_NIC = {"name": "enP7s7", "kind": "wired", "carrier": True}
_SAMPLE = {
    "boot_id": "00000000-0000-4000-8000-000000000001",
    "observed_at": "2026-10-06T10:00:00+00:00",
    "memory_total_bytes": 10,
    "memory_available_bytes": 5,
    "disk_total_bytes": 10,
    "disk_free_bytes": 5,
    "gpu_utilization_percent": 1.0,
    "gpu_memory_total_bytes": 10,
    "gpu_memory_free_bytes": 5,
}
_FENCE = str(uuid.UUID(int=7))
_CLAIM = {
    "protocol_version": 4,
    "wait_seconds": 0,
    "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
}


def _diagnostics() -> dict[str, object]:
    return json.loads((VECTORS / "failure-diagnostics-v1.json").read_text())


def _failed_result(**extra: object) -> dict[str, object]:
    return {
        "fence": _FENCE,
        "state": "failed",
        "result": {"error_code": "recipe_build_failed", "status": "failed", **extra},
    }


#: (model, valid document, {first path of a group: the document with that
#: group made invalid}).
CASES: list[
    tuple[type[BaseModel], Mapping[str, object], dict[str, Mapping[str, object]]]
] = [
    (
        InventoryRequest,
        _INVENTORY,
        {
            "network_interfaces": _INVENTORY | {"network_interfaces": [_NIC, _NIC]},
            "nas_route_interface": _INVENTORY
            | {"network_interfaces": [_NIC], "nas_route_interface": "wlan9"},
            "fabric_address": _INVENTORY | {"fabric_address": "192.168.100.2"},
        },
    ),
    (
        TelemetrySample,
        _SAMPLE,
        {
            "gpu_temperature_c": _SAMPLE | {"gpu_temperature_c": 900},
            "cpu_frequency_avg_mhz": _SAMPLE
            | {"cpu_frequency_avg_mhz": 1000, "cpu_frequency_min_mhz": 2000},
        },
    ),
    (
        AgentProgress,
        {"fence": _FENCE},
        {
            "progress": {
                "fence": _FENCE,
                "progress": {"phase": "", "completed_bytes": -1},
            }
        },
    ),
    (
        AgentResult,
        _failed_result(),
        {
            "result.diagnostics": _failed_result(diagnostics={"phase": 1}),
            "result.diagnostic": _failed_result(diagnostic="x" * 600),
            "result.stage": _failed_result(stage="s" * 200),
            "result.helper_error_code": _failed_result(helper_error_code=""),
            "result.helper_exit_code": _failed_result(helper_exit_code=900),
        },
    ),
    (
        ClaimRequest,
        _CLAIM,
        {"hostname": _CLAIM | {"hostname": "bad host", "preflight_fingerprint": "x"}},
    ),
]


def _paths(group: EvidenceGroup) -> set[str]:
    return {".".join(path) for path in group.paths}


@pytest.mark.parametrize(
    "model", [case[0] for case in CASES], ids=lambda model: model.__name__
)
def test_every_evidence_group_is_covered_by_an_injection(
    model: type[OptionalEvidenceModel],
) -> None:
    """Catches a new evidence group that no injection proves fail-open."""

    (poison,) = [case[2] for case in CASES if case[0] is model]
    for group in model.EVIDENCE_GROUPS:
        assert _paths(group) & set(poison), group
    assert set(poison) <= {
        path for group in model.EVIDENCE_GROUPS for path in _paths(group)
    }


@pytest.mark.parametrize(
    ("model", "valid", "poisoned"),
    [
        pytest.param(model, valid, poisoned, id=f"{model.__name__}:{key}")
        for model, valid, patches in CASES
        for key, poisoned in patches.items()
    ],
)
def test_invalid_optional_evidence_keeps_the_mandatory_core(
    model: type[OptionalEvidenceModel],
    valid: Mapping[str, object],
    poisoned: Mapping[str, object],
) -> None:
    """Catches a report refused whole because one optional part is invalid."""

    assert not model.model_validate(valid).evidence_warnings
    accepted = model.model_validate(poisoned)
    # A warning exists only because strict validation of the document failed.
    assert accepted.evidence_warnings
    assert all(
        isinstance(code, AgentEvidenceCode) for code in accepted.evidence_warnings
    )
    core = model.model_validate(valid)
    for name in ("disk_total_bytes", "gpu_count", "memory_total_bytes", "fence"):
        if name in model.model_fields:
            assert getattr(accepted, name) == getattr(core, name)
    # The json path (what the agent actually sends) is tolerant the same way.
    assert model.model_validate_json(json.dumps(poisoned)).evidence_warnings


def test_telemetry_batch_survives_one_samples_invalid_reading() -> None:
    """Catches a 16-sample batch refused for one optional CPU clock."""

    second = _SAMPLE | {"observed_at": "2026-10-06T10:00:10+00:00"}
    bad = second | {"cpu_frequency_avg_mhz": 1000, "cpu_frequency_min_mhz": 2000}
    batch = TelemetryRequest.model_validate({"samples": [_SAMPLE, bad]})
    assert [sample.cpu_frequency_min_mhz for sample in batch.samples] == [None, None]
    assert batch.samples[1].evidence_warnings


@pytest.mark.parametrize(
    "ruined",
    [
        {"disk_free_bytes": 50},
        {"memory_pool": "elsewhere"},
        {"nvidia_driver_version": ""},
    ],
)
def test_a_broken_core_is_still_refused_when_evidence_is_also_broken(
    ruined: dict[str, object],
) -> None:
    """Catches tolerance that repairs an invalid core: only evidence is dropped."""

    document = _INVENTORY | ruined | {"nas_route_interface": "wlan9"}
    with pytest.raises(ValidationError):
        InventoryRequest.model_validate(document)


def test_evidence_validator_is_declared_last_on_every_evidence_model() -> None:
    """Catches a model whose consistency checks run outside the fail-open wrap."""

    with pytest.raises(TypeError, match="end its body"):

        class Misordered(OptionalEvidenceModel):
            EVIDENCE_GROUPS: typing.ClassVar = (
                EvidenceGroup(AgentEvidenceCode.PROGRESS_DROPPED, (("x",),)),
            )
            x: int | None = None
