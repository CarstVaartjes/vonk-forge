"""Typed recipe build and image-import request/result wire contracts."""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

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
_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
BuildArgumentName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9._-]{0,63}$"),
]
EnvironmentName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]{0,127}$"),
]
MetadataName = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"
    ),
]
OsFeature = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
]
OsVersion = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._+-]+$")
]
_CAPABILITIES = {
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "MKNOD",
    "NET_BIND_SERVICE",
    "SETFCAP",
    "SETGID",
    "SETPCAP",
    "SETUID",
}


def _bundle_path(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and "\x00" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _pinned_image(reference: str, manifest_digest: str) -> bool:
    name, separator, digest = reference.rpartition("@")
    return bool(
        separator
        and name
        and len(name) <= 512
        and digest == manifest_digest
        and name[0].isalnum()
        and all(
            char.isascii() and (char.islower() or char.isdigit() or char in "._:/-")
            for char in name
        )
    )


def _public_host(value: str) -> bool:
    lowered = value.lower()
    if not value or len(value) > 253 or value.startswith(".") or value.endswith("."):
        return False
    if lowered in {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data.ec2.internal",
    } or lowered.endswith((".localhost", ".localdomain", ".internal")):
        return False
    if not all(char.isascii() and (char.isalnum() or char in ".-") for char in value):
        return False
    if all(char.isdigit() or char == "." for char in value):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        if not isinstance(address, ipaddress.IPv4Address):
            return True
        first, second = address.packed[:2]
        return (
            first not in {0, 10, 127, 169, 192, 198, 224}
            and not (first == 100 and 64 <= second <= 127)
            and not (first == 172 and 16 <= second <= 31)
            and not (first == 169 and second == 254)
            and not (first == 192 and second == 168)
            and not (first == 198 and 18 <= second <= 19)
        )
    return True


def _validate_options(options: RecipeBuildOptions) -> None:
    contexts = [item.name for item in options.additional_contexts]
    if len(set(contexts)) != len(contexts) or any(
        not _NAME.fullmatch(item.name) or not _bundle_path(item.path)
        for item in options.additional_contexts
    ):
        raise ValueError("additional build contexts are invalid")
    for entries in (options.annotations, options.labels, options.layer_labels):
        names = [item.name for item in entries]
        if len(set(names)) != len(names):
            raise ValueError("build metadata is invalid")
    environment = [item.name for item in options.environment]
    if len(set(environment)) != len(environment):
        raise ValueError("build environment is invalid")
    if options.ignorefile is not None and not _bundle_path(options.ignorefile):
        raise ValueError("build ignorefile is invalid")
    if len(set(options.os_features)) != len(options.os_features):
        raise ValueError("build OS features are invalid")
    if len(set(options.unset_environment)) != len(options.unset_environment):
        raise ValueError("build unset environment is invalid")
    if len(set(options.unset_labels)) != len(options.unset_labels):
        raise ValueError("build unset labels are invalid")


# The Rust producer accepts JSON strings, booleans, and integral numbers for
# build arguments.  Keeping this union strict prevents Pydantic from turning
# JSON floats/nulls into an accepted build argument.
JsonScalar = (
    Annotated[str, StringConstraints(max_length=1024)]
    | Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
    | bool
)


class RecipeBuildArgument(WireModel):
    name: str = Field(min_length=1, max_length=128)
    value: JsonScalar


class RecipeBuildEnvironmentArgument(WireModel):
    name: EnvironmentName
    value: JsonScalar


class RecipeBuildBaseImage(WireModel):
    manifest_digest: OciDigest
    reference: str = Field(min_length=1, max_length=512)


class RecipeBuildNetwork(WireModel):
    hosts: list[str] = Field(max_length=64)
    mode: Literal["none", "public"]


class RecipeBuildAdditionalContext(WireModel):
    name: BuildArgumentName
    path: str = Field(min_length=1, max_length=512)


class RecipeBuildMetadata(WireModel):
    name: MetadataName
    value: Annotated[str, StringConstraints(max_length=1024, pattern=r"^[^\x00]*$")]


class RecipeBuildOptions(WireModel):
    additional_contexts: list[RecipeBuildAdditionalContext] = Field(max_length=16)
    annotations: list[RecipeBuildMetadata] = Field(max_length=64)
    environment: list[RecipeBuildEnvironmentArgument] = Field(max_length=64)
    format: Literal["oci", "docker"]
    identity_label: bool
    ignorefile: str | None = Field(default=None, max_length=512)
    jobs: int = Field(ge=1, le=32)
    labels: list[RecipeBuildMetadata] = Field(max_length=64)
    layer_compression: Literal["disabled", "gzip"]
    layer_labels: list[RecipeBuildMetadata] = Field(max_length=64)
    layers: bool
    no_hostname: bool
    no_hosts: bool
    omit_history: bool
    os_features: list[OsFeature] = Field(max_length=32)
    os_version: OsVersion | None = None
    shm_bytes: int = Field(ge=65_536, le=16 * 1024**4)
    skip_unused_stages: bool
    squash: Literal["none", "new", "all"]
    timestamp: int | None = Field(default=None, ge=0, le=4_102_444_800)
    unset_environment: list[EnvironmentName] = Field(max_length=64)
    unset_labels: list[MetadataName] = Field(max_length=64)


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

    @model_validator(mode="after")
    def validate_execution_policy(self) -> RecipeBuildRequest:
        if not _bundle_path(self.dockerfile):
            raise ValueError("build Dockerfile path is invalid")
        if self.target is not None and (
            not self.target
            or any(
                char
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for char in self.target
            )
        ):
            raise ValueError("build target is invalid")
        if (
            any(capability not in _CAPABILITIES for capability in self.capabilities)
        ):
            raise ValueError("capabilities are not allowed")
        if (
            len(set(self.capabilities)) != len(self.capabilities)
            or (not self.base_images and self.base_image_storage_bytes != 0)
            or (
                self.base_images
                and not 1 <= self.base_image_storage_bytes <= 16 * 1024**4
            )
        ):
            raise ValueError("build identity or capability policy is invalid")
        references = [item.reference for item in self.base_images]
        if len(set(references)) != len(references) or any(
            not _pinned_image(item.reference, item.manifest_digest)
            for item in self.base_images
        ):
            raise ValueError("build base image pinning is invalid")
        if self.network.mode == "none":
            if self.network.hosts:
                raise ValueError("network hosts require public mode")
        elif not self.network.hosts or any(
            not _public_host(host) for host in self.network.hosts
        ):
            raise ValueError("public build network hosts are invalid")
        if (
            self.limits.gpu != 0
            or self.limits.privileged
            or self.limits.host_mounts
            or self.limits.container_socket
        ):
            raise ValueError("build hardening limits are invalid")
        _validate_options(self.options)
        if any(not _NAME.fullmatch(item.name) for item in self.arguments):
            raise ValueError("build argument name is invalid")
        return self


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

    @model_validator(mode="after")
    def require_successful_policy(self) -> RecipeBuildEvidence:
        if self.policy.passed is not True or self.policy.findings:
            raise ValueError("recipe build evidence policy did not pass")
        return self


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
