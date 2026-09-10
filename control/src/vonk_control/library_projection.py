"""Bounded canonical Model to Recipe Library projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
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
    LibraryFacetValues,
    LibraryLocalProgress,
    LibraryLocalState,
    LibraryModel,
    LibraryModelIdentity,
    LibraryModelProjection,
    LibraryRecipeDetail,
    LibraryRecipeIdentity,
    LibraryRecipeList,
    LibraryRecipeModel,
    LibraryRecipeProjection,
    LibraryRecipeSummary,
    LibraryResourceProjection,
    LibrarySnapshot,
    ModelDetailResponse,
    ModelLibraryResponse,
    OperationalState,
    RecipeDetailResponse,
    RecipeLibraryResponse,
    _utc,
)
from .models import CatalogDocumentRevision


class LibraryProjectionError(RuntimeError):
    """The active catalog contains a document outside the public authority."""


class LibrarySelectorAmbiguous(ValueError):
    """A short selector names more than one canonical catalog identity."""

    def __init__(self, selector: str, candidates: Sequence[str]) -> None:
        self.selector = selector
        self.candidates = tuple(candidates)
        super().__init__(f"selector is ambiguous: {selector}")


_MODEL_RESOURCE = "canonical-library-models"
_RECIPE_RESOURCE = "canonical-library-recipes"
_ORDER = "publisher/slug/content-digest-asc"
_LIBRARY_ORDER = "catalog"


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


def _filter_digest(filters: Mapping[str, object]) -> str:
    encoded = json.dumps(
        filters, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        local_state: Callable[[], Mapping[str, Mapping[str, object]]] | None = None,
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
        self._local_state = local_state

    @staticmethod
    def selector(publisher: str, slug: str) -> str:
        return f"{publisher}/{slug}"

    def _local(self, digest: str, *, kind: str) -> LibraryLocalState:
        raw = {} if self._local_state is None else self._local_state().get(digest, {})
        if not isinstance(raw, Mapping):
            raise LibraryProjectionError(f"{kind} local state is not a mapping")
        controller = raw.get("controller", "unknown")
        if controller not in {"cached", "preparing", "not_cached", "failed", "unknown"}:
            raise LibraryProjectionError(f"{kind} local state is invalid")
        running = raw.get("running_on", [])
        if not isinstance(running, list) or not all(isinstance(item, str) for item in running):
            raise LibraryProjectionError(f"{kind} running state is invalid")
        preparation_value = raw.get("preparation")
        preparation = None
        if preparation_value is not None:
            if not isinstance(preparation_value, Mapping):
                raise LibraryProjectionError(f"{kind} preparation state is invalid")
            preparation = LibraryLocalProgress.model_validate(preparation_value)
        return LibraryLocalState(controller=controller, running_on=running, preparation=preparation)

    @staticmethod
    def _model_usage(document: ModelDefinition) -> list[str]:
        return sorted(fact.capability for fact in document.capabilities.facts if fact.support == "supported")

    @staticmethod
    def _recipe_resources(document: RecipeDefinition) -> LibraryResourceProjection:
        roles = document.topology.roles
        memory = max((role.resources.memory.startup_peak_bytes for role in roles), default=None)
        disk = max(
            (
                role.resources.disk.image_bytes
                + role.resources.disk.artifact_bytes
                + role.resources.disk.staging_bytes
                + role.resources.disk.cache_bytes
                + role.resources.disk.rollback_bytes
                + role.resources.disk.safety_margin_bytes
                for role in roles
            ),
            default=None,
        )
        image = max((role.resources.disk.image_bytes for role in roles), default=None)
        return LibraryResourceProjection(
            memory_bytes=memory,
            disk_bytes=disk,
            runtime_memory_bytes=memory,
            image_bytes=image,
        )

    def _model_projection(self, revision: CatalogDocumentRevision, document: ModelDefinition) -> LibraryModelProjection:
        return LibraryModelProjection(
            selector=self.selector(document.identity.publisher, document.identity.slug),
            identity=_model_identity(revision, document),
            document=document,
            family=self.selector(document.identity.family.publisher, document.identity.family.slug),
            version=document.identity.version,
            variant=document.identity.variant,
            quantization=document.format.quantization,
            usage=self._model_usage(document),
            resources=LibraryResourceProjection(disk_bytes=document.download_bytes),
            local=self._local(revision.content_digest, kind="model"),
            updated_at=_utc(revision.created_at),
        )

    def _recipe_projection(
        self,
        revision: CatalogDocumentRevision,
        document: RecipeDefinition,
        model_by_key: Mapping[tuple[str, str, str], ModelDefinition],
    ) -> LibraryRecipeProjection:
        model_selectors = [
            self.selector(selection.model.publisher, selection.model.slug)
            for selection in document.models
        ]
        known_models = [
            model_by_key[key]
            for selection in document.models
            if (key := (selection.model.publisher, selection.model.slug, selection.model.content_sha256)) in model_by_key
        ]
        usage = sorted({item for model in known_models for item in self._model_usage(model)})
        resources = self._recipe_resources(document)
        model_disk = sum(model.download_bytes for model in known_models)
        if model_disk:
            resources = LibraryResourceProjection(
                memory_bytes=resources.memory_bytes,
                disk_bytes=(resources.disk_bytes or 0) + model_disk,
                runtime_memory_bytes=resources.runtime_memory_bytes,
                image_bytes=resources.image_bytes,
            )
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
            local=self._local(revision.content_digest, kind="recipe"),
            updated_at=_utc(revision.created_at),
        )

    def _active_documents(self) -> tuple[list[CatalogDocumentRevision], list[CatalogDocumentRevision]]:
        with self._sessions() as session:
            models = list(session.scalars(select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model", CatalogDocumentRevision.state == "active"
            )))
            recipes = list(session.scalars(select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe", CatalogDocumentRevision.state == "active"
            )))
        return models, recipes

    @staticmethod
    def _matches_any(values: Sequence[str], selected: Sequence[str]) -> bool:
        return not selected or bool({value.casefold() for value in values} & {value.casefold() for value in selected})

    @staticmethod
    def _resolve_selector(items: Sequence[object], selector: str, getter: Callable[[object], tuple[str, str]]) -> object:
        wanted = selector.casefold()
        matches = [item for item in items if wanted in {
            "/".join(getter(item)).casefold(), getter(item)[1].casefold()
        }]
        if not matches:
            raise KeyError(selector)
        if len(matches) > 1:
            raise LibrarySelectorAmbiguous(selector, ["/".join(getter(item)) for item in matches])
        return matches[0]

    @staticmethod
    def _facet_values(models: Sequence[LibraryModelProjection]) -> LibraryFacetValues:
        return LibraryFacetValues(
            usage=sorted({value for model in models for value in model.usage}),
            family=sorted({model.family for model in models}),
            version=sorted({model.version for model in models}),
            quantization=sorted({model.quantization for model in models}),
        )

    def models(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        usage: Sequence[str] = (),
        family: Sequence[str] = (),
        version: Sequence[str] = (),
        quantization: Sequence[str] = (),
        search: str | None = None,
        updated_since: datetime | None = None,
        sort: str = "updated",
        local_only: bool = False,
    ) -> ModelLibraryResponse:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
            raise ValueError("model library limit is invalid")
        if sort not in {"updated", "name"}:
            raise ValueError("model library sort is invalid")
        model_rows, _ = self._active_documents()
        entries = [self._model_projection(row, _canonical_model(row)) for row in model_rows]
        if local_only and self._local_state is not None:
            entries = [
                item for item in entries
                if item.local.controller in {"cached", "preparing"} or item.local.running_on
            ]
        wanted_search = search.casefold() if search else None
        filtered = [
            item for item in entries
            if self._matches_any(item.usage, usage)
            and self._matches_any([item.family], family)
            and self._matches_any([item.version], version)
            and self._matches_any([item.quantization], quantization)
            and (
                wanted_search is None
                or wanted_search in item.selector.casefold()
                or wanted_search in item.document.identity.model.title.casefold()
                or wanted_search in item.document.metadata.description.casefold()
            )
            and (updated_since is None or item.updated_at >= _utc(updated_since))
        ]
        key = self._model_sort_key(sort)
        filtered.sort(key=key, reverse=sort == "updated")
        context = {
            "l": limit,
            "s": sort,
            "f": _filter_digest(
                {
                    "usage": list(usage),
                    "family": list(family),
                    "version": list(version),
                    "quantization": list(quantization),
                    "search": search,
                    "updated_since": None
                    if updated_since is None
                    else _utc(updated_since).isoformat(),
                    "local_only": local_only,
                }
            ),
        }
        if cursor is not None:
            boundary = self._cursors.decode(
                cursor, resource="models", order=_LIBRARY_ORDER, context=context
            )
            expected_length = 3 if sort == "updated" else 2
            if not isinstance(boundary, list) or len(boundary) != expected_length:
                raise ValueError("model library cursor is invalid")
            boundary_key = tuple(str(value) for value in boundary)
            if sort == "updated":
                filtered = [item for item in filtered if key(item) < boundary_key]
            else:
                filtered = [item for item in filtered if key(item) > boundary_key]
        page = filtered[:limit]
        next_cursor = None
        if len(filtered) > limit and page:
            next_cursor = self._cursors.encode(
                resource="models", order=_LIBRARY_ORDER, context=context,
                boundary=list(key(page[-1])),
            )
        return ModelLibraryResponse(
            generated_at=_utc(self._clock()), models=page,
            facets=self._facet_values(entries), next_cursor=next_cursor,
            filters={
                "usage": list(usage), "family": list(family), "version": list(version),
                "quantization": list(quantization), "search": search,
                "updated_since": None if updated_since is None else _utc(updated_since).isoformat(),
                "sort": sort, "local_only": local_only,
            }, freshness_policy=self._freshness,
        )

    @staticmethod
    def _model_sort_key(sort: str) -> Callable[[LibraryModelProjection], tuple[str, ...]]:
        if sort == "updated":
            return lambda item: (
                item.updated_at.strftime("%Y%m%dT%H%M%SZ"),
                item.selector.casefold(),
                item.identity.content_sha256,
            )
        return lambda item: (item.selector.casefold(), item.identity.content_sha256)

    def model_detail(self, selector: str) -> ModelDetailResponse:
        model_rows, _ = self._active_documents()
        entries = [self._model_projection(row, _canonical_model(row)) for row in model_rows]
        entry = self._resolve_selector(
            entries, selector,
            lambda item: (item.identity.publisher, item.identity.slug),
        )
        assert isinstance(entry, LibraryModelProjection)
        return ModelDetailResponse.model_validate(entry.model_dump(mode="python"))

    def recipe_library(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        model_selector: str | None = None,
        all_models: bool = False,
        usage: Sequence[str] = (),
        search: str | None = None,
        updated_since: datetime | None = None,
        sort: str = "updated",
    ) -> RecipeLibraryResponse:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
            raise ValueError("recipe library limit is invalid")
        if sort not in {"updated", "name"}:
            raise ValueError("recipe library sort is invalid")
        model_rows, recipe_rows = self._active_documents()
        models = [_canonical_model(row) for row in model_rows]
        model_by_key = {
            (model.identity.publisher, model.identity.slug, row.content_digest): model
            for row, model in zip(model_rows, models, strict=True)
        }
        model_entries = [
            self._model_projection(row, model)
            for row, model in zip(model_rows, models, strict=True)
        ]
        selected_keys: set[tuple[str, str, str]] | None = None
        if model_selector and not all_models:
            selected = self._resolve_selector(
                model_entries, model_selector,
                lambda item: (item.identity.publisher, item.identity.slug),
            )
            assert isinstance(selected, LibraryModelProjection)
            selected_keys = {(selected.identity.publisher, selected.identity.slug, selected.identity.content_sha256)}
        elif not all_models and self._local_state is not None:
            selected_keys = {
                (item.identity.publisher, item.identity.slug, item.identity.content_sha256)
                for item in model_entries
                if item.local.controller in {"cached", "preparing"} or item.local.running_on
            }
        entries = [
            self._recipe_projection(row, _canonical_recipe(row), model_by_key)
            for row in recipe_rows
        ]
        wanted_search = search.casefold() if search else None
        filtered = [
            item for item in entries
            if (
                selected_keys is None
                or any(
                    (selection.model.publisher, selection.model.slug, selection.model.content_sha256) in selected_keys
                    for selection in item.document.models
                )
            )
            and self._matches_any(item.usage, usage)
            and (
                wanted_search is None
                or wanted_search in item.selector.casefold()
                or wanted_search in item.document.metadata.title.casefold()
                or wanted_search in item.document.metadata.description.casefold()
            )
            and (updated_since is None or item.updated_at >= _utc(updated_since))
        ]
        key = self._recipe_sort_key(sort)
        filtered.sort(key=key, reverse=sort == "updated")
        context = {
            "l": limit,
            "s": sort,
            "f": _filter_digest(
                {
                    "model": model_selector,
                    "all_models": all_models,
                    "usage": list(usage),
                    "search": search,
                    "updated_since": None
                    if updated_since is None
                    else _utc(updated_since).isoformat(),
                }
            ),
        }
        if cursor is not None:
            boundary = self._cursors.decode(
                cursor, resource="recipes", order=_LIBRARY_ORDER, context=context
            )
            if not isinstance(boundary, list) or len(boundary) != 3:
                raise ValueError("recipe library cursor is invalid")
            boundary_key = tuple(str(value) for value in boundary)
            filtered = [item for item in filtered if (key(item) < boundary_key if sort == "updated" else key(item) > boundary_key)]
        page = filtered[:limit]
        next_cursor = None
        if len(filtered) > limit and page:
            next_cursor = self._cursors.encode(
                resource="recipes", order=_LIBRARY_ORDER, context=context,
                boundary=list(key(page[-1])),
            )
        return RecipeLibraryResponse(
            generated_at=_utc(self._clock()), recipes=page,
            facets=self._facet_values(model_entries), next_cursor=next_cursor,
            filters={
                "model": model_selector, "all_models": all_models, "usage": list(usage),
                "search": search,
                "updated_since": None if updated_since is None else _utc(updated_since).isoformat(),
                "sort": sort,
            }, freshness_policy=self._freshness,
        )

    @staticmethod
    def _recipe_sort_key(sort: str) -> Callable[[LibraryRecipeProjection], tuple[str, ...]]:
        if sort == "updated":
            return lambda item: (
                item.updated_at.strftime("%Y%m%dT%H%M%SZ"),
                item.selector.casefold(),
                item.identity.content_sha256,
            )
        return lambda item: (item.selector.casefold(), item.identity.content_sha256, "")

    def recipe_detail(self, selector: str) -> RecipeDetailResponse:
        model_rows, recipe_rows = self._active_documents()
        models = [_canonical_model(row) for row in model_rows]
        model_by_key = {
            (model.identity.publisher, model.identity.slug, row.content_digest): model
            for row, model in zip(model_rows, models, strict=True)
        }
        entries = [
            self._recipe_projection(row, _canonical_recipe(row), model_by_key)
            for row in recipe_rows
        ]
        entry = self._resolve_selector(
            entries, selector,
            lambda item: (item.identity.publisher, item.identity.slug),
        )
        assert isinstance(entry, LibraryRecipeProjection)
        recipe_row = next(row for row in recipe_rows if row.content_digest == entry.identity.content_sha256)
        recipe = _canonical_recipe(recipe_row)
        model_documents = []
        for selection in recipe.models:
            model = model_by_key.get((selection.model.publisher, selection.model.slug, selection.model.content_sha256))
            if model is None:
                raise LibraryProjectionError("active recipe references a missing active Model document")
            model_documents.append(LibraryRecipeModel(selection=selection, model_document=model))
        return RecipeDetailResponse.model_validate(
            entry.model_dump(mode="python")
            | {"model_documents": [model.model_dump(mode="json") for model in model_documents]}
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
