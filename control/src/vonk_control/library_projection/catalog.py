"""Library projection: catalog concerns."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from ..catalog_queries import active_head_revision
from ..library_contract import (
    LibraryFacetValues,
    LibraryLocalState,
    LibraryModelProjection,
    LibraryRecipeIdentity,
    LibraryRecipeProjection,
    LibraryRelease,
    LibraryResourceProjection,
    _utc,
)
from ..models import CatalogDocumentRevision, RecipeLibrarySyncRun
from .common import (
    _canonical_model,
    _canonical_recipe,
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
    document: ModelDefinition,
    snapshot: Mapping[str, LibraryLocalState] | None,
    alignment: Sequence[str] = (),
) -> LibraryModelProjection:
    return LibraryModelProjection(
        selector=self.selector(document.identity.publisher, document.identity.slug),
        identity=_model_identity(revision, document),
        document=document,
        family=self.selector(
            document.identity.family.publisher, document.identity.family.slug
        ),
        version=document.identity.version,
        variant=document.identity.variant,
        quantization=document.format.quantization,
        usage=self._model_usage(document),
        resources=LibraryResourceProjection(disk_bytes=document.download_bytes),
        local=self._local(revision.content_digest, kind="model", snapshot=snapshot),
        updated_at=_utc(revision.created_at),
        alignment=sorted(set(alignment)),
    )


def _recipe_projection(
    self: LibraryProjection,
    revision: CatalogDocumentRevision,
    document: RecipeDefinition,
    model_by_key: Mapping[tuple[str, str, str], ModelDefinition],
    snapshot: Mapping[str, LibraryLocalState] | None,
) -> LibraryRecipeProjection:
    model_selectors = [
        self.selector(selection.model.publisher, selection.model.slug)
        for selection in document.models
    ]
    known_models = [
        model_by_key[key]
        for selection in document.models
        if (
            key := (
                selection.model.publisher,
                selection.model.slug,
                selection.model.content_sha256,
            )
        )
        in model_by_key
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
        document=document,
        model_selectors=model_selectors,
        usage=usage,
        resources=resources,
        local=self._local(revision.content_digest, kind="recipe", snapshot=snapshot),
        updated_at=_utc(revision.created_at),
        alignment=document.metadata.alignment,
        node_count=document.topology.node_count,
        engine=document.runtime.engine,
        creator=_recipe_creator(document),
    )


def _catalog_documents[T: ModelDefinition | RecipeDefinition](
    self,
    *,
    kind: str,
    local_digests: Sequence[str],
    reader: Callable[[CatalogDocumentRevision], T],
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
            ),
        )
        documents: list[tuple[CatalogDocumentRevision, T]] = []
        for row in session.scalars(query):
            document = _readable_document(row, reader)
            if document is not None:
                documents.append((row, document))
        return documents


def _documents_for_snapshot(
    self: LibraryProjection, snapshot: Mapping[str, LibraryLocalState] | None
) -> tuple[
    list[tuple[CatalogDocumentRevision, ModelDefinition]],
    list[tuple[CatalogDocumentRevision, RecipeDefinition]],
]:
    local_digests = tuple(snapshot) if snapshot is not None else ()
    return (
        self._catalog_documents(
            kind="model", local_digests=local_digests, reader=_canonical_model
        ),
        self._catalog_documents(
            kind="recipe", local_digests=local_digests, reader=_canonical_recipe
        ),
    )


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
