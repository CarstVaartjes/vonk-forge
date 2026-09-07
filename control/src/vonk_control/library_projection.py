"""Bounded canonical Model to Recipe Library projection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .auth import CursorCodec
from .library_contract import (
    _MAX_PAGE_RECIPES,
    FreshnessPolicy,
    LibraryCapabilityInventory,
    LibraryModel,
    LibraryModelIdentity,
    LibraryRecipeDetail,
    LibraryRecipeList,
    LibraryRecipeModel,
    LibraryRecipeSummary,
    LibrarySnapshot,
    OperationalState,
    _utc,
)
from .models import CatalogDocumentRevision


class LibraryProjectionError(RuntimeError):
    """The active catalog contains a document outside the public authority."""


_MODEL_RESOURCE = "canonical-library-models"
_RECIPE_RESOURCE = "canonical-library-recipes"
_ORDER = "publisher/slug/content-digest-asc/v1"


def _after_boundary(boundary: object) -> tuple[str, str, str]:
    if (
        not isinstance(boundary, list)
        or len(boundary) != 3
        or not all(isinstance(value, str) for value in boundary)
    ):
        raise ValueError("canonical library cursor is invalid")
    return boundary[0], boundary[1], boundary[2]


def _after_clause(boundary: tuple[str, str, str]):
    publisher, slug, digest = boundary
    return or_(
        CatalogDocumentRevision.publisher > publisher,
        and_(
            CatalogDocumentRevision.publisher == publisher,
            CatalogDocumentRevision.slug > slug,
        ),
        and_(
            CatalogDocumentRevision.publisher == publisher,
            CatalogDocumentRevision.slug == slug,
            CatalogDocumentRevision.content_digest > digest,
        ),
    )


def _canonical_document(
    revision: CatalogDocumentRevision,
    document_type: type[ModelDefinition | RecipeDefinition],
) -> ModelDefinition | RecipeDefinition:
    try:
        document = document_type.model_validate(revision.document)
    except ValidationError as error:
        raise LibraryProjectionError(
            f"active {revision.kind} document is not canonical"
        ) from error
    if content_sha256(document) != revision.content_digest:
        raise LibraryProjectionError(
            f"active {revision.kind} document digest does not match catalog authority"
        )
    return document


def _canonical_model(revision: CatalogDocumentRevision) -> ModelDefinition:
    document = _canonical_document(revision, ModelDefinition)
    assert isinstance(document, ModelDefinition)
    return document


def _canonical_recipe(revision: CatalogDocumentRevision) -> RecipeDefinition:
    document = _canonical_document(revision, RecipeDefinition)
    assert isinstance(document, RecipeDefinition)
    return document


def _model_identity(
    revision: CatalogDocumentRevision, document: ModelDefinition
) -> LibraryModelIdentity:
    return LibraryModelIdentity(
        kind="model",
        publisher=document.identity.publisher,
        slug=document.identity.slug,
        content_sha256=revision.content_digest,
    )


def _canonical_recipe_summary(
    revision: CatalogDocumentRevision,
    document: RecipeDefinition,
) -> LibraryRecipeSummary:
    return LibraryRecipeSummary(
        recipe_id=revision.document_id,
        recipe_revision_id=revision.id,
        publisher=document.identity.publisher,
        slug=document.identity.slug,
        content_sha256=revision.content_digest,
        title=document.metadata.title,
        description=document.metadata.description,
        recipe_document=document,
        capabilities=[],
        topology_name=document.topology.name,
        installations=[],
        installation_total_count=0,
        installation_returned_count=0,
        installations_truncated=False,
        runs=[],
        run_total_count=0,
        run_returned_count=0,
        runs_truncated=False,
        reasons=[],
        recipe_capabilities=LibraryCapabilityInventory(),
    )


class LibraryProjection:
    """Read active canonical Model and Recipe revisions only."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        cursors: CursorCodec,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        inventory_fresh_seconds: int = 300,
        telemetry_live_seconds: int = 6,
        telemetry_delayed_seconds: int = 20,
        **_: object,
    ) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                inventory_fresh_seconds,
                telemetry_live_seconds,
                telemetry_delayed_seconds,
            )
        ):
            raise ValueError("Library freshness windows must be positive integers")
        if telemetry_delayed_seconds < telemetry_live_seconds:
            raise ValueError("Library telemetry freshness windows are invalid")
        self._sessions = sessions
        self._cursors = cursors
        self._clock = clock
        self._freshness = FreshnessPolicy(
            inventory_fresh_seconds=inventory_fresh_seconds,
            telemetry_live_seconds=telemetry_live_seconds,
            telemetry_delayed_seconds=telemetry_delayed_seconds,
        )

    def list(self, *, limit: int = 100, cursor: str | None = None) -> LibrarySnapshot:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
            raise ValueError("library limit is invalid")
        context = {"limit": limit}
        boundary = None
        if cursor is not None:
            try:
                boundary = _after_boundary(
                    self._cursors.decode(
                        cursor,
                        resource=_MODEL_RESOURCE,
                        order=_ORDER,
                        context=context,
                    )
                )
            except (TypeError, ValueError):
                raise ValueError("canonical library cursor is invalid") from None
        with self._sessions() as session:
            model_query = select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.state == "active",
            )
            if boundary is not None:
                model_query = model_query.where(_after_clause(boundary))
            models = list(
                session.scalars(
                    model_query.order_by(
                        CatalogDocumentRevision.publisher,
                        CatalogDocumentRevision.slug,
                        CatalogDocumentRevision.content_digest,
                    ).limit(limit + 1)
                )
            )
            recipes = list(
                session.scalars(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                    )
                    .order_by(
                        CatalogDocumentRevision.publisher,
                        CatalogDocumentRevision.slug,
                        CatalogDocumentRevision.content_digest,
                    )
                )
            )
        has_more = len(models) > limit
        models = models[:limit]
        next_cursor = None
        if has_more:
            last = models[-1]
            next_cursor = self._cursors.encode(
                resource=_MODEL_RESOURCE,
                order=_ORDER,
                context=context,
                boundary=[last.publisher, last.slug, last.content_digest],
            )
        model_documents = {
            revision.id: _canonical_model(revision) for revision in models
        }
        recipe_documents = {
            revision.id: _canonical_recipe(revision) for revision in recipes
        }
        grouped: dict[tuple[str, str, str], list[LibraryRecipeSummary]] = {}
        unlinked: list[LibraryRecipeSummary] = []
        for revision in recipes:
            document = recipe_documents[revision.id]
            summary = _canonical_recipe_summary(revision, document)
            linked = False
            for selection in document.models:
                reference = selection.model
                key = (
                    reference.publisher,
                    reference.slug,
                    reference.content_sha256,
                )
                grouped.setdefault(key, []).append(summary)
                linked = True
            if not linked:
                unlinked.append(summary)
        return LibrarySnapshot(
            generated_at=_utc(self._clock()),
            models=[
                LibraryModel(
                    model=_model_identity(revision, model_documents[revision.id]),
                    model_document=model_documents[revision.id],
                    recipes=grouped.get(
                        (revision.publisher, revision.slug, revision.content_digest), []
                    ),
                )
                for revision in models
            ],
            unlinked_recipes=unlinked,
            next_cursor=next_cursor,
            freshness_policy=self._freshness,
        )

    def detail(self, recipe_id: str) -> LibraryRecipeDetail:
        with self._sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.document_id == recipe_id,
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
            model_revisions: list[CatalogDocumentRevision] = []
            if revision is not None:
                recipe_document = _canonical_recipe(revision)
                references = [selection.model for selection in recipe_document.models]
                if references:
                    active_models = list(
                        session.scalars(
                            select(CatalogDocumentRevision).where(
                                CatalogDocumentRevision.kind == "model",
                                CatalogDocumentRevision.state == "active",
                            )
                        )
                    )
                    model_by_key = {
                        (model.publisher, model.slug, model.content_digest): model
                        for model in active_models
                    }
                    for reference in references:
                        model_revision = model_by_key.get(
                            (
                                reference.publisher,
                                reference.slug,
                                reference.content_sha256,
                            )
                        )
                        if model_revision is None:
                            raise LibraryProjectionError(
                                "active recipe references a missing active Model document"
                            )
                        model_revisions.append(model_revision)
        if revision is None:
            raise KeyError(recipe_id)
        document = recipe_document
        model_documents = [
            LibraryRecipeModel(
                selection=selection,
                model_document=_canonical_model(model_revision),
            )
            for selection, model_revision in zip(
                document.models, model_revisions, strict=True
            )
        ]
        return LibraryRecipeDetail(
            generated_at=_utc(self._clock()),
            recipe=_canonical_recipe_summary(revision, document),
            definition=document,
            topology=document.topology,
            operational_state=OperationalState(builds=[], mappings=[], installations=[], runs=[]),
            placement=[],
            reasons=[],
            model_documents=model_documents,
            model_capabilities=LibraryCapabilityInventory(),
            recipe_capabilities=LibraryCapabilityInventory(),
        )

    def recipes(
        self, *, limit: int = 100, cursor: str | None = None
    ) -> LibraryRecipeList:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
            raise ValueError("library recipe limit is invalid")
        context = {"limit": limit}
        boundary = None
        if cursor is not None:
            try:
                boundary = _after_boundary(
                    self._cursors.decode(
                        cursor,
                        resource=_RECIPE_RESOURCE,
                        order=_ORDER,
                        context=context,
                    )
                )
            except (TypeError, ValueError):
                raise ValueError("canonical library recipe cursor is invalid") from None
        with self._sessions() as session:
            recipe_query = select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
            if boundary is not None:
                recipe_query = recipe_query.where(_after_clause(boundary))
            revisions = list(
                session.scalars(
                    recipe_query.order_by(
                        CatalogDocumentRevision.publisher,
                        CatalogDocumentRevision.slug,
                        CatalogDocumentRevision.content_digest,
                    ).limit(limit + 1)
                )
            )
        has_more = len(revisions) > limit
        revisions = revisions[:limit]
        next_cursor = None
        if has_more:
            last = revisions[-1]
            next_cursor = self._cursors.encode(
                resource=_RECIPE_RESOURCE,
                order=_ORDER,
                context=context,
                boundary=[last.publisher, last.slug, last.content_digest],
            )
        return LibraryRecipeList(
            generated_at=_utc(self._clock()),
            recipes=[
                _canonical_recipe_summary(revision, _canonical_recipe(revision))
                for revision in revisions
            ],
            next_cursor=next_cursor,
            freshness_policy=self._freshness,
        )
