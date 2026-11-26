"""The one provider table: what each artifact kind is and how it is fetched."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, get_args
from urllib.parse import quote, urlsplit

import httpx2
from pydantic import BaseModel, ValidationError
from vonk_forge_contracts.model import (
    GitHubLfsSource,
    GitHubReleaseSource,
    ModelSource,
)

from vonk_agent_protocol.reason_codes.model_cache import ModelCacheCode
from vonk_control.model_cache.constants import (
    _GITHUB_API_HOST,
    _GITHUB_LFS_MEDIA_HOST,
    _REVISION_PATTERN,
    ProviderKind,
)
from vonk_control.model_cache.errors import (
    ModelCacheResolutionError,
    ModelCacheResolutionInvalid,
    ModelCacheStorageInvalid,
    ModelCacheStorageRefused,
)
from vonk_control.model_cache.source_helpers import _is_private_host

if TYPE_CHECKING:
    from vonk_control.model_cache.artifacts import ArtifactSpec
    from vonk_control.model_cache.input_contracts import CatalogArtifact
    from vonk_control.model_cache.service import ModelCacheService


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
    # The contract Model source class that downloads as this kind; None when
    # no Model source does.
    model_source: type[BaseModel] | None = None


def _repository_catalog_source(artifact: CatalogArtifact) -> tuple[str, str | None]:
    return artifact.repository, artifact.revision


def _bind_nothing(_spec: ArtifactSpec) -> None:
    return None


def _bind_github_release(spec: ArtifactSpec) -> None:
    _github_release_asset_binding(spec)


def _validate_release_download(_cache: ModelCacheService, spec: ArtifactSpec) -> None:
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


def _validate_lfs_download(_cache: ModelCacheService, spec: ArtifactSpec) -> None:
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


def _huggingface_catalog_source(artifact: CatalogArtifact) -> tuple[str, str | None]:
    repository = artifact.repository
    path = artifact.path
    revision = artifact.revision
    assert isinstance(repository, str)
    assert isinstance(path, str)
    # Catalog repositories are immutable HTTPS URLs.  Parse and bind the
    # repository path to the one trusted Hugging Face authority before
    # constructing the file URL; accepting the raw URL here would permit
    # catalog metadata to redirect the Controller to another host.
    try:
        parsed = urlsplit(repository)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID,
            "catalog Hugging Face repository is invalid",
        ) from error
    if parsed.scheme:
        if (
            parsed.scheme != "https"
            or hostname is None
            or hostname.lower().rstrip(".") != "huggingface.co"
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.SOURCE_INVALID,
                "catalog Hugging Face repository must use canonical HTTPS",
            )
        repository_path = parsed.path.strip("/")
    else:
        # Older catalog fixtures store the repository as the immutable
        # Hugging Face ``namespace/name`` identifier.  It is safe to
        # normalize that bounded identifier to the canonical authority;
        # arbitrary URI repositories still take the guarded path above.
        repository_path = repository
    if not _valid_repository(repository_path):
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID,
            "catalog Hugging Face repository is invalid",
        )
    if not re.fullmatch(_REVISION_PATTERN, revision):
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.REVISION_INVALID,
            "catalog artifact revision is not immutable",
        )
    source = (
        f"https://huggingface.co/{repository_path}/resolve/{revision}/"
        f"{quote(path, safe='/')}"
    )
    return source, str(revision)


def _github_release_catalog_source(
    artifact: CatalogArtifact,
) -> tuple[str, str | None]:
    repository = artifact.repository
    assert isinstance(repository, str)
    release_id = artifact.release_id
    asset_id = artifact.asset_id
    owner_and_name = _github_repository_parts(repository)
    if (
        type(release_id) is not int
        or release_id < 1
        or type(asset_id) is not int
        or asset_id < 1
    ):
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID,
            "catalog GitHub release asset identity is invalid",
        )
    owner, name = owner_and_name
    revision = f"github-release:{release_id}"
    source = (
        f"https://{_GITHUB_API_HOST}/repos/{owner}/{name}/releases/assets/{asset_id}"
    )
    return source, revision


def _github_lfs_media_url(owner: str, name: str, revision: str, path: str) -> str:
    """The stable, secret-free locator of one LFS object at one commit."""

    return (
        f"https://{_GITHUB_LFS_MEDIA_HOST}/media/{owner}/{name}/{revision}/"
        f"{quote(path, safe='/')}"
    )


def _github_lfs_catalog_source(artifact: CatalogArtifact) -> tuple[str, str | None]:
    owner, name = _github_repository_parts(artifact.repository)
    revision = artifact.revision
    if re.fullmatch(_REVISION_PATTERN, revision) is None:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.REVISION_INVALID,
            "catalog GitHub LFS revision is not a full commit SHA",
        )
    return _github_lfs_media_url(owner, name, revision, artifact.path), revision


def _valid_repository(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}/"
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            value,
        )
    )


def _github_repository_parts(repository: str) -> tuple[str, str]:
    """Validate and split the canonical HTTPS GitHub repository URL."""

    try:
        # Reuse the current published contract's repository validator instead
        # of keeping a second URL policy for persisted artifact locators.
        GitHubReleaseSource.canonical_github_repository(repository)
        parsed = urlsplit(repository)
    except (TypeError, ValueError, ValidationError) as error:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID, "catalog GitHub repository is invalid"
        ) from error
    parts = parsed.path.removeprefix("/").split("/")
    if len(parts) != 2 or parsed.path != f"/{parts[0]}/{parts[1]}":
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID, "catalog GitHub repository is invalid"
        )
    return parts[0], parts[1]


def _github_release_asset_binding(spec: ArtifactSpec) -> tuple[int, int, str, str]:
    """Validate that a persisted GitHub source binds one exact release asset."""

    if not isinstance(spec.repository, str):
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID, "GitHub release asset identity is invalid"
        )
    owner, name = _github_repository_parts(spec.repository)
    revision_match = (
        re.fullmatch(r"github-release:([1-9][0-9]*)", spec.revision)
        if isinstance(spec.revision, str)
        else None
    )
    try:
        parsed = urlsplit(spec.source)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID, "GitHub release asset URL is invalid"
        ) from error
    expected_prefix = f"/repos/{owner}/{name}/releases/assets/"
    asset_id_text = parsed.path.removeprefix(expected_prefix)
    if (
        revision_match is None
        or parsed.scheme != "https"
        or parsed.hostname != _GITHUB_API_HOST
        or parsed.netloc != _GITHUB_API_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(expected_prefix)
        or re.fullmatch(r"[1-9][0-9]*", asset_id_text) is None
        or spec.source != f"https://{_GITHUB_API_HOST}{expected_prefix}{asset_id_text}"
    ):
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID, "GitHub release asset URL is invalid"
        )
    return int(revision_match.group(1)), int(asset_id_text), owner, name


def _github_lfs_binding(spec: ArtifactSpec) -> tuple[str, str, str, str]:
    """Validate that a persisted LFS source is the locator of its own identity.

    The locator is recomputed from the repository, commit and path and must
    match exactly, so a stored row cannot point the opener anywhere else.
    """

    revision = spec.revision
    if (
        not isinstance(spec.repository, str)
        or not isinstance(revision, str)
        or re.fullmatch(_REVISION_PATTERN, revision) is None
    ):
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID, "GitHub LFS object identity is invalid"
        )
    owner, name = _github_repository_parts(spec.repository)
    locator = _github_lfs_media_url(owner, name, revision, spec.path)
    if spec.source != locator:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID, "GitHub LFS object locator is invalid"
        )
    return owner, name, revision, spec.path


# A Model source with no ``provider`` word is a Hugging Face repository.
HUGGINGFACE_SOURCE = "huggingface"

PROVIDERS: dict[str, Provider] = {
    ProviderKind.HUGGINGFACE_FILE: Provider(
        catalog_source=_huggingface_catalog_source,
        bind=_bind_nothing,
        validate=_validate_trusted_https,
        open=_open_http,
        production=True,
        parts=True,
        model_source=ModelSource,
    ),
    ProviderKind.GITHUB_RELEASE_ASSET: Provider(
        catalog_source=_github_release_catalog_source,
        bind=_bind_github_release,
        validate=_validate_release_download,
        open=_open_github_release,
        production=True,
        transport_failure="GitHub release asset transfer failed",
        model_source=GitHubReleaseSource,
    ),
    ProviderKind.GITHUB_LFS_OBJECT: Provider(
        catalog_source=_github_lfs_catalog_source,
        bind=_bind_github_lfs,
        validate=_validate_lfs_download,
        open=_open_github_lfs,
        production=True,
        transport_failure="GitHub LFS object transfer failed",
        model_source=GitHubLfsSource,
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


def source_provider_word(source: type[BaseModel]) -> str:
    """The ``provider`` word a contract Model source class carries.

    Read from the class's ``provider`` Literal; a class without one (a Hugging
    Face repository) is ``HUGGINGFACE_SOURCE``.
    """

    field = source.model_fields.get("provider")
    if field is None:
        return HUGGINGFACE_SOURCE
    (word,) = get_args(field.annotation)
    return str(word)


def model_source_kind(source: type[BaseModel]) -> str | None:
    """The artifact kind a contract Model source class downloads as, by identity.

    None for a class no entry names: the caller refuses it, never guesses.
    """

    return next(
        (
            str(kind)
            for kind, entry in PROVIDERS.items()
            if entry.model_source is source
        ),
        None,
    )


# The Model source provider words this cache downloads;
# scripts/recipes/platform-features publishes them to recipes CI.
MODEL_SOURCE_PROVIDERS = frozenset(
    source_provider_word(entry.model_source)
    for entry in PROVIDERS.values()
    if entry.model_source is not None
)


def source_is_repository(kind: str) -> bool:
    """Whether a fixture of this kind names its source by repository."""

    provider = PROVIDERS.get(kind)
    return provider is not None and provider.source_is_repository
