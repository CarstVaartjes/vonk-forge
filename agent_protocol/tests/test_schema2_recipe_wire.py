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
from vonk_agent_protocol.compiled_execution_plan import (
    MAX_COMPILED_EXECUTION_PLAN_MOUNTS,
)

PLAN = json.loads(
    (Path(__file__).parent / "fixtures" / "compiled-execution-plan-v2.json").read_text()
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
        "run_generation": 1,
    }


def _distributed_start() -> dict[str, object]:
    plan = copy.deepcopy(PLAN)
    plan["topology"].update(
        world_size=2, node_count=2, rank=1, role="worker", mode="distributed"
    )
    plan["runtime"]["placement"].update(
        world_size=2,
        rank=1,
        role="worker",
        endpoint_address=None,
        local_address="100.100.20.31",
        master_address="100.100.20.30",
        master_port=29500,
    )
    plan["security"]["network_mode"] = "bridge"
    return {
        **_start(),
        "phase": "rank-launch",
        "start_deadline": "2026-09-07T12:05:00+00:00",
        "run_generation": 1,
        "rank": 1,
        "role": "worker",
        "world_size": 2,
        "endpoint_address": "100.100.20.31",
        "local_address": "100.100.20.31",
        "master_address": "100.100.20.30",
        "master_port": 29500,
        "compiled_execution_plan": plan,
    }


def test_sanitized_compiled_plan_and_current_outer_payloads_round_trip() -> None:
    assert CompiledExecutionPlan.parse(PLAN).endpoint is not None
    install = RecipeOperationRequest.parse(AgentOperation.RECIPE_INSTALL, _install())
    start = RecipeOperationRequest.parse(AgentOperation.RECIPE_START, _start())
    assert install.schema_version == start.schema_version == 2
    assert start.mapping_id == MAPPING_ID


@pytest.mark.parametrize(
    "mode",
    [
        "single",
        "distributed",
        "tensor_parallel",
        "pipeline_parallel",
        "data_parallel",
        "hybrid",
        "ray",
        "mpi",
    ],
)
@pytest.mark.parametrize("backend", ["tcp", "ucx", "future-engine-Δ"])
def test_recipe_topology_vocabulary_survives_the_wire(mode, backend) -> None:
    plan = copy.deepcopy(PLAN)
    plan["topology"].update(mode=mode, backend=backend)
    result = CompiledExecutionPlan.parse(plan)
    assert result.topology.mode == mode
    assert result.topology.backend == backend


def test_install_accepts_deferred_distributed_rendezvous() -> None:
    plan = copy.deepcopy(PLAN)
    plan["topology"].update(
        world_size=2, node_count=2, mode="distributed", backend="nccl"
    )
    plan["runtime"]["placement"].update(
        world_size=2,
        local_address=None,
        master_address=None,
        master_port=29500,
    )
    plan["security"]["network_mode"] = "bridge"
    payload = _install()
    payload["compiled_execution_plan"] = plan
    RecipeOperationRequest.parse(AgentOperation.RECIPE_INSTALL, payload)


def test_compiled_single_node_plan_rejects_rendezvous_addresses() -> None:
    plan = copy.deepcopy(PLAN)
    plan["runtime"]["placement"]["local_address"] = "100.100.20.30"
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(plan)


def test_start_allows_controller_route_alias_distinct_from_engine_alias() -> None:
    payload = _start()
    payload["alias"] = "controller-route"
    request = RecipeOperationRequest.parse(AgentOperation.RECIPE_START, payload)
    assert request.alias == "controller-route"


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


def test_plan_accepts_canonical_multi_model_mount_projection() -> None:
    value = copy.deepcopy(PLAN)
    value["security"]["mounts"] = [
        {"source": "model", "target": "/models/target", "read_only": True},
        {"source": "model", "target": "/models/text-encoder", "read_only": True},
        {"source": "model", "target": "/models/vae", "read_only": True},
        {"source": "inputs", "target": "/inputs", "read_only": True},
        {"source": "outputs", "target": "/outputs", "read_only": False},
    ]

    plan = CompiledExecutionPlan.parse(value)
    assert len(plan.security.mounts) == 5


@pytest.mark.parametrize(
    ("source", "target", "read_only"),
    [
        ("model", "/models", True),
        ("model", "/models/secondary", True),
        ("inputs", "/inputs", True),
        ("outputs", "/outputs", False),
    ],
)
def test_mount_source_and_target_policy_accepts_canonical_matrix(
    source: str, target: str, read_only: bool
) -> None:
    value = copy.deepcopy(PLAN)
    value["security"]["mounts"] = [
        {"source": source, "target": target, "read_only": read_only}
    ]
    CompiledExecutionPlan.parse(value)


@pytest.mark.parametrize(
    ("source", "target", "read_only"),
    [
        ("model", "/inputs", True),
        ("inputs", "/models", True),
        ("outputs", "/models", False),
    ],
)
def test_mount_source_and_target_policy_rejects_swapped_matrix(
    source: str, target: str, read_only: bool
) -> None:
    value = copy.deepcopy(PLAN)
    value["security"]["mounts"] = [
        {"source": source, "target": target, "read_only": read_only}
    ]
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


@pytest.mark.parametrize("kind", ["over", "duplicate", "unsafe"])
def test_plan_rejects_over_duplicate_or_unsafe_mounts(kind: str) -> None:
    value = copy.deepcopy(PLAN)
    mounts = value["security"]["mounts"]
    if kind == "over":
        mounts.extend(
            {
                "source": "model",
                "target": f"/models/extra-{index}",
                "read_only": True,
            }
            for index in range(MAX_COMPILED_EXECUTION_PLAN_MOUNTS - len(mounts) + 1)
        )
    elif kind == "duplicate":
        mounts.append(mounts[0].copy())
    else:
        mounts[0]["target"] = "/models/../escape"
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


def test_plan_preserves_one_physical_file_as_distinct_mount_projections() -> None:
    value = copy.deepcopy(PLAN)
    projection = copy.deepcopy(value["artifacts"][0])
    projection["mount"]["target"] = "/models/target"
    value["artifacts"].append(projection)
    plan = CompiledExecutionPlan.parse(value)
    assert [(item.mount.target, item.path) for item in plan.artifacts] == [
        ("/models", "weights.bin"),
        ("/models/target", "weights.bin"),
    ]


def test_plan_rejects_conflicting_duplicate_physical_identity() -> None:
    value = copy.deepcopy(PLAN)
    projection = copy.deepcopy(value["artifacts"][0])
    projection["mount"]["target"] = "/models/target"
    projection["file_id"] = "different-file"
    value["artifacts"].append(projection)
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


def test_plan_rejects_duplicate_final_projection_target() -> None:
    value = copy.deepcopy(PLAN)
    projection = copy.deepcopy(value["artifacts"][0])
    value["artifacts"].append(projection)
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


def test_schema_version_is_an_integer_discriminator() -> None:
    value = copy.deepcopy(PLAN)
    value["schema_version"] = 2.0
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


def test_start_phase_shape_matches_single_and_distributed_controller_modes() -> None:
    singleton_with_generation = _start()
    singleton_with_generation["run_generation"] = 1
    singleton = RecipeOperationRequest.parse(
        AgentOperation.RECIPE_START, singleton_with_generation
    )
    assert singleton.run_generation == 1

    phased_single = _start()
    phased_single.update(
        phase="rank-launch",
        start_deadline="2026-09-07T12:05:00+00:00",
        run_generation=1,
    )
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(AgentOperation.RECIPE_START, phased_single)

    unphased_distributed = _distributed_start()
    for key in ("phase", "start_deadline", "run_generation"):
        del unphased_distributed[key]
    unphased = RecipeOperationRequest.parse(
        AgentOperation.RECIPE_START, unphased_distributed
    )
    assert unphased.phase is None
    for key in ("phase", "start_deadline", "run_generation"):
        partial = _distributed_start()
        del partial[key]
        with pytest.raises(AgentProtocolError):
            RecipeOperationRequest.parse(AgentOperation.RECIPE_START, partial)

    assert (
        RecipeOperationRequest.parse(
            AgentOperation.RECIPE_START, _distributed_start()
        ).phase
        == "rank-launch"
    )


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
