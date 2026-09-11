"""Bounded canonical Model to Recipe Library projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .auth import CursorCodec, CursorError
from .catalog_queries import active_head_revision
from .library_contract import (
    _MAX_PAGE_RECIPES,
    FreshnessPolicy,
    LibraryCapabilityInventory,
    LibraryFacetValues,
    LibraryFilterValues,
    LibraryLocalProgress,
    LibraryLocalState,
    LibraryModelIdentity,
    LibraryModelProjection,
    LibraryRecipeAuthoringDetail,
    LibraryRecipeIdentity,
    LibraryRecipeModel,
    LibraryRecipeProjection,
    LibraryRecipeSummary,
    LibraryResourceProjection,
    ModelDetailResponse,
    ModelLibraryResponse,
    OperationalState,
    RecipeDetailResponse,
    RecipeLibraryResponse,
    _utc,
)
from .models import (
    CatalogDocumentRevision,
    InstallationNode,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .request_fault import RequestFault


class LibraryProjectionError(RuntimeError):
    """The active catalog contains a document outside the public authority."""


class LibrarySelectorAmbiguous(ValueError):
    """A short selector names more than one canonical catalog identity."""

    def __init__(self, selector: str, candidates: Sequence[str]) -> None:
        self.selector = selector
        self.candidates = tuple(candidates)
        super().__init__(f"selector is ambiguous: {selector}")


_LIBRARY_ORDER = "catalog"
_LOCAL_STATE_PRIORITY = {"unknown": 0, "failed": 1, "preparing": 2, "cached": 3}

type LibraryControllerState = Literal[
    "cached", "preparing", "not_cached", "failed", "unknown"
]


def _controller_state(
    states: Mapping[str, LibraryControllerState], value: str, detail: str
) -> LibraryControllerState:
    """Map a persisted state onto the projected controller vocabulary."""

    controller = states.get(value)
    if controller is None:
        raise LibraryProjectionError(detail)
    return controller


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
        self._local_state = local_state or self._database_local_state

    @staticmethod
    def selector(publisher: str, slug: str) -> str:
        return f"{publisher}/{slug}"

    def _local_state_snapshot(self) -> Mapping[str, Mapping[str, object]]:
        snapshot = self._local_state()
        if not isinstance(snapshot, Mapping):
            raise LibraryProjectionError("local state provider did not return a mapping")
        return snapshot

    def _local(
        self,
        digest: str,
        *,
        kind: str,
        snapshot: Mapping[str, Mapping[str, object]],
    ) -> LibraryLocalState:
        raw = snapshot.get(digest, {})
        if not isinstance(raw, Mapping):
            raise LibraryProjectionError(f"{kind} local state is not a mapping")
        candidate = raw.get("controller", "unknown")
        if not isinstance(candidate, str) or candidate not in _LOCAL_STATE_PRIORITY:
            raise LibraryProjectionError(f"{kind} local state is invalid")
        controller = cast(LibraryControllerState, candidate)
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
    def _merge_local(
        result: dict[str, dict[str, object]],
        digest: str | None,
        *,
        controller: LibraryControllerState,
        running_on: Sequence[str] = (),
        preparation: Mapping[str, object] | None = None,
    ) -> None:
        if digest is None:
            return
        current = result.setdefault(
            digest, {"controller": "unknown", "running_on": []}
        )
        if _LOCAL_STATE_PRIORITY[controller] > _LOCAL_STATE_PRIORITY[
            cast(LibraryControllerState, str(current["controller"]))
        ]:
            current["controller"] = controller
        nodes = current["running_on"]
        if not isinstance(nodes, list):
            raise LibraryProjectionError("local state running set is not a list")
        current["running_on"] = sorted(set(nodes) | set(running_on))
        if preparation is not None:
            previous = current.get("preparation")
            previous_operation = (
                str(previous.get("operation_id", ""))
                if isinstance(previous, Mapping)
                else ""
            )
            if previous is None or str(preparation.get("operation_id", "")) >= previous_operation:
                current["preparation"] = dict(preparation)

    @staticmethod
    def _cache_progress(operation: ModelCacheOperation) -> dict[str, object]:
        progress = operation.progress
        if not isinstance(progress, Mapping):
            raise LibraryProjectionError("persisted cache progress is not a mapping")
        measurement = progress.get("measurement", progress)
        if not isinstance(measurement, Mapping):
            raise LibraryProjectionError("persisted cache measurement is not a mapping")
        state = operation.state
        projected_state = {
            "queued": "queued",
            "running": "running",
            "partial": "partial",
            "succeeded": "succeeded",
            "failed": "failed",
            "cancelled": "failed",
        }.get(state)
        if projected_state is None:
            raise LibraryProjectionError("persisted cache operation state is invalid")
        completed = measurement.get("completed_bytes", progress.get("downloaded_bytes", 0))
        total = measurement.get("total_bytes", progress.get("expected_bytes"))
        if type(completed) is not int or completed < 0:
            raise LibraryProjectionError("persisted cache progress bytes are invalid")
        if total is not None and (type(total) is not int or total < 1):
            raise LibraryProjectionError("persisted cache progress total is invalid")
        phase = measurement.get("phase")
        return {
            "operation_id": operation.id,
            "state": projected_state,
            "phase": phase if isinstance(phase, str) else None,
            "completed_bytes": completed,
            "total_bytes": total,
        }

    def _database_local_state(self) -> Mapping[str, Mapping[str, object]]:
        """Read all controller/cache/Spark-local evidence in one bounded batch.

        This is deliberately a projection query, not a per-document callback.  A
        local revision remains visible even when the active catalog pointer has
        moved on; the immutable catalog row supplies its canonical document.
        """
        with self._sessions() as session:
            cache_sets = list(session.scalars(select(ModelCacheSet)))
            cache_operations = list(session.scalars(select(ModelCacheOperation)))
            revisions = list(session.scalars(select(CatalogDocumentRevision)))
            builds = list(session.scalars(select(RecipeBuild)))
            installations = list(session.scalars(select(RecipeInstallation)))
            installation_nodes = list(session.scalars(select(InstallationNode)))
            runs = list(session.scalars(select(RecipeRun)))
            run_nodes = list(session.scalars(select(RunNode)))

        revision_digests = {revision.id: revision.content_digest for revision in revisions}
        result: dict[str, dict[str, object]] = {}
        for cache_set in cache_sets:
            controller = _controller_state(
                {
                    "cached": "cached",
                    "downloading": "preparing",
                    "verifying": "preparing",
                    "incomplete": "preparing",
                    "needs-repair": "failed",
                    "failed": "failed",
                },
                cache_set.state,
                "persisted cache set state is invalid",
            )
            self._merge_local(
                result,
                cache_set.model_content_sha256,
                controller=controller,
            )
            self._merge_local(
                result,
                cache_set.recipe_revision_sha256,
                controller=controller,
            )
        cache_sets_by_id = {
            cache_set.artifact_set_sha256: cache_set for cache_set in cache_sets
        }
        for operation in cache_operations:
            if operation.kind not in {"download", "repair"}:
                continue
            payload = operation.payload
            if not isinstance(payload, Mapping):
                raise LibraryProjectionError("persisted cache payload is not a mapping")
            digest_values = {
                payload.get("model_content_sha256"),
                payload.get("recipe_revision_sha256"),
            }
            if operation.artifact_set_sha256:
                cache_set = cache_sets_by_id.get(operation.artifact_set_sha256)
                if cache_set is not None:
                    digest_values.update(
                        digest
                        for digest in (
                            cache_set.model_content_sha256,
                            cache_set.recipe_revision_sha256,
                        )
                        if digest is not None
                    )
            preparation = self._cache_progress(operation)
            controller = "preparing" if operation.state in {"queued", "running", "partial"} else (
                "cached" if operation.state == "succeeded" else "failed"
            )
            for digest in digest_values:
                if digest is not None and not isinstance(digest, str):
                    raise LibraryProjectionError("persisted cache digest is invalid")
                self._merge_local(
                    result,
                    digest,
                    controller=controller,
                    preparation=preparation,
                )
        for build in builds:
            controller = _controller_state(
                {
                    "planned": "preparing",
                    "building": "preparing",
                    "succeeded": "cached",
                    "failed": "failed",
                },
                build.state,
                "persisted recipe build state is invalid",
            )
            self._merge_local(
                result,
                revision_digests.get(build.recipe_revision_id),
                controller=controller,
            )
        for installation in installations:
            controller = _controller_state(
                {
                    "planned": "preparing",
                    "installing": "preparing",
                    "installed": "cached",
                    "partial": "preparing",
                    "failed": "failed",
                    "uninstalled": "unknown",
                },
                installation.state,
                "persisted installation state is invalid",
            )
            self._merge_local(
                result,
                revision_digests.get(installation.recipe_revision_id),
                controller=controller,
            )
        running_nodes_by_installation: dict[str, list[str]] = {}
        for node in installation_nodes:
            if node.state == "installed":
                running_nodes_by_installation.setdefault(node.installation_id, []).append(node.node_id)
        installations_by_id = {installation.id: installation for installation in installations}
        for run in runs:
            if run.state not in {"planned", "starting", "running", "stopping", "stopped", "failed", "lost"}:
                raise LibraryProjectionError("persisted recipe run state is invalid")
            if run.state in {"planned", "starting", "running", "stopping"}:
                installation = installations_by_id.get(run.installation_id)
                plan = run.plan
                if not isinstance(plan, Mapping):
                    raise LibraryProjectionError("persisted recipe run plan is not a mapping")
                model_digest = plan.get("model_content_sha256")
                if model_digest is not None and not isinstance(model_digest, str):
                    raise LibraryProjectionError("persisted recipe run model digest is invalid")
                run_nodes_for_run = [
                    node
                    for node in run_nodes
                    if node.run_id == run.id and node.state == "running"
                ]
                nodes = [node.node_id for node in run_nodes_for_run]
                if model_digest is not None:
                    self._merge_local(
                        result,
                        model_digest,
                        controller="cached",
                        running_on=nodes,
                    )
                self._merge_local(
                    result,
                    revision_digests.get(installation.recipe_revision_id)
                    if installation is not None
                    else None,
                    controller="cached",
                    running_on=nodes,
                )
        return result

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

    def _model_projection(
        self,
        revision: CatalogDocumentRevision,
        document: ModelDefinition,
        snapshot: Mapping[str, Mapping[str, object]],
        alignment: Sequence[str] = (),
    ) -> LibraryModelProjection:
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
            local=self._local(
                revision.content_digest, kind="model", snapshot=snapshot
            ),
            updated_at=_utc(revision.created_at),
            alignment=sorted(set(alignment)),
        )

    def _recipe_projection(
        self,
        revision: CatalogDocumentRevision,
        document: RecipeDefinition,
        model_by_key: Mapping[tuple[str, str, str], ModelDefinition],
        snapshot: Mapping[str, Mapping[str, object]],
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
            local=self._local(
                revision.content_digest, kind="recipe", snapshot=snapshot
            ),
            updated_at=_utc(revision.created_at),
            alignment=document.metadata.alignment,
            node_count=document.topology.node_count,
        )

    @staticmethod
    def _alignment_by_model(
        recipe_rows: Sequence[CatalogDocumentRevision],
    ) -> dict[tuple[str, str], set[str]]:
        """Map each model publisher/slug to the alignments of recipes serving it."""

        alignments: dict[tuple[str, str], set[str]] = {}
        for row in recipe_rows:
            document = _canonical_recipe(row)
            alignment = document.metadata.alignment
            if alignment is None:
                continue
            for selection in document.models:
                alignments.setdefault(
                    (selection.model.publisher, selection.model.slug), set()
                ).add(alignment)
        return alignments

    def _catalog_documents(
        self,
        *,
        kind: str,
        local_digests: Sequence[str],
    ) -> list[CatalogDocumentRevision]:
        with self._sessions() as session:
            query = select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == kind,
                or_(
                    CatalogDocumentRevision.state == "active",
                    CatalogDocumentRevision.content_digest.in_(local_digests),
                ),
            )
            return list(session.scalars(query))

    def _documents_for_snapshot(
        self, snapshot: Mapping[str, Mapping[str, object]]
    ) -> tuple[list[CatalogDocumentRevision], list[CatalogDocumentRevision]]:
        local_digests = tuple(snapshot)
        return (
            self._catalog_documents(kind="model", local_digests=local_digests),
            self._catalog_documents(kind="recipe", local_digests=local_digests),
        )

    @staticmethod
    def _matches_any(values: Sequence[str], selected: Sequence[str]) -> bool:
        return not selected or bool({value.casefold() for value in values} & {value.casefold() for value in selected})

    @staticmethod
    def _resolve_selector[T](
        items: Sequence[T], selector: str, getter: Callable[[T], tuple[str, str]]
    ) -> T:
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
            publisher=sorted({model.identity.publisher for model in models}),
            alignment=sorted({value for model in models for value in model.alignment}),
        )

    def _recipe_facet_values(
        self,
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
        publisher: Sequence[str] = (),
        alignment: Sequence[str] = (),
        search: str | None = None,
        updated_since: datetime | None = None,
        sort: Literal["updated", "name"] = "updated",
        local_only: bool = False,
    ) -> ModelLibraryResponse:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
            raise RequestFault("model library limit is invalid")
        if sort not in {"updated", "name"}:
            raise RequestFault("model library sort is invalid")
        snapshot = self._local_state_snapshot()
        model_rows, recipe_rows = self._documents_for_snapshot(snapshot)
        alignment_by_model = self._alignment_by_model(recipe_rows)
        entries = [
            self._model_projection(
                row,
                model := _canonical_model(row),
                snapshot,
                alignment=sorted(
                    alignment_by_model.get(
                        (model.identity.publisher, model.identity.slug), ()
                    )
                ),
            )
            for row in model_rows
        ]
        if local_only:
            entries = [
                item
                for item in entries
                if item.local.controller in {"cached", "preparing"}
                or item.local.running_on
            ]
        wanted_search = search.casefold() if search else None
        filtered = [
            item for item in entries
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
                raise CursorError("model library cursor is invalid")
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
            filters=LibraryFilterValues(
                usage=list(usage), family=list(family), version=list(version),
                quantization=list(quantization), publisher=list(publisher),
                alignment=list(alignment), search=search,
                updated_since=None if updated_since is None else _utc(updated_since).isoformat(),
                sort=sort, local_only=local_only,
            ), freshness_policy=self._freshness,
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
        snapshot = self._local_state_snapshot()
        model_rows, recipe_rows = self._documents_for_snapshot(snapshot)
        alignment_by_model = self._alignment_by_model(recipe_rows)
        entries = [
            self._model_projection(
                row,
                model := _canonical_model(row),
                snapshot,
                alignment=sorted(
                    alignment_by_model.get(
                        (model.identity.publisher, model.identity.slug), ()
                    )
                ),
            )
            for row in model_rows
        ]
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
        model_selectors: Sequence[str] = (),
        all_models: bool = False,
        usage: Sequence[str] = (),
        publisher: Sequence[str] = (),
        alignment: Sequence[str] = (),
        sparks: Sequence[int] = (),
        search: str | None = None,
        updated_since: datetime | None = None,
        sort: Literal["updated", "name"] = "updated",
    ) -> RecipeLibraryResponse:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
            raise RequestFault("recipe library limit is invalid")
        if sort not in {"updated", "name"}:
            raise RequestFault("recipe library sort is invalid")
        snapshot = self._local_state_snapshot()
        model_rows, recipe_rows = self._documents_for_snapshot(snapshot)
        models = [_canonical_model(row) for row in model_rows]
        model_by_key = {
            (model.identity.publisher, model.identity.slug, row.content_digest): model
            for row, model in zip(model_rows, models, strict=True)
        }
        alignment_by_model = self._alignment_by_model(recipe_rows)
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
            for row, model in zip(model_rows, models, strict=True)
        ]
        selected_keys: set[tuple[str, str, str]] | None = None
        local_recipe_digests: set[str] = set()
        if model_selectors and not all_models:
            # Every requested selector must resolve; the union is the scope.
            selected_keys = set()
            for selector in model_selectors:
                selected = self._resolve_selector(
                    model_entries, selector,
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
        elif not all_models:
            selected_keys = {
                (item.identity.publisher, item.identity.slug, item.identity.content_sha256)
                for item in model_entries
                if item.local.controller in {"cached", "preparing"} or item.local.running_on
            }
            local_recipe_digests = set(snapshot)
        entries = [
            self._recipe_projection(row, _canonical_recipe(row), model_by_key, snapshot)
            for row in recipe_rows
        ]
        wanted_search = search.casefold() if search else None
        filtered = [
            item for item in entries
            if (
                selected_keys is None
                or item.identity.content_sha256 in local_recipe_digests
                or any(
                    (selection.model.publisher, selection.model.slug, selection.model.content_sha256) in selected_keys
                    for selection in item.document.models
                )
            )
            and self._matches_any(item.usage, usage)
            and self._matches_any([item.identity.publisher], publisher)
            and self._matches_any(
                [item.alignment] if item.alignment else [], alignment
            )
            and (not sparks or item.node_count in sparks)
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
                    "model": list(model_selectors),
                    "all_models": all_models,
                    "usage": list(usage),
                    "publisher": list(publisher),
                    "alignment": list(alignment),
                    "sparks": list(sparks),
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
                raise CursorError("recipe library cursor is invalid")
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
            facets=self._recipe_facet_values(model_entries, entries), next_cursor=next_cursor,
            filters=LibraryFilterValues(
                model=list(model_selectors), all_models=all_models, usage=list(usage),
                publisher=list(publisher), alignment=list(alignment),
                sparks=list(sparks),
                search=search,
                updated_since=None if updated_since is None else _utc(updated_since).isoformat(),
                sort=sort,
            ), freshness_policy=self._freshness,
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
        snapshot = self._local_state_snapshot()
        model_rows, recipe_rows = self._documents_for_snapshot(snapshot)
        models = [_canonical_model(row) for row in model_rows]
        model_by_key = {
            (model.identity.publisher, model.identity.slug, row.content_digest): model
            for row, model in zip(model_rows, models, strict=True)
        }
        entries = [
            self._recipe_projection(row, _canonical_recipe(row), model_by_key, snapshot)
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

    def authoring_recipe_detail(self, recipe_id: str) -> LibraryRecipeAuthoringDetail:
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
            model_revisions: list[CatalogDocumentRevision] = []
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
        return LibraryRecipeAuthoringDetail(
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
