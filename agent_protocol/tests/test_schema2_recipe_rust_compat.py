from __future__ import annotations

import copy
import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from vonk_agent_protocol import (
    AgentOperation,
    RecipeInstallPayload,
    RecipeStartPayload,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledJob,
    CompiledSecurityMount,
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
        "installation_id": INSTALLATION_ID,
        "plan_digest": "b" * 64,
        "expected_bytes": 1,
        "compiled_execution_plan": copy.deepcopy(plan),
    }


def _start(plan: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = copy.deepcopy(plan or PLAN)
    if plan["runtime"]["placement"]["world_size"] == 1:
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


def _distributed_start(
    *, phase: str | None = None, collective: bool = False
) -> dict[str, Any]:
    plan = copy.deepcopy(PLAN)
    plan["topology"].update(node_count=2)
    plan["runtime"]["placement"].update(
        world_size=2,
        rank=1,
        role="worker",
        endpoint_address=None,
        local_address="100.100.20.31",
        master_address="100.100.20.31" if collective else "100.100.20.30",
        master_port=29500,
    )
    plan["security"].update(network_mode="host")
    payload = _start(plan)
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
        }
    ]
    return plan


def _bridge_plan() -> dict[str, Any]:
    plan = copy.deepcopy(PLAN)
    plan["runtime"]["placement"]["endpoint_address"] = "100.100.20.30"
    plan["security"]["network_mode"] = "bridge"
    return plan


def _job_plan() -> dict[str, Any]:
    plan = copy.deepcopy(PLAN)
    plan["endpoint"] = None
    plan["runtime"]["placement"]["port"] = None
    plan["job"] = {
        "interface": "artifact-job",
        "input": {
            "required": True,
            "media_types": ["application/json"],
            "max_bytes": 1024,
            "slots": [
                {
                    "id": "document",
                    "label": "Document",
                    "description": "A JSON document to process",
                    "media_types": ["application/json"],
                    "extensions": [".json"],
                    "min_files": 1,
                    "max_files": 1,
                    "max_file_bytes": 1024,
                    "max_total_bytes": 1024,
                }
            ],
        },
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
        "deadline": "2026-09-07T12:05:00+00:00",
        "fence": "00000000-0000-4000-8000-000000000011",
        "operation": operation.value,
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


def _rust_accepts_many(
    probe: Path, cases: list[tuple[AgentOperation, dict[str, Any]]]
) -> list[bool]:
    """Ask one probe process about many claims and read one verdict per line.

    Each claim is still an independent trip through the production Rust parser;
    only the process boundary is shared.  Building the typed validators costs
    about 100ms once per process against about 4ms for every further claim, so
    the per-claim spawn in ``_rust_accepts`` dominates the whole wire tier.
    """

    if not cases:
        return []
    timings = os.environ.get("VONK_WIRE_PROBE_TIMINGS") == "1"
    completed = subprocess.run(
        [str(probe), "--verdicts", *(["--timings"] if timings else [])],
        input="".join(
            json.dumps(_claim(operation, payload), separators=(",", ":")) + "\n"
            for operation, payload in cases
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    if timings:
        print(completed.stderr, end="")
    verdicts = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(verdicts) != len(cases):
        raise AssertionError(
            f"wire probe answered {len(verdicts)} of {len(cases)} claims "
            f"(exit {completed.returncode}): {completed.stderr[-400:]}"
        )
    return [verdict == "1" for verdict in verdicts]


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
        placement = ("compiled_execution_plan", "runtime", "placement")
        local = _at(result, (*placement, "local_address"))
        _set(result, (*placement, "master_address"), local)
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
    # Every claim is answered by one probe process, as in the tests below.
    cases: list[tuple[AgentOperation, dict[str, Any]]] = []
    expectations: list[bool] = []
    labels: list[object] = []
    for operation, model, payload in _plans_and_models():
        schema = model.model_json_schema()
        model.model_validate(payload)
        cases.append((operation, payload))
        expectations.append(True)
        labels.append(operation.value)
        for path, object_schema in _object_paths(schema, schema, payload):
            unknown = _with_unknown(payload, path)
            with pytest.raises(ValueError):
                model.model_validate(unknown)
            cases.append((operation, unknown))
            expectations.append(False)
            labels.append(path)
            checked_paths.add((operation, path))
            assert object_schema.get("additionalProperties") is False
    verdicts = _rust_accepts_many(wire_probe, cases)
    assert [
        label
        for label, verdict, expected in zip(labels, verdicts, expectations, strict=True)
        if verdict is not expected
    ] == []
    assert (
        AgentOperation.RECIPE_INSTALL,
        ("compiled_execution_plan", "runtime", "placement"),
    ) in checked_paths
    assert (
        AgentOperation.RECIPE_INSTALL,
        ("compiled_execution_plan", "runtime_image"),
    ) in checked_paths
    assert (
        AgentOperation.RECIPE_INSTALL,
        ("compiled_execution_plan", "job"),
    ) in checked_paths
    assert (
        AgentOperation.RECIPE_START,
        ("compiled_execution_plan", "endpoint"),
    ) in checked_paths


# Slow by design: well over a thousand claims, each a trip through the
# debug-built production Rust parser (one probe process, about 4 ms a claim).
@pytest.mark.slow(30)
def test_required_and_exact_type_fields_are_rejected_by_both_models(
    wire_probe: Path,
) -> None:
    checked_array_items: set[tuple[str | int, ...]] = set()
    # Every question this test asks the real Rust parser is collected first and
    # answered by one probe process: the process boundary, not the claim, is
    # what costs about 100ms per call.
    cases: list[tuple[AgentOperation, dict[str, Any]]] = []
    expectations: list[bool] = []
    labels: list[tuple[object, ...]] = []
    for operation, model, payload in _plans_and_models():
        schema = model.model_json_schema()
        model.model_validate(payload)
        cases.append((operation, payload))
        expectations.append(True)
        labels.append((operation.value,))
        for path in _required_paths(schema, schema, payload):
            missing = _without(payload, path)
            with pytest.raises(ValueError):
                model.model_validate(missing)
            cases.append((operation, missing))
            expectations.append(False)
            labels.append((operation.value, *path))

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
                cases.append((operation, changed))
                expectations.append(False)
                labels.append((operation.value, *path, name))

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
            cases.append((operation, changed))
            expectations.append(False)
            labels.append((operation.value, *path))
            checked_array_items.add(path)
    for (label, expected), accepted in zip(
        zip(labels, expectations), _rust_accepts_many(wire_probe, cases)
    ):
        assert accepted == expected, (label, expected)
    assert ("compiled_execution_plan", "runtime", "argv", 0) in checked_array_items
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
    cases: list[tuple[AgentOperation, dict[str, Any]]] = []
    expectations: list[bool] = []
    labels: list[object] = []
    for operation, model, payload in _plans_and_models():
        schema = model.model_json_schema()
        model.model_validate(payload)
        cases.append((operation, payload))
        expectations.append(True)
        labels.append(operation.value)
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
                cases.append((operation, changed))
                expectations.append(False)
                labels.append((*path, name))
    verdicts = _rust_accepts_many(wire_probe, cases)
    assert [
        label
        for label, verdict, expected in zip(labels, verdicts, expectations, strict=True)
        if verdict is not expected
    ] == []


# Slow by design: well over a thousand claims, each a trip through the
# debug-built production Rust parser (one probe process, about 4 ms a claim).
@pytest.mark.slow(30)
def test_every_canonical_enum_value_is_accepted_by_both_models(
    wire_probe: Path,
) -> None:
    checked: set[tuple[str, tuple[str | int, ...], str]] = set()
    cases: list[tuple[AgentOperation, dict[str, Any]]] = []
    labels: list[tuple[str, tuple[str | int, ...], str]] = []
    for operation, model, payload in _positive_enum_cases():
        schema = model.model_json_schema()
        for path, object_schema in _object_paths(schema, schema, payload):
            for name, field_schema in object_schema.get("properties", {}).items():
                if name not in _at(payload, path):
                    continue
                resolved = _schema_for_value(
                    schema, field_schema, _at(payload, (*path, name))
                )
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
                    cases.append((operation, changed))
                    labels.append((operation.value, (*path, name), str(candidate)))
    for (operation_name, path, candidate), accepted in zip(
        labels, _rust_accepts_many(wire_probe, cases)
    ):
        assert accepted, (path, candidate)
        checked.add((operation_name, path, candidate))
    assert any(path[-1] == "source" for _, path, _ in checked)
    assert any(path[-1] == "interface" for _, path, _ in checked)


def test_required_nullable_fields_cannot_be_omitted_on_either_wire(
    wire_probe: Path,
) -> None:
    for field in (
        "endpoint_address",
        "local_address",
        "master_address",
        "master_port",
        "memory_floor_bytes",
    ):
        payload = _install(PLAN)
        del payload["compiled_execution_plan"]["runtime"]["placement"][field]
        with pytest.raises(ValueError):
            RecipeInstallPayload.model_validate(payload)
        assert not _rust_accepts(wire_probe, AgentOperation.RECIPE_INSTALL, payload)


CATALOG_LAUNCH = Path(__file__).parent / "fixtures" / "catalog-launch"


def _catalog_launch_plans() -> list[tuple[str, dict[str, Any]]]:
    plans = [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(CATALOG_LAUNCH.glob("*.json"))
    ]
    assert plans, "catalog launch fixtures must exercise the wire consumer"
    return plans


def _catalog_start(plan: dict[str, Any]) -> dict[str, Any] | None:
    """The start claim for a catalog plan, or None where the plan never starts.

    Jobs are installed and then invoked per run; they have no recipe.start.
    """
    plan = copy.deepcopy(plan)
    if plan["job"] is not None:
        return None
    placement = plan["runtime"]["placement"]
    if placement["world_size"] == 1:
        return _start(plan)
    # A rank of a distributed launch starts on the connected fabric.
    plan["security"]["network_mode"] = "host"
    placement.update(endpoint_address=None, master_port=29500)
    return _start(plan)


def test_catalog_launch_payloads_are_accepted_by_python_and_rust(
    wire_probe: Path,
) -> None:
    """Every payload the Controller compiles from the real catalog is readable.

    The fixtures are compiled by control/tests/test_catalog_launch_fixtures.py
    from verbatim release recipes; here the same documents cross the production
    Rust install/start parser and the Python wire models.
    """
    started = time.perf_counter()
    cases: list[tuple[AgentOperation, dict[str, Any]]] = []
    labels: list[str] = []
    models = {
        AgentOperation.RECIPE_INSTALL: RecipeInstallPayload,
        AgentOperation.RECIPE_START: RecipeStartPayload,
    }
    for name, plan in _catalog_launch_plans():
        cases.append((AgentOperation.RECIPE_INSTALL, _install(plan)))
        labels.append(f"install {name}")
        start = _catalog_start(plan)
        if start is not None:
            cases.append((AgentOperation.RECIPE_START, start))
            labels.append(f"start {name}")
    compiled = time.perf_counter()
    for (operation, payload), label in zip(cases, labels, strict=True):
        try:
            models[operation].model_validate(payload)
        except ValueError as error:
            raise AssertionError(f"{label}: {error}") from error
    validated = time.perf_counter()
    verdicts = _rust_accepts_many(wire_probe, cases)
    if os.environ.get("VONK_WIRE_PROBE_TIMINGS") == "1":
        print(
            f"wire_catalog_timing cases={len(cases)} "
            f"fixtures_s={compiled - started:.6f} "
            f"python_validation_s={validated - compiled:.6f} "
            f"native_batch_s={time.perf_counter() - validated:.6f}"
        )
    assert [
        label for label, verdict in zip(labels, verdicts, strict=True) if not verdict
    ] == []
    assert any(label.startswith("start") for label in labels)
