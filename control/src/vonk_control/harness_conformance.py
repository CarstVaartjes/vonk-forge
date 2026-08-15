"""Deterministic observed lifecycle conformance for compiled harness projections."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

from .catalog_contract import (
    canonical_catalog_document,
    catalog_content_sha256,
    parse_catalog_json,
)
from .harnesses import BUILTIN_HARNESS_SLUGS, HarnessProjection, HarnessRegistry
from .harnesses.common import HarnessCompileError
from .schema_resources import read_runtime_schema


class HarnessConformanceError(RuntimeError):
    pass


class LifecycleInterrupted(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LifecycleRequest:
    projection: HarnessProjection
    recipe: dict[str, object]
    execution_harness: dict[str, object]
    runtime_distribution: dict[str, object]


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
        return any(
            observation.operation == "start"
            and observation.result.get("interrupted") is True
            for observation in self.observations
        ) and any(
            observation.operation == "start"
            and observation.result.get("state") == "running"
            for observation in self.observations
        )

    @property
    def interrupted_stop_recovered(self) -> bool:
        return any(
            observation.operation == "stop"
            and observation.result.get("interrupted") is True
            for observation in self.observations
        ) and any(
            observation.operation == "stop"
            and observation.result.get("state") == "stopped"
            for observation in self.observations
        )

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
        if (
            not isinstance(seconds, (int, float))
            or isinstance(seconds, bool)
            or seconds < 0
        ):
            raise ValueError("synthetic clock advance is invalid")
        self._now += seconds


class _ProjectionLifecycleExecutor:
    """The only synthetic executor; it is built from the compiled projection."""

    def __init__(self, request: LifecycleRequest, *, clock: DeterministicClock) -> None:
        binding = request.projection.binding
        if binding is None:
            raise HarnessConformanceError("compiled projection lacks exact identities")
        self._request = request
        self._clock = clock
        self._state = "stopped"
        self._start_interrupted = False
        self._stop_interrupted = False

    def inspect(self) -> Mapping[str, object]:
        return {"state": self._state}

    def prepare(self) -> Mapping[str, object]:
        self._require("stopped")
        projection = self._request.projection
        self._state = "prepared"
        return {
            "security": {
                "architecture": projection.architecture,
                "capabilities": list(projection.capabilities),
                "docker_socket": _has_container_runtime_socket(projection),
                "image": projection.image,
                "model_mounts_read_only": all(
                    mount.read_only for mount in projection.model_mounts
                ),
                "mount_paths_isolated": _mount_paths_are_isolated(projection),
                "network_mode": projection.network_mode,
                "no_new_privileges": projection.no_new_privileges,
                "numeric_non_root_uid": projection.user not in {"0", "0:0"},
            }
        }

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
        return {
            "offline": self._request.projection.network_mode == "none",
            "state": self._state,
        }

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
        projection = self._request.projection
        binding = projection.binding
        assert binding is not None
        return {
            "evidence": {
                "schema_version": 1,
                "recipe": self._request.recipe,
                "execution_harness": self._request.execution_harness,
                "runtime_distribution": self._request.runtime_distribution,
                "projection": {
                    "image": projection.image,
                    "harness_contract_version": projection.contract_version,
                    "topology": {
                        "node_count": binding.topology_node_count,
                        "role": binding.role,
                        "rank": binding.rank,
                    },
                },
                "outcome": "passed",
                "artifacts": [{"name": "conformance.json", "sha256": "e" * 64}],
            }
        }

    def _require(self, state: str) -> None:
        if self._state != state:
            raise HarnessConformanceError("synthetic lifecycle state is invalid")


def run_synthetic_conformance(
    slug: str, *, registry: HarnessRegistry | None = None
) -> HarnessEvidence:
    """Compile first, then construct the lifecycle executor from that projection only."""
    if slug not in BUILTIN_HARNESS_SLUGS:
        raise HarnessConformanceError("unknown execution harness")
    harness, distribution = _documents(slug)
    recipe = _conformance_recipe(slug, harness, distribution)
    active_registry = registry or HarnessRegistry.with_builtins()
    try:
        projection = active_registry.compile(
            harness,
            recipe=recipe,
            distribution=distribution,
            patch=None,
            parameters={},
            topology=recipe["topology"],
            role="entrypoint",
            rank=0,
        )
    except HarnessCompileError as error:
        raise HarnessConformanceError(str(error)) from error
    request = LifecycleRequest(
        projection=projection,
        recipe={
            "publisher": "vonk-forge",
            "slug": "synthetic-harness",
            "content_sha256": "b" * 64,
        },
        execution_harness={
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": slug,
            "content_sha256": projection.binding.harness_content_sha256
            if projection.binding is not None
            else "0" * 64,
        },
        runtime_distribution={
            "kind": "runtime-distribution",
            "publisher": "vonk-forge",
            "slug": "synthetic-arm64",
            "content_sha256": projection.binding.distribution_content_sha256
            if projection.binding is not None
            else "0" * 64,
        },
    )
    return _run_projection_conformance(request, clock=DeterministicClock())


def validate_terminal_evidence(
    document: Mapping[str, object], request: LifecycleRequest
) -> dict[str, object]:
    """Validate schema and exact terminal identities for observed executor evidence."""
    if not isinstance(document, dict):
        raise HarnessConformanceError("lifecycle evidence is invalid")
    try:
        schema = json.loads(read_runtime_schema("harness-evidence-v1.schema.json"))
        Draft202012Validator(schema).validate(document)
    except Exception as error:
        raise HarnessConformanceError("lifecycle evidence is invalid") from error
    projection = request.projection
    binding = projection.binding
    if binding is None:
        raise HarnessConformanceError("compiled projection lacks exact identities")
    if (
        document.get("recipe") != request.recipe
        or document.get("execution_harness") != request.execution_harness
        or document.get("runtime_distribution") != request.runtime_distribution
    ):
        raise HarnessConformanceError("lifecycle evidence identities are invalid")
    evidence_projection = document.get("projection")
    if not isinstance(evidence_projection, Mapping) or evidence_projection != {
        "image": projection.image,
        "harness_contract_version": projection.contract_version,
        "topology": {
            "node_count": binding.topology_node_count,
            "role": binding.role,
            "rank": binding.rank,
        },
    }:
        raise HarnessConformanceError(
            "lifecycle evidence projection identity is invalid"
        )
    return document


def _run_projection_conformance(
    request: LifecycleRequest, *, clock: DeterministicClock
) -> HarnessEvidence:
    executor = _ProjectionLifecycleExecutor(request, clock=clock)
    observations: list[LifecycleObservation] = []
    _observe_state(observations, "inspect", executor.inspect(), "stopped")
    prepared = _observe(observations, "prepare", executor.prepare())
    _validated_security(prepared, request.projection)
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
    deadline = clock() + 30
    stopped = _recover_stop(executor, observations, deadline)
    if stopped.get("within_deadline") is not True or clock() > deadline:
        raise HarnessConformanceError("bounded stop deadline elapsed")
    terminal = _observe(observations, "verify-stopped", executor.verify_stopped())
    document = terminal.get("evidence")
    validated_document = validate_terminal_evidence(document, request)
    return HarnessEvidence(tuple(observations), validated_document)


def _observe(
    observations: list[LifecycleObservation], operation: str, value: object
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HarnessConformanceError(f"{operation} result is invalid")
    result = dict(value)
    observations.append(LifecycleObservation(operation, result))
    return result


def _observe_state(
    observations: list[LifecycleObservation],
    operation: str,
    value: object,
    expected: str,
) -> Mapping[str, object]:
    result = _observe(observations, operation, value)
    if result.get("state") != expected:
        raise HarnessConformanceError(f"{operation} state is invalid")
    return result


def _recover_start(
    executor: _ProjectionLifecycleExecutor, observations: list[LifecycleObservation]
) -> None:
    try:
        _observe(observations, "start", executor.start())
    except LifecycleInterrupted:
        observations.append(LifecycleObservation("start", {"interrupted": True}))
        first = _observe(observations, "inspect", executor.inspect())
        second = _observe(observations, "inspect", executor.inspect())
        if first != second:
            raise HarnessConformanceError(
                "inspect is not idempotent during start recovery"
            )
        _observe_state(observations, "start", executor.start(), "running")
        return
    raise HarnessConformanceError("start interruption was not observed")


def _recover_stop(
    executor: _ProjectionLifecycleExecutor,
    observations: list[LifecycleObservation],
    deadline: float,
) -> Mapping[str, object]:
    try:
        _observe(observations, "stop", executor.stop(deadline))
    except LifecycleInterrupted:
        observations.append(LifecycleObservation("stop", {"interrupted": True}))
        first = _observe(observations, "inspect", executor.inspect())
        second = _observe(observations, "inspect", executor.inspect())
        if first != second:
            raise HarnessConformanceError(
                "inspect is not idempotent during stop recovery"
            )
        return _observe_state(observations, "stop", executor.stop(deadline), "stopped")
    raise HarnessConformanceError("stop interruption was not observed")


def _observation(
    observations: tuple[LifecycleObservation, ...], operation: str
) -> Mapping[str, object]:
    for observation in observations:
        if observation.operation == operation:
            return observation.result
    return {}


def _validated_security(
    prepared: Mapping[str, object], projection: HarnessProjection
) -> None:
    security = prepared.get("security")
    expected = {
        "architecture": projection.architecture,
        "capabilities": list(projection.capabilities),
        "docker_socket": _has_container_runtime_socket(projection),
        "image": projection.image,
        "model_mounts_read_only": all(
            mount.read_only for mount in projection.model_mounts
        ),
        "mount_paths_isolated": _mount_paths_are_isolated(projection),
        "network_mode": projection.network_mode,
        "no_new_privileges": projection.no_new_privileges,
        "numeric_non_root_uid": projection.user not in {"0", "0:0"},
    }
    if not isinstance(security, Mapping) or dict(security) != expected:
        raise HarnessConformanceError("lifecycle security evidence is invalid")


def _has_container_runtime_socket(projection: HarnessProjection) -> bool:
    return any(
        name in path.lower()
        for mount in (*projection.model_mounts, projection.output_mount)
        for path in (mount.source, mount.target)
        for name in (
            "docker.sock",
            "podman.sock",
            "containerd.sock",
            "cri-dockerd.sock",
        )
    )


def _mount_paths_are_isolated(projection: HarnessProjection) -> bool:
    mounts = (*projection.model_mounts, projection.output_mount)
    paths = tuple(path for mount in mounts for path in (mount.source, mount.target))
    return len(paths) == len(set(paths)) and all(path != "/" for path in paths)


def _documents(slug: str) -> tuple[dict[str, object], dict[str, object]]:
    harness = _builtin_harness_document(slug)
    distribution = {
        "schema_version": 1,
        "kind": "runtime-distribution",
        "identity": {
            "publisher": "vonk-forge",
            "slug": f"{slug}-conformance-arm64",
        },
        "metadata": {
            "title": "Synthetic ARM64 runtime",
            "description": "Offline digest-pinned runtime for conformance.",
            "tags": ["synthetic"],
        },
        "implements_harness": {
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": slug,
            "content_sha256": catalog_content_sha256(harness),
        },
        "platform": "linux/arm64",
        "image": "registry.example/vonk/synthetic@sha256:" + "f" * 64,
        "security": {
            "network_mode": "none",
            "user": "10001:10001",
            "no_new_privileges": True,
            "capabilities": [],
        },
        "sha256": "d" * 64,
    }
    return harness, distribution


def _builtin_harness_document(slug: str) -> dict[str, object]:
    packaged = files("vonk_control").joinpath("execution-harnesses", f"{slug}.json")
    if packaged.is_file():
        payload = packaged.read_bytes()
    else:
        root = Path(__file__).resolve().parents[3]
        payload = (root / "config/execution-harnesses" / f"{slug}.json").read_bytes()
    document = dict(parse_catalog_json(payload))
    if payload != canonical_catalog_document(document) + b"\n":
        raise HarnessConformanceError(
            "built-in harness catalog document is noncanonical"
        )
    return document


def _conformance_recipe(
    slug: str,
    harness: Mapping[str, object],
    distribution: Mapping[str, object],
) -> dict[str, object]:
    cases: dict[str, tuple[list[str], list[dict[str, object]], str]] = {
        "vllm": (
            ["/opt/vonk/bin/vllm", "serve", "/models"],
            [
                {"name": "max-model-len", "value": 32768},
                {"name": "tensor-parallel-size", "value": 1},
            ],
            "openai",
        ),
        "sglang": (
            ["/opt/vonk/bin/sglang-serve"],
            [
                {"name": "model-path", "value": "/models"},
                {"name": "context-length", "value": 32768},
                {"name": "tensor-parallel-size", "value": 1},
            ],
            "openai",
        ),
        "tensorrt-llm": (
            ["/usr/local/bin/trtllm-serve", "serve", "/models"],
            [
                {"name": "backend", "value": "pytorch"},
                {"name": "max-batch-size", "value": 8},
                {"name": "max-num-tokens", "value": 4096},
                {"name": "max-seq-len", "value": 32768},
                {"name": "tp-size", "value": 1},
                {"name": "pp-size", "value": 1},
                {"name": "ep-size", "value": 1},
            ],
            "openai",
        ),
        "llama-cpp": (
            ["/opt/vonk/bin/llama-server"],
            [
                {"name": "model", "value": "/models/model.gguf"},
                {"name": "ctx-size", "value": 32768},
                {"name": "n-gpu-layers", "value": 999},
            ],
            "openai",
        ),
        "ds4": (
            ["/opt/vonk/bin/ds4-serve"],
            [
                {"name": "model", "value": "/models/target.gguf"},
                {"name": "draft-model", "value": "/models/drafter.gguf"},
                {"name": "ctx-size", "value": 32768},
            ],
            "openai",
        ),
        "diffusers": (
            ["/opt/vonk/bin/diffusers-job"],
            [
                {"name": "pipeline", "value": "text-to-image"},
                {"name": "output-mime", "value": "image/png"},
            ],
            "image-job",
        ),
        "comfyui": (
            ["/opt/vonk/bin/comfyui-job"],
            [
                {
                    "name": "workflow",
                    "value": "/opt/vonk/source/workflows/image.json",
                },
                {"name": "workflow-sha256", "value": "e" * 64},
                {"name": "output-mime", "value": "image/png"},
            ],
            "image-job",
        ),
        "pytorch-pipeline": (
            ["/opt/vonk/bin/pytorch-pipeline"],
            [
                {
                    "name": "entrypoint",
                    "value": "/opt/vonk/source/pipelines/run.py",
                },
                {"name": "output-mime", "value": "model/gltf-binary"},
            ],
            "mesh-job",
        ),
    }
    entrypoint, arguments, interface = cases[slug]
    harness_identity = harness["identity"]
    distribution_identity = distribution["identity"]
    assert isinstance(harness_identity, Mapping)
    assert isinstance(distribution_identity, Mapping)
    job = interface != "openai"
    interfaces = (
        [{"adapter": interface, "path": "/outputs"}]
        if job
        else [
            {
                "adapter": "openai",
                "port": 8000,
                "model_aliases": ["synthetic"],
                "health_path": "/v1/models",
            }
        ]
    )
    checks = (
        [
            "artifact.mime."
            + str(
                next(
                    item["value"] for item in arguments if item["name"] == "output-mime"
                )
            ).replace("/", "-")
        ]
        if job
        else ["endpoint.healthy"]
    )
    topology = {
        "mode": "single",
        "node_count": 1,
        "parallelism": {
            "world_size": 1,
            "tensor": 1,
            "pipeline": 1,
            "data": 1,
            "backend": "local",
        },
        "fabric": {"connectivity": "none", "minimum_bandwidth_mbps": 0},
        "roles": [{"name": "entrypoint", "count": 1}],
    }
    return {
        "execution": {
            "harness": {
                "kind": "execution-harness",
                "publisher": harness_identity["publisher"],
                "slug": harness_identity["slug"],
                "content_sha256": catalog_content_sha256(harness),
            },
            "patch_bundle": None,
        },
        "runtime": {
            "distribution": {
                "kind": "runtime-distribution",
                "publisher": distribution_identity["publisher"],
                "slug": distribution_identity["slug"],
                "content_sha256": catalog_content_sha256(distribution),
            },
            "entrypoint": entrypoint,
            "arguments": arguments,
            "environment": [],
            "security": {
                "devices": ["nvidia.com/gpu=all"],
                "capabilities": [],
                "host_network": False,
                "privileged": False,
                "user": "10001:10001",
                "mounts": [
                    {"source": "model", "target": "/models", "read_only": True},
                    {
                        "source": "outputs",
                        "target": "/outputs",
                        "read_only": False,
                    },
                ],
            },
        },
        "parameters": [],
        "build": {
            "context": {
                "sha256": "f" * 64,
                "expected_bytes": 2048,
                "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
            }
        },
        "artifacts": [{"mount": {"target": "/models", "read_only": True}}],
        "interfaces": interfaces,
        "validation": {"validators": [{"interface": interface, "checks": checks}]},
        "topology": topology,
    }
