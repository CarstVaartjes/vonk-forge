from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProtocolError,
    CompiledExecutionPlan,
    RecipeOperationRequest,
    RecipeStartResult,
)
from vonk_agent_protocol.compiled_execution_plan import (
    MAX_COMPILED_EXECUTION_PLAN_MOUNTS,
    CompiledJobInput,
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
        "installation_id": INSTALLATION_ID,
        "plan_digest": "b" * 64,
        "expected_bytes": 1,
        "compiled_execution_plan": copy.deepcopy(PLAN),
    }


def _start() -> dict[str, object]:
    plan = copy.deepcopy(PLAN)
    plan["runtime"]["placement"]["endpoint_address"] = "100.100.20.30"
    plan["security"]["network_mode"] = "bridge"
    return {
        "run_id": RUN_ID,
        "installation_id": INSTALLATION_ID,
        "recipe_revision_id": REVISION_ID,
        "mapping_id": MAPPING_ID,
        "plan_digest": "c" * 64,
        "compiled_execution_plan": plan,
        "run_generation": 1,
    }


def _distributed_start() -> dict[str, object]:
    plan = copy.deepcopy(PLAN)
    plan["topology"].update(node_count=2)
    plan["runtime"]["placement"].update(
        world_size=2,
        rank=1,
        role="worker",
        endpoint_address=None,
        local_address="100.100.20.31",
        master_address="100.100.20.30",
        master_port=29500,
    )
    plan["security"].update(network_mode="host", gpu=True)
    return {
        **_start(),
        "phase": "rank-launch",
        "start_deadline": "2026-09-07T12:05:00+00:00",
        "run_generation": 1,
        "compiled_execution_plan": plan,
    }


def test_sanitized_compiled_plan_and_current_outer_payloads_round_trip() -> None:
    assert CompiledExecutionPlan.parse(PLAN).endpoint is not None
    install = RecipeOperationRequest.parse(AgentOperation.RECIPE_INSTALL, _install())
    start = RecipeOperationRequest.parse(AgentOperation.RECIPE_START, _start())
    assert install.installation_id == start.installation_id == INSTALLATION_ID
    assert start.mapping_id == MAPPING_ID


def test_start_result_carries_only_the_serving_endpoint() -> None:
    assert RecipeStartResult.model_validate({}).endpoint is None
    served = RecipeStartResult.model_validate({"endpoint": "http://100.100.20.31:8000"})
    assert served.endpoint == "http://100.100.20.31:8000"
    with pytest.raises(ValueError):
        RecipeStartResult.model_validate({"endpoint": None, "ready": True})


def test_install_accepts_deferred_distributed_rendezvous() -> None:
    plan = copy.deepcopy(PLAN)
    plan["topology"].update(node_count=2)
    plan["runtime"]["placement"].update(
        world_size=2,
        local_address=None,
        master_address=None,
        master_port=None,
    )
    plan["security"]["network_mode"] = "none"
    payload = _install()
    payload["compiled_execution_plan"] = plan
    RecipeOperationRequest.parse(AgentOperation.RECIPE_INSTALL, payload)


def test_compiled_single_node_plan_rejects_rendezvous_addresses() -> None:
    plan = copy.deepcopy(PLAN)
    plan["runtime"]["placement"]["local_address"] = "100.100.20.30"
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(plan)


def test_agent_claim_dispatches_the_same_typed_install_and_start_models() -> None:
    for operation, payload in (
        (AgentOperation.RECIPE_INSTALL, _install()),
        (AgentOperation.RECIPE_START, _start()),
    ):
        claim = AgentClaim.parse(
            {
                "deadline": "2026-09-07T12:05:00+00:00",
                "fence": "00000000-0000-4000-8000-000000000011",
                "operation": operation.value,
                "payload": payload,
            }
        )
        assert claim.payload.installation_id == INSTALLATION_ID


def test_plan_rejects_unsafe_paths() -> None:
    value = copy.deepcopy(PLAN)
    value["artifacts"][0]["path"] = "../escape"
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


@pytest.mark.parametrize(
    "path",
    [
        "模型 weights_file.safetensors",
        "模型 file_" * 64,
    ],
)
def test_plan_accepts_canonical_unicode_space_and_max_length_paths(path: str) -> None:
    value = copy.deepcopy(PLAN)
    value["artifacts"][0]["path"] = path
    publisher = "发布者 " + "_" * 124
    value["artifacts"][0]["model"]["publisher"] = publisher

    plan = CompiledExecutionPlan.parse(value)
    assert plan.artifacts[0].path == path
    assert len(plan.artifacts[0].path) <= 512
    assert plan.artifacts[0].model.publisher == publisher


@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "../escape",
        "nested//file",
        "nested/./file",
        "nested/../file",
        "bad\\name",
        "bad\x00name",
        "x" * 513,
    ],
)
def test_plan_rejects_unsafe_or_oversized_model_paths(path: str) -> None:
    value = copy.deepcopy(PLAN)
    value["artifacts"][0]["path"] = path
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


@pytest.mark.parametrize("publisher", ["publisher\x00", "p" * 129])
def test_plan_rejects_unsafe_or_oversized_model_publisher(publisher: str) -> None:
    value = copy.deepcopy(PLAN)
    value["artifacts"][0]["model"]["publisher"] = publisher
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


@pytest.fixture(scope="session")
def compiled_plan_wire_probe() -> Path:
    repository = Path(__file__).resolve().parents[2]
    configured_probe = os.environ.get("VONK_COMPILED_PLAN_WIRE_PROBE")
    if configured_probe:
        probe = Path(configured_probe)
        if not probe.is_absolute():
            probe = repository / probe
    else:
        target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
        if not target_root.is_absolute():
            target_root = repository / target_root
        probe = target_root / "debug" / "examples" / "compiled_plan_wire_probe"
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--package",
                "vonk-agent",
                "--example",
                "compiled_plan_wire_probe",
            ],
            cwd=repository,
            check=True,
        )
    if not probe.is_file() or not os.access(probe, os.X_OK):
        raise AssertionError(f"compiled plan wire probe is not executable: {probe}")
    return probe


def _rust_compiled_plan_accepts(probe: Path, value: dict[str, object]) -> bool:
    completed = subprocess.run(
        [str(probe)],
        input=json.dumps(value, ensure_ascii=False) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _rust_compiled_plan_round_trip(
    probe: Path, value: dict[str, object]
) -> dict[str, object] | None:
    completed = subprocess.run(
        [str(probe)],
        input=json.dumps(value, ensure_ascii=False) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return json.loads(completed.stdout)


def test_python_compiled_plan_producer_crosses_rust_parser(
    compiled_plan_wire_probe: Path,
) -> None:
    for path in ("模型 weights_file.safetensors", "模型 file_" * 64):
        value = copy.deepcopy(PLAN)
        value["artifacts"][0]["path"] = path
        value["artifacts"][0]["model"]["publisher"] = "发布者 " + "_" * 124
        authored = CompiledExecutionPlan.parse(value).model_dump(mode="json")
        assert _rust_compiled_plan_accepts(compiled_plan_wire_probe, authored)

    for path in ("../escape", "nested//file", "bad\\name", "bad\x00name", "x" * 513):
        value = copy.deepcopy(PLAN)
        value["artifacts"][0]["path"] = path
        assert not _rust_compiled_plan_accepts(compiled_plan_wire_probe, value)

    value = copy.deepcopy(PLAN)
    value["artifacts"][0]["model"]["publisher"] = "p" * 129
    assert not _rust_compiled_plan_accepts(compiled_plan_wire_probe, value)


def test_compiled_job_input_is_typed_and_round_trips_both_wire_directions(
    compiled_plan_wire_probe: Path,
) -> None:
    input_value = {
        "required": True,
        "media_types": ["application/json"],
        "max_bytes": 1024,
        "slots": [
            {
                "id": "document",
                "label": "Document",
                "description": "A JSON document to process",
                "media_types": ["application/json"],
                "extensions": [".7z"],
                "min_files": 1,
                "max_files": 1,
                "max_file_bytes": 1024,
                "max_total_bytes": 1024,
            }
        ],
    }
    CompiledJobInput.model_validate(input_value)
    value = copy.deepcopy(PLAN)
    value["endpoint"] = None
    value["runtime"]["placement"]["port"] = None
    value["job"] = {
        "interface": "artifact-job",
        "input": input_value,
        "timeout_seconds": 60,
    }
    authored = CompiledExecutionPlan.parse(value).to_mapping()
    returned = _rust_compiled_plan_round_trip(compiled_plan_wire_probe, authored)
    assert returned == authored
    assert CompiledExecutionPlan.parse(returned).to_mapping() == authored

    for mutation in (
        lambda item: item.update(declared_content={"vendor": "free-form"}),
        lambda item: item.pop("required"),
        lambda item: item.update(max_bytes="1024"),
        lambda item: item["slots"][0].update(max_files=0),
        lambda item: item["slots"][0].update(max_total_bytes=2048),
    ):
        invalid = copy.deepcopy(value)
        mutation(invalid["job"]["input"])
        with pytest.raises(AgentProtocolError):
            CompiledExecutionPlan.parse(invalid)
        assert not _rust_compiled_plan_accepts(compiled_plan_wire_probe, invalid)


def test_plan_rejects_non_boolean_security_values() -> None:
    value = copy.deepcopy(PLAN)
    value["security"]["gpu"] = 1
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


def test_plan_accepts_canonical_multi_model_mount_projection() -> None:
    value = copy.deepcopy(PLAN)
    value["security"]["mounts"] = [
        {"source": "model", "target": "/models/target"},
        {"source": "model", "target": "/models/text-encoder"},
        {"source": "model", "target": "/models/vae"},
        {"source": "inputs", "target": "/inputs"},
        {"source": "outputs", "target": "/outputs"},
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
    value["security"]["mounts"] = [{"source": source, "target": target}]
    plan = CompiledExecutionPlan.parse(value)
    assert plan.security.mounts[0].read_only is read_only


@pytest.mark.parametrize(
    ("source", "target"),
    [("model", "/inputs"), ("inputs", "/models"), ("outputs", "/models")],
)
def test_mount_source_and_target_policy_rejects_swapped_matrix(
    source: str, target: str
) -> None:
    value = copy.deepcopy(PLAN)
    value["security"]["mounts"] = [{"source": source, "target": target}]
    with pytest.raises(AgentProtocolError):
        CompiledExecutionPlan.parse(value)


@pytest.mark.parametrize("kind", ["over", "duplicate", "unsafe"])
def test_plan_rejects_over_duplicate_or_unsafe_mounts(kind: str) -> None:
    value = copy.deepcopy(PLAN)
    mounts = value["security"]["mounts"]
    if kind == "over":
        mounts.extend(
            {"source": "model", "target": f"/models/extra-{index}"}
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


def test_start_phase_shape_matches_single_and_distributed_controller_modes() -> None:
    singleton_with_generation = _start()
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
    for key in ("phase", "start_deadline"):
        del unphased_distributed[key]
    unphased = RecipeOperationRequest.parse(
        AgentOperation.RECIPE_START, unphased_distributed
    )
    assert unphased.phase is None
    unphased_distributed.pop("run_generation")
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(AgentOperation.RECIPE_START, unphased_distributed)
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
            {"installation_id": INSTALLATION_ID},
        )
    value = _install()
    del value["compiled_execution_plan"]["job"]
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(AgentOperation.RECIPE_INSTALL, value)
