"""Deterministic observed lifecycle conformance for harness registrations."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from .harnesses import HarnessProjection, HarnessRegistry
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
class HarnessEvidence:
    phases: tuple[str, ...]
    offline_runtime: bool
    security: dict[str, object]
    interrupted_start_recovered: bool
    interrupted_stop_recovered: bool
    stop_bounded: bool
    recovery_phases: tuple[str, ...]
    document: dict[str, object]


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


class SyntheticLifecycleDriver:
    """A deterministic executor double whose results are the conformance evidence."""

    def __init__(self, request: LifecycleRequest, *, clock: DeterministicClock) -> None:
        self.request = request
        self.clock = clock
        self.calls: list[str] = []
        self._state = "stopped"
        self._start_interrupted = False
        self._stop_interrupted = False

    def inspect(self) -> Mapping[str, object]:
        self.calls.append("inspect")
        return {"state": self._state}

    def prepare(self) -> Mapping[str, object]:
        self.calls.append("prepare")
        self._require("stopped")
        self._state = "prepared"
        projection = self.request.projection
        return {
            "security": {
                "architecture": projection.architecture,
                "capabilities": list(projection.capabilities),
                "docker_socket": _has_container_runtime_socket(projection),
                "image": projection.image,
                "model_mounts_read_only": all(
                    mount.read_only for mount in projection.model_mounts
                ),
                "network_mode": projection.network_mode,
                "no_new_privileges": projection.no_new_privileges,
                "numeric_non_root_uid": projection.user != "0"
                and projection.user != "0:0",
                "outputs_isolated": projection.output_mount.isolated,
            }
        }

    def verify(self) -> Mapping[str, object]:
        self.calls.append("verify")
        self._require("prepared")
        return {"verified": True}

    def start(self) -> Mapping[str, object]:
        self.calls.append("start")
        self._require("prepared")
        if not self._start_interrupted:
            self._start_interrupted = True
            raise LifecycleInterrupted("synthetic start interrupted")
        self._state = "running"
        return {"state": self._state}

    def ready(self) -> Mapping[str, object]:
        self.calls.append("ready")
        self._require("running")
        return {"ready": True}

    def invoke(self) -> Mapping[str, object]:
        self.calls.append("invoke")
        self._require("running")
        return {"offline": self.request.projection.network_mode == "none"}

    def stop(self, deadline: float) -> Mapping[str, object]:
        self.calls.append("stop")
        self._require("running")
        if not self._stop_interrupted:
            self._stop_interrupted = True
            raise LifecycleInterrupted("synthetic stop interrupted")
        self.clock.advance(1)
        if self.clock() > deadline:
            raise HarnessConformanceError("synthetic stop exceeded its deadline")
        self._state = "stopped"
        return {"state": self._state, "stopped_at": self.clock()}

    def verify_stopped(self) -> Mapping[str, object]:
        self.calls.append("verify-stopped")
        self._require("stopped")
        return {
            "evidence": {
                "schema_version": 1,
                "recipe": self.request.recipe,
                "execution_harness": self.request.execution_harness,
                "runtime_distribution": self.request.runtime_distribution,
                "outcome": "passed",
                "artifacts": [{"name": "conformance.json", "sha256": "e" * 64}],
            }
        }

    def _require(self, state: str) -> None:
        if self._state != state:
            raise HarnessConformanceError("synthetic lifecycle state is invalid")


def run_synthetic_conformance(
    slug: str,
    *,
    driver_factory: Callable[
        [LifecycleRequest, DeterministicClock], SyntheticLifecycleDriver
    ]
    | None = None,
) -> HarnessEvidence:
    """Exercise a lifecycle driver and derive all conformance evidence from it."""
    harness, distribution = _documents(slug)
    try:
        projection = HarnessRegistry.with_builtins().compile(
            harness,
            recipe={"runtime": {"entrypoint": ["synthetic-runner", "--offline"]}},
            distribution=distribution,
            patch=None,
            parameters={},
            topology={"node_count": 1},
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
            "content_sha256": "c" * 64,
        },
        runtime_distribution={
            "kind": "runtime-distribution",
            "publisher": "vonk-forge",
            "slug": "synthetic-arm64",
            "content_sha256": "d" * 64,
        },
    )
    clock = DeterministicClock()
    driver = (
        driver_factory
        or (lambda value, current: SyntheticLifecycleDriver(value, clock=current))
    )(request, clock)
    _require_state(driver.inspect(), "stopped", "initial inspection")
    prepared = _require_mapping(driver.prepare(), "prepare")
    if _require_mapping(driver.verify(), "verify").get("verified") is not True:
        raise HarnessConformanceError("verify evidence is invalid")
    start_recovery = _recover_start(driver)
    if _require_mapping(driver.ready(), "ready").get("ready") is not True:
        raise HarnessConformanceError("ready evidence is invalid")
    invocation = _require_mapping(driver.invoke(), "invoke")
    if invocation.get("offline") is not True:
        raise HarnessConformanceError("offline invocation evidence is invalid")
    _require_state(driver.inspect(), "running", "running inspection")
    stop_deadline = clock() + 30
    stop_recovery = _recover_stop(driver, stop_deadline)
    if clock() > stop_deadline:
        raise HarnessConformanceError("bounded stop deadline elapsed")
    terminal = _require_mapping(driver.verify_stopped(), "verify-stopped")
    document = terminal.get("evidence")
    if not isinstance(document, dict):
        raise HarnessConformanceError("lifecycle evidence is invalid")
    try:
        schema = json.loads(read_runtime_schema("harness-evidence-v1.schema.json"))
        Draft202012Validator(schema).validate(document)
    except Exception as error:
        raise HarnessConformanceError("lifecycle evidence is invalid") from error
    security = _validated_security(prepared, projection)
    return HarnessEvidence(
        phases=tuple(driver.calls),
        offline_runtime=True,
        security=security,
        interrupted_start_recovered=start_recovery,
        interrupted_stop_recovered=stop_recovery,
        stop_bounded=True,
        recovery_phases=(
            "start-interrupted",
            "inspect-idempotent",
            "start-recovered",
            "stop-interrupted",
            "inspect-idempotent",
            "stop-recovered",
        ),
        document=document,
    )


def _recover_start(driver: SyntheticLifecycleDriver) -> bool:
    try:
        driver.start()
    except LifecycleInterrupted:
        first = _require_mapping(driver.inspect(), "start recovery inspection")
        second = _require_mapping(driver.inspect(), "start recovery inspection")
        if first != second:
            raise HarnessConformanceError(
                "inspect is not idempotent during start recovery"
            )
        _require_state(driver.start(), "running", "start recovery")
        return True
    raise HarnessConformanceError("start interruption was not observed")


def _recover_stop(driver: SyntheticLifecycleDriver, deadline: float) -> bool:
    try:
        driver.stop(deadline)
    except LifecycleInterrupted:
        first = _require_mapping(driver.inspect(), "stop recovery inspection")
        second = _require_mapping(driver.inspect(), "stop recovery inspection")
        if first != second:
            raise HarnessConformanceError(
                "inspect is not idempotent during stop recovery"
            )
        _require_state(driver.stop(deadline), "stopped", "stop recovery")
        return True
    raise HarnessConformanceError("stop interruption was not observed")


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HarnessConformanceError(f"{label} result is invalid")
    return value


def _require_state(value: object, expected: str, label: str) -> Mapping[str, object]:
    observed = _require_mapping(value, label)
    if observed.get("state") != expected:
        raise HarnessConformanceError(f"{label} state is invalid")
    return observed


def _validated_security(
    prepared: Mapping[str, object], projection: HarnessProjection
) -> dict[str, object]:
    security = prepared.get("security")
    if not isinstance(security, dict) or security != {
        "architecture": projection.architecture,
        "capabilities": list(projection.capabilities),
        "docker_socket": _has_container_runtime_socket(projection),
        "image": projection.image,
        "model_mounts_read_only": all(
            mount.read_only for mount in projection.model_mounts
        ),
        "network_mode": projection.network_mode,
        "no_new_privileges": projection.no_new_privileges,
        "numeric_non_root_uid": projection.user != "0" and projection.user != "0:0",
        "outputs_isolated": projection.output_mount.isolated,
    }:
        raise HarnessConformanceError("lifecycle security evidence is invalid")
    if security["docker_socket"] is not False:
        raise HarnessConformanceError("lifecycle security evidence exposes a socket")
    return security


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


def _documents(slug: str) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "schema_version": 1,
            "kind": "execution-harness",
            "identity": {"publisher": "vonk-forge", "slug": slug},
            "metadata": {
                "title": f"{slug} synthetic harness",
                "description": "Synthetic lifecycle harness for conformance.",
                "tags": ["synthetic"],
            },
            "runtime_interface": "vonk.runtime.v1",
            "adapters": ["openai"],
            "source_bundle": {
                "sha256": "a" * 64,
                "expected_bytes": 2048,
                "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
            },
        },
        {
            "schema_version": 1,
            "kind": "runtime-distribution",
            "identity": {"publisher": "vonk-forge", "slug": "synthetic-arm64"},
            "metadata": {
                "title": "Synthetic ARM64 runtime",
                "description": "Offline digest-pinned runtime for conformance.",
                "tags": ["synthetic"],
            },
            "implements_harness": {
                "kind": "execution-harness",
                "publisher": "vonk-forge",
                "slug": slug,
                "content_sha256": "c" * 64,
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
        },
    )
