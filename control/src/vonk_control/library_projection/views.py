"""Library projection: views concerns."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from ..catalog_queries import active_head_revision
from ..library_contract import (
    _MAX_PAGE_RECIPES,
    LibraryFilterValues,
    LibraryModelProjection,
    LibraryRecipeAuthoringDetail,
    LibraryRecipeModel,
    LibraryRecipeProjection,
    ModelDetailResponse,
    ModelLibraryResponse,
    OperationalState,
    RecipeAlternative,
    RecipeDetailResponse,
    RecipeLibraryResponse,
    _utc,
)
from ..model_cache_contract import DIGEST_PATTERN, UUID_PATTERN
from ..models import CatalogDocumentRevision, ModelCacheSet
from ..request_fault import RequestFault
from .common import (
    _LIBRARY_ORDER,
    LibrarySelectorAmbiguous,
    _bounded_library_page,
    _canonical_model,
    _canonical_recipe,
    _canonical_recipe_summary,
    _filter_digest,
    _note_unreadable,
)

if TYPE_CHECKING:
    from .service import LibraryProjection


def models(
    self: LibraryProjection,
    *,
    limit: int = 100,
    cursor: str | None = None,
    usage: Sequence[str] = (),
    family: Sequence[str] = (),
    version: Sequence[str] = (),
    quantization: Sequence[str] = (),
    publisher: Sequence[str] = (),
    alignment: Sequence[str] = (),
    search: str | None = None,
    updated_since: datetime | None = None,
    sort: Literal["updated", "name"] = "updated",
    cached: bool = False,
) -> ModelLibraryResponse:
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
        raise RequestFault("model library limit is invalid")
    if sort not in {"updated", "name"}:
        raise RequestFault("model library sort is invalid")
    snapshot = self._local_state_snapshot()
    model_rows, recipe_rows = self._documents_for_snapshot(snapshot)
    alignment_by_model = self._alignment_by_model(
        document for _, document in recipe_rows
    )
    entries = [
        self._model_projection(
            row,
            model,
            snapshot,
            alignment=sorted(
                alignment_by_model.get(
                    (model.identity.publisher, model.identity.slug), ()
                )
            ),
        )
        for row, model in model_rows
    ]
    if cached:
        entries = [
            item
            for item in entries
            if item.local.controller in {"cached", "preparing"} or item.local.running_on
        ]
    wanted_search = search.casefold() if search else None
    filtered = [
        item
        for item in entries
        if self._matches_any(item.usage, usage)
        and self._matches_any([item.family], family)
        and self._matches_any([item.version], version)
        and self._matches_any([item.quantization], quantization)
        and self._matches_any([item.identity.publisher], publisher)
        and self._matches_any(item.alignment, alignment)
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
                "publisher": list(publisher),
                "alignment": list(alignment),
                "search": search,
                "updated_since": None
                if updated_since is None
                else _utc(updated_since).isoformat(),
                "cached": cached,
            }
        ),
    }
    if cursor is not None:
        boundary = self._cursors.decode(
            cursor, resource="models", order=_LIBRARY_ORDER, context=context
        )
        expected_length = 2 if sort == "updated" else 1
        if not isinstance(boundary, list) or len(boundary) != expected_length:
            boundary = [None] * expected_length
        if sort == "updated":
            boundary_updated_at, boundary_digest = map(str, boundary)
            boundary_items = [
                item
                for item in filtered
                if item.updated_at.strftime("%Y%m%dT%H%M%SZ") == boundary_updated_at
                and item.identity.content_sha256 == boundary_digest
            ]
        else:
            (boundary_digest,) = map(str, boundary)
            boundary_items = [
                item
                for item in filtered
                if item.identity.content_sha256 == boundary_digest
            ]
        boundary_key = key(boundary_items[0]) if boundary_items else None
        filtered = [
            item
            for item in filtered
            if boundary_key is None
            or (
                key(item) < boundary_key
                if sort == "updated"
                else key(item) > boundary_key
            )
        ]
    response = ModelLibraryResponse(
        generated_at=_utc(self._clock()),
        library=self._library_release(),
        models=[],
        facets=self._facet_values(entries),
        next_cursor=None,
        filters=LibraryFilterValues(
            usage=list(usage),
            family=list(family),
            version=list(version),
            quantization=list(quantization),
            publisher=list(publisher),
            alignment=list(alignment),
            search=search,
            updated_since=None
            if updated_since is None
            else _utc(updated_since).isoformat(),
            sort=sort,
            cached=cached,
        ),
        freshness_policy=self._freshness,
    )

    def encode_cursor(item: LibraryModelProjection) -> str:
        item_key = key(item)
        boundary = (
            [item_key[0], item.identity.content_sha256]
            if sort == "updated"
            else [item.identity.content_sha256]
        )
        return self._cursors.encode(
            resource="models",
            order=_LIBRARY_ORDER,
            context=context,
            boundary=boundary,
        )

    page, next_cursor = _bounded_library_page(
        filtered[:limit],
        has_more_after_candidates=len(filtered) > limit,
        collection_field="models",
        empty_response=response,
        encode_cursor=encode_cursor,
    )
    return response.model_copy(update={"models": page, "next_cursor": next_cursor})


def model_detail(self: LibraryProjection, selector: str) -> ModelDetailResponse:
    selected = selector.strip().casefold()
    if not selected:
        raise RequestFault("model selector is required")
    snapshot = self._local_state_snapshot()
    model_rows, recipe_rows = self._documents_for_snapshot(snapshot)
    alignment_by_model = self._alignment_by_model(
        document for _, document in recipe_rows
    )
    entries = [
        self._model_projection(
            row,
            model,
            snapshot,
            alignment=sorted(
                alignment_by_model.get(
                    (model.identity.publisher, model.identity.slug), ()
                )
            ),
        )
        for row, model in model_rows
    ]
    pairs = list(zip((row for row, _ in model_rows), entries, strict=True))
    candidates: list[LibraryModelProjection]
    if re.fullmatch(DIGEST_PATTERN, selected):
        candidates = [
            entry
            for revision, entry in pairs
            if revision.state == "active" and revision.content_digest == selected
        ]
        if not candidates:
            with self._sessions() as session:
                cached = session.scalar(
                    select(ModelCacheSet.model_content_sha256)
                    .where(ModelCacheSet.model_content_sha256 == selected)
                    .limit(1)
                )
            if cached is not None:
                candidates = [
                    entry
                    for revision, entry in pairs
                    if revision.content_digest == selected
                ]
    elif re.fullmatch(UUID_PATTERN, selected):
        candidates = [
            entry
            for revision, entry in pairs
            if revision.state == "active"
            and selected in {revision.id.casefold(), revision.document_id.casefold()}
        ]
    else:
        if "/" in selected:
            publisher, slug = selected.split("/", 1)
            candidates = [
                entry
                for revision, entry in pairs
                if revision.state == "active"
                and revision.publisher.casefold() == publisher
                and revision.slug.casefold() == slug
            ]
        else:
            candidates = [
                entry
                for revision, entry in pairs
                if revision.state == "active" and revision.slug.casefold() == selected
            ]

    if not candidates:
        raise KeyError(selector)
    if len(candidates) > 1:
        if re.fullmatch(DIGEST_PATTERN, selected):
            # A content digest names identical canonical bytes even when
            # the catalog has more than one active reference to them.
            entry = max(candidates, key=lambda item: item.updated_at)
        else:
            raise LibrarySelectorAmbiguous(
                selector,
                sorted({item.identity.content_sha256 for item in candidates}),
            )
    else:
        entry = candidates[0]
    return ModelDetailResponse.model_validate(entry.model_dump(mode="python"))


def recipe_library(
    self: LibraryProjection,
    *,
    limit: int = 100,
    cursor: str | None = None,
    model_selectors: Sequence[str] = (),
    cached: bool = False,
    ready: bool | None = None,
    fits_fleet: bool | None = None,
    assess: bool = True,
    usage: Sequence[str] = (),
    publisher: Sequence[str] = (),
    alignment: Sequence[str] = (),
    sparks: Sequence[int] = (),
    engine: Sequence[str] = (),
    creator: Sequence[str] = (),
    search: str | None = None,
    updated_since: datetime | None = None,
    sort: Literal["updated", "name"] = "updated",
) -> RecipeLibraryResponse:
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
        raise RequestFault("recipe library limit is invalid")
    if sort not in {"updated", "name"}:
        raise RequestFault("recipe library sort is invalid")
    if not assess and (ready is not None or fits_fleet is not None):
        raise RequestFault("readiness filters require assessment")
    deadline = time.monotonic() + self._request_budget
    snapshot = self._local_state_snapshot()
    model_rows, recipe_rows = self._documents_for_snapshot(snapshot)
    model_by_key = {
        (model.identity.publisher, model.identity.slug, row.content_digest): model
        for row, model in model_rows
    }
    alignment_by_model = self._alignment_by_model(
        document for _, document in recipe_rows
    )
    model_entries = [
        self._model_projection(
            row,
            model,
            snapshot,
            alignment=sorted(
                alignment_by_model.get(
                    (model.identity.publisher, model.identity.slug), ()
                )
            ),
        )
        for row, model in model_rows
    ]
    selected_keys: set[tuple[str, str, str]] | None = None
    local_recipe_digests: set[str] = set()
    if model_selectors:
        # Every requested selector must resolve; the union is the scope.
        selected_keys = set()
        for selector in model_selectors:
            selected = self._resolve_selector(
                model_entries,
                selector,
                lambda item: (item.identity.publisher, item.identity.slug),
            )
            assert isinstance(selected, LibraryModelProjection)
            selected_keys.add(
                (
                    selected.identity.publisher,
                    selected.identity.slug,
                    selected.identity.content_sha256,
                )
            )
    if cached:
        cached_keys = {
            (
                item.identity.publisher,
                item.identity.slug,
                item.identity.content_sha256,
            )
            for item in model_entries
            if item.local.controller in {"cached", "preparing"} or item.local.running_on
        }
        if selected_keys is None:
            # A recipe with local state of its own counts as cached too.
            local_recipe_digests = set(snapshot) if snapshot is not None else set()
            selected_keys = cached_keys
        else:
            selected_keys &= cached_keys
    entries = [
        self._recipe_projection(row, recipe, model_by_key, snapshot)
        for row, recipe in recipe_rows
    ]
    wanted_search = search.casefold() if search else None
    filtered = [
        item
        for item in entries
        if (
            selected_keys is None
            or item.identity.content_sha256 in local_recipe_digests
            or any(
                (
                    selection.model.publisher,
                    selection.model.slug,
                    selection.model.content_sha256,
                )
                in selected_keys
                for selection in item.document.models
            )
        )
        and self._matches_any(item.usage, usage)
        and self._matches_any([item.identity.publisher], publisher)
        and self._matches_any([item.alignment] if item.alignment else [], alignment)
        and (not sparks or item.node_count in sparks)
        and self._matches_any([item.engine], engine)
        and self._matches_any([item.creator] if item.creator else [], creator)
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
    readiness_filters = {"readiness": ready, "fleet_fit": fits_fleet}
    filtering_assessment = any(
        value is not None for value in readiness_filters.values()
    )
    if filtering_assessment:
        filtered = self._assessed(filtered, deadline=deadline)
        # Unknown evidence is not a negative filter match. Keep the
        # candidate visible with its typed unavailable assessment so a
        # stale node cannot hide the rest of the library or claim absence.
        filtered = [
            item
            for item in filtered
            if all(
                expected is None
                or item.assessment is None
                or getattr(item.assessment, name).state == "unavailable"
                or (getattr(item.assessment, name).state == "ready") == expected
                for name, expected in readiness_filters.items()
            )
        ]
    context = {
        "l": limit,
        "s": sort,
        "f": _filter_digest(
            {
                "model": list(model_selectors),
                "cached": cached,
                "ready": ready,
                "fits_fleet": fits_fleet,
                "assess": assess,
                "usage": list(usage),
                "publisher": list(publisher),
                "alignment": list(alignment),
                "sparks": list(sparks),
                "engine": list(engine),
                "creator": list(creator),
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
        if not isinstance(boundary, list) or len(boundary) != (
            2 if sort == "updated" else 1
        ):
            boundary = [None] * (2 if sort == "updated" else 1)
        if sort == "updated":
            boundary_updated_at, boundary_digest = map(str, boundary)
            boundary_items = [
                item
                for item in filtered
                if item.updated_at.strftime("%Y%m%dT%H%M%SZ") == boundary_updated_at
                and item.identity.content_sha256 == boundary_digest
            ]
        else:
            (boundary_digest,) = map(str, boundary)
            boundary_items = [
                item
                for item in filtered
                if item.identity.content_sha256 == boundary_digest
            ]
        boundary_key = key(boundary_items[0]) if boundary_items else None
        filtered = [
            item
            for item in filtered
            if boundary_key is None
            or (
                key(item) < boundary_key
                if sort == "updated"
                else key(item) > boundary_key
            )
        ]
    candidates = filtered[:limit]
    if assess and not filtering_assessment:
        candidates = self._assessed(candidates, deadline=deadline)
    response = RecipeLibraryResponse(
        generated_at=_utc(self._clock()),
        library=self._library_release(),
        recipes=[],
        facets=self._recipe_facet_values(model_entries, entries),
        next_cursor=None,
        filters=LibraryFilterValues(
            model=list(model_selectors),
            cached=cached,
            ready=ready,
            fits_fleet=fits_fleet,
            usage=list(usage),
            publisher=list(publisher),
            alignment=list(alignment),
            sparks=list(sparks),
            engine=list(engine),
            creator=list(creator),
            search=search,
            updated_since=None
            if updated_since is None
            else _utc(updated_since).isoformat(),
            sort=sort,
        ),
        freshness_policy=self._freshness,
    )

    def encode_cursor(item: LibraryRecipeProjection) -> str:
        item_key = key(item)
        boundary = (
            [item_key[0], item.identity.content_sha256]
            if sort == "updated"
            else [item.identity.content_sha256]
        )
        return self._cursors.encode(
            resource="recipes",
            order=_LIBRARY_ORDER,
            context=context,
            boundary=boundary,
        )

    page, next_cursor = _bounded_library_page(
        candidates,
        has_more_after_candidates=len(filtered) > len(candidates),
        collection_field="recipes",
        empty_response=response,
        encode_cursor=encode_cursor,
    )
    return response.model_copy(update={"recipes": page, "next_cursor": next_cursor})


def recipe_detail(self: LibraryProjection, selector: str) -> RecipeDetailResponse:
    deadline = time.monotonic() + self._request_budget
    snapshot = self._local_state_snapshot()
    model_rows, recipe_rows = self._documents_for_snapshot(snapshot)
    model_by_key = {
        (model.identity.publisher, model.identity.slug, row.content_digest): model
        for row, model in model_rows
    }
    entries = [
        self._recipe_projection(row, recipe, model_by_key, snapshot)
        for row, recipe in recipe_rows
    ]
    entry = self._resolve_selector(
        entries,
        selector,
        lambda item: (item.identity.publisher, item.identity.slug),
    )
    assert isinstance(entry, LibraryRecipeProjection)
    entry = self._assessed([entry], deadline=deadline)[0]
    recipe = next(
        document
        for row, document in recipe_rows
        if row.content_digest == entry.identity.content_sha256
    )
    siblings = [
        item
        for item in entries
        if item.identity.content_sha256 != entry.identity.content_sha256
        and set(item.model_selectors) & set(entry.model_selectors)
    ]
    alternatives = [
        RecipeAlternative(
            selector=item.selector,
            title=item.identity.title,
            engine=item.engine,
            creator=item.creator,
            node_count=item.node_count,
            version=item.document.release.version,
            cache=item.local.controller,
            fits_fleet=(
                "unavailable"
                if item.assessment is None
                else item.assessment.fleet_fit.state
            ),
        )
        for item in sorted(
            self._assessed(siblings, deadline=deadline),
            key=lambda item: (
                item.node_count,
                item.engine,
                item.creator or "",
                item.selector,
            ),
        )
    ]
    model_documents = []
    for selection in recipe.models:
        model = model_by_key.get(
            (
                selection.model.publisher,
                selection.model.slug,
                selection.model.content_sha256,
            )
        )
        if model is None:
            # The recipe pins a Model document that is not active: leave it
            # out of the detail (the pin stays in ``selection``) instead of
            # refusing the whole recipe.
            _note_unreadable(
                "recipe model document",
                f"{selection.model.publisher}/{selection.model.slug}",
                "no active document matches the pinned content",
            )
            continue
        model_documents.append(
            LibraryRecipeModel(selection=selection, model_document=model)
        )
    return RecipeDetailResponse.model_validate(
        entry.model_dump(mode="python")
        | {
            "model_documents": [
                model.model_dump(mode="json") for model in model_documents
            ],
            "alternatives": [item.model_dump(mode="json") for item in alternatives],
        }
    )


def authoring_recipe_detail(
    self: LibraryProjection, recipe_id: str
) -> LibraryRecipeAuthoringDetail:
    with self._sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.document_id == recipe_id,
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
                active_head_revision(),
            )
        )
        if revision is None:
            raise KeyError(recipe_id)
        recipe_document = _canonical_recipe(revision)
        model_revisions: list[tuple[Any, CatalogDocumentRevision]] = []
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
            for selection in recipe_document.models:
                reference = selection.model
                model_revision = model_by_key.get(
                    (
                        reference.publisher,
                        reference.slug,
                        reference.content_sha256,
                    )
                )
                if model_revision is None:
                    _note_unreadable(
                        "recipe model document",
                        f"{reference.publisher}/{reference.slug}",
                        "no active document matches the pinned content",
                    )
                    continue
                model_revisions.append((selection, model_revision))
    document = recipe_document
    model_documents = [
        LibraryRecipeModel(
            selection=selection,
            model_document=_canonical_model(model_revision),
        )
        for selection, model_revision in model_revisions
    ]
    return LibraryRecipeAuthoringDetail(
        generated_at=_utc(self._clock()),
        recipe=_canonical_recipe_summary(revision, document),
        definition=document,
        topology=document.topology,
        operational_state=OperationalState(
            builds=[], mappings=[], installations=[], runs=[]
        ),
        placement=[],
        reasons=[],
        model_documents=model_documents,
    )
