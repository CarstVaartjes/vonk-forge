"""Catalog helpers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import ValidationError
from vonk_agent_protocol import ModelCacheCode
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    read_model,
    read_recipe,
)
from vonk_forge_contracts.model import GitHubReleaseSource, ModelReference

from ..bounded_json import require_mapping
from ..catalog_revision_contract import read_catalog_document
from ..categorized_errors import InvalidType
from ..models import CatalogDocumentRevision
from .artifacts import ArtifactSetManifest, ArtifactSpec, _is_hex
from .constants import _DIGEST_LENGTH, _GITHUB_API_HOST, _MAX_ARTIFACTS
from .errors import (
    ModelCacheConflictInvalid,
    ModelCacheResolutionError,
    ModelCacheResolutionInvalid,
    ModelCacheResolutionRefused,
)
from .input_contracts import CatalogArtifact, CatalogArtifactPart


def _validate_source(source: str) -> None:
    try:
        parsed = urlsplit(source)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "cache source URL is invalid"
        ) from error
    if parsed.scheme in {"http", "https"}:
        if (
            not hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
            or port is not None
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_INVALID, "cache source URL is invalid"
            )
        return
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_INVALID, "cache file source is invalid"
            )
        return
    raise ModelCacheResolutionRefused(
        ModelCacheCode.SOURCE_INVALID, "cache source must use HTTPS, HTTP or file"
    )


def _source_for_catalog_artifact(
    artifact: CatalogArtifact,
) -> tuple[str, str | None]:
    kind = artifact.kind
    repository = artifact.repository
    path = artifact.path
    revision = artifact.revision
    if not isinstance(repository, str) or not repository:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "catalog artifact repository is invalid"
        )
    if not isinstance(path, str) or not path:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.ARTIFACT_INVALID, "catalog artifact path is invalid"
        )
    if kind == "huggingface.file":
        from urllib.parse import quote

        # Catalog repositories are immutable HTTPS URLs.  Parse and bind the
        # repository path to the one trusted Hugging Face authority before
        # constructing the file URL; accepting the raw URL here would permit
        # catalog metadata to redirect the Controller to another host.
        try:
            parsed = urlsplit(repository)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionRefused(
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
                raise ModelCacheResolutionRefused(
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
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_INVALID,
                "catalog Hugging Face repository is invalid",
            )
        if not isinstance(revision, str) or not re.fullmatch(
            r"[0-9a-f]{40,64}", revision
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.REVISION_INVALID,
                "catalog artifact revision is not immutable",
            )
        source = (
            f"https://huggingface.co/{repository_path}/resolve/{revision}/"
            f"{quote(path, safe='/')}"
        )
    elif kind == "http.file":
        source = repository
    elif kind == "github-release.asset":
        release_id = artifact.release_id
        asset_id = artifact.asset_id
        owner_and_name = _github_repository_parts(repository)
        if (
            type(release_id) is not int
            or release_id < 1
            or type(asset_id) is not int
            or asset_id < 1
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.SOURCE_INVALID,
                "catalog GitHub release asset identity is invalid",
            )
        owner, name = owner_and_name
        revision = f"github-release:{release_id}"
        source = f"https://{_GITHUB_API_HOST}/repos/{owner}/{name}/releases/assets/{asset_id}"
    else:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_UNSUPPORTED,
            "catalog artifact source cannot be downloaded by the NAS cache",
        )
    return source, str(revision) if revision is not None else None


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
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "catalog GitHub repository is invalid"
        ) from error
    parts = parsed.path.removeprefix("/").split("/")
    if len(parts) != 2 or parsed.path != f"/{parts[0]}/{parts[1]}":
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "catalog GitHub repository is invalid"
        )
    return parts[0], parts[1]


def _github_release_asset_binding(spec: ArtifactSpec) -> tuple[int, int, str, str]:
    """Validate that a persisted GitHub source binds one exact release asset."""

    if spec.kind != "github-release.asset" or not isinstance(spec.repository, str):
        raise ModelCacheResolutionRefused(
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
        raise ModelCacheResolutionRefused(
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
        raise ModelCacheResolutionRefused(
            ModelCacheCode.SOURCE_INVALID, "GitHub release asset URL is invalid"
        )
    return int(revision_match.group(1)), int(asset_id_text), owner, name


def _datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso_now(value: datetime) -> str:
    """The ISO-8601 UTC spelling of a point in time that is always present."""

    return _datetime(value).astimezone(UTC).isoformat()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else _datetime(value).astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    try:
        return _datetime(datetime.fromisoformat(value))
    except (TypeError, ValueError) as error:
        raise ModelCacheConflictInvalid(
            ModelCacheCode.CURSOR_INVALID, "cache cursor boundary is invalid"
        ) from error


def _recipe_definition(
    document: object,
) -> RecipeDefinition:
    if isinstance(document, RecipeDefinition):
        return document
    try:
        return read_recipe(require_mapping(document, "canonical recipe document"))
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.RECIPE_INVALID, "canonical recipe definition is invalid"
        ) from error


def _recipe_model_content_digests(
    document: RecipeDefinition,
) -> list[str]:
    recipe = _recipe_definition(document)
    raw_models = recipe.models
    if not raw_models:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.RECIPE_MODEL_MISSING,
            "canonical recipe does not declare model selections",
        )
    result: list[str] = []
    for selection in raw_models:
        digest = selection.model.content_sha256
        if (
            not isinstance(digest, str)
            or not _is_hex(digest)
            or len(digest) != _DIGEST_LENGTH
        ):
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MODEL_PIN_INVALID,
                "canonical recipe model pin is invalid",
            )
        if digest not in result:
            result.append(digest)
    if len(result) > _MAX_ARTIFACTS:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.DEPENDENCY_COUNT, "recipe model set is too large"
        )
    return result


def _recipe_model_file_ids(
    document: RecipeDefinition | None, digest: str
) -> set[str] | None:
    if document is None:
        return None
    recipe = _recipe_definition(document)
    selected: set[str] = set()
    found = False
    for selection in recipe.models:
        if selection.model.content_sha256 != digest:
            continue
        found = True
        selected.update(file.file_id for file in selection.files)
    return selected if found else None


def _canonical_model_artifacts(row: CatalogDocumentRevision) -> list[CatalogArtifact]:
    try:
        definition = read_catalog_document(row)
        if not isinstance(definition, ModelDefinition):
            raise InvalidType("catalog revision is not a model")
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.MODEL_DEFINITION_INVALID,
            "canonical model definition is invalid",
        ) from error
    if isinstance(definition.source, GitHubReleaseSource):
        repository = definition.source.repository
        revision = f"github-release:{definition.source.release_id}"
        release_id = definition.source.release_id
        assets = {asset.file_id: asset.asset_id for asset in definition.source.assets}
        provider_kind = "github-release.asset"
    else:
        repository = definition.source.repository
        revision = definition.source.revision
        release_id = None
        assets = {}
        provider_kind = "huggingface.file"
    result: list[CatalogArtifact] = []
    for value in definition.files:
        result.append(
            CatalogArtifact(
                id=value.id,
                path=value.path,
                kind=provider_kind,
                repository=repository,
                revision=revision,
                sha256=value.sha256,
                download_bytes=value.size_bytes,
                roles=list(value.roles),
                release_id=release_id,
                asset_id=assets.get(value.id),
                parts=None
                if value.parts is None
                else [
                    CatalogArtifactPart(
                        path=part.path,
                        sha256=part.sha256,
                        download_bytes=part.size_bytes,
                    )
                    for part in value.parts
                ],
            )
        )
    return result


def _same_model_artifact_identity(
    row: CatalogDocumentRevision, manifest: ArtifactSetManifest
) -> bool:
    """Compare selected file bytes while ignoring revision/editorial facts."""
    try:
        current = {
            item.id: (
                item.path,
                item.sha256,
                item.download_bytes,
            )
            for item in _canonical_model_artifacts(row)
        }
    except (KeyError, TypeError, ValueError, ModelCacheResolutionError):
        return False
    selected = {
        item.artifact_id: (item.path, item.sha256, item.expected_bytes)
        for item in manifest.artifacts
    }
    return bool(selected) and all(
        current.get(key) == identity for key, identity in selected.items()
    )


def _model_lineage_signature(
    document: ModelDefinition,
) -> tuple[object, object, object]:
    """Return the logical model and representation identity from ModelDefinition."""

    model = document if isinstance(document, ModelDefinition) else read_model(document)
    identity = model.identity
    publisher = identity.model.publisher
    slug = identity.model.slug
    variant = identity.variant
    representation = model.format.model_dump(mode="json")
    return (
        (publisher, slug),
        variant,
        json.dumps(representation, sort_keys=True, separators=(",", ":")),
    )


def _revision_identity(row: CatalogDocumentRevision | None) -> ModelReference | None:
    if row is None or not isinstance(row.content_digest, str):
        return None
    return ModelReference(
        publisher=row.publisher,
        slug=row.slug,
        content_sha256=row.content_digest,
    )
