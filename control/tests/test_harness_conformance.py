from __future__ import annotations

import copy

import pytest
from vonk_control.compiled_execution_plan import CompiledExecutionPlan
from vonk_control.harness_conformance import (
    HarnessConformanceError,
    _fixture_request,
    run_synthetic_conformance,
    validate_terminal_evidence,
)
from vonk_control.harnesses.canonical_metadata import CANONICAL_HARNESSES
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

CANONICAL_SLUGS = tuple(item.slug for item in CANONICAL_HARNESSES)


@pytest.mark.parametrize("slug", CANONICAL_SLUGS)
def test_canonical_harness_completes_observed_synthetic_lifecycle(slug: str) -> None:
    evidence = run_synthetic_conformance(slug)

    assert evidence.phases == (
        "inspect",
        "prepare",
        "verify",
        "start",
        "inspect",
        "inspect",
        "start",
        "ready",
        "invoke",
        "inspect",
        "stop",
        "inspect",
        "inspect",
        "stop",
        "verify-stopped",
    )
    assert evidence.offline_runtime is True
    assert evidence.security["docker_socket"] is False
    assert evidence.security["plan_schema_version"] == 2
    assert evidence.interrupted_start_recovered is True
    assert evidence.interrupted_stop_recovered is True
    assert evidence.stop_bounded is True
    assert evidence.recovery_phases == (
        "start-interrupted",
        "inspect-idempotent",
        "start-recovered",
        "stop-interrupted",
        "inspect-idempotent",
        "stop-recovered",
    )
    assert evidence.document["schema_version"] == 2
    assert CompiledExecutionPlan.model_validate(evidence.document["plan"])


def test_conformance_fixture_uses_canonical_pydantic_definitions() -> None:
    request = _fixture_request("vllm")
    assert isinstance(request.recipe, RecipeDefinition)
    assert request.models and all(isinstance(item, ModelDefinition) for item in request.models)
    assert isinstance(request.plan, CompiledExecutionPlan)
    assert request.plan.schema_version == 2
    runtime_identity = request.runtime_spec["identity"]
    assert isinstance(runtime_identity, dict)
    assert runtime_identity["recipe_revision_sha256"]


def test_artifact_job_uses_production_nullable_placement() -> None:
    request = _fixture_request("diffusers")

    assert request.launch_payload["endpoint"] is None
    assert request.launch_payload["job"] is not None
    assert request.placement["port"] is None
    reserved_memory_bytes = request.placement["reserved_memory_bytes"]
    assert isinstance(reserved_memory_bytes, int)
    assert reserved_memory_bytes > 0


def test_conformance_rejects_unknown_harness() -> None:
    with pytest.raises(HarnessConformanceError, match="unknown execution harness"):
        run_synthetic_conformance("legacy-harness")


def test_conformance_rejects_tampered_plan_evidence() -> None:
    request = _fixture_request("vllm")
    document = copy.deepcopy(run_synthetic_conformance("vllm").document)
    plan = document["plan"]
    assert isinstance(plan, dict)
    plan["harness_sha256"] = "0" * 64

    with pytest.raises(HarnessConformanceError, match="plan identity"):
        validate_terminal_evidence(document, request)


def test_conformance_rejects_invalid_schema_or_retired_identity_evidence() -> None:
    request = _fixture_request("vllm")
    document = copy.deepcopy(run_synthetic_conformance("vllm").document)
    document["schema_version"] = 0

    with pytest.raises(HarnessConformanceError, match="evidence is invalid"):
        validate_terminal_evidence(document, request)


def test_conformance_fails_closed_for_mutated_canonical_recipe() -> None:
    request = _fixture_request("vllm")
    raw = request.recipe.model_dump(mode="json")
    raw["runtime"]["entrypoint"] = ["/bin/sh", "-c", "unsafe"]
    with pytest.raises(HarnessConformanceError):
        from vonk_control.harness_conformance import run_recipe_conformance

        run_recipe_conformance(raw, request.models)
