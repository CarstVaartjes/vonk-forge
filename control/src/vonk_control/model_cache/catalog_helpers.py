"""Catalog helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import assert_never

from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    read_model,
    read_recipe,
)
from vonk_forge_contracts.model import (
    GitHubLfsSource,
    GitHubReleaseSource,
    ModelReference,
    ModelSource,
)

from vonk_agent_protocol.reason_codes.model_cache import ModelCacheCode

from ..bounded_json import require_mapping
from ..catalog.revision_contract import read_shared_catalog_document
from ..categorized_errors import InvalidType
from ..models.catalog import CatalogDocumentRevision
from .artifacts import ArtifactSetManifest, _is_hex
from .constants import (
    _DIGEST_LENGTH,
    ProviderKind,
)
from .errors import (
    ModelCacheConflictInvalid,
    ModelCacheResolutionError,
    ModelCacheResolutionInvalid,
)
from .input_contracts import CatalogArtifact, CatalogArtifactPart
from .providers import PROVIDERS


def _source_for_catalog_artifact(
    artifact: CatalogArtifact,
) -> tuple[str, str | None]:
    kind = artifact.kind
    repository = artifact.repository
    path = artifact.path
    revision = artifact.revision
    if not isinstance(repository, str) or not repository:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_INVALID, "catalog artifact repository is invalid"
        )
    if not isinstance(path, str) or not path:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.ARTIFACT_INVALID, "catalog artifact path is invalid"
        )

    provider = PROVIDERS.get(kind)
    if provider is None or provider.catalog_source is None:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SOURCE_UNSUPPORTED,
            "catalog artifact source cannot be downloaded by the NAS cache",
        )
    source, revision = provider.catalog_source(artifact)
    return source, str(revision) if revision is not None else None


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
        definition = read_shared_catalog_document(row)
        if not isinstance(definition, ModelDefinition):
            raise InvalidType("catalog revision is not a model")
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.MODEL_DEFINITION_INVALID,
            "canonical model definition is invalid",
        ) from error
    source = definition.source
    if isinstance(source, GitHubReleaseSource):
        repository = source.repository
        revision = f"github-release:{source.release_id}"
        release_id = source.release_id
        assets = {asset.file_id: asset.asset_id for asset in source.assets}
        provider_kind = ProviderKind.GITHUB_RELEASE_ASSET
    elif isinstance(source, GitHubLfsSource):
        repository = source.repository
        revision = source.revision
        release_id = None
        assets = {}
        provider_kind = ProviderKind.GITHUB_LFS_OBJECT
    elif isinstance(source, ModelSource):
        repository = source.repository
        revision = source.revision
        release_id = None
        assets = {}
        provider_kind = ProviderKind.HUGGINGFACE_FILE
    else:
        assert_never(source)
    result: list[CatalogArtifact] = []
    for value in definition.files:
        result.append(
            CatalogArtifact(
                id=value.id,
                path=value.path,
                kind=provider_kind.value,
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
    """Return where the files come from, the logical model and its representation.

    An update successor is a newer commit or release of the same source
    repository by the same republisher. Other repositories holding the same
    logical model (another republisher, or another bpw of one republisher)
    are siblings, not revisions.
    """

    model = document if isinstance(document, ModelDefinition) else read_model(document)
    identity = model.identity
    variant = identity.variant
    representation = model.format.model_dump(mode="json")
    return (
        (
            identity.publisher,
            model.source.repository,
            identity.model.publisher,
            identity.model.slug,
        ),
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
