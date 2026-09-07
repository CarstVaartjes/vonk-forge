"""Typed recipe build and image-import request/result wire contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .wire_model import WireModel

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
OciDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
UuidId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    ),
]
NodeId = Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]
# The Rust producer accepts JSON strings, booleans, and integral numbers for
# build arguments.  Keeping this union strict prevents Pydantic from turning
# JSON floats/nulls into an accepted build argument.
JsonScalar = str | int | bool


class RecipeBuildArgument(WireModel):
    name: str = Field(min_length=1, max_length=64)
    value: JsonScalar


class RecipeBuildBaseImage(WireModel):
    manifest_digest: OciDigest
    reference: str = Field(min_length=1, max_length=512)


class RecipeBuildNetwork(WireModel):
    hosts: list[str] = Field(max_length=64)
    mode: Literal["none", "public"]


class RecipeBuildAdditionalContext(WireModel):
    name: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=512)


class RecipeBuildMetadata(WireModel):
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=1024)


class RecipeBuildOptions(WireModel):
    additional_contexts: list[RecipeBuildAdditionalContext] = Field(max_length=16)
    annotations: list[RecipeBuildMetadata] = Field(max_length=64)
    environment: list[RecipeBuildArgument] = Field(max_length=64)
    format: Literal["oci", "docker"]
    identity_label: bool
    ignorefile: str | None = Field(default=None, max_length=512)
    jobs: int = Field(ge=1, le=32)
    labels: list[RecipeBuildMetadata] = Field(max_length=64)
    layer_compression: str = Field(min_length=1, max_length=64)
    layer_labels: list[RecipeBuildMetadata] = Field(max_length=64)
    layers: bool
    no_hostname: bool
    no_hosts: bool
    omit_history: bool
    os_features: list[str] = Field(max_length=32)
    os_version: str | None = Field(default=None, max_length=64)
    shm_bytes: int = Field(ge=65_536, le=16 * 1024**4)
    skip_unused_stages: bool
    squash: Literal["none", "new", "all"]
    timestamp: int | None = Field(default=None, ge=0)
    unset_environment: list[str] = Field(max_length=64)
    unset_labels: list[str] = Field(max_length=64)


class RecipeBuildLimits(WireModel):
    container_socket: bool
    cpu_cores: int = Field(ge=1, le=256)
    gpu: int = Field(ge=0, le=64)
    host_mounts: bool
    memory_bytes: int = Field(gt=0, le=16 * 1024**4)
    output_bytes: int = Field(gt=0, le=16 * 1024**4)
    privileged: bool
    processes: int = Field(ge=1, le=65_535)
    temporary_bytes: int = Field(gt=0, le=16 * 1024**4)
    timeout_seconds: int = Field(ge=1, le=86_400)


class RecipeBuildRequest(WireModel):
    arguments: list[RecipeBuildArgument] = Field(max_length=64)
    base_image_storage_bytes: int = Field(ge=0, le=16 * 1024**4)
    base_images: list[RecipeBuildBaseImage] = Field(max_length=8)
    capabilities: list[str] = Field(max_length=11)
    build_id: UuidId
    build_input_sha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    dockerfile: str = Field(min_length=1, max_length=512)
    kind: Literal["recipe.build.v1"]
    limits: RecipeBuildLimits
    network: RecipeBuildNetwork
    options: RecipeBuildOptions
    platform: Literal["linux/arm64"]
    recipe_content_sha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    recipe_revision_id: UuidId
    schema_version: Literal[1]
    source_bundle_bytes: int = Field(ge=1, le=64 * 1024**2)
    source_bundle_sha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    target: str | None = Field(default=None, max_length=64)


class RecipeBuildPolicyFinding(WireModel):
    code: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=512)
    line: int | None = Field(default=None, ge=1)
    detail: str = Field(min_length=1, max_length=512)


class RecipeBuildPolicy(WireModel):
    passed: bool
    dockerfile: str = Field(min_length=1, max_length=512)
    findings: list[RecipeBuildPolicyFinding] = Field(max_length=128)


class RecipeBuildEvidence(WireModel):
    build_input_sha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    image_bytes: int = Field(gt=0, le=16 * 1024**4)
    image_digest: OciDigest = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    oci_layout_sha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    policy: RecipeBuildPolicy


class RecipeImageImportRequest(WireModel):
    build_id: UuidId
    image_bytes: int = Field(gt=0, le=16 * 1024**4)
    image_digest: OciDigest = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    kind: Literal["recipe.image.import.v1"]
    mapping_generation: int = Field(ge=1)
    mapping_id: UuidId
    oci_layout_sha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal[1]
    source_node_id: NodeId


class RecipeImageImportEvidence(WireModel):
    build_id: UuidId
    image_bytes: int = Field(gt=0, le=16 * 1024**4)
    image_digest: OciDigest = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    oci_layout_sha256: Digest = Field(pattern=r"^[0-9a-f]{64}$")
