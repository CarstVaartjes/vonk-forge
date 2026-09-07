from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Union
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .contracts import AgentProtocolError, canonical_message
from .wire_model import WireModel

MAX_RELEASE_LOCK_BYTES = 1024 * 1024
MAX_COMPONENT_SIZE = 2**63 - 1
MAX_DEPENDENCY_DEPTH = 8
MAX_AGGREGATE_COMPONENTS = 256
MAX_SOURCES = 8
MAX_EVIDENCE = 16
MAX_PACKAGE_HELPER_GRANT_SECONDS = 15 * 60

PACKAGE_HELPER_AUTHORITY = "vonk.workload-package-helper"
PACKAGE_HELPER_GRANT_DOMAIN = b"Vonk Forge-WORKLOAD-PACKAGE-HELPER-GRANT-V1\0"
PACKAGE_OBJECT_RECEIPT_DOMAIN = b"Vonk Forge-WORKLOAD-PACKAGE-OBJECT-RECEIPT-V1\0"


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CONTENT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _duplicate_free_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_document(value: Any) -> Mapping[str, Any]:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, Mapping):
        return value
    else:
        raise AgentProtocolError("workload release lock must be JSON or an object")
    if len(raw) > MAX_RELEASE_LOCK_BYTES:
        raise AgentProtocolError("workload release lock is too large")
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_free_object,
        )
    except AgentProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentProtocolError(
            "workload release lock is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, Mapping):
        raise AgentProtocolError("workload release lock must be a JSON object")
    return document


def _bounded_text(value: Any, *, name: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or "\\" in value
    ):
        raise AgentProtocolError(f"{name} is not bounded canonical text")
    return value


def _uuid4(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise AgentProtocolError(f"{name} must be a canonical UUIDv4")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise AgentProtocolError(f"{name} must be a canonical UUIDv4") from error
    if parsed.version != 4 or str(parsed) != value:
        raise AgentProtocolError(f"{name} must be a canonical UUIDv4")
    return value


def _sha256(value: Any, *, name: str, prefixed: bool) -> str:
    pattern = CONTENT_DIGEST if prefixed else SHA256
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        label = "sha256:<64 lowercase hex>" if prefixed else "64 lowercase hex"
        raise AgentProtocolError(f"{name} must be {label}")
    return value


def _https_url(value: Any, *, name: str) -> str:
    text = _bounded_text(value, name=name, maximum=2048)
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AgentProtocolError(
            f"{name} must be an HTTPS URL without credentials or query data"
        )
    return text


class _HttpsSource(WireModel):
    provider: Literal["https"]
    url: str

    @field_validator("url")
    @classmethod
    def url_is_safe(cls, value: str) -> str:
        return _https_url(value, name="source URL")


class _OciSource(WireModel):
    provider: Literal["oci"]
    reference: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._:/-]{0,510}@sha256:[0-9a-f]{64}$"
    )


class _GitSource(WireModel):
    provider: Literal["git"]
    repository: str
    commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

    @field_validator("repository")
    @classmethod
    def repository_is_safe(cls, value: str) -> str:
        return _https_url(value, name="Git repository")


class _HuggingFaceSource(WireModel):
    provider: Literal["huggingface"]
    repository: str = Field(
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,95})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,95})$"
    )
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class _IndexSource(WireModel):
    provider: Literal["python-index", "signed-http-index"]
    url: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("url")
    @classmethod
    def url_is_safe(cls, value: str) -> str:
        return _https_url(value, name="index URL")


ComponentSource = Annotated[
    Union[_HttpsSource, _OciSource, _GitSource, _HuggingFaceSource, _IndexSource],
    Field(discriminator="provider"),
]


class ComponentEvidence(WireModel):
    kind: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


Identifier = Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")]
Platform = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")]


class OciBundleMetadata(WireModel):
    """Signed metadata for an immutable OCI-rootfs workload component.

    Workload locks carry this metadata in the component's materialization
    object.  The archive itself must repeat the same canonical document as
    ``oci-bundle.json``; the agent verifies both before a helper can launch it.
    ``component`` is an identifier, never a host path.  The agent derives the
    actual generation path from its fixed package root.
    """

    schema_version: Literal[1]
    component: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rootfs_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    architecture: str = Field(pattern=r"^(?:linux-arm64|linux-x86_64)$")
    runtime: Literal["runc"]
    rootfs: str = Field(min_length=1, max_length=256)
    entrypoint: str = Field(min_length=1, max_length=256)

    @field_validator("rootfs", "entrypoint")
    @classmethod
    def relative_path_is_safe(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F for character in value
            )
        ):
            raise ValueError("OCI bundle path is invalid")
        return value

    @classmethod
    def parse(cls, value: Any) -> OciBundleMetadata:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError(
                f"OCI bundle metadata is invalid: {error}"
            ) from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class _SimpleMaterialization(WireModel):
    method: Literal[
        "file",
        "snapshot",
        "archive",
        "oci-content",
        "configuration",
        "native-archive",
        "wheel",
        "pylock-environment",
        "executable",
    ]


class _OciBundleMaterialization(OciBundleMetadata):
    method: Literal["oci-bundle"]


ComponentMaterialization = Annotated[
    Union[_SimpleMaterialization, _OciBundleMaterialization],
    Field(discriminator="method"),
]


class ComponentDescriptor(WireModel):
    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
    kind: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
    media_type: str = Field(
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
    )
    sources: tuple[ComponentSource, ...] = Field(min_length=1, max_length=MAX_SOURCES)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size: int = Field(ge=1, le=MAX_COMPONENT_SIZE)
    unpacked_size: int | None = Field(default=None, ge=1, le=MAX_COMPONENT_SIZE)
    platforms: tuple[Platform, ...] = Field(min_length=1, max_length=16)
    materialization: ComponentMaterialization
    evidence: tuple[ComponentEvidence, ...] = Field(max_length=MAX_EVIDENCE)

    @model_validator(mode="after")
    def platforms_are_unique(self) -> ComponentDescriptor:
        if len(set(self.platforms)) != len(self.platforms):
            raise ValueError("component platforms contain duplicates")
        return self

    @classmethod
    def parse(cls, value: Any) -> ComponentDescriptor:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError(f"component is invalid: {error}") from error

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "media_type": self.media_type,
            "sources": [source.model_dump(mode="json") for source in self.sources],
            "digest": self.digest,
            "size": self.size,
            "unpacked_size": self.unpacked_size,
            "platforms": list(self.platforms),
            "materialization": self.materialization.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in self.evidence],
        }


class _PythonIndexIdentity(WireModel):
    provider: Literal["python-index"]
    project: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
    version: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("version")
    @classmethod
    def version_is_bounded(cls, value: str) -> str:
        return _bounded_text(value, name="Python version", maximum=128)


class _SignedHttpIndexIdentity(WireModel):
    provider: Literal["signed-http-index"]
    url: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("url")
    @classmethod
    def url_is_safe(cls, value: str) -> str:
        return _https_url(value, name="signed index URL")


UpstreamIdentity = Annotated[
    Union[
        _GitSource,
        _HuggingFaceSource,
        _OciSource,
        _PythonIndexIdentity,
        _SignedHttpIndexIdentity,
    ],
    Field(discriminator="provider"),
]


class PythonRuntimeMetadata(WireModel):
    environment_component: str = Field(
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$"
    )
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment_tree_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    interpreter_component: str = Field(
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$"
    )
    interpreter_component_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    interpreter_entrypoint: str = Field(min_length=1, max_length=256)
    interpreter_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("interpreter_entrypoint")
    @classmethod
    def entrypoint_is_safe(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            "\\" in value
            or path.is_absolute()
            or str(path) != value
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.name in {"apt", "apt-get", "bash", "dash", "sh", "sudo"}
        ):
            raise ValueError("Python interpreter entrypoint is invalid")
        return value

    @model_validator(mode="after")
    def components_are_distinct(self) -> PythonRuntimeMetadata:
        if self.environment_component == self.interpreter_component:
            raise ValueError(
                "Python environment and interpreter components must differ"
            )
        return self


class Compatibility(WireModel):
    architectures: tuple[Identifier, ...] = Field(min_length=1, max_length=32)
    operating_systems: tuple[Identifier, ...] = Field(min_length=1, max_length=32)
    required_capabilities: tuple[Identifier, ...] = Field(max_length=32)
    minimum_storage_bytes: int = Field(ge=1, le=MAX_COMPONENT_SIZE)
    minimum_memory_bytes: int | None = Field(default=None, ge=1, le=MAX_COMPONENT_SIZE)
    minimum_driver: str | None = Field(default=None, min_length=1, max_length=64)
    minimum_cuda: str | None = Field(default=None, min_length=1, max_length=64)
    backends: tuple[Literal["oci", "python-venv", "native"], ...] | None = Field(
        default=None, min_length=1, max_length=3
    )
    python_runtime: PythonRuntimeMetadata | None = None

    @field_validator("architectures", "operating_systems", "required_capabilities")
    @classmethod
    def identifiers_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("compatibility identifiers contain duplicates")
        return value

    @model_validator(mode="after")
    def runtime_matches_backend(self) -> Compatibility:
        if self.python_runtime is not None and (
            self.backends is None or "python-venv" not in self.backends
        ):
            raise ValueError("Python runtime metadata requires the python-venv backend")
        if (
            self.backends is not None
            and "python-venv" in self.backends
            and self.python_runtime is None
        ):
            raise ValueError(
                "python-venv compatibility requires Python runtime metadata"
            )
        return self


class ValidationRecord(WireModel):
    kind: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
    component: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
    )
    digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    required: bool | None = None


class Resolver(WireModel):
    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
    version: int = Field(ge=1, le=2**31 - 1)


_RESOURCE_FIELDS = (
    "download_bytes",
    "installed_bytes",
    "transient_bytes",
    "output_bytes",
    "host_memory_bytes",
    "resident_memory_bytes",
    "auxiliary_memory_bytes",
    "activation_memory_bytes",
    "workspace_memory_bytes",
    "gpu_memory_bytes",
    "gpu_count",
    "cpu_millicores",
    "kv_cache_base_bytes",
    "kv_cache_per_token_bytes",
)


class ResourceValues(WireModel):
    download_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    installed_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    transient_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    output_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    host_memory_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    resident_memory_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    auxiliary_memory_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    activation_memory_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    workspace_memory_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    gpu_memory_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    gpu_count: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    cpu_millicores: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    kv_cache_base_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)
    kv_cache_per_token_bytes: int = Field(ge=0, le=MAX_COMPONENT_SIZE)

    @model_validator(mode="after")
    def memory_breakdown_fits(self) -> ResourceValues:
        total = sum(
            getattr(self, field)
            for field in (
                "resident_memory_bytes",
                "auxiliary_memory_bytes",
                "activation_memory_bytes",
                "workspace_memory_bytes",
            )
        )
        if self.host_memory_bytes < total:
            raise ValueError("host_memory_bytes is below memory breakdown")
        return self


class ResourceRank(WireModel):
    rank: int = Field(ge=0, le=511)
    role: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


class ResourceFabric(WireModel):
    kind: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
    min_bandwidth_mbps: int = Field(ge=0, le=1_000_000_000)


class ResourceEnvelope(WireModel):
    schema_version: Literal[1]
    per_node: ResourceValues
    aggregate: ResourceValues
    required_nodes: int = Field(ge=1, le=512)
    topology: Literal["single", "replicated", "gang"]
    world_size: int = Field(ge=1, le=512)
    ranks: tuple[ResourceRank, ...] = Field(min_length=1, max_length=512)
    fabric: ResourceFabric
    measurement: Literal["declared", "measured"]
    evidence: tuple[ComponentEvidence, ...] = Field(
        min_length=1, max_length=MAX_EVIDENCE
    )

    @model_validator(mode="after")
    def topology_is_consistent(self) -> ResourceEnvelope:
        if self.topology == "single" and (
            self.required_nodes != 1 or self.world_size != 1
        ):
            raise ValueError(
                "single resource_envelope requires one node and world size"
            )
        if self.topology == "replicated" and self.world_size != 1:
            raise ValueError("replicated resource_envelope requires world_size one")
        if self.topology == "gang" and (
            self.required_nodes < 2 or self.world_size < self.required_nodes
        ):
            raise ValueError("gang resource_envelope topology is invalid")
        if len(self.ranks) != self.world_size or any(
            rank.rank != expected for expected, rank in enumerate(self.ranks)
        ):
            raise ValueError("resource_envelope ranks must be contiguous")
        for field in _RESOURCE_FIELDS:
            if (
                getattr(self.aggregate, field)
                < getattr(self.per_node, field) * self.required_nodes
            ):
                raise ValueError(
                    f"resource_envelope aggregate {field} is below per-node total"
                )
        return self


class PackageHelperOperation(StrEnum):
    """Closed workload-only operation vocabulary accepted by the root helper."""

    PREPARE = "prepare"
    VERIFY = "verify"
    START = "start"
    HEALTH = "health"
    INFER = "infer"
    STOP = "stop"
    VERIFY_RELEASE = "verify-release"


class PackageHelperSignature(WireModel):
    algorithm: str
    key_id: str
    value: str

    @field_validator("algorithm")
    @classmethod
    def algorithm_is_ed25519(cls, value: str) -> str:
        if value != "ed25519":
            raise ValueError("package helper signature algorithm is invalid")
        return value

    key_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    value: str = Field(pattern=r"^[0-9a-f]{128}$")

    @classmethod
    def parse(cls, value: Any) -> PackageHelperSignature:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError(
                f"package helper signature is invalid: {error}"
            ) from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class PackageHelperGrantClaims(WireModel):
    schema_version: Literal[1]
    authority: Literal[PACKAGE_HELPER_AUTHORITY]
    request_id: str
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    job_id: str
    operation_id: str
    attempt: int = Field(ge=1, le=2**31 - 1)
    fence: str
    release_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: str
    operation: PackageHelperOperation
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: int = Field(ge=1, le=2**63 - 1)
    expires_at: int = Field(ge=1, le=2**63 - 1)

    @field_validator("request_id", "job_id", "operation_id", "fence")
    @classmethod
    def ids_are_uuid4(cls, value: str, info: Any) -> str:
        return _uuid4(value, name=f"package helper {info.field_name}")

    generation: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")

    @field_validator("operation", mode="before")
    @classmethod
    def operation_is_closed(cls, value: Any) -> PackageHelperOperation:
        try:
            return PackageHelperOperation(value)
        except (TypeError, ValueError) as error:
            raise ValueError("package helper operation is invalid") from error

    @model_validator(mode="after")
    def expiry_is_bounded(self) -> PackageHelperGrantClaims:
        if (
            not 1
            <= self.expires_at - self.issued_at
            <= MAX_PACKAGE_HELPER_GRANT_SECONDS
        ):
            raise ValueError("package helper grant expiry is invalid")
        return self

    @classmethod
    def parse(cls, value: Any) -> PackageHelperGrantClaims:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError(
                f"package helper grant claims are invalid: {error}"
            ) from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class SignedPackageHelperGrant(WireModel):
    claims: PackageHelperGrantClaims
    signature: PackageHelperSignature

    @classmethod
    def parse(cls, value: Any) -> SignedPackageHelperGrant:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError(
                f"signed package helper grant is invalid: {error}"
            ) from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class PackageObjectReceiptClaims(WireModel):
    schema_version: Literal[1]
    authority: Literal[PACKAGE_HELPER_AUTHORITY]
    object_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=1, le=2**63 - 1)
    relative_name: str

    @model_validator(mode="after")
    def relative_name_matches_digest(self) -> PackageObjectReceiptClaims:
        if self.relative_name != f"objects/sha256/{self.object_digest}":
            raise ValueError("package object receipt relative name is invalid")
        return self

    @classmethod
    def parse(cls, value: Any) -> PackageObjectReceiptClaims:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError(
                f"package object receipt claims are invalid: {error}"
            ) from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class SignedPackageObjectReceipt(WireModel):
    claims: PackageObjectReceiptClaims
    signature: PackageHelperSignature

    @classmethod
    def parse(cls, value: Any) -> SignedPackageObjectReceipt:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError(
                f"signed package object receipt is invalid: {error}"
            ) from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @property
    def object_digest(self) -> str:
        return self.claims.object_digest

    @property
    def size(self) -> int:
        return self.claims.size

    @property
    def relative_name(self) -> str:
        return self.claims.relative_name


def package_helper_grant_signing_bytes(claims: PackageHelperGrantClaims) -> bytes:
    if type(claims) is not PackageHelperGrantClaims:
        raise AgentProtocolError("package helper grant claims are invalid")
    return PACKAGE_HELPER_GRANT_DOMAIN + canonical_message(claims.to_mapping())


def package_object_receipt_signing_bytes(
    claims: PackageObjectReceiptClaims,
) -> bytes:
    if type(claims) is not PackageObjectReceiptClaims:
        raise AgentProtocolError("package object receipt claims are invalid")
    return PACKAGE_OBJECT_RECEIPT_DOMAIN + canonical_message(claims.to_mapping())


class PackageReleaseLock(WireModel):
    schema_version: Literal[1]
    family_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
    upstream_version: str = Field(min_length=1, max_length=128)
    upstream_identity: UpstreamIdentity
    components: tuple[ComponentDescriptor, ...] = Field(max_length=255)
    dependency_digests: tuple[Digest, ...] = Field(max_length=256)
    adapter: ComponentDescriptor
    adapter_abi: int = Field(ge=1, le=255)
    compatibility: Compatibility
    validation: tuple[ValidationRecord, ...] = Field(max_length=64)
    provenance: tuple[ComponentEvidence, ...] = Field(max_length=64)
    resolver: Resolver
    resource_envelope: ResourceEnvelope | None = None

    @field_validator("upstream_version")
    @classmethod
    def upstream_version_is_bounded(cls, value: str) -> str:
        return _bounded_text(value, name="upstream_version", maximum=128)

    @model_validator(mode="after")
    def release_graph_is_consistent(self) -> PackageReleaseLock:
        component_names = [item.name for item in self.components]
        if len(set(component_names)) != len(component_names):
            raise ValueError("duplicate component name")
        if self.adapter.kind != "adapter":
            raise ValueError("adapter component kind must be adapter")
        if self.adapter.name in component_names:
            raise ValueError("duplicate component name")
        if len(set(self.dependency_digests)) != len(self.dependency_digests):
            raise ValueError("duplicate dependency digest")
        declared_names = {*component_names, self.adapter.name}
        if any(
            item.component is not None and item.component not in declared_names
            for item in self.validation
        ):
            raise ValueError("validation component is not declared")
        if len(self.canonical_bytes) > MAX_RELEASE_LOCK_BYTES:
            raise ValueError("workload release lock is too large")
        return self

    @property
    def canonical_bytes(self) -> bytes:
        document: dict[str, object] = {
            "schema_version": self.schema_version,
            "family_id": self.family_id,
            "upstream_version": self.upstream_version,
            "upstream_identity": self.upstream_identity.model_dump(mode="json"),
            "components": [component.to_mapping() for component in self.components],
            "dependency_digests": self.dependency_digests,
            "adapter": self.adapter.to_mapping(),
            "adapter_abi": self.adapter_abi,
            "compatibility": self.compatibility.model_dump(mode="json"),
            "validation": [item.model_dump(mode="json") for item in self.validation],
            "provenance": [item.model_dump(mode="json") for item in self.provenance],
            "resolver": self.resolver.model_dump(mode="json"),
        }
        if self.resource_envelope is not None:
            document["resource_envelope"] = self.resource_envelope.model_dump(
                mode="json"
            )
        return canonical_message(document)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def parse(cls, value: Any) -> PackageReleaseLock:
        document = _load_document(value)
        try:
            return cls.model_validate_json(canonical_message(document))
        except ValidationError as error:
            raise AgentProtocolError(
                f"workload release lock is invalid: {error}"
            ) from error


WORKLOAD_RELEASE_LOCK_SCHEMA_ID = (
    "https://vonk-forge.invalid/schemas/workload-release-lock.schema.json"
)


def workload_release_lock_schema() -> dict[str, object]:
    """Return the deterministic JSON Schema derived from the canonical wire model."""
    schema = PackageReleaseLock.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = WORKLOAD_RELEASE_LOCK_SCHEMA_ID
    schema["title"] = "Vonk Forge immutable workload release lock"
    return schema


class PackageReleaseGraph(WireModel):
    root_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    releases: tuple[PackageReleaseLock, ...]

    @property
    def component_count(self) -> int:
        return sum(len(release.components) + 1 for release in self.releases)

    @property
    def total_size(self) -> int:
        return sum(
            component.size
            for release in self.releases
            for component in (*release.components, release.adapter)
        )

    @classmethod
    def resolve(
        cls,
        root_digest: str,
        releases: Mapping[str, PackageReleaseLock],
    ) -> PackageReleaseGraph:
        root_digest = _sha256(root_digest, name="root digest", prefixed=False)
        if not isinstance(releases, Mapping):
            raise AgentProtocolError("releases must be a digest mapping")
        visiting: set[str] = set()
        resolved: set[str] = set()
        ordered: list[PackageReleaseLock] = []
        component_count = 0

        def visit(digest: str, depth: int) -> None:
            nonlocal component_count
            if digest in visiting:
                raise AgentProtocolError("package dependency cycle detected")
            if digest in resolved:
                return
            if depth > MAX_DEPENDENCY_DEPTH:
                raise AgentProtocolError("package dependency depth exceeds 8")
            release = releases.get(digest)
            if not isinstance(release, PackageReleaseLock):
                raise AgentProtocolError(f"package dependency is missing: {digest}")
            visiting.add(digest)
            ordered.append(release)
            component_count += len(release.components) + 1
            if component_count > MAX_AGGREGATE_COMPONENTS:
                raise AgentProtocolError("package graph component count exceeds 256")
            for dependency in release.dependency_digests:
                visit(dependency, depth + 1)
            if release.digest != digest:
                raise AgentProtocolError("package release digest mismatch")
            visiting.remove(digest)
            resolved.add(digest)

        visit(root_digest, 0)
        return cls(root_digest=root_digest, releases=tuple(ordered))


__all__ = [
    "MAX_PACKAGE_HELPER_GRANT_SECONDS",
    "PACKAGE_HELPER_AUTHORITY",
    "WORKLOAD_RELEASE_LOCK_SCHEMA_ID",
    "Compatibility",
    "ComponentEvidence",
    "ComponentDescriptor",
    "ComponentSource",
    "OciBundleMetadata",
    "PackageHelperGrantClaims",
    "PackageHelperOperation",
    "PackageHelperSignature",
    "PackageObjectReceiptClaims",
    "PackageReleaseGraph",
    "PackageReleaseLock",
    "PythonRuntimeMetadata",
    "ResourceEnvelope",
    "ResourceFabric",
    "ResourceRank",
    "ResourceValues",
    "Resolver",
    "SignedPackageHelperGrant",
    "SignedPackageObjectReceipt",
    "package_helper_grant_signing_bytes",
    "package_object_receipt_signing_bytes",
    "workload_release_lock_schema",
]
