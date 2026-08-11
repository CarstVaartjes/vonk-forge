"""Typed immutable workload-component fetch providers and source policy."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_PROVIDERS = frozenset(
    {"https", "git", "oci", "huggingface", "python-index", "signed-http-index"}
)


class ProviderError(RuntimeError):
    """A provider could not supply the exact immutable source."""


class SourcePolicyError(ProviderError):
    """A source or observed network route violates local policy."""


class CredentialError(ProviderError):
    """A referenced credential is unavailable or expired."""


@dataclass(frozen=True)
class Validators:
    etag: str | None = None
    last_modified: str | None = None

    def __post_init__(self) -> None:
        for value in (self.etag, self.last_modified):
            if value is not None and (
                not isinstance(value, str)
                or not 1 <= len(value) <= 512
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("fetch validators are invalid")

    @property
    def if_range(self) -> str | None:
        return self.etag or self.last_modified

    def compatible_with(self, other: Validators) -> bool:
        if self.etag is not None:
            return other.etag == self.etag
        if self.last_modified is not None:
            return other.last_modified == self.last_modified
        return True


@dataclass(frozen=True)
class SourceLocation:
    provider: str
    url: str
    immutable_id: str
    allowed_domains: tuple[str, ...]
    credential_ref: str | None = None
    signature_digest: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        try:
            port = parsed.port
        except ValueError as error:
            raise SourcePolicyError("source URL is invalid") from error
        host = parsed.hostname or ""
        normalized = f"https://{host}"
        if port is not None:
            normalized += f":{port}"
        normalized += parsed.path
        if (
            self.provider not in _PROVIDERS
            or parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or self.url != normalized
            or not parsed.path.startswith("/")
        ):
            raise SourcePolicyError("source URL is invalid")
        if (
            not isinstance(self.immutable_id, str)
            or not 1 <= len(self.immutable_id) <= 256
            or any(character.isspace() for character in self.immutable_id)
        ):
            raise SourcePolicyError("source immutable identity is invalid")
        if (
            not isinstance(self.allowed_domains, tuple)
            or not 1 <= len(self.allowed_domains) <= 32
            or len(set(self.allowed_domains)) != len(self.allowed_domains)
            or any(
                not isinstance(domain, str)
                or domain != domain.lower()
                or not 1 <= len(domain) <= 253
                or domain.startswith(".")
                or domain.endswith(".")
                for domain in self.allowed_domains
            )
            or host not in self.allowed_domains
        ):
            raise SourcePolicyError("source domain policy is invalid")
        if self.credential_ref is not None and not _NAME.fullmatch(self.credential_ref):
            raise SourcePolicyError("source credential reference is invalid")
        if (
            self.signature_digest is not None
            and _DIGEST.fullmatch(self.signature_digest) is None
        ):
            raise SourcePolicyError("source signature identity is invalid")

    def __repr__(self) -> str:
        credential = "<configured>" if self.credential_ref else None
        return (
            "SourceLocation("
            f"provider={self.provider!r}, url={self.url!r}, "
            f"immutable_id={self.immutable_id!r}, "
            f"allowed_domains={self.allowed_domains!r}, credential_ref={credential!r}, "
            f"signature_digest={self.signature_digest!r})"
        )


@dataclass(frozen=True)
class ComponentDescriptor:
    name: str
    kind: str
    digest: str
    size: int
    sources: tuple[SourceLocation, ...]
    unpacked_size: int | None = None

    def __post_init__(self) -> None:
        if (
            not _NAME.fullmatch(self.name)
            or not _NAME.fullmatch(self.kind)
            or _DIGEST.fullmatch(self.digest) is None
            or isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or not 1 <= self.size <= 1024**4
        ):
            raise ValueError("component descriptor is invalid")
        if (
            not isinstance(self.sources, tuple)
            or not 1 <= len(self.sources) <= 8
            or any(not isinstance(source, SourceLocation) for source in self.sources)
        ):
            raise ValueError("component sources are invalid")
        if any(
            source.immutable_id != self.digest
            for source in self.sources
            if source.provider in {"https", "oci", "python-index"}
        ):
            raise ValueError("component source digest binding is invalid")
        if self.unpacked_size is not None and (
            isinstance(self.unpacked_size, bool)
            or not isinstance(self.unpacked_size, int)
            or self.unpacked_size < self.size
            or self.unpacked_size > min(4 * self.size, 4 * 1024**4)
        ):
            raise ValueError("component unpacked size is invalid")


@dataclass(frozen=True)
class Credential:
    authorization: str
    expires_at: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authorization, str)
            or not 1 <= len(self.authorization) <= 16 * 1024
            or "\r" in self.authorization
            or "\n" in self.authorization
            or isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
        ):
            raise ValueError("credential is invalid")

    def __repr__(self) -> str:
        return f"Credential(authorization='<redacted>', expires_at={self.expires_at!r})"


@dataclass(frozen=True)
class NetworkHop:
    url: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class FetchResponse:
    status_code: int
    start_offset: int
    total_size: int | None
    validators: Validators
    chunks: Iterable[bytes]
    hops: tuple[NetworkHop, ...]

    def __post_init__(self) -> None:
        if (
            self.status_code not in {200, 206}
            or isinstance(self.start_offset, bool)
            or not isinstance(self.start_offset, int)
            or self.start_offset < 0
            or (
                self.total_size is not None
                and (
                    isinstance(self.total_size, bool)
                    or not isinstance(self.total_size, int)
                    or self.total_size < 1
                )
            )
            or not isinstance(self.validators, Validators)
            or not isinstance(self.hops, tuple)
            or not self.hops
        ):
            raise ValueError("fetch response metadata is invalid")


@dataclass(frozen=True)
class ProviderRequest:
    url: str
    headers: Mapping[str, str]
    authorization: str | None

    def __repr__(self) -> str:
        return (
            f"ProviderRequest(url={self.url!r}, headers={dict(self.headers)!r}, "
            f"authorization={'<redacted>' if self.authorization else None!r})"
        )


class ProviderTransport(Protocol):
    def open(
        self, request: ProviderRequest, deadline: object | None
    ) -> FetchResponse: ...


@dataclass(frozen=True)
class SourcePolicy:
    max_redirects: int = 5
    allow_private_addresses: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_redirects, bool)
            or not isinstance(self.max_redirects, int)
            or not 0 <= self.max_redirects <= 16
        ):
            raise ValueError("source policy is invalid")

    def validate(self, source: SourceLocation, hops: Sequence[NetworkHop]) -> None:
        if not 1 <= len(hops) <= self.max_redirects + 1:
            raise SourcePolicyError("source redirect limit was exceeded")
        for hop in hops:
            parsed = urlsplit(hop.url)
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.hostname not in source.allowed_domains
            ):
                raise SourcePolicyError("source redirect domain is not allowed")
            if not hop.addresses:
                raise SourcePolicyError("source address observation is missing")
            for raw_address in hop.addresses:
                try:
                    address = ipaddress.ip_address(raw_address)
                except ValueError as error:
                    raise SourcePolicyError("source address is invalid") from error
                if not self.allow_private_addresses and (
                    address.is_private
                    or address.is_loopback
                    or address.is_link_local
                    or address.is_multicast
                    or address.is_reserved
                    or address.is_unspecified
                ):
                    raise SourcePolicyError("source address is not allowed")


class FetchProvider(Protocol):
    name: str

    def open(
        self,
        source: SourceLocation,
        offset: int,
        validators: Validators,
        deadline: object | None,
    ) -> FetchResponse: ...


class VerifiedHTTPSProvider:
    name = "https"

    def __init__(
        self,
        transport: ProviderTransport,
        *,
        policy: SourcePolicy,
        credentials: Callable[[str], Credential] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._transport = transport
        self._policy = policy
        self._credentials = credentials
        self._monotonic = monotonic or __import__("time").monotonic

    def open(
        self,
        source: SourceLocation,
        offset: int,
        validators: Validators,
        deadline: object | None,
    ) -> FetchResponse:
        if (
            source.provider != self.name
            or _DIGEST.fullmatch(source.immutable_id) is None
        ):
            raise ProviderError("source immutable identity is invalid")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ProviderError("source range offset is invalid")
        if not isinstance(validators, Validators):
            raise ProviderError("source validators are invalid")
        if deadline is not None and hasattr(deadline, "check"):
            deadline.check()
        authorization: str | None = None
        if source.credential_ref is not None:
            if self._credentials is None:
                raise CredentialError("source credential is unavailable")
            try:
                credential = self._credentials(source.credential_ref)
            except Exception as error:
                raise CredentialError("source credential is unavailable") from error
            if (
                not isinstance(credential, Credential)
                or credential.expires_at <= self._monotonic()
            ):
                raise CredentialError("source credential is unavailable")
            authorization = credential.authorization
        headers: dict[str, str] = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if validators.if_range is not None:
                headers["If-Range"] = validators.if_range
        request = ProviderRequest(source.url, headers, authorization)
        try:
            response = self._transport.open(request, deadline)
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError("source transfer failed") from error
        if not isinstance(response, FetchResponse):
            raise ProviderError("source response is invalid")
        self._policy.validate(source, response.hops)
        if deadline is not None and hasattr(deadline, "check"):
            deadline.check()
        return response


class _TypedProvider:
    name = ""

    def __init__(self, delegate: VerifiedHTTPSProvider) -> None:
        self._delegate = delegate

    def _valid_identity(self, source: SourceLocation) -> bool:
        raise NotImplementedError

    def open(
        self,
        source: SourceLocation,
        offset: int,
        validators: Validators,
        deadline: object | None,
    ) -> FetchResponse:
        if source.provider != self.name or not self._valid_identity(source):
            raise ProviderError("source immutable identity is invalid")
        transport_source = SourceLocation(
            provider="https",
            url=source.url,
            immutable_id=(
                source.immutable_id
                if _DIGEST.fullmatch(source.immutable_id)
                else "sha256:" + "0" * 64
            ),
            allowed_domains=source.allowed_domains,
            credential_ref=source.credential_ref,
            signature_digest=source.signature_digest,
        )
        return self._delegate.open(transport_source, offset, validators, deadline)


class GitSnapshotProvider(_TypedProvider):
    name = "git"

    def _valid_identity(self, source: SourceLocation) -> bool:
        return (
            _HEX_40.fullmatch(source.immutable_id) is not None
            or _HEX_64.fullmatch(source.immutable_id) is not None
        )


class OCIFetchProvider(_TypedProvider):
    name = "oci"

    def _valid_identity(self, source: SourceLocation) -> bool:
        return _DIGEST.fullmatch(source.immutable_id) is not None


class HuggingFaceProvider(_TypedProvider):
    name = "huggingface"

    def _valid_identity(self, source: SourceLocation) -> bool:
        return _HEX_40.fullmatch(source.immutable_id) is not None


class PythonArtifactProvider(_TypedProvider):
    name = "python-index"

    def _valid_identity(self, source: SourceLocation) -> bool:
        return _DIGEST.fullmatch(source.immutable_id) is not None


class SignedIndexProvider(_TypedProvider):
    name = "signed-http-index"

    def _valid_identity(self, source: SourceLocation) -> bool:
        return (
            _DIGEST.fullmatch(source.immutable_id) is not None
            and source.signature_digest is not None
        )


class ProviderRegistry:
    def __init__(self, providers: Sequence[FetchProvider]) -> None:
        by_name = {provider.name: provider for provider in providers}
        if len(by_name) != len(providers) or not by_name:
            raise ValueError("provider registry is invalid")
        self._providers = by_name

    def provider(self, name: str) -> FetchProvider:
        try:
            return self._providers[name]
        except KeyError as error:
            raise ProviderError("source provider is unsupported") from error
