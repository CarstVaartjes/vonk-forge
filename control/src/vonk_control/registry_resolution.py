"""SSRF-aware, digest-verifying OCI image identity resolution."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REFERENCE = re.compile(
    r"^(?P<host>[a-z0-9.-]+(?::[0-9]+)?)/(?P<repository>[A-Za-z0-9_./-]+?)(?:(?::(?P<tag>[A-Za-z0-9_.-]{1,128}))|(?:@(?P<digest>sha256:[0-9a-f]{64})))$"
)
_INDEX_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
_MANIFEST_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_MAX_MANIFEST = 2 * 1024 * 1024


class RegistryResolutionError(RuntimeError):
    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        self.code, self.retryable = code, retryable
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ManifestEnvelope:
    body: bytes
    digest: str
    media_type: str
    platform: str | None
    redirect_chain: tuple[str, ...]


class RegistryTransport(Protocol):
    def resolve(self, host: str) -> tuple[str, ...]: ...
    def manifest(
        self, host: str, repository: str, reference: str, *, maximum_bytes: int
    ) -> ManifestEnvelope: ...


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    reference: str
    manifest_digest: str
    platform: str
    compressed_bytes: int
    layer_digests: tuple[str, ...]


def resolve_public_image(reference: str, transport: RegistryTransport) -> ResolvedImage:
    match = _REFERENCE.fullmatch(reference)
    if match is None:
        raise RegistryResolutionError(
            "registry.reference_invalid", "OCI image reference is invalid"
        )
    host, repository = match.group("host"), match.group("repository")
    _public_host(host, transport)
    requested = match.group("digest") or match.group("tag")
    assert requested is not None
    envelope = transport.manifest(
        host, repository, requested, maximum_bytes=_MAX_MANIFEST
    )
    _verify_envelope(envelope, transport)
    if match.group("digest") is not None and envelope.digest != requested:
        raise RegistryResolutionError(
            "registry.digest_mismatch", "OCI reference digest does not match content"
        )
    document = _document(envelope)
    if envelope.media_type in _INDEX_TYPES:
        manifests = document.get("manifests")
        if not isinstance(manifests, list):
            raise RegistryResolutionError(
                "registry.manifest_invalid", "OCI image index is invalid"
            )
        arm = [item for item in manifests if _is_arm64(item)]
        if (
            len(arm) != 1
            or not isinstance(arm[0].get("digest"), str)
            or _DIGEST.fullmatch(arm[0]["digest"]) is None
        ):
            raise RegistryResolutionError(
                "registry.arm64_missing",
                "OCI index must contain exactly one linux/arm64 manifest",
            )
        selected_digest = arm[0]["digest"]
        envelope = transport.manifest(
            host, repository, selected_digest, maximum_bytes=_MAX_MANIFEST
        )
        _verify_envelope(envelope, transport)
        if envelope.digest != selected_digest:
            raise RegistryResolutionError(
                "registry.digest_mismatch", "selected OCI manifest digest changed"
            )
        document = _document(envelope)
    if envelope.media_type not in _MANIFEST_TYPES or envelope.platform != "linux/arm64":
        raise RegistryResolutionError(
            "registry.platform_invalid", "OCI manifest is not verified linux/arm64"
        )
    layers = document.get("layers")
    if not isinstance(layers, list) or len(layers) > 4096:
        raise RegistryResolutionError(
            "registry.manifest_invalid", "OCI layers are invalid"
        )
    digests: list[str] = []
    total = 0
    for layer in layers:
        if (
            not isinstance(layer, dict)
            or _DIGEST.fullmatch(str(layer.get("digest"))) is None
        ):
            raise RegistryResolutionError(
                "registry.manifest_invalid", "OCI layer identity is invalid"
            )
        size = layer.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RegistryResolutionError(
                "registry.manifest_invalid", "OCI layer size is invalid"
            )
        digests.append(layer["digest"])
        total += size
    return ResolvedImage(
        reference=f"{host}/{repository}@{envelope.digest}",
        manifest_digest=envelope.digest,
        platform="linux/arm64",
        compressed_bytes=total,
        layer_digests=tuple(digests),
    )


def _verify_envelope(envelope: ManifestEnvelope, transport: RegistryTransport) -> None:
    if len(envelope.body) > _MAX_MANIFEST or _DIGEST.fullmatch(envelope.digest) is None:
        raise RegistryResolutionError(
            "registry.manifest_invalid", "OCI manifest response is invalid"
        )
    actual = "sha256:" + hashlib.sha256(envelope.body).hexdigest()
    if actual != envelope.digest:
        raise RegistryResolutionError(
            "registry.digest_mismatch", "OCI manifest digest verification failed"
        )
    if len(envelope.redirect_chain) > 3:
        raise RegistryResolutionError(
            "registry.redirect_forbidden", "OCI redirect limit exceeded"
        )
    for location in envelope.redirect_chain:
        parsed = urlparse(location)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RegistryResolutionError(
                "registry.redirect_forbidden", "OCI redirect is not HTTPS"
            )
        _public_host(parsed.hostname, transport)


def _public_host(host_with_port: str, transport: RegistryTransport) -> None:
    host = (
        host_with_port.rsplit(":", 1)[0]
        if ":" in host_with_port and host_with_port.count(":") == 1
        else host_with_port
    )
    try:
        addresses = transport.resolve(host)
    except OSError as error:
        raise RegistryResolutionError(
            "registry.unavailable", "registry DNS resolution failed", retryable=True
        ) from error
    if not addresses:
        raise RegistryResolutionError(
            "registry.unavailable", "registry DNS returned no addresses", retryable=True
        )
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise RegistryResolutionError(
                "registry.destination_forbidden", "registry address is invalid"
            ) from error
        if not address.is_global:
            raise RegistryResolutionError(
                "registry.destination_forbidden", "registry destination is not public"
            )


def _document(envelope: ManifestEnvelope) -> dict[str, object]:
    try:
        value = json.loads(envelope.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RegistryResolutionError(
            "registry.manifest_invalid", "OCI manifest JSON is invalid"
        ) from error
    if not isinstance(value, dict) or value.get("mediaType") != envelope.media_type:
        raise RegistryResolutionError(
            "registry.manifest_invalid", "OCI manifest media type is inconsistent"
        )
    return value


def _is_arm64(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("platform"), dict):
        return False
    platform = value["platform"]
    return (
        platform.get("os") == "linux"
        and platform.get("architecture") == "arm64"
        and platform.get("variant") in {None, "v8"}
    )
