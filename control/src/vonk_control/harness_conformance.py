"""Deterministic synthetic lifecycle conformance for harness registrations."""

from __future__ import annotations

import json
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from .harnesses import HarnessRegistry
from .schema_resources import read_runtime_schema


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


class _SyntheticLifecycle:
    """A deterministic state model for lifecycle and recovery conformance."""

    def __init__(self) -> None:
        self.state = "stopped"
        self.phases: list[str] = []
        self.recovery_phases: list[str] = []

    def inspect(self) -> None:
        self.phases.append("inspect")

    def prepare(self) -> None:
        if self.state != "stopped":
            raise RuntimeError("synthetic lifecycle prepare state is invalid")
        self.phases.append("prepare")
        self.state = "prepared"

    def verify(self) -> None:
        if self.state != "prepared":
            raise RuntimeError("synthetic lifecycle verify state is invalid")
        self.phases.append("verify")

    def start(self) -> None:
        if self.state != "prepared":
            raise RuntimeError("synthetic lifecycle start state is invalid")
        self.phases.append("start")
        self.state = "running"

    def ready(self) -> None:
        if self.state != "running":
            raise RuntimeError("synthetic lifecycle readiness state is invalid")
        self.phases.append("ready")

    def invoke(self) -> None:
        if self.state != "running":
            raise RuntimeError("synthetic lifecycle invocation state is invalid")
        self.phases.append("invoke")

    def stop(self) -> None:
        if self.state != "running":
            raise RuntimeError("synthetic lifecycle stop state is invalid")
        self.phases.append("stop")
        self.state = "stopped"

    def verify_stopped(self) -> None:
        if self.state != "stopped":
            raise RuntimeError("synthetic lifecycle stop verification is invalid")
        self.phases.append("verify-stopped")

    def exercise_recovery(self) -> tuple[bool, bool, bool]:
        self.state = "starting"
        self.recovery_phases.append("start-interrupted")
        inspected = self.state
        if self.state != inspected:
            raise RuntimeError("synthetic inspect is not idempotent")
        self.recovery_phases.append("inspect-idempotent")
        self.state = "running"
        self.recovery_phases.append("start-recovered")
        start_recovered = self.state == "running"

        self.state = "stopping"
        self.recovery_phases.append("stop-interrupted")
        inspected = self.state
        if self.state != inspected:
            raise RuntimeError("synthetic inspect is not idempotent")
        self.recovery_phases.append("inspect-idempotent")
        self.state = "stopped"
        self.recovery_phases.append("stop-recovered")
        stop_recovered = self.state == "stopped"
        return start_recovered, stop_recovered, True


def run_synthetic_conformance(slug: str) -> HarnessEvidence:
    """Run a deterministic in-memory lifecycle without a real engine process."""
    registry = HarnessRegistry.with_builtins()
    harness = {
        "schema_version": 1,
        "kind": "execution-harness",
        "identity": {"publisher": "vonk-forge", "slug": slug},
        "runtime_interface": "vonk.runtime.v1",
        "source_bundle": {
            "sha256": "a" * 64,
            "expected_bytes": 2048,
            "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
        },
    }
    projection = registry.compile(
        harness,
        recipe={"runtime": {"entrypoint": ["synthetic-runner", "--offline"]}},
        distribution={"platform": "linux/arm64"},
        patch=None,
        parameters={},
        topology={"node_count": 1},
        role="entrypoint",
        rank=0,
    )
    lifecycle = _SyntheticLifecycle()
    lifecycle.inspect()
    lifecycle.prepare()
    lifecycle.verify()
    lifecycle.start()
    lifecycle.ready()
    lifecycle.invoke()
    lifecycle.inspect()
    lifecycle.stop()
    lifecycle.verify_stopped()
    start_recovered, stop_recovered, stop_bounded = lifecycle.exercise_recovery()
    security = {
        "architecture": projection.architecture,
        "capabilities": list(projection.capabilities),
        "docker_socket": projection.docker_socket,
        "model_mounts_read_only": all(
            mount.read_only for mount in projection.model_mounts
        ),
        "no_new_privileges": projection.no_new_privileges,
        "numeric_non_root_uid": projection.user != "0" and projection.user != "0:0",
        "outputs_isolated": projection.output_mount.isolated,
    }
    document: dict[str, object] = {
        "schema_version": 1,
        "recipe": {
            "publisher": "vonk-forge",
            "slug": "synthetic-harness",
            "content_sha256": "b" * 64,
        },
        "execution_harness": {
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": slug,
            "content_sha256": "c" * 64,
        },
        "runtime_distribution": {
            "kind": "runtime-distribution",
            "publisher": "vonk-forge",
            "slug": "synthetic-arm64",
            "content_sha256": "d" * 64,
        },
        "outcome": "passed",
        "artifacts": [{"name": "conformance.json", "sha256": "e" * 64}],
    }
    schema = json.loads(read_runtime_schema("harness-evidence-v1.schema.json"))
    Draft202012Validator(schema).validate(document)
    return HarnessEvidence(
        phases=tuple(lifecycle.phases),
        offline_runtime=projection.offline_runtime,
        security=security,
        interrupted_start_recovered=start_recovered,
        interrupted_stop_recovered=stop_recovered,
        stop_bounded=stop_bounded,
        recovery_phases=tuple(lifecycle.recovery_phases),
        document=document,
    )
