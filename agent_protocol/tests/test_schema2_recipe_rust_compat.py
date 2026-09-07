from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from vonk_agent_protocol import (
    AgentOperation,
    RecipeInstallPayload,
    RecipeOperationRequest,
    RecipeStartPayload,
    canonical_message,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledJob,
    CompiledSecurityMount,
    CompiledTopology,
)

PLAN = json.loads(
    (Path(__file__).parent / "fixtures" / "compiled-execution-plan-v2.json").read_text(
        encoding="utf-8"
    )
)
INSTALLATION_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000003"
REVISION_ID = "00000000-0000-4000-8000-000000000002"
MAPPING_ID = "00000000-0000-4000-8000-000000000007"


def _install(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "installation_id": INSTALLATION_ID,
        "plan_digest": "b" * 64,
        "rank": 0,
        "role": "entrypoint",
        "expected_bytes": 1,
        "compiled_execution_plan": copy.deepcopy(plan),
    }


def _start(plan: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = copy.deepcopy(plan or PLAN)
    if plan["topology"]["world_size"] == 1:
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


def _distributed_start(
    *, phase: str | None = None, collective: bool = False
) -> dict[str, Any]:
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
        master_address="100.100.20.31" if collective else "100.100.20.30",
        master_port=29500,
    )
    plan["security"]["network_mode"] = "bridge"
    payload = _start(plan)
    payload.update(
        rank=1,
        role="worker",
        world_size=2,
        endpoint_address="100.100.20.31",
        local_address="100.100.20.31",
        master_address="100.100.20.31" if collective else "100.100.20.30",
        master_port=29500,
    )
    if phase is not None:
        payload.update(
            phase=phase,
            start_deadline="2026-09-07T12:05:00+00:00",
            run_generation=1,
        )
    return payload


def _security_mount_plan(source: str) -> dict[str, Any]:
    plan = copy.deepcopy(PLAN)
    plan["security"]["mounts"] = [
        {
            "source": source,
            "target": {"model": "/models", "inputs": "/inputs", "outputs": "/outputs"}[
                source
            ],
            "read_only": source != "outputs",
        }
    ]
    return plan


def _published_plan() -> dict[str, Any]:
    plan = copy.deepcopy(PLAN)
    image = plan["runtime_image"]
    image["source"] = "published"
    image["build_id"] = None
    image["registry_manifest_digest"] = "sha256:" + "6" * 64
    return plan


def _bridge_plan() -> dict[str, Any]:
    plan = copy.deepcopy(PLAN)
    plan["runtime"]["placement"]["endpoint_address"] = "100.100.20.30"
    plan["security"]["network_mode"] = "bridge"
    return plan


def _job_plan() -> dict[str, Any]:
    plan = copy.deepcopy(PLAN)
    plan["endpoint"] = None
    plan["job"] = {
        "interface": "artifact-job",
        "input": {"path": "/inputs", "declared_content": {"vendor": "free-form"}},
        "output_path": "/outputs",
        "timeout_seconds": 60,
    }
    return plan


def _ltx_plan() -> dict[str, Any]:
    plan = copy.deepcopy(PLAN)
    original = copy.deepcopy(plan["artifacts"][0])
    duplicate = copy.deepcopy(original)
    duplicate["mount"]["target"] = "/models/secondary"
    plan["artifacts"].append(duplicate)
    return plan


def _claim(operation: AgentOperation, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "attempt": 1,
        "deadline": "2026-09-07T12:05:00+00:00",
        "fence": "00000000-0000-4000-8000-000000000011",
        "job_id": "00000000-0000-4000-8000-000000000012",
        "operation": operation.value,
        "operation_id": "00000000-0000-4000-8000-000000000013",
        "node_id": "spk_11111111111111111111111111111111",
        "authority_revision": "a" * 64,
        "payload_digest": hashlib.sha256(canonical_message(payload)).hexdigest(),
        "payload": payload,
    }


@pytest.fixture(scope="session")
def wire_probe() -> Path:
    configured = os.environ.get("VONK_INSTALL_START_WIRE_PROBE")
    repository = Path(__file__).resolve().parents[2]
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = repository / path
        path = path.resolve()
    else:
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--package",
                "vonk-agent",
                "--example",
                "install_start_wire_probe",
            ],
            cwd=repository,
            check=True,
        )
        target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
        if not target_root.is_absolute():
            target_root = repository / target_root
        path = target_root / "debug" / "examples" / "install_start_wire_probe"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AssertionError(f"wire probe is not executable: {path}")
    return path


def _rust_accepts(
    probe: Path, operation: AgentOperation, payload: dict[str, Any]
) -> bool:
    completed = subprocess.run(
        [str(probe)],
        input=json.dumps(_claim(operation, payload), separators=(",", ":")) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _schema_for_value(
    root: dict[str, Any], schema: dict[str, Any], value: Any
) -> dict[str, Any]:
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return _schema_for_value(root, target, value)
    for candidate in schema.get("anyOf", []):
        candidate_type = candidate.get("type")
        matches = (
            (value is None and candidate_type == "null")
            or (isinstance(value, str) and candidate_type == "string")
            or (isinstance(value, bool) and candidate_type == "boolean")
            or (
                isinstance(value, int)
                and not isinstance(value, bool)
                and candidate_type == "integer"
            )
            or (isinstance(value, list) and candidate_type == "array")
            or (isinstance(value, dict) and candidate_type == "object")
        )
        if matches or (isinstance(value, dict) and "$ref" in candidate):
            return _schema_for_value(root, candidate, value)
    return schema


def _object_paths(
    root: dict[str, Any],
    schema: dict[str, Any],
    value: Any,
    path: tuple[str | int, ...] = (),
) -> Iterator[tuple[tuple[str | int, ...], dict[str, Any]]]:
    schema = _schema_for_value(root, schema, value)
    if not isinstance(value, (dict, list)):
        return
    if isinstance(value, dict) and schema.get("type") == "object":
        if schema.get("additionalProperties") is False:
            yield path, schema
        for name, child in schema.get("properties", {}).items():
            if name in value:
                yield from _object_paths(root, child, value[name], (*path, name))
    elif isinstance(value, list) and value:
        yield from _object_paths(root, schema.get("items", {}), value[0], (*path, 0))


def _required_paths(
    root: dict[str, Any],
    schema: dict[str, Any],
    value: Any,
    path: tuple[str | int, ...] = (),
) -> Iterator[tuple[str | int, ...]]:
    schema = _schema_for_value(root, schema, value)
    if not isinstance(value, (dict, list)):
        return
    if isinstance(value, dict) and schema.get("type") == "object":
        for name in schema.get("required", []):
            if name in value:
                yield (*path, name)
        for name, child in schema.get("properties", {}).items():
            if name in value:
                yield from _required_paths(root, child, value[name], (*path, name))
    elif isinstance(value, list) and value:
        yield from _required_paths(root, schema.get("items", {}), value[0], (*path, 0))


def _scalar_array_item_paths(
    root: dict[str, Any],
    schema: dict[str, Any],
    value: Any,
    path: tuple[str | int, ...] = (),
) -> Iterator[tuple[tuple[str | int, ...], dict[str, Any]]]:
    schema = _schema_for_value(root, schema, value)
    if isinstance(value, dict) and schema.get("type") == "object":
        for name, child in schema.get("properties", {}).items():
            if name in value:
                yield from _scalar_array_item_paths(
                    root, child, value[name], (*path, name)
                )
    elif isinstance(value, list) and value:
        item_schema = _schema_for_value(root, schema.get("items", {}), value[0])
        if item_schema.get("type") in {"string", "integer", "boolean", "number"}:
            yield (*path, 0), item_schema
        else:
            yield from _scalar_array_item_paths(root, item_schema, value[0], (*path, 0))


def _at(document: dict[str, Any], path: tuple[str | int, ...]) -> Any:
    value: Any = document
    for part in path:
        value = value[part]
    return value


def _set(document: dict[str, Any], path: tuple[str | int, ...], value: Any) -> None:
    _at(document, path[:-1])[path[-1]] = value


def _with_unknown(
    document: dict[str, Any], path: tuple[str | int, ...]
) -> dict[str, Any]:
    result = copy.deepcopy(document)
    target = _at(result, path)
    assert isinstance(target, dict)
    target["__structural_probe_unknown__"] = True
    return result


def _without(document: dict[str, Any], path: tuple[str | int, ...]) -> dict[str, Any]:
    result = copy.deepcopy(document)
    parent = _at(result, path[:-1])
    del parent[path[-1]]
    return result


def _enum_variant(
    document: dict[str, Any],
    path: tuple[str | int, ...],
    name: str,
    candidate: Any,
) -> dict[str, Any]:
    result = copy.deepcopy(document)
    _at(result, path)[name] = candidate
    # The phase vocabulary has one cross-field ownership rule in the Rust
    # validator. Keep the mutation structurally positive before comparing it.
    if name == "phase" and candidate == "collective-readiness":
        local = _at(result, ("local_address",))
        _set(result, ("master_address",), local)
        _set(
            result,
            ("compiled_execution_plan", "runtime", "placement", "master_address"),
            local,
        )
    return result


def _plans_and_models() -> Iterator[
    tuple[AgentOperation, type[BaseModel], dict[str, Any]]
]:
    yield AgentOperation.RECIPE_INSTALL, RecipeInstallPayload, _install(PLAN)
    yield AgentOperation.RECIPE_INSTALL, RecipeInstallPayload, _install(_job_plan())
    yield AgentOperation.RECIPE_INSTALL, RecipeInstallPayload, _install(_ltx_plan())
    yield AgentOperation.RECIPE_START, RecipeStartPayload, _start()
    yield AgentOperation.RECIPE_START, RecipeStartPayload, _distributed_start()
    yield (
        AgentOperation.RECIPE_START,
        RecipeStartPayload,
        _distributed_start(phase="rank-launch"),
    )
    yield (
        AgentOperation.RECIPE_START,
        RecipeStartPayload,
        _distributed_start(phase="collective-readiness", collective=True),
    )


def _positive_enum_cases() -> Iterator[
    tuple[AgentOperation, type[BaseModel], dict[str, Any]]
]:
    yield from _plans_and_models()
    for mode in CompiledTopology.model_json_schema()["properties"]["mode"]["enum"]:
        plan = copy.deepcopy(PLAN)
        plan["topology"]["mode"] = mode
        for backend in ("local", "tcp", "ucx", "future-engine-Δ"):
            plan["topology"]["backend"] = backend
            yield AgentOperation.RECIPE_INSTALL, RecipeInstallPayload, _install(plan)
    yield (
        AgentOperation.RECIPE_INSTALL,
        RecipeInstallPayload,
        _install(_published_plan()),
    )
    yield AgentOperation.RECIPE_INSTALL, RecipeInstallPayload, _install(_bridge_plan())
    for source in CompiledSecurityMount.model_json_schema()["properties"]["source"][
        "enum"
    ]:
        yield (
            AgentOperation.RECIPE_INSTALL,
            RecipeInstallPayload,
            _install(_security_mount_plan(source)),
        )
    for interface in CompiledJob.model_json_schema()["properties"]["interface"]["enum"]:
        plan = _job_plan()
        plan["job"]["interface"] = interface
        yield AgentOperation.RECIPE_INSTALL, RecipeInstallPayload, _install(plan)


def test_canonical_pydantic_schema_is_exercised_by_rust_serde(
    wire_probe: Path,
) -> None:
    checked_paths: set[tuple[AgentOperation, tuple[str | int, ...]]] = set()
    for operation, model, payload in _plans_and_models():
        schema = model.model_json_schema()
        model.model_validate(payload)
        assert _rust_accepts(wire_probe, operation, payload)
        for path, object_schema in _object_paths(schema, schema, payload):
            unknown = _with_unknown(payload, path)
            with pytest.raises(ValueError):
                model.model_validate(unknown)
            assert not _rust_accepts(wire_probe, operation, unknown), path
            checked_paths.add((operation, path))
            assert object_schema.get("additionalProperties") is False
    assert (
        AgentOperation.RECIPE_INSTALL,
        ("compiled_execution_plan", "runtime", "placement"),
    ) in checked_paths
    assert (
        AgentOperation.RECIPE_INSTALL,
        ("compiled_execution_plan", "runtime_image", "distribution_object"),
    ) in checked_paths
    assert (
        AgentOperation.RECIPE_INSTALL,
        ("compiled_execution_plan", "job"),
    ) in checked_paths
    assert (
        AgentOperation.RECIPE_START,
        ("compiled_execution_plan", "endpoint"),
    ) in checked_paths


def test_required_and_exact_type_fields_are_rejected_by_both_models(
    wire_probe: Path,
) -> None:
    checked_array_items: set[tuple[str | int, ...]] = set()
    for operation, model, payload in _plans_and_models():
        schema = model.model_json_schema()
        model.model_validate(payload)
        assert _rust_accepts(wire_probe, operation, payload)
        for path in _required_paths(schema, schema, payload):
            missing = _without(payload, path)
            with pytest.raises(ValueError):
                model.model_validate(missing)
            assert not _rust_accepts(wire_probe, operation, missing), path

        for path, object_schema in _object_paths(schema, schema, payload):
            for name, field_schema in object_schema.get("properties", {}).items():
                if name not in _at(payload, path):
                    continue
                resolved = _schema_for_value(
                    schema, field_schema, _at(payload, (*path, name))
                )
                if resolved.get("type") == "null":
                    resolved = next(
                        candidate
                        for candidate in field_schema.get("anyOf", [])
                        if candidate.get("type") != "null"
                    )
                if resolved.get("type") == "string":
                    invalid = 1
                elif resolved.get("type") == "integer":
                    invalid = "1"
                elif resolved.get("type") == "boolean":
                    invalid = 1
                elif resolved.get("type") == "array":
                    invalid = {}
                elif (
                    resolved.get("type") == "object"
                    and resolved.get("additionalProperties") is not True
                ):
                    invalid = []
                else:
                    continue
                changed = copy.deepcopy(payload)
                _at(changed, path)[name] = invalid
                with pytest.raises(ValueError):
                    model.model_validate(changed)
                assert not _rust_accepts(wire_probe, operation, changed), (*path, name)

        for path, item_schema in _scalar_array_item_paths(schema, schema, payload):
            if item_schema.get("type") == "string":
                invalid = 1
            elif item_schema.get("type") == "integer":
                invalid = "1"
            elif item_schema.get("type") == "boolean":
                invalid = 1
            else:
                continue
            changed = copy.deepcopy(payload)
            _at(changed, path[:-1])[path[-1]] = invalid
            with pytest.raises(ValueError):
                model.model_validate(changed)
            assert not _rust_accepts(wire_probe, operation, changed), path
            checked_array_items.add(path)
    assert ("compiled_execution_plan", "runtime", "argv", 0) in checked_array_items
    assert ("compiled_execution_plan", "security", "devices", 0) in checked_array_items
    assert (
        "compiled_execution_plan",
        "artifacts",
        0,
        "roles",
        0,
    ) in checked_array_items


def test_enum_and_const_vocabulary_is_rejected_by_both_models(
    wire_probe: Path,
) -> None:
    for operation, model, payload in _plans_and_models():
        schema = model.model_json_schema()
        model.model_validate(payload)
        assert _rust_accepts(wire_probe, operation, payload)
        for path, object_schema in _object_paths(schema, schema, payload):
            for name, field_schema in object_schema.get("properties", {}).items():
                if name not in _at(payload, path):
                    continue
                resolved = _schema_for_value(
                    schema, field_schema, _at(payload, (*path, name))
                )
                if "enum" not in resolved and "const" not in resolved:
                    continue
                changed = copy.deepcopy(payload)
                changed_value: Any = "__structural_probe_unknown__"
                _at(changed, path)[name] = changed_value
                with pytest.raises(ValueError):
                    model.model_validate(changed)
                assert not _rust_accepts(wire_probe, operation, changed), (*path, name)


def test_every_canonical_enum_value_is_accepted_by_both_models(
    wire_probe: Path,
) -> None:
    checked: set[tuple[str, tuple[str | int, ...], str]] = set()
    topology_modes: set[str] = set()
    topology_backends: set[str] = set()
    for operation, model, payload in _positive_enum_cases():
        schema = model.model_json_schema()
        for path, object_schema in _object_paths(schema, schema, payload):
            for name, field_schema in object_schema.get("properties", {}).items():
                if name not in _at(payload, path):
                    continue
                resolved = _schema_for_value(
                    schema, field_schema, _at(payload, (*path, name))
                )
                if name == "backend":
                    topology_backends.add(str(_at(payload, (*path, name))))
                candidates = resolved.get("enum")
                if candidates is None and "const" in resolved:
                    candidates = [resolved["const"]]
                if not candidates:
                    continue
                for candidate in candidates:
                    changed = _enum_variant(payload, path, name, candidate)
                    try:
                        model.model_validate(changed)
                    except ValueError:
                        continue
                    assert _rust_accepts(wire_probe, operation, changed), (
                        *path,
                        name,
                        candidate,
                    )
                    checked.add((operation.value, (*path, name), str(candidate)))
                    if name == "mode":
                        topology_modes.add(str(candidate))
    assert topology_modes == set(
        CompiledTopology.model_json_schema()["properties"]["mode"]["enum"]
    )
    assert topology_backends == {"local", "tcp", "ucx", "future-engine-Δ"}
    assert any(path[-1] == "source" for _, path, _ in checked)
    assert any(path[-1] == "interface" for _, path, _ in checked)


def test_required_nullable_fields_cannot_be_omitted_on_either_wire(
    wire_probe: Path,
) -> None:
    for field in ("local_address", "master_address", "master_port"):
        payload = _start()
        del payload[field]
        with pytest.raises(ValueError):
            RecipeStartPayload.model_validate(payload)
        assert not _rust_accepts(wire_probe, AgentOperation.RECIPE_START, payload)

    for field in ("endpoint_address", "local_address", "master_address", "master_port"):
        payload = _install(PLAN)
        del payload["compiled_execution_plan"]["runtime"]["placement"][field]
        with pytest.raises(ValueError):
            RecipeInstallPayload.model_validate(payload)
        assert not _rust_accepts(wire_probe, AgentOperation.RECIPE_INSTALL, payload)


@pytest.mark.parametrize(
    ("source", "build_id", "registry_manifest_digest"),
    [
        ("controller-build", "00000000-0000-4000-8000-000000000008", None),
        ("published", None, "sha256:" + "6" * 64),
    ],
)
def test_runtime_image_build_reference_variants_round_trip(
    wire_probe: Path,
    source: str,
    build_id: str | None,
    registry_manifest_digest: str | None,
) -> None:
    plan = copy.deepcopy(PLAN)
    image = plan["runtime_image"]
    image["source"] = source
    image["build_id"] = build_id
    image["registry_manifest_digest"] = registry_manifest_digest
    payload = _install(plan)
    RecipeOperationRequest.parse(AgentOperation.RECIPE_INSTALL, payload)
    assert _rust_accepts(wire_probe, AgentOperation.RECIPE_INSTALL, payload)
