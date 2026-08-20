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
class ControlRelease:
    config_version: int
    protocol: ProtocolRange
    api_image: Artifact
    worker_image: Artifact
    assets: tuple[Artifact, ...]


@dataclass(frozen=True)
class DatabaseRelease:
    revision: str


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
class PlatformRelease:
    platform_version: str
    build_digest: str
    deployment_bundle: OciDeploymentBundle
    control: ControlRelease
    database: DatabaseRelease
    agent_packages: tuple[DebianPackage, ...]
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
        control_protocol = _protocol(control_document["protocol"])
        deployment_bundle = _deployment_bundle(document["deployment_bundle"])
        agent_packages = _debian_packages(
            document["agent_packages"], platform_version=platform_version
        )
        canonical = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        images = control_document["images"]
        return cls(
            platform_version=platform_version,
            build_digest=_prefixed_digest(document["build_digest"], "build digest"),
            deployment_bundle=deployment_bundle,
            control=ControlRelease(
                config_version=control_document["config_version"],
                protocol=control_protocol,
                api_image=_artifact(images["api"]),
                worker_image=_artifact(images["worker"]),
                assets=tuple(_artifact(item) for item in control_document["assets"]),
            ),
            database=DatabaseRelease(
                revision=database_document["revision"],
            ),
            agent_packages=agent_packages,
            digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        )

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
    package_architectures = {"linux-amd64": "amd64", "linux-arm64": "arm64"}
    for document in documents:
        architecture = document["architecture"]
        if architecture in seen:
            raise PlatformReleaseError("agent package architectures overlap")
        if document["version"] != platform_version:
            raise PlatformReleaseError("agent package version disagrees with platform")
        expected_filename = (
            f"vonk-forge-agent_{platform_version}_"
            f"{package_architectures[architecture]}.deb"
        )
        if document["name"] != "vonk-forge-agent" or document["filename"] != expected_filename:
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


def _protocol(document: dict[str, int]) -> ProtocolRange:
    value = ProtocolRange(document["minimum"], document["maximum"])
    if value.minimum > value.maximum:
        raise PlatformReleaseError("protocol range is invalid")
    return value


def _semantic_version(value: str) -> str:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        raise PlatformReleaseError("semantic version is invalid")
    return value


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
