"""Deterministic lifecycle conformance for canonical compiled execution plans."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files

from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .compiled_execution_plan import (
    CompiledExecutionPlan,
    CompiledExecutionPlanError,
    CompiledRuntimeImage,
    compile_verified_execution_plan,
    execution_identity_sha256,
)
from .execution_plan_service import _placement, _PlacementTarget
from .harnesses.canonical import compile_canonical_harness
from .harnesses.common import HarnessCompileError
from .recipe_runtime_specs import RecipeRuntimeSpecError, compile_runtime_spec


class HarnessConformanceError(RuntimeError):
    """A canonical recipe could not satisfy lifecycle conformance."""


class LifecycleInterrupted(RuntimeError):
    """The deterministic executor reports one recoverable interruption."""


@dataclass(frozen=True, slots=True)
class LifecycleRequest:
    recipe: RecipeDefinition
    models: tuple[ModelDefinition, ...]
    runtime_spec: Mapping[str, object]
    plan: CompiledExecutionPlan
    placement: Mapping[str, object]
    launch_payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class LifecycleObservation:
    operation: str
    result: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class HarnessEvidence:
    observations: tuple[LifecycleObservation, ...]
    document: dict[str, object]

    @property
    def phases(self) -> tuple[str, ...]:
        return tuple(observation.operation for observation in self.observations)

    @property
    def offline_runtime(self) -> bool:
        return _observation(self.observations, "invoke").get("offline") is True

    @property
    def security(self) -> dict[str, object]:
        prepared = _observation(self.observations, "prepare")
        security = prepared.get("security")
        return dict(security) if isinstance(security, Mapping) else {}

    @property
    def interrupted_start_recovered(self) -> bool:
        return _interrupted_then_state(self.observations, "start", "running")

    @property
    def interrupted_stop_recovered(self) -> bool:
        return _interrupted_then_state(self.observations, "stop", "stopped")

    @property
    def stop_bounded(self) -> bool:
        return any(
            observation.operation == "stop"
            and observation.result.get("state") == "stopped"
            and observation.result.get("within_deadline") is True
            for observation in self.observations
        )

    @property
    def recovery_phases(self) -> tuple[str, ...]:
        phases: list[str] = []
        for operation, recovered_state in (("start", "running"), ("stop", "stopped")):
            interrupted = next(
                (
                    index
                    for index, observation in enumerate(self.observations)
                    if observation.operation == operation
                    and observation.result.get("interrupted") is True
                ),
                None,
            )
            if interrupted is None:
                continue
            phases.append(f"{operation}-interrupted")
            after = self.observations[interrupted + 1 :]
            if (
                len(after) >= 2
                and after[0].operation == after[1].operation == "inspect"
                and after[0].result == after[1].result
            ):
                phases.append("inspect-idempotent")
            if any(
                observation.operation == operation
                and observation.result.get("state") == recovered_state
                for observation in after
            ):
                phases.append(f"{operation}-recovered")
        return tuple(phases)


class DeterministicClock:
    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if type(seconds) not in (int, float) or seconds < 0:
            raise ValueError("synthetic clock advance is invalid")
        self._now += seconds


class _PlanLifecycleExecutor:
    """The synthetic executor consumes only canonical compiler output."""

    def __init__(self, request: LifecycleRequest, *, clock: DeterministicClock) -> None:
        self._request = request
        self._clock = clock
        self._state = "stopped"
        self._start_interrupted = False
        self._stop_interrupted = False

    def inspect(self) -> Mapping[str, object]:
        return {"state": self._state}

    def prepare(self) -> Mapping[str, object]:
        self._require("stopped")
        self._state = "prepared"
        return {"security": _runtime_security(self._request.runtime_spec, self._request.plan)}

    def verify(self) -> Mapping[str, object]:
        self._require("prepared")
        return {"state": self._state, "verified": True}

    def start(self) -> Mapping[str, object]:
        self._require("prepared")
        if not self._start_interrupted:
            self._start_interrupted = True
            raise LifecycleInterrupted("synthetic start interrupted")
        self._state = "running"
        return {"state": self._state}

    def ready(self) -> Mapping[str, object]:
        self._require("running")
        return {"ready": True, "state": self._state}

    def invoke(self) -> Mapping[str, object]:
        self._require("running")
        security = _mapping(self._request.runtime_spec.get("security"), "security")
        return {"offline": security.get("network_mode") == "none", "state": self._state}

    def stop(self, deadline: float) -> Mapping[str, object]:
        self._require("running")
        if not self._stop_interrupted:
            self._stop_interrupted = True
            raise LifecycleInterrupted("synthetic stop interrupted")
        self._clock.advance(1)
        if self._clock() > deadline:
            raise HarnessConformanceError("synthetic stop exceeded its deadline")
        self._state = "stopped"
        return {
            "state": self._state,
            "stopped_at": self._clock(),
            "within_deadline": self._clock() <= deadline,
        }

    def verify_stopped(self) -> Mapping[str, object]:
        self._require("stopped")
        plan = self._request.plan
        return {
            "evidence": {
                "schema_version": 2,
                "recipe_revision_sha256": content_sha256(self._request.recipe),
                "harness_sha256": plan.harness_sha256,
                "execution_sha256": plan.execution_sha256,
                "plan": plan.model_dump(mode="json"),
                "outcome": "passed",
                "artifacts": [{"name": "conformance.json", "sha256": "e" * 64}],
            }
        }

    def _require(self, state: str) -> None:
        if self._state != state:
            raise HarnessConformanceError("synthetic lifecycle state is invalid")


def run_synthetic_conformance(slug: str) -> HarnessEvidence:
    """Compile a canonical fixture and exercise its full recoverable lifecycle."""
    try:
        request = _fixture_request(slug)
    except (HarnessCompileError, RecipeRuntimeSpecError, CompiledExecutionPlanError, ValueError, TypeError) as error:
        raise HarnessConformanceError(str(error)) from error
    return _run_plan_conformance(request, clock=DeterministicClock())


def run_recipe_conformance(
    recipe: RecipeDefinition | Mapping[str, object],
    models: Sequence[ModelDefinition],
    *,
    package_handle: object = None,
    runtime_image: CompiledRuntimeImage | Mapping[str, object] | None = None,
    artifact_set_sha256: str = "a" * 64,
    role: str = "entrypoint",
    rank: int = 0,
) -> HarnessEvidence:
    """Run conformance for any current canonical recipe/model projection."""
    try:
        parsed = recipe if isinstance(recipe, RecipeDefinition) else RecipeDefinition.model_validate(recipe)
        parsed_models = tuple(
            item if isinstance(item, ModelDefinition) else ModelDefinition.model_validate(item)
            for item in models
        )
        request = _compile_request(
            parsed,
            parsed_models,
            package_handle=package_handle,
            runtime_image=runtime_image,
            artifact_set_sha256=artifact_set_sha256,
            role=role,
            rank=rank,
        )
    except (HarnessCompileError, RecipeRuntimeSpecError, CompiledExecutionPlanError, ValueError, TypeError) as error:
        raise HarnessConformanceError(str(error)) from error
    return _run_plan_conformance(request, clock=DeterministicClock())


def validate_terminal_evidence(document: Mapping[str, object], request: LifecycleRequest) -> dict[str, object]:
    """Validate strict schema-two terminal evidence and exact plan identity."""
    if not isinstance(document, dict):
        raise HarnessConformanceError("lifecycle evidence is invalid")
    expected_keys = {
        "schema_version", "recipe_revision_sha256", "harness_sha256",
        "execution_sha256", "plan", "outcome", "artifacts",
    }
    if set(document) != expected_keys or document.get("schema_version") != 2:
        raise HarnessConformanceError("lifecycle evidence is invalid")
    try:
        plan = CompiledExecutionPlan.model_validate(document["plan"])
    except Exception as error:
        raise HarnessConformanceError("lifecycle evidence plan is invalid") from error
    if plan.model_dump(mode="json") != request.plan.model_dump(mode="json"):
        raise HarnessConformanceError("lifecycle evidence plan identity is invalid")
    expected = {
        "recipe_revision_sha256": content_sha256(request.recipe),
        "harness_sha256": request.plan.harness_sha256,
        "execution_sha256": request.plan.execution_sha256,
    }
    if any(document.get(name) != value for name, value in expected.items()):
        raise HarnessConformanceError("lifecycle evidence identity is invalid")
    if document.get("outcome") != "passed" or not _valid_artifacts(document.get("artifacts")):
        raise HarnessConformanceError("lifecycle evidence outcome is invalid")
    return document


def _run_plan_conformance(request: LifecycleRequest, *, clock: DeterministicClock) -> HarnessEvidence:
    executor = _PlanLifecycleExecutor(request, clock=clock)
    observations: list[LifecycleObservation] = []
    _observe_state(observations, "inspect", executor.inspect(), "stopped")
    prepared = _observe(observations, "prepare", executor.prepare())
    _validated_security(prepared, request.runtime_spec, request.plan)
    verified = _observe(observations, "verify", executor.verify())
    if verified.get("verified") is not True:
        raise HarnessConformanceError("verify evidence is invalid")
    _recover_start(executor, observations)
    ready = _observe(observations, "ready", executor.ready())
    if ready.get("ready") is not True:
        raise HarnessConformanceError("ready evidence is invalid")
    invocation = _observe(observations, "invoke", executor.invoke())
    if invocation.get("offline") is not True:
        raise HarnessConformanceError("offline invocation evidence is invalid")
    _observe_state(observations, "inspect", executor.inspect(), "running")
    deadline = clock() + _runtime_timeout(request.runtime_spec)
    stopped = _recover_stop(executor, observations, deadline)
    if stopped.get("within_deadline") is not True or clock() > deadline:
        raise HarnessConformanceError("bounded stop deadline elapsed")
    terminal = _observe(observations, "verify-stopped", executor.verify_stopped())
    document = terminal.get("evidence")
    if not isinstance(document, Mapping):
        raise HarnessConformanceError("lifecycle evidence is invalid")
    return HarnessEvidence(tuple(observations), validate_terminal_evidence(document, request))


def _fixture_request(slug: str) -> LifecycleRequest:
    cases = _fixture_cases()
    if slug not in cases:
        raise HarnessConformanceError("unknown execution harness")
    recipe, model = _fixture_recipe(slug, cases[slug])
    return _compile_request(recipe, (model,))


def _compile_request(
    recipe: RecipeDefinition,
    models: tuple[ModelDefinition, ...],
    *,
    package_handle: object = None,
    runtime_image: CompiledRuntimeImage | Mapping[str, object] | None = None,
    artifact_set_sha256: str = "a" * 64,
    role: str = "entrypoint",
    rank: int = 0,
) -> LifecycleRequest:
    # Exercise both canonical seams explicitly; the runtime compiler calls the
    # harness compiler again to produce the transport envelope.
    compile_canonical_harness(recipe, models, package_handle, role=role, rank=rank, settings=None)
    runtime_spec = compile_runtime_spec(
        recipe, models=models, package_handle=package_handle, role=role, rank=rank
    )
    runtime_spec = _bind_runtime_artifacts(runtime_spec, models)
    plan = compile_verified_execution_plan(
        runtime_spec,
        model_artifact_set_sha256=artifact_set_sha256,
        model_objects=_verified_model_objects(recipe, models, role=role),
        runtime_image=runtime_image or _fixture_runtime_image(recipe),
    )
    node = _PlacementTarget(rank=rank, role=role)
    try:
        placement = dict(
            _placement(
                recipe,
                runtime_spec,
                node,
                recipe.topology.parallelism.world_size,
            )
        )
        # The production placement compiler owns ports and reserved memory.
        # Conformance supplies only deterministic addresses for distributed
        # lifecycle evidence; it never invents job endpoints or resources.
        if recipe.topology.parallelism.world_size > 1:
            placement.update(
                local_address=f"192.0.2.{rank + 2}",
                master_address="192.0.2.1",
            )
            if runtime_spec.get("endpoint") is not None:
                placement["endpoint_address"] = "192.0.2.1"
        launch_payload = plan.to_compiled_launch_payload(
            runtime_spec,
            placement=placement,
        )
    except Exception as error:
        raise CompiledExecutionPlanError(
            "canonical placement cannot be bound to the execution plan"
        ) from error
    return LifecycleRequest(recipe, models, runtime_spec, plan, placement, launch_payload)


def _bind_runtime_artifacts(runtime_spec: Mapping[str, object], models: Sequence[ModelDefinition]) -> dict[str, object]:
    by_identity: dict[tuple[str, str], Mapping[str, object]] = {}
    for model in models:
        for item in model.files:
            by_identity[(content_sha256(model), item.id)] = item.model_dump(mode="json")
    raw_artifacts = runtime_spec.get("artifacts")
    if not isinstance(raw_artifacts, Sequence) or isinstance(raw_artifacts, (str, bytes)):
        raise HarnessConformanceError("canonical runtime model artifacts are unavailable")
    bound: list[dict[str, object]] = []
    for raw in raw_artifacts:
        if not isinstance(raw, Mapping):
            raise HarnessConformanceError("canonical runtime model artifact is invalid")
        model = raw.get("model")
        model_digest = model.get("content_sha256") if isinstance(model, Mapping) else None
        file_id = raw.get("file_id")
        if not isinstance(model_digest, str) or not isinstance(file_id, str):
            raise HarnessConformanceError(
                "selected model file is absent from the canonical model manifest"
            )
        source = by_identity.get((model_digest, file_id))
        if source is None:
            raise HarnessConformanceError("selected model file is absent from the canonical model manifest")
        if raw.get("path") != source.get("path"):
            raise HarnessConformanceError("selected model file path does not match the canonical manifest")
        item = dict(raw)
        item["sha256"] = source.get("sha256")
        item["bytes"] = source.get("size_bytes")
        mount = raw.get("mount")
        if not isinstance(mount, Mapping):
            raise HarnessConformanceError("selected model file mount is invalid")
        item["mount"] = {
            "source": f"/run/vonk/models/{raw.get('selection_id')}",
            "target": mount.get("target"),
            "read_only": mount.get("read_only"),
        }
        bound.append(item)
    result = dict(runtime_spec)
    result["artifacts"] = bound
    identity = dict(_mapping(result.get("identity"), "runtime identity"))
    identity["execution_sha256"] = execution_identity_sha256(result)
    result["identity"] = identity
    return result


def _verified_model_objects(
    recipe: RecipeDefinition,
    models: Sequence[ModelDefinition],
    *,
    role: str,
) -> tuple[dict[str, object], ...]:
    by_identity = {(content_sha256(model), model.identity.publisher, model.identity.slug): model for model in models}
    objects: list[dict[str, object]] = []
    selected: set[tuple[str, str]] = set()
    for selection in recipe.models:
        model = by_identity.get((selection.model.content_sha256, selection.model.publisher, selection.model.slug))
        if model is None:
            raise HarnessConformanceError("selected canonical model is unavailable")
        for selector in selection.files:
            if role in selector.roles:
                selected.add((content_sha256(model), selector.file_id))
    for model in models:
        for item in model.files:
            if (content_sha256(model), item.id) not in selected:
                continue
            objects.append(
                {
                    "model_content_sha256": content_sha256(model),
                    "file_id": item.id,
                    "path": item.path,
                    "sha256": item.sha256,
                    "bytes": item.size_bytes,
                    "roles": list(item.roles),
                    "distribution_object": {
                        "name": item.path,
                        "sha256": item.sha256,
                        "bytes": item.size_bytes,
                        "kind": "model",
                    },
                }
            )
    return tuple(objects)


def _fixture_runtime_image(recipe: RecipeDefinition) -> dict[str, object]:
    digest = recipe.execution.image.digest if recipe.execution.mode == "image" else "d" * 64
    image_digest = f"sha256:{digest}"
    layout_digest = "f" * 64
    return {
        "image_digest": image_digest,
        "oci_layout_sha256": layout_digest,
        "image_bytes": 4096,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "registry_manifest_digest": image_digest,
        "platform_manifest_digest": image_digest,
        "local_image_config_id": "sha256:" + "c" * 64,
        "runtime_interface_label": "v1",
        "source": "published",
        "build_id": None,
        "distribution_object": {"name": "image.oci.tar", "sha256": layout_digest, "bytes": 4096, "kind": "oci-archive"},
    }


def _fixture_cases() -> dict[str, tuple[list[str], list[dict[str, object]], str]]:
    return {
        "vllm": (["/opt/vonk/bin/vllm", "serve", "/models"], [{"name": "max-model-len", "value": 32768}, {"name": "tensor-parallel-size", "value": 1}], "openai"),
        "sglang": (["/opt/vonk/bin/sglang-serve", "serve", "/models"], [{"name": "model-path", "value": "/models"}, {"name": "context-length", "value": 32768}, {"name": "tensor-parallel-size", "value": 1}], "openai"),
        "tensorrt-llm": (["/opt/vonk/bin/trtllm-serve", "serve", "/models"], [{"name": "backend", "value": "pytorch"}, {"name": "max-batch-size", "value": 8}, {"name": "max-num-tokens", "value": 4096}, {"name": "max-seq-len", "value": 32768}, {"name": "tp-size", "value": 1}, {"name": "pp-size", "value": 1}, {"name": "ep-size", "value": 1}], "openai"),
        "llama-cpp": (["/opt/vonk/bin/llama-server", "/models"], [{"name": "model", "value": "/models/model.gguf"}, {"name": "ctx-size", "value": 32768}, {"name": "n-gpu-layers", "value": 999}], "openai"),
        "ds4": (["/opt/vonk/bin/ds4-serve", "/models"], [{"name": "model", "value": "/models/target.gguf"}, {"name": "draft-model", "value": "/models/drafter.gguf"}, {"name": "ctx-size", "value": 32768}], "openai"),
        "diffusers": (["/opt/vonk/bin/diffusers-job"], [{"name": "pipeline", "value": "text-to-image"}, {"name": "output-mime", "value": "image/png"}], "image-job"),
        "comfyui": (["/opt/vonk/bin/comfyui-job"], [{"name": "workflow", "value": "/opt/vonk/source/workflows/image.json"}, {"name": "workflow-sha256", "value": "e" * 64}, {"name": "output-mime", "value": "image/png"}], "image-job"),
        "pytorch-pipeline": (["/opt/vonk/bin/pytorch-pipeline"], [{"name": "entrypoint", "value": "/opt/vonk/source/pipelines/run.py"}, {"name": "output-mime", "value": "model/gltf-binary"}], "mesh-job"),
    }


def _fixture_recipe(slug: str, case: tuple[list[str], list[dict[str, object]], str]) -> tuple[RecipeDefinition, ModelDefinition]:
    entrypoint, arguments, interface = case
    base_name = "recipe-image.json" if interface == "openai" else "recipe-job.json"
    raw_recipe = json.loads(files("vonk_forge_contracts").joinpath("examples", base_name).read_text(encoding="utf-8"))
    raw_model = json.loads(files("vonk_forge_contracts").joinpath("examples", "model-definition.json").read_text(encoding="utf-8"))
    model = ModelDefinition.model_validate(raw_model)
    raw_recipe["identity"]["slug"] = f"synthetic-{slug}"
    raw_recipe["runtime"]["engine"] = slug
    raw_recipe["runtime"]["entrypoint"] = entrypoint
    raw_recipe["runtime"]["arguments"] = copy.deepcopy(arguments)
    raw_recipe["models"][0]["model"]["content_sha256"] = content_sha256(model)
    if interface != "openai":
        raw_recipe["interfaces"][0]["adapter"] = interface
        raw_recipe["validation"]["serving"]["interface"] = interface
        raw_recipe["validation"]["serving"]["checks"][0]["kind"] = f"{interface}.output"
    return RecipeDefinition.model_validate(raw_recipe), model


def _runtime_security(runtime_spec: Mapping[str, object], plan: CompiledExecutionPlan) -> dict[str, object]:
    runtime = _mapping(runtime_spec.get("runtime"), "runtime")
    security = _mapping(runtime_spec.get("security"), "security")
    mounts = security.get("mounts")
    if not isinstance(mounts, Sequence) or isinstance(mounts, (str, bytes)):
        raise HarnessConformanceError("compiled security mounts are invalid")
    model_mounts = [item for item in mounts if isinstance(item, Mapping) and str(item.get("source", "")).startswith("/run/vonk/models")]
    input_mounts = [item for item in mounts if isinstance(item, Mapping) and item.get("source") == "/run/vonk/inputs"]
    output_mounts = [item for item in mounts if isinstance(item, Mapping) and item.get("source") == "/run/vonk/outputs"]
    if len(output_mounts) != 1:
        raise HarnessConformanceError("compiled output mount is invalid")
    return {
        "architecture": runtime.get("architecture"),
        "capabilities": security.get("capabilities"),
        "docker_socket": any(".sock" in str(item.get("source", "")).lower() or ".sock" in str(item.get("target", "")).lower() for item in mounts if isinstance(item, Mapping)),
        "image": runtime.get("image"),
        "model_mounts_read_only": all(item.get("read_only") is True for item in model_mounts),
        "input_mount_read_only": not input_mounts or all(item.get("read_only") is True for item in input_mounts),
        "mount_paths_isolated": _mount_paths_are_isolated(mounts),
        "network_mode": security.get("network_mode"),
        "no_new_privileges": security.get("no_new_privileges"),
        "numeric_non_root_uid": security.get("user") not in {"0", "0:0"},
        "plan_schema_version": plan.schema_version,
        "output_mount_writable": output_mounts[0].get("read_only") is False,
    }


def _validated_security(prepared: Mapping[str, object], runtime_spec: Mapping[str, object], plan: CompiledExecutionPlan) -> None:
    security = prepared.get("security")
    expected = _runtime_security(runtime_spec, plan)
    if not isinstance(security, Mapping) or dict(security) != expected:
        raise HarnessConformanceError("lifecycle security evidence is invalid")
    if expected["architecture"] != "linux/arm64" or expected["network_mode"] != "none":
        raise HarnessConformanceError("canonical runtime security is invalid")
    if expected["docker_socket"] is not False or expected["no_new_privileges"] is not True:
        raise HarnessConformanceError("canonical runtime security is invalid")
    if expected["model_mounts_read_only"] is not True or expected["output_mount_writable"] is not True:
        raise HarnessConformanceError("canonical runtime mounts are invalid")
    if expected["plan_schema_version"] != 2:
        raise HarnessConformanceError("compiled execution plan schema is invalid")


def _mount_paths_are_isolated(mounts: Sequence[object]) -> bool:
    paths = tuple(path for item in mounts if isinstance(item, Mapping) for path in (item.get("source"), item.get("target")))
    return all(isinstance(path, str) and path != "/" for path in paths) and len(paths) == len(set(paths))


def _runtime_timeout(runtime_spec: Mapping[str, object]) -> float:
    value = _mapping(runtime_spec.get("lifecycle"), "lifecycle").get("stop_timeout_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise HarnessConformanceError("runtime stop timeout is invalid")
    return float(value)


def _recover_start(executor: _PlanLifecycleExecutor, observations: list[LifecycleObservation]) -> None:
    try:
        _observe(observations, "start", executor.start())
    except LifecycleInterrupted:
        observations.append(LifecycleObservation("start", {"interrupted": True}))
        first = _observe(observations, "inspect", executor.inspect())
        second = _observe(observations, "inspect", executor.inspect())
        if first != second:
            raise HarnessConformanceError("inspect is not idempotent during start recovery")
        _observe_state(observations, "start", executor.start(), "running")
        return
    raise HarnessConformanceError("start interruption was not observed")


def _recover_stop(executor: _PlanLifecycleExecutor, observations: list[LifecycleObservation], deadline: float) -> Mapping[str, object]:
    try:
        _observe(observations, "stop", executor.stop(deadline))
    except LifecycleInterrupted:
        observations.append(LifecycleObservation("stop", {"interrupted": True}))
        first = _observe(observations, "inspect", executor.inspect())
        second = _observe(observations, "inspect", executor.inspect())
        if first != second:
            raise HarnessConformanceError("inspect is not idempotent during stop recovery")
        return _observe_state(observations, "stop", executor.stop(deadline), "stopped")
    raise HarnessConformanceError("stop interruption was not observed")


def _observe(observations: list[LifecycleObservation], operation: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HarnessConformanceError(f"{operation} result is invalid")
    result = dict(value)
    observations.append(LifecycleObservation(operation, result))
    return result


def _observe_state(observations: list[LifecycleObservation], operation: str, value: object, expected: str) -> Mapping[str, object]:
    result = _observe(observations, operation, value)
    if result.get("state") != expected:
        raise HarnessConformanceError(f"{operation} state is invalid")
    return result


def _observation(observations: tuple[LifecycleObservation, ...], operation: str) -> Mapping[str, object]:
    for observation in observations:
        if observation.operation == operation:
            return observation.result
    return {}


def _interrupted_then_state(observations: tuple[LifecycleObservation, ...], operation: str, state: str) -> bool:
    return any(item.operation == operation and item.result.get("interrupted") is True for item in observations) and any(item.operation == operation and item.result.get("state") == state for item in observations)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HarnessConformanceError(f"{label} is invalid")
    return value


def _valid_artifacts(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 1:
        return False
    item = value[0]
    return (
        isinstance(item, Mapping)
        and set(item) == {"name", "sha256"}
        and type(item.get("name")) is str
        and bool(item["name"])
        and type(item.get("sha256")) is str
        and len(item["sha256"]) == 64
        and all(char in "0123456789abcdef" for char in item["sha256"])
    )


__all__ = [
    "DeterministicClock",
    "HarnessConformanceError",
    "HarnessEvidence",
    "LifecycleInterrupted",
    "LifecycleObservation",
    "LifecycleRequest",
    "run_recipe_conformance",
    "run_synthetic_conformance",
    "validate_terminal_evidence",
]
