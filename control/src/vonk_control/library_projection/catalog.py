"""Library projection: catalog concerns."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import defer
from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from ..catalog_queries import active_head_revision
from ..catalog_revision_contract import (
    ModelRevisionProjection,
    RecipeRevisionProjection,
    read_catalog_projection,
)
from ..library_contract import (
    LibraryCacheState,
    LibraryFacetValues,
    LibraryLocalState,
    LibraryModelProjection,
    LibraryRecipeIdentity,
    LibraryRecipeProjection,
    LibraryRelease,
    LibraryResourceProjection,
    _utc,
)
from ..model_cache_contract import CacheManifest, ModelCacheUsage
from ..models import CatalogDocumentRevision, ModelCacheSet, RecipeLibrarySyncRun
from ..operator_receipt_contract import ReceiptOwner
from ..stored_json import read_row_column
from .common import (
    _model_identity,
    _readable_document,
    _recipe_creator,
)

if TYPE_CHECKING:
    from .service import LibraryProjection


def _library_release(self: LibraryProjection) -> LibraryRelease | None:
    with self._sessions() as session:
        run = session.scalar(
            select(RecipeLibrarySyncRun)
            .where(
                RecipeLibrarySyncRun.state == "succeeded",
                RecipeLibrarySyncRun.library_version.is_not(None),
            )
            .order_by(RecipeLibrarySyncRun.completed_at.desc())
            .limit(1)
        )
        if (
            run is None
            or run.library_version is None
            or run.library_updated_at is None
            or run.observed_commit is None
        ):
            return None
        return LibraryRelease(
            version=run.library_version,
            updated_at=_utc(run.library_updated_at),
            commit=run.observed_commit,
        )


def _model_projection(
    self: LibraryProjection,
    revision: CatalogDocumentRevision,
    document: ModelDefinition | ModelRevisionProjection,
    snapshot: Mapping[str, LibraryLocalState] | None,
    alignment: Sequence[str] = (),
    storage: Mapping[str, ModelCacheUsage] | None = None,
) -> LibraryModelProjection:
    usage = storage.get(revision.content_digest) if storage is not None else None
    local = self._local(
        revision.content_digest, kind=RunSwitchReasonScopeName.MODEL, snapshot=snapshot
    )
    if usage is not None:
        if usage.cached:
            local = local.model_copy(
                update={"controller": LibraryCacheState.CACHED.value}
            )
        elif local.controller == LibraryCacheState.CACHED:
            local = local.model_copy(
                update={"controller": LibraryCacheState.NOT_CACHED.value}
            )
    elif storage is not None and local.controller == LibraryCacheState.CACHED:
        local = local.model_copy(update={"controller": LibraryCacheState.UNKNOWN.value})
    return LibraryModelProjection(
        selector=self.selector(document.identity.publisher, document.identity.slug),
        identity=_model_identity(revision, document),
        projected=document
        if isinstance(document, ModelRevisionProjection)
        else cast(ModelRevisionProjection, read_catalog_projection(revision)),
        family=self.selector(
            document.identity.family.publisher, document.identity.family.slug
        ),
        version=document.identity.version,
        variant=document.identity.variant,
        quantization=document.format.quantization,
        usage=self._model_usage(document),
        resources=LibraryResourceProjection(
            disk_bytes=None if usage is None else usage.disk_bytes
        ),
        cache_usage=usage,
        local=local,
        updated_at=_utc(revision.created_at),
        alignment=sorted(set(alignment)),
    )


def _recipe_projection(
    self: LibraryProjection,
    revision: CatalogDocumentRevision,
    document: RecipeDefinition | RecipeRevisionProjection,
    model_by_key: Mapping[str, ModelDefinition | ModelRevisionProjection],
    snapshot: Mapping[str, LibraryLocalState] | None,
) -> LibraryRecipeProjection:
    projected = (
        document
        if isinstance(document, RecipeRevisionProjection)
        else cast(RecipeRevisionProjection, read_catalog_projection(revision))
    )
    model_selectors = [
        self.selector(reference.publisher, reference.slug)
        for reference in projected.models
    ]
    known_models = [
        model_by_key[reference.content_sha256]
        for reference in projected.models
        if reference.content_sha256 in model_by_key
    ]
    usage = sorted(
        {item for model in known_models for item in self._model_usage(model)}
    )
    resources = self._recipe_resources(document)
    return LibraryRecipeProjection(
        selector=self.selector(document.identity.publisher, document.identity.slug),
        identity=LibraryRecipeIdentity(
            recipe_id=revision.document_id,
            recipe_revision_id=revision.id,
            publisher=document.identity.publisher,
            slug=document.identity.slug,
            content_sha256=revision.content_digest,
            title=document.metadata.title,
            description=document.metadata.description,
        ),
        projected=projected.model_copy(
            update={
                "build_resources": None,
                "build_security": None,
                "build_options": None,
                "build_model_artifacts": None,
                "artifact_inputs": None,
                "package_handle": None,
            }
        ),
        prebuilt_image=projected.prebuilt_image,
        model_selectors=model_selectors,
        usage=usage,
        resources=resources,
        local=self._local(
            revision.content_digest, kind=ReceiptOwner.RECIPE, snapshot=snapshot
        ),
        updated_at=_utc(revision.created_at),
        alignment=document.metadata.alignment,
        node_count=document.topology.node_count,
        engine=projected.runtime_engine,
        creator=_recipe_creator(document),
    )


def _catalog_documents[T](
    self,
    *,
    kind: str,
    local_digests: Sequence[str],
    digests: Sequence[str] | None = None,
    reader: Callable[[CatalogDocumentRevision], T],
    projection_only: bool = False,
) -> list[tuple[CatalogDocumentRevision, T]]:
    # Historical recipe revisions remain valid for exact workload reads,
    # but only the accepted head is a discoverable Library choice.
    with self._sessions() as session:
        query = select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.kind == kind,
            and_(
                CatalogDocumentRevision.state == "active",
                active_head_revision(),
            )
            if kind == "recipe"
            else or_(
                CatalogDocumentRevision.state == "active",
                CatalogDocumentRevision.content_digest.in_(local_digests),
                select(ModelCacheSet.artifact_set_sha256)
                .where(
                    ModelCacheSet.model_content_sha256
                    == CatalogDocumentRevision.content_digest
                )
                .exists(),
            ),
        )
        if projection_only:
            query = query.options(defer(CatalogDocumentRevision.document))
        if digests is not None:
            query = query.where(CatalogDocumentRevision.content_digest.in_(digests))
        documents: list[tuple[CatalogDocumentRevision, T]] = []
        for row in session.scalars(query):
            document = _readable_document(row, reader)
            if document is not None:
                documents.append((row, document))
        return documents


def _documents_for_snapshot(
    self: LibraryProjection, snapshot: Mapping[str, LibraryLocalState] | None
) -> tuple[
    list[tuple[CatalogDocumentRevision, ModelRevisionProjection]],
    list[tuple[CatalogDocumentRevision, RecipeRevisionProjection]],
]:
    # The disposable catalog index is refreshed outside request handling. Local
    # history and availability are deliberately absent from this immutable view.
    return self._catalog_snapshot


def _model_revision_projection(row: CatalogDocumentRevision) -> ModelRevisionProjection:
    parsed = read_catalog_projection(row)
    if not isinstance(parsed, ModelRevisionProjection):
        raise TypeError("catalog revision is not a model")
    return parsed


def _recipe_revision_projection(
    row: CatalogDocumentRevision,
) -> RecipeRevisionProjection:
    parsed = read_catalog_projection(row)
    if not isinstance(parsed, RecipeRevisionProjection):
        raise TypeError("catalog revision is not a recipe")
    return parsed


def refresh_catalog(self: LibraryProjection) -> None:
    # Cached bindings keep exact metadata discoverable after its catalog head
    # changes. This background index refresh never advances cache work.
    cached_models: set[str] = set()
    with self._sessions() as session:
        for row in session.scalars(select(ModelCacheSet)):
            manifest = read_row_column(row, "manifest")
            if isinstance(manifest, CacheManifest):
                cached_models.update(
                    reference.content_sha256 for reference in manifest.verified_models
                )
    snapshot = (
        self._catalog_documents(
            kind="model",
            local_digests=tuple(cached_models),
            reader=_model_revision_projection,
            projection_only=True,
        ),
        self._catalog_documents(
            kind=ReceiptOwner.RECIPE,
            local_digests=(),
            reader=_recipe_revision_projection,
            projection_only=True,
        ),
    )
    self._catalog_snapshot = snapshot
    self._catalog_observed_at = self._clock()


def _recipe_facet_values(
    self: LibraryProjection,
    models: Sequence[LibraryModelProjection],
    recipes: Sequence[LibraryRecipeProjection],
) -> LibraryFacetValues:
    """Recipe facets combine the model vocabulary with recipe-only facts."""

    model_facets = self._facet_values(models)
    return LibraryFacetValues(
        usage=model_facets.usage,
        family=model_facets.family,
        version=model_facets.version,
        quantization=model_facets.quantization,
        publisher=sorted(
            {recipe.identity.publisher for recipe in recipes}
            | set(model_facets.publisher)
        ),
        alignment=sorted(
            {recipe.alignment for recipe in recipes if recipe.alignment}
            | set(model_facets.alignment)
        ),
        sparks=sorted({recipe.node_count for recipe in recipes}),
        engine=sorted({recipe.engine for recipe in recipes}, key=str.casefold),
        creator=sorted(
            {recipe.creator for recipe in recipes if recipe.creator},
            key=str.casefold,
        ),
    )
