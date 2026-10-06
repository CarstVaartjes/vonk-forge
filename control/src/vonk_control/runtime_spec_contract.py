"""The compiled runtime spec: what one recipe role needs to launch, as a contract.

``compile_runtime_spec`` turns a canonical recipe role into this document and
every later step reads it: the model-cache and image services bind exact bytes
to it, ``compiled_execution_plan`` projects it into the plan an agent receives,
and the execution identity is the digest of its launch-affecting parts.  The
models are closed (unknown fields refuse), so a retired authority cannot ride
along, and the two identity digests are built here from the typed values, in the
exact JSON shape their stored digests were computed over.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from vonk_forge_contracts.recipe import RecipeJobInput, RuntimeArgumentValue


class _SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpecModelIdentity(_SpecModel):
    publisher: str
    slug: str
    content_sha256: str


class SpecModelMount(_SpecModel):
    source: str
    target: str


class SpecArtifact(_SpecModel):
    """One selected model file; ``sha256`` and ``bytes`` are bound from the model."""

    id: str
    selection_id: str
    file_id: str
    path: str
    roles: list[str]
    mount: SpecModelMount
    model: SpecModelIdentity
    sha256: str | None = None
    bytes: int | None = None


class SpecModelDependency(_SpecModel):
    selection_id: str
    publisher: str
    slug: str
    content_sha256: str
    artifact_key: str | None = None


class SpecIdentity(_SpecModel):
    recipe_revision_sha256: str
    model_dependencies: list[SpecModelDependency] = Field(default_factory=list)
    harness_sha256: str
    execution_sha256: str | None = None
    build_input_sha256: str | None = None


class SpecTelemetry(_SpecModel):
    engine: str
    engine_version: str | None = None
    metrics_format: str | None = None
    metrics_path: str | None = None


class SpecArgument(_SpecModel):
    name: str
    value: RuntimeArgumentValue | None


class SpecEnvironmentEntry(_SpecModel):
    name: str
    value: str


class SpecWritablePath(_SpecModel):
    name: str
    path: str
    persistent: bool


class SpecPlacementEnvironment(_SpecModel):
    local_address: Literal["VONK_LOCAL_ADDR"] = "VONK_LOCAL_ADDR"
    master_address: Literal["VONK_MASTER_ADDR"] = "VONK_MASTER_ADDR"
    master_port: Literal["VONK_MASTER_PORT"] = "VONK_MASTER_PORT"


class SpecRuntime(_SpecModel):
    interface: str
    adapter: str
    adapter_version: int
    telemetry: SpecTelemetry
    image: str
    architecture: str
    entrypoint: list[str]
    arguments: list[SpecArgument]
    environment: list[SpecEnvironmentEntry]
    writable_paths: list[SpecWritablePath]
    placement_environment: SpecPlacementEnvironment | None = None

    def document(self) -> dict[str, object]:
        """The runtime as JSON; a single-node runtime has no placement environment."""

        return self.model_dump(
            mode="json",
            exclude=None
            if self.placement_environment is not None
            else {"placement_environment"},
        )


class SpecSecurity(_SpecModel):
    gpu: bool
    user: str
    network_mode: str
    mounts: list[SpecModelMount]


class SpecLifecycle(_SpecModel):
    stop_timeout_seconds: int


class SpecTopology(_SpecModel):
    name: str
    node_count: int
    rank: int
    role: str


class SpecEndpoint(_SpecModel):
    port: int
    model_aliases: list[str]
    health_path: str


class SpecJob(_SpecModel):
    interface: str
    input: RecipeJobInput | None
    timeout_seconds: int


class RuntimeSpec(_SpecModel):
    """One recipe role, compiled: identity, runtime, mounts, lifecycle, interface."""

    identity: SpecIdentity
    model_dependencies: list[SpecModelDependency]
    runtime: SpecRuntime
    artifacts: list[SpecArtifact]
    security: SpecSecurity
    lifecycle: SpecLifecycle
    topology: SpecTopology
    endpoint: SpecEndpoint | None = None
    job: SpecJob | None = None

    def document(self) -> dict[str, object]:
        """The spec as JSON: one interface key, bound file facts only once bound."""

        document = self.model_dump(mode="json")
        document["runtime"] = self.runtime.document()
        document["artifacts"] = [
            item.model_dump(
                mode="json",
                exclude=None if item.sha256 is not None else {"sha256", "bytes"},
            )
            for item in self.artifacts
        ]
        for name in ("endpoint", "job"):
            if document[name] is None:
                del document[name]
        return document

    def compile_identity_sha256(self) -> str:
        """The execution digest the compiler records beside the unbound artifacts."""

        interface = self.endpoint if self.endpoint is not None else self.job
        return _digest(
            {
                "harness_sha256": self.identity.harness_sha256,
                "model_dependencies": [
                    item.model_dump(mode="json") for item in self.model_dependencies
                ],
                "artifacts": [
                    item.model_dump(mode="json", exclude={"sha256", "bytes"})
                    for item in self.artifacts
                ],
                "runtime": self.runtime.document(),
                "security": self.security.model_dump(mode="json"),
                "lifecycle": self.lifecycle.model_dump(mode="json"),
                "topology": self.topology.model_dump(mode="json"),
                "interface": None
                if interface is None
                else interface.model_dump(mode="json"),
            }
        )

    def launch_identity_sha256(self) -> str:
        """The canonical full launch identity, excluding editorial notes."""

        selected = sorted(
            (
                {
                    "selection_id": item.selection_id,
                    "file_id": item.file_id,
                    "path": item.path,
                    "sha256": item.sha256,
                    "bytes": item.bytes,
                    "roles": item.roles,
                    "mount": item.mount.model_dump(mode="json"),
                    "model": item.model.model_dump(mode="json"),
                }
                for item in self.artifacts
            ),
            key=lambda item: (
                str(item["selection_id"]),
                str(item["file_id"]),
                str(item["path"]),
            ),
        )
        dependencies = sorted(
            (
                {
                    "selection_id": item.selection_id,
                    "publisher": item.publisher,
                    "slug": item.slug,
                    "content_sha256": item.content_sha256,
                }
                for item in self.model_dependencies
            ),
            key=lambda item: item["selection_id"],
        )
        return _digest(
            {
                "harness_sha256": self.identity.harness_sha256,
                "runtime": self.runtime.document(),
                "security": self.security.model_dump(mode="json"),
                "lifecycle": self.lifecycle.model_dump(mode="json"),
                "topology": self.topology.model_dump(mode="json"),
                "endpoint": None
                if self.endpoint is None
                else self.endpoint.model_dump(mode="json"),
                "job": None if self.job is None else self.job.model_dump(mode="json"),
                "model_dependencies": dependencies,
                "artifacts": selected,
            }
        )


def _digest(document: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


__all__ = [
    "RuntimeSpec",
    "SpecArgument",
    "SpecArtifact",
    "SpecEndpoint",
    "SpecEnvironmentEntry",
    "SpecIdentity",
    "SpecJob",
    "SpecLifecycle",
    "SpecModelDependency",
    "SpecModelIdentity",
    "SpecModelMount",
    "SpecPlacementEnvironment",
    "SpecRuntime",
    "SpecSecurity",
    "SpecTelemetry",
    "SpecTopology",
    "SpecWritablePath",
]
