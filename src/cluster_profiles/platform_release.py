"""Strict, content-addressed Vonk Forge platform release contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validators

_MAX_MANIFEST_BYTES = 1024 * 1024
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
_RAW_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_VERSIONED_TARGET = re.compile(
    r"platform/releases/"
    r"(?P<version>(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))/"
    r"(?P<sha256>[0-9a-f]{64})\.json\Z"
)
_OCI_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_OCI_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_OCI_REFERENCE = re.compile(
    rf"(?P<registry>{_OCI_HOST_LABEL}(?:\.{_OCI_HOST_LABEL})*)"
    rf"(?::(?P<port>[1-9][0-9]{{0,4}}))?/"
    rf"(?P<repository>{_OCI_COMPONENT}(?:/{_OCI_COMPONENT})*)"
    r"@sha256:(?P<sha256>[0-9a-f]{64})\Z"
)


class PlatformReleaseError(ValueError):
    """A platform release manifest is invalid or unsafe."""


@dataclass(frozen=True)
class ProtocolRange:
    minimum: int
    maximum: int

    def contains(self, version: int) -> bool:
        return self.minimum <= version <= self.maximum


@dataclass(frozen=True)
class Artifact:
    name: str
    reference: str
    sha256: str
    size: int
    sbom_sha256: str
    provenance_sha256: str


@dataclass(frozen=True)
class DebianPackage:
    architecture: str
    name: str
    version: str
    filename: str
    sha256: str
    size: int
    sbom_sha256: str
    provenance_sha256: str
    sigstore_bundle_sha256: str


@dataclass(frozen=True)
class ArchitectureArtifact:
    architecture: str
    artifact: Artifact
    payload_name: str
    payload_sha256: str
    payload_size: int
    protocol: ProtocolRange | None = None


@dataclass(frozen=True)
class ControlRelease:
    config_version: int
    protocol: ProtocolRange
    api_image: Artifact
    worker_image: Artifact
    assets: tuple[Artifact, ...]


@dataclass(frozen=True)
class DatabaseRelease:
    expand_revision: str
    contract_revision: str | None
    predecessor_compatible: bool


@dataclass(frozen=True)
class OciDeploymentBundle:
    reference: str
    manifest_digest: str
    manifest_size: int
    manifest_media_type: str
    layer_digest: str
    layer_size: int
    layer_media_type: str


@dataclass(frozen=True)
class AuthorizedPredecessor:
    target_name: str
    target_sha256: str
    release_digest: str
    build_digest: str
    deployment_bundle_digest: str


@dataclass(frozen=True)
class PlatformIdentity:
    platform_version: str
    platform_target_name: str
    platform_target_sha256: str
    release_digest: str
    build_digest: str
    deployment_bundle_digest: str
    architecture: str
    control_api_protocol: int
    agent_protocol: int

    def __post_init__(self) -> None:
        _semantic_version(self.platform_version)
        target = _VERSIONED_TARGET.fullmatch(self.platform_target_name)
        if (
            target is None
            or _RAW_DIGEST.fullmatch(self.platform_target_sha256) is None
            or target.group("version") != self.platform_version
            or target.group("sha256") != self.platform_target_sha256
        ):
            raise PlatformReleaseError("platform target identity is invalid")
        _prefixed_digest(self.release_digest, "release digest")
        _prefixed_digest(self.build_digest, "build digest")
        _prefixed_digest(self.deployment_bundle_digest, "deployment bundle digest")
        if self.architecture not in {"linux-arm64", "linux-x86_64"}:
            raise PlatformReleaseError("platform architecture is invalid")
        for value in (self.control_api_protocol, self.agent_protocol):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 65535
            ):
                raise PlatformReleaseError("platform protocol version is invalid")


@dataclass(frozen=True)
class CompatibilityReport:
    compatible: bool
    update_recommended: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlatformRelease:
    platform_version: str
    build_digest: str
    host_updater_abi: ProtocolRange
    deployment_bundle: OciDeploymentBundle
    control: ControlRelease
    database: DatabaseRelease
    agents: tuple[ArchitectureArtifact, ...]
    agent_packages: tuple[DebianPackage, ...]
    supervisors: tuple[ArchitectureArtifact, ...]
    tooling: tuple[ArchitectureArtifact, ...]
    predecessors: tuple[AuthorizedPredecessor, ...]
    digest: str

    @classmethod
    def load(cls, path: Path) -> PlatformRelease:
        try:
            raw = Path(path).read_bytes()
        except OSError as error:
            raise PlatformReleaseError(
                "platform release manifest is unreadable"
            ) from error
        return cls.from_bytes(raw)

    @classmethod
    def from_bytes(cls, raw: bytes) -> PlatformRelease:
        if not isinstance(raw, bytes):
            raise PlatformReleaseError("platform release manifest must be bytes")
        if not raw or len(raw) > _MAX_MANIFEST_BYTES:
            raise PlatformReleaseError("platform release manifest size is invalid")
        try:
            document = json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PlatformReleaseError(
                "platform release manifest is not valid JSON"
            ) from error
        if not isinstance(document, dict):
            raise PlatformReleaseError("platform release manifest must be an object")
        try:
            _validator().validate(document)
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path)
            prefix = f"{location}: " if location else ""
            raise PlatformReleaseError(f"{prefix}{error.message}") from error
        return cls._parse(document)

    @classmethod
    def _parse(cls, document: dict[str, Any]) -> PlatformRelease:
        platform_version = _semantic_version(document["platform_version"])
        control_document = document["control"]
        database_document = document["database"]
        agents = _architecture_artifacts(document["agents"], require_protocol=True)
        supervisors = _architecture_artifacts(
            document["supervisors"], require_protocol=False
        )
        tooling = _architecture_artifacts(document["tooling"], require_protocol=False)
        control_protocol = _protocol(control_document["protocol"])
        if (
            database_document["contract_revision"] is not None
            and not database_document["predecessor_compatible"]
        ):
            raise PlatformReleaseError(
                "contract migration is not predecessor compatible"
            )
        host_updater_abi = _protocol(document["host_updater_abi"])
        deployment_bundle = _deployment_bundle(document["deployment_bundle"])
        predecessors = _predecessors(
            document["rollback"]["predecessors"],
            platform_version=platform_version,
        )
        agent_packages = _debian_packages(
            document.get("agent_packages", []), platform_version=platform_version
        )
        canonical = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        images = control_document["images"]
        return cls(
            platform_version=platform_version,
            build_digest=_prefixed_digest(document["build_digest"], "build digest"),
            host_updater_abi=host_updater_abi,
            deployment_bundle=deployment_bundle,
            control=ControlRelease(
                config_version=control_document["config_version"],
                protocol=control_protocol,
                api_image=_artifact(images["api"]),
                worker_image=_artifact(images["worker"]),
                assets=tuple(_artifact(item) for item in control_document["assets"]),
            ),
            database=DatabaseRelease(
                expand_revision=database_document["expand_revision"],
                contract_revision=database_document["contract_revision"],
                predecessor_compatible=database_document["predecessor_compatible"],
            ),
            agents=agents,
            agent_packages=agent_packages,
            supervisors=supervisors,
            tooling=tooling,
            predecessors=predecessors,
            digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        )

    def agent_for(self, architecture: str) -> ArchitectureArtifact:
        for artifact in self.agents:
            if artifact.architecture == architecture:
                return artifact
        raise PlatformReleaseError("agent architecture is not published")

    def validate_target_identity(self, target_name: str, target_sha256: str) -> None:
        """Bind this parsed release to its independently TUF-verified target bytes."""
        match = (
            _VERSIONED_TARGET.fullmatch(target_name)
            if isinstance(target_name, str)
            else None
        )
        if (
            match is None
            or not isinstance(target_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", target_sha256) is None
            or match.group("version") != self.platform_version
            or match.group("sha256") != target_sha256
        ):
            raise PlatformReleaseError("platform release target identity is invalid")

    def compatibility(self, current: PlatformIdentity) -> CompatibilityReport:
        reasons: list[str] = []
        agent = next(
            (item for item in self.agents if item.architecture == current.architecture),
            None,
        )
        if agent is None:
            reasons.append("architecture-not-published")
        elif agent.protocol is not None and not agent.protocol.contains(
            current.agent_protocol
        ):
            reasons.append("agent-protocol-incompatible")
        if not self.control.protocol.contains(current.control_api_protocol):
            reasons.append("control-protocol-incompatible")
        if current.build_digest != self.build_digest:
            exact_predecessor = next(
                (
                    predecessor
                    for predecessor in self.predecessors
                    if predecessor.target_name == current.platform_target_name
                    and predecessor.target_sha256 == current.platform_target_sha256
                    and predecessor.release_digest == current.release_digest
                    and predecessor.build_digest == current.build_digest
                    and predecessor.deployment_bundle_digest
                    == current.deployment_bundle_digest
                ),
                None,
            )
            if exact_predecessor is None:
                reasons.append("predecessor-not-recovery-compatible")
        if _semantic_tuple(current.platform_version) > _semantic_tuple(
            self.platform_version
        ):
            reasons.append("platform-downgrade-forbidden")
        return CompatibilityReport(
            compatible=not reasons,
            update_recommended=current.build_digest != self.build_digest,
            reasons=tuple(reasons),
        )


def _schema() -> dict[str, Any]:
    try:
        schema = resources.files("cluster_profiles").joinpath(
            "schemas", "platform-update-manifest.schema.json"
        )
        with schema.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as error:
        raise RuntimeError("platform update schema is unavailable") from error
    if not isinstance(value, dict):
        raise TypeError("platform update schema is invalid")
    return value


@lru_cache(maxsize=1)
def _validator() -> Any:
    schema = _schema()
    validator_class = validators.validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(schema)


def _artifact(document: dict[str, Any]) -> Artifact:
    match = _oci_reference(document["reference"], "artifact reference")
    if match.group("sha256") != document["sha256"]:
        raise PlatformReleaseError("artifact reference digest does not match sha256")
    return Artifact(
        name=document["name"],
        reference=document["reference"],
        sha256=document["sha256"],
        size=document["size"],
        sbom_sha256=document["sbom_sha256"],
        provenance_sha256=document["provenance_sha256"],
    )


def _debian_packages(
    documents: list[dict[str, Any]], *, platform_version: str
) -> tuple[DebianPackage, ...]:
    seen: set[str] = set()
    result: list[DebianPackage] = []
    for document in documents:
        architecture = document["architecture"]
        if architecture in seen:
            raise PlatformReleaseError("agent package architectures overlap")
        if document["version"] != platform_version:
            raise PlatformReleaseError("agent package version disagrees with platform")
        if not document["filename"].endswith(".deb"):
            raise PlatformReleaseError("agent package filename is invalid")
        seen.add(architecture)
        result.append(
            DebianPackage(
                architecture=architecture,
                name=document["name"],
                version=document["version"],
                filename=document["filename"],
                sha256=document["sha256"],
                size=document["size"],
                sbom_sha256=document["sbom_sha256"],
                provenance_sha256=document["provenance_sha256"],
                sigstore_bundle_sha256=document["sigstore_bundle_sha256"],
            )
        )
    return tuple(result)


def _deployment_bundle(document: dict[str, Any]) -> OciDeploymentBundle:
    match = _oci_reference(document["reference"], "deployment bundle reference")
    if f"sha256:{match.group('sha256')}" != document["manifest_digest"]:
        raise PlatformReleaseError(
            "deployment bundle reference digest does not match manifest digest"
        )
    return OciDeploymentBundle(
        reference=document["reference"],
        manifest_digest=_prefixed_digest(
            document["manifest_digest"], "deployment bundle manifest digest"
        ),
        manifest_size=document["manifest_size"],
        manifest_media_type=document["manifest_media_type"],
        layer_digest=_prefixed_digest(
            document["layer_digest"], "deployment bundle layer digest"
        ),
        layer_size=document["layer_size"],
        layer_media_type=document["layer_media_type"],
    )


def _predecessors(
    documents: list[dict[str, Any]], *, platform_version: str
) -> tuple[AuthorizedPredecessor, ...]:
    seen_targets: set[str] = set()
    seen_target_digests: set[str] = set()
    result: list[AuthorizedPredecessor] = []
    for document in documents:
        target_name = document["target_name"]
        target_sha256 = document["target_sha256"]
        match = _VERSIONED_TARGET.fullmatch(target_name)
        if (
            match is None
            or match.group("sha256") != target_sha256
            or _semantic_tuple(match.group("version"))
            >= _semantic_tuple(platform_version)
        ):
            raise PlatformReleaseError("predecessor target identity is invalid")
        if target_name in seen_targets or target_sha256 in seen_target_digests:
            raise PlatformReleaseError("predecessor targets overlap")
        seen_targets.add(target_name)
        seen_target_digests.add(target_sha256)
        result.append(
            AuthorizedPredecessor(
                target_name=target_name,
                target_sha256=target_sha256,
                release_digest=_prefixed_digest(
                    document["release_digest"], "predecessor release digest"
                ),
                build_digest=_prefixed_digest(
                    document["build_digest"], "predecessor build digest"
                ),
                deployment_bundle_digest=_prefixed_digest(
                    document["deployment_bundle_digest"],
                    "predecessor deployment bundle digest",
                ),
            )
        )
    return tuple(result)


def _architecture_artifacts(
    documents: list[dict[str, Any]], *, require_protocol: bool
) -> tuple[ArchitectureArtifact, ...]:
    seen: set[str] = set()
    result: list[ArchitectureArtifact] = []
    for document in documents:
        architecture = document["architecture"]
        if architecture in seen:
            raise PlatformReleaseError("architecture entries overlap")
        seen.add(architecture)
        result.append(
            ArchitectureArtifact(
                architecture=architecture,
                artifact=_artifact(document["artifact"]),
                payload_name=document["payload"]["name"],
                payload_sha256=document["payload"]["sha256"],
                payload_size=document["payload"]["size"],
                protocol=_protocol(document["protocol"]) if require_protocol else None,
            )
        )
    return tuple(result)


def _protocol(document: dict[str, int]) -> ProtocolRange:
    value = ProtocolRange(document["minimum"], document["maximum"])
    if value.minimum > value.maximum:
        raise PlatformReleaseError("protocol range is invalid")
    return value


def _semantic_version(value: str) -> str:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        raise PlatformReleaseError("semantic version is invalid")
    return value


def _semantic_tuple(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise PlatformReleaseError("semantic version is invalid")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _prefixed_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise PlatformReleaseError(f"{label} is invalid")
    return value


def _oci_reference(value: object, label: str) -> re.Match[str]:
    match = _OCI_REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if match is None or len(value) > 512:
        raise PlatformReleaseError(f"{label} is invalid")
    port = match.group("port")
    if port is not None and int(port) > 65535:
        raise PlatformReleaseError(f"{label} is invalid")
    return match


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise PlatformReleaseError("platform release contains duplicate fields")
        document[key] = value
    return document
