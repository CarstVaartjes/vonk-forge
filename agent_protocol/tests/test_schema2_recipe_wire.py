from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from vonk_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProtocolError,
    CompiledExecutionPlan,
    RecipeOperationRequest,
    canonical_message,
)


PLAN = json.loads(
    (
        Path(__file__).parent / "fixtures" / "compiled-execution-plan-v2.json"
    ).read_text()
)
INSTALLATION_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000003"
REVISION_ID = "00000000-0000-4000-8000-000000000002"
MAPPING_ID = "00000000-0000-4000-8000-000000000007"


def _install() -> dict[str, object]:
    return {
        "schema_version": 2,
        "installation_id": INSTALLATION_ID,
        "plan_digest": "b" * 64,
        "rank": 0,
        "role": "entrypoint",
        "expected_bytes": 1,
        "compiled_execution_plan": copy.deepcopy(PLAN),
    }


def _start() -> dict[str, object]:
    plan = copy.deepcopy(PLAN)
    plan["runtime"]["placement"]["endpoint_address"] = "100.100.20.30"
    plan["security"]["network_mode"] = "bridge"
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "installation_id": INSTALLATION_ID,
        "recipe_revision_id": REVISION_ID,
        "recipe_content_sha256": PLAN["identity"]["recipe_revision_sha256"],
        "mapping_id": MAPPING_ID,
        "mapping_generation": 1,
        "image_digest": PLAN["runtime"]["image_digest"],
        "plan_digest": "c" * 64,
        "alias": "test-model",
        "rank": 0,
        "role": "entrypoint",
        "port": 8000,
        "reserved_memory_bytes": 67108864,
        "endpoint_address": "100.100.20.30",
        "world_size": 1,
        "compiled_execution_plan": plan,
        "local_address": None,
        "master_address": None,
        "master_port": None,
    }


def test_sanitized_compiled_plan_and_current_outer_payloads_round_trip() -> None:
    assert CompiledExecutionPlan.parse(PLAN).endpoint is not None
    install = RecipeOperationRequest.parse(AgentOperation.RECIPE_INSTALL, _install())
    start = RecipeOperationRequest.parse(AgentOperation.RECIPE_START, _start())
    assert install.schema_version == start.schema_version == 2
    assert start.mapping_id == MAPPING_ID


def test_agent_claim_dispatches_the_same_typed_install_and_start_models() -> None:
    for operation, payload in (
        (AgentOperation.RECIPE_INSTALL, _install()),
        (AgentOperation.RECIPE_START, _start()),
    ):
        claim = {
            "schema_version": 1,
            "attempt": 1,
            "deadline": "2026-09-07T12:05:00+00:00",
            "fence": "00000000-0000-4000-8000-000000000011",
            "job_id": "00000000-0000-4000-8000-000000000012",
            "operation": operation.value,
            "operation_id": "00000000-0000-4000-8000-000000000013",
            "node_id": "spk_11111111111111111111111111111111",
            "authority_revision": "a" * 64,
            "payload": payload,
        }
        claim["payload_digest"] = hashlib.sha256(canonical_message(payload)).hexdigest()
        claim = AgentClaim.parse(claim)
        assert claim.payload["schema_version"] == 2


def test_plan_rejects_unsafe_paths() -> None:
    value = copy.deepcopy(PLAN)
    value["artifacts"][0]["path"] = "../escape"
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


def test_plan_rejects_non_boolean_security_values() -> None:
    value = copy.deepcopy(PLAN)
    value["security"]["privileged"] = 1
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


def test_schema_version_is_an_integer_discriminator() -> None:
    value = copy.deepcopy(PLAN)
    value["schema_version"] = 2.0
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


def test_schema2_rejects_legacy_flat_install_and_missing_required_options() -> None:
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(
            AgentOperation.RECIPE_INSTALL,
            {"schema_version": 1, "installation_id": INSTALLATION_ID},
        )
    value = _install()
    del value["compiled_execution_plan"]["job"]
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(AgentOperation.RECIPE_INSTALL, value)
