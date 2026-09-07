"""Typed schema-2 compiled launch plan shared by Controller and agents."""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from .contracts import AgentProtocolError

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class CompiledExecutionPlanError(ValueError):
    """The schema-2 launch projection is invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _safe_path(value: str, *, absolute: bool) -> str:
    if absolute and value == "/":
        return value
    parts = value[1:].split("/") if absolute and value.startswith("/") else value.split("/")
    if not value or len(value) > 512 or "\\" in value or "\x00" in value or any(part in {"", ".", ".."} for part in parts) or (absolute and not value.startswith("/")) or (not absolute and value.startswith("/")):
        raise ValueError("path is unsafe")
    return value


def _valid_name(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}\Z", value))


def _valid_role(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,63}\Z", value))


def _validate_argv(value: list[str], *, required: bool = False) -> list[str]:
    if (
        (required and (not value or not value[0]))
        or len(value) > 512
        or any("\x00" in item or len(item.encode()) > 65536 for item in value)
        or sum(len(item.encode()) for item in value) > 1024 * 1024
    ):
        raise ValueError("argv is invalid")
    return value


def _validate_ip(value: str | None) -> str | None:
    if value is None:
        return value
    parsed = ipaddress.ip_address(value)
    if parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified or str(parsed) != value:
        raise ValueError("address is invalid")
    return value


class CompiledIdentity(_Strict):
    recipe_revision_sha256: Digest
    execution_sha256: Digest
    harness_sha256: Digest
    build_input_sha256: Digest | None
    model_artifact_set_sha256: Digest
    model_artifact_bytes: int = Field(ge=0, le=16 * 1024**4)


class CompiledEnvironmentEntry(_Strict):
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=65536)

    @field_validator("name")
    @classmethod
    def name_is_canonical(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*\Z", value):
            raise ValueError("environment name is invalid")
        return value

    @field_validator("value")
    @classmethod
    def value_is_safe(cls, value: str) -> str:
        if "\x00" in value or len(value.encode()) > 65536:
            raise ValueError("environment value is invalid")
        return value


class CompiledPlacement(_Strict):
    endpoint_address: str | None
    rank: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=64)
    world_size: int = Field(ge=1)
    local_address: str | None
    master_address: str | None
    master_port: int | None
    port: int = Field(ge=1, le=65535)
    reserved_memory_bytes: int = Field(gt=0, le=16 * 1024**4)

    _addresses_are_safe = field_validator("endpoint_address", "local_address", "master_address")(_validate_ip)


class CompiledRuntime(_Strict):
    executable: str = Field(min_length=1, max_length=65536)
    argv: list[str] = Field(max_length=512)
    env: list[CompiledEnvironmentEntry] = Field(max_length=128)
    image_digest: ImageDigest
    placement: CompiledPlacement

    @field_validator("executable")
    @classmethod
    def executable_is_absolute(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or "\x00" in value
            or "\r" in value
            or "\n" in value
            or len(value.encode()) > 65536
        ):
            raise ValueError("executable is unsafe")
        return value

    _argv_is_safe = field_validator("argv")(_validate_argv)


class CompiledArtifactMount(_Strict):
    target: str
    read_only: StrictBool

    @field_validator("target")
    @classmethod
    def target_is_model_path(cls, value: str) -> str:
        value = _safe_path(value, absolute=True)
        if value != "/models" and not value.startswith("/models/"):
            raise ValueError("model mount target is unsafe")
        return value

    @model_validator(mode="after")
    def mount_is_read_only(self) -> CompiledArtifactMount:
        if not self.read_only:
            raise ValueError("model mount must be read-only")
        return self


class CompiledModelIdentity(_Strict):
    publisher: str = Field(min_length=1, max_length=64)
    slug: str = Field(min_length=1, max_length=64)
    content_sha256: Digest

    @field_validator("publisher", "slug")
    @classmethod
    def names_are_canonical(cls, value: str) -> str:
        if not _valid_name(value):
            raise ValueError("model identity name is invalid")
        return value


class CompiledDistributionObject(_Strict):
    name: str
    sha256: Digest
    bytes: int = Field(ge=0, le=16 * 1024**4)
    kind: Literal["model", "oci-archive"]

    @field_validator("name")
    @classmethod
    def name_is_relative(cls, value: str) -> str:
        return _safe_path(value, absolute=False)

    @model_validator(mode="after")
    def empty_only_for_model(self) -> CompiledDistributionObject:
        if self.bytes == 0 and (self.kind != "model" or self.sha256 != _EMPTY_SHA256):
            raise ValueError("zero-sized distribution object is invalid")
        return self


class CompiledArtifact(_Strict):
    selection_id: str = Field(min_length=1, max_length=64)
    file_id: str = Field(min_length=1, max_length=64)
    path: str
    sha256: Digest
    size_bytes: int = Field(ge=0, le=16 * 1024**4)
    roles: list[str] = Field(min_length=1)
    mount: CompiledArtifactMount
    model: CompiledModelIdentity
    distribution_object: CompiledDistributionObject

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        if not value or len(value) > 512 or "\\" in value or "\x00" in value or any(not part or part in {".", ".."} or any(not char.isascii() or not (char.isalnum() or char in "._-") for char in part) for part in value.split("/")):
            raise ValueError("model path is unsafe")
        return value

    @field_validator("selection_id", "file_id")
    @classmethod
    def names_are_canonical(cls, value: str) -> str:
        if not _valid_name(value):
            raise ValueError("artifact identity is invalid")
        return value

    @field_validator("roles")
    @classmethod
    def roles_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)) or any(not _valid_role(role) for role in value):
            raise ValueError("artifact roles are not canonical")
        return value

    @model_validator(mode="after")
    def receipt_matches(self) -> CompiledArtifact:
        if self.distribution_object.kind != "model" or self.distribution_object.name != self.path or self.distribution_object.sha256 != self.sha256 or self.distribution_object.bytes != self.size_bytes:
            raise ValueError("artifact distribution receipt is inconsistent")
        if self.size_bytes == 0 and any(
            role in {"model", "weight", "weights"} for role in self.roles
        ):
            raise ValueError("zero-sized model artifact role is invalid")
        return self


class CompiledRuntimeImage(_Strict):
    image_digest: ImageDigest
    oci_layout_sha256: Digest
    image_bytes: int = Field(gt=0, le=16 * 1024**4)
    architecture: Literal["linux-arm64"]
    runtime_interface: Literal["vonk.runtime.v1"]
    source: Literal["published", "controller-build"]
    build_id: str | None
    distribution_object: CompiledDistributionObject
    registry_manifest_digest: ImageDigest | None
    platform_manifest_digest: ImageDigest
    local_image_config_id: ImageDigest
    local_image_reference: str
    runtime_interface_label: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def receipt_matches(self) -> CompiledRuntimeImage:
        if self.platform_manifest_digest != self.image_digest or self.distribution_object.kind != "oci-archive" or self.distribution_object.name != "image.oci.tar" or self.distribution_object.sha256 != self.oci_layout_sha256 or self.distribution_object.bytes != self.image_bytes:
            raise ValueError("runtime image receipt is inconsistent")
        if self.source == "published" and (self.build_id is not None or self.registry_manifest_digest is None):
            raise ValueError("published image receipt is invalid")
        if self.source == "controller-build" and (not self.build_id or self.registry_manifest_digest is not None):
            raise ValueError("Controller image receipt is invalid")
        expected = f"localhost/vonk/compiled-runtime-{self.oci_layout_sha256}@{self.platform_manifest_digest}"
        if self.local_image_reference != expected:
            raise ValueError("runtime image reference is not bound")
        return self


class CompiledSecurityMount(_Strict):
    source: Literal["model", "inputs", "outputs"]
    target: str
    read_only: StrictBool

    @field_validator("target")
    @classmethod
    def target_is_safe(cls, value: str) -> str:
        return _safe_path(value, absolute=True)

    @model_validator(mode="after")
    def policy_matches_source(self) -> CompiledSecurityMount:
        if self.source == "outputs" and (self.target != "/outputs" or self.read_only):
            raise ValueError("outputs mount policy is invalid")
        if self.source == "inputs" and (self.target != "/inputs" or not self.read_only):
            raise ValueError("inputs mount policy is invalid")
        if self.source == "model" and (self.target != "/models" and not self.target.startswith("/models/")):
            raise ValueError("model mount policy is invalid")
        if self.source == "model" and not self.read_only:
            raise ValueError("model mount must be read-only")
        return self


class CompiledSecurity(_Strict):
    devices: list[str]
    capabilities: list[str]
    network_mode: Literal["none", "bridge"]
    host_network: StrictBool
    privileged: StrictBool
    user: str
    mounts: list[CompiledSecurityMount]
    read_only_root: StrictBool
    no_new_privileges: StrictBool

    @model_validator(mode="after")
    def security_is_bounded(self) -> CompiledSecurity:
        if (
            self.host_network
            or self.privileged
            or not self.read_only_root
            or not self.no_new_privileges
            or (
                self.devices
                and (
                    len(self.devices) > 1
                    or self.devices != ["nvidia.com/gpu=all"]
                )
            )
        ):
            raise ValueError("security policy is invalid")
        if self.capabilities or len(self.mounts) > 4:
            raise ValueError("security policy is invalid")
        parts = self.user.split(":")
        if len(parts) not in {1, 2} or any(not part or part.startswith("0") or not part.isdigit() for part in parts):
            raise ValueError("security user is invalid")
        return self


class CompiledTopology(_Strict):
    name: str
    mode: Literal["single", "distributed"]
    backend: Literal["local", "nccl", "gloo"]
    node_count: int = Field(gt=0)
    world_size: int = Field(gt=0)
    rank: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def name_is_canonical(cls, value: str) -> str:
        if not _valid_name(value):
            raise ValueError("topology name is invalid")
        return value

    @field_validator("role")
    @classmethod
    def role_is_canonical(cls, value: str) -> str:
        if not _valid_role(value):
            raise ValueError("topology role is invalid")
        return value

    @model_validator(mode="after")
    def topology_is_bounded(self) -> CompiledTopology:
        if self.world_size < self.node_count or self.rank >= self.world_size:
            raise ValueError("topology bounds are invalid")
        return self


class CompiledLifecycle(_Strict):
    pre_start: list[list[str]] = Field(max_length=16)
    post_stop: list[list[str]] = Field(max_length=16)
    stop_timeout_seconds: int = Field(ge=1, le=600)

    @field_validator("pre_start", "post_stop")
    @classmethod
    def hooks_are_argv(cls, value: list[list[str]]) -> list[list[str]]:
        for argv in value:
            _validate_argv(argv, required=True)
        return value


class CompiledEndpoint(_Strict):
    protocol: Literal["openai"]
    port: int = Field(ge=1024, le=65535)
    model_aliases: list[str] = Field(min_length=1)
    health_path: str = Field(max_length=256)

    @field_validator("health_path")
    @classmethod
    def health_path_is_safe(cls, value: str) -> str:
        if ".." in value:
            raise ValueError("health path is unsafe")
        return _safe_path(value, absolute=True)


class CompiledJob(_Strict):
    interface: Literal["image-job", "audio-job", "video-job", "mesh-job", "artifact-job"]
    input: dict[str, Any] | None
    output_path: Literal["/outputs"]
    timeout_seconds: int = Field(ge=1, le=3600)

    @model_validator(mode="after")
    def input_path_is_bound(self) -> CompiledJob:
        if self.input is not None and self.input.get("path") != "/inputs":
            raise ValueError("job input path is invalid")
        return self


class CompiledExecutionPlan(_Strict):
    schema_version: Literal[2]
    identity: CompiledIdentity
    runtime: CompiledRuntime
    artifacts: list[CompiledArtifact] = Field(min_length=1, max_length=4096)
    runtime_image: CompiledRuntimeImage
    security: CompiledSecurity
    topology: CompiledTopology
    lifecycle: CompiledLifecycle
    endpoint: CompiledEndpoint | None
    job: CompiledJob | None

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_strict(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("schema version is invalid")
        return value

    @model_validator(mode="after")
    def cross_fields_match(self) -> CompiledExecutionPlan:
        if (self.endpoint is None) == (self.job is None):
            raise ValueError("exactly one compiled interface is required")
        placement = self.runtime.placement
        if self.runtime.image_digest != self.runtime_image.image_digest or (placement.rank, placement.role, placement.world_size) != (self.topology.rank, self.topology.role, self.topology.world_size) or placement.rank >= placement.world_size:
            raise ValueError("compiled placement identity is inconsistent")
        if self.topology.world_size < self.topology.node_count:
            raise ValueError("compiled topology bounds are invalid")
        if self.topology.world_size == 1 and (
            placement.rank != 0
            or placement.local_address is not None
            or placement.master_address is not None
            or placement.master_port is not None
        ):
            raise ValueError("single-node rendezvous is invalid")
        by_digest: dict[str, int] = {}
        selected: set[tuple[str, str]] = set()
        paths: set[tuple[str, str]] = set()
        for artifact in self.artifacts:
            if (artifact.selection_id, artifact.file_id) in selected or (artifact.selection_id, artifact.path) in paths:
                raise ValueError("compiled artifact identity is duplicated")
            selected.add((artifact.selection_id, artifact.file_id))
            paths.add((artifact.selection_id, artifact.path))
            previous = by_digest.setdefault(artifact.sha256, artifact.size_bytes)
            if previous != artifact.size_bytes:
                raise ValueError("compiled artifact digest sizes conflict")
        if sum(by_digest.values()) != self.identity.model_artifact_bytes:
            raise ValueError("compiled artifact bytes do not match identity")
        expected_network = "bridge" if placement.endpoint_address is not None or placement.master_port is not None else "none"
        if self.security.network_mode != expected_network:
            raise ValueError("compiled network policy does not match placement")
        return self

    @classmethod
    def parse(cls, value: object) -> CompiledExecutionPlan:
        try:
            return cls.model_validate(value)
        except Exception as error:
            raise AgentProtocolError("compiled execution plan is invalid") from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def validate_compiled_execution_plan(value: object) -> dict[str, object]:
    return CompiledExecutionPlan.parse(value).to_mapping()


__all__ = [
    "CompiledArtifact",
    "CompiledArtifactMount",
    "CompiledDistributionObject",
    "CompiledEndpoint",
    "CompiledEnvironmentEntry",
    "CompiledExecutionPlan",
    "CompiledExecutionPlanError",
    "CompiledIdentity",
    "CompiledJob",
    "CompiledLifecycle",
    "CompiledModelIdentity",
    "CompiledPlacement",
    "CompiledRuntime",
    "CompiledRuntimeImage",
    "CompiledSecurity",
    "CompiledSecurityMount",
    "CompiledTopology",
    "validate_compiled_execution_plan",
]
