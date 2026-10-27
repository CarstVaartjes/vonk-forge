"""The one provider table: what each artifact kind is and how it is fetched."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx2
from vonk_agent_protocol import ModelCacheCode

from .catalog_helpers import (
    _github_lfs_binding,
    _github_lfs_catalog_source,
    _github_release_asset_binding,
    _github_release_catalog_source,
    _huggingface_catalog_source,
)
from .constants import _GITHUB_API_HOST, ProviderKind
from .errors import (
    ModelCacheResolutionError,
    ModelCacheStorageInvalid,
    ModelCacheStorageRefused,
)
from .source_helpers import _is_private_host

if TYPE_CHECKING:
    from .artifacts import ArtifactSpec
    from .input_contracts import CatalogArtifact
    from .service import ModelCacheService


@dataclass(frozen=True, slots=True)
class Provider:
    """What one artifact kind means for binding, validation and transfer."""

    # The persisted source URL and revision a catalog artifact resolves to;
    # None when the kind is a fixture the catalog cannot name.
    catalog_source: Callable[[CatalogArtifact], tuple[str, str | None]] | None
    # Raises when a persisted spec does not bind to its provider identity.
    bind: Callable[[ArtifactSpec], None]
    # Refuses a spec whose source the cache must not fetch.
    validate: Callable[[ModelCacheService, ArtifactSpec], None]
    # Opens one (ranged) response for the spec.
    open: Callable[
        [ModelCacheService, httpx2.Client, ArtifactSpec, Mapping[str, str]],
        httpx2.Response,
    ]
    # Catalog references the production cache accepts without fixture sources.
    production: bool = False
    # A remote kind must pin an immutable revision; a local file has none.
    requires_revision: bool = True
    # A fixture whose repository is itself the source URL.
    source_is_repository: bool = False
    # A file of this kind may be published as split parts.
    parts: bool = False
    # Detail of the typed error a transport failure becomes, if the provider
    # owns one; otherwise the transport error propagates unchanged.
    transport_failure: str | None = None


def _repository_catalog_source(artifact: CatalogArtifact) -> tuple[str, str | None]:
    return artifact.repository, artifact.revision


def _bind_nothing(spec: ArtifactSpec) -> None:
    return None


def _bind_github_release(spec: ArtifactSpec) -> None:
    _github_release_asset_binding(spec)


def _validate_release_download(cache: ModelCacheService, spec: ArtifactSpec) -> None:
    try:
        _github_release_asset_binding(spec)
        parsed = urlsplit(spec.source)
    except (ModelCacheResolutionError, TypeError, ValueError) as error:
        raise ModelCacheStorageInvalid(
            ModelCacheCode.SOURCE_INVALID, "GitHub release asset URL is invalid"
        ) from error
    if parsed.scheme != "https" or parsed.hostname != _GITHUB_API_HOST:
        raise ModelCacheStorageRefused(
            ModelCacheCode.SOURCE_UNTRUSTED,
            "GitHub release downloads must use the canonical GitHub API host",
        )


def _bind_github_lfs(spec: ArtifactSpec) -> None:
    _github_lfs_binding(spec)


def _validate_lfs_download(cache: ModelCacheService, spec: ArtifactSpec) -> None:
    """The locator must be the media URL of the spec's own commit and path."""
    try:
        _github_lfs_binding(spec)
    except ModelCacheResolutionError as error:
        raise ModelCacheStorageInvalid(
            ModelCacheCode.SOURCE_INVALID, "GitHub LFS object locator is invalid"
        ) from error


def _validate_trusted_https(cache: ModelCacheService, spec: ArtifactSpec) -> None:
    try:
        parsed = urlsplit(spec.source)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ModelCacheStorageInvalid(
            ModelCacheCode.SOURCE_INVALID, "cache source URL is invalid"
        ) from error
    if not cache._fixture_sources and (
        parsed.scheme != "https"
        or hostname is None
        or port is not None
        or hostname.lower().rstrip(".") not in cache._trusted_source_hosts
        or _is_private_host(hostname)
    ):
        raise ModelCacheStorageRefused(
            ModelCacheCode.SOURCE_UNTRUSTED,
            "production cache downloads require a trusted HTTPS artifact host",
        )


def _open_http(
    cache: ModelCacheService,
    client: httpx2.Client,
    spec: ArtifactSpec,
    headers: Mapping[str, str],
) -> httpx2.Response:
    return cache._open_http_response(client, spec.source, headers)


def _open_github_release(
    cache: ModelCacheService,
    client: httpx2.Client,
    spec: ArtifactSpec,
    headers: Mapping[str, str],
) -> httpx2.Response:
    return cache._open_github_release_asset(client, spec, headers)


def _open_github_lfs(
    cache: ModelCacheService,
    client: httpx2.Client,
    spec: ArtifactSpec,
    headers: Mapping[str, str],
) -> httpx2.Response:
    return cache._open_github_lfs_object(client, spec, headers)


PROVIDERS: dict[str, Provider] = {
    ProviderKind.HUGGINGFACE_FILE: Provider(
        catalog_source=_huggingface_catalog_source,
        bind=_bind_nothing,
        validate=_validate_trusted_https,
        open=_open_http,
        production=True,
        parts=True,
    ),
    ProviderKind.GITHUB_RELEASE_ASSET: Provider(
        catalog_source=_github_release_catalog_source,
        bind=_bind_github_release,
        validate=_validate_release_download,
        open=_open_github_release,
        production=True,
        transport_failure="GitHub release asset transfer failed",
    ),
    ProviderKind.GITHUB_LFS_OBJECT: Provider(
        catalog_source=_github_lfs_catalog_source,
        bind=_bind_github_lfs,
        validate=_validate_lfs_download,
        open=_open_github_lfs,
        production=True,
        transport_failure="GitHub LFS object transfer failed",
    ),
    ProviderKind.HTTP_FILE: Provider(
        catalog_source=_repository_catalog_source,
        bind=_bind_nothing,
        validate=_validate_trusted_https,
        open=_open_http,
        source_is_repository=True,
    ),
    ProviderKind.FILE: Provider(
        requires_revision=False,
        source_is_repository=True,
        catalog_source=None,
        bind=_bind_nothing,
        validate=_validate_trusted_https,
        open=_open_http,
    ),
}


def provider_for(kind: str) -> Provider:
    """Return the provider of one artifact kind; an unknown kind is invalid."""

    provider = PROVIDERS.get(kind)
    if provider is None:
        raise ModelCacheStorageInvalid(
            ModelCacheCode.ARTIFACT_INVALID, "cache artifact kind is unsupported"
        )
    return provider


def source_is_repository(kind: str) -> bool:
    """Whether a fixture of this kind names its source by repository."""

    provider = PROVIDERS.get(kind)
    return provider is not None and provider.source_is_repository
