"""Bounded canonical Model to Recipe Library projection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .auth import CursorCodec, CursorError
from .catalog_queries import active_head_revision
from .library_assessment import unassessed
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
from .model_cache_contract import DIGEST_PATTERN, UUID_PATTERN
from .models import (
    CatalogDocumentRevision,
    InstallationNode,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
    RuntimeImageAuthorization,
)
from .request_fault import RequestFault


class LibraryProjectionError(RuntimeError):
    """The active catalog contains a document outside the public authority."""


class LibraryAssessmentUnavailable(RuntimeError):
    """A requested readiness filter could hide candidates with unknown evidence."""


class LibrarySelectorAmbiguous(ValueError):
    """A short selector names more than one canonical catalog identity."""

    def __init__(self, selector: str, candidates: Sequence[str]) -> None:
        self.selector = selector
        self.candidates = tuple(candidates)
        super().__init__(f"selector is ambiguous: {selector}")


_LIBRARY_ORDER = "catalog"
_LOCAL_STATE_PRIORITY = {
    "unknown": 0,
    "not_cached": 0,
    "failed": 1,
    "preparing": 2,
    "cached": 3,
}

type LibraryControllerState = Literal[
    "cached", "preparing", "not_cached", "failed", "unknown"
]


def _wire_json_bytes(value: object) -> bytes:
    """Serialize as the compact UTF-8 JSON response used by Starlette."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_library_page[T: BaseModel](
    candidates: Sequence[T],
    *,
    has_more_after_candidates: bool,
    collection_field: Literal["models", "recipes"],
    empty_response: ModelLibraryResponse | RecipeLibraryResponse,
    encode_cursor: Callable[[T], str],
) -> tuple[list[T], str | None]:
    """Choose the largest contiguous page that fits the shared wire budget.

    The empty response includes the full filter/facet envelope. Item documents
    are encoded once and added by byte length, including the exact continuation
    token for each possible boundary. The caller's requested limit remains in
    the cursor context; this function only shortens a page when its JSON bytes
    require it.
    """

    envelope = empty_response.model_dump(mode="json")
    empty_items = envelope.get(collection_field)
    if empty_items != [] or envelope.get("next_cursor") is not None:
        raise AssertionError("byte page sizing requires an empty response envelope")
    envelope_bytes = len(_wire_json_bytes(envelope))
    if envelope_bytes > MAX_CONTROL_DOCUMENT_BYTES:
        raise RequestFault(
            "library response envelope requires "
            f"{envelope_bytes} bytes before entries; document limit is "
            f"{MAX_CONTROL_DOCUMENT_BYTES} bytes; narrow the library filters"
        )
    if not candidates:
        return [], None

    item_sizes = [
        len(_wire_json_bytes(item.model_dump(mode="json"))) for item in candidates
    ]
    prefix_sizes = [0]
    for item_size in item_sizes:
        prefix_sizes.append(prefix_sizes[-1] + item_size)

    selected_count = 0
    selected_cursor: str | None = None
    first_item_response_bytes: int | None = None
    first_cursor_too_large = False
    for count in range(len(candidates), 0, -1):
        needs_cursor = count < len(candidates) or has_more_after_candidates
        try:
            cursor = encode_cursor(candidates[count - 1]) if needs_cursor else None
        except ValueError as error:
            if str(error) != "cursor document is too large":
                raise
            if count == 1:
                first_cursor_too_large = True
            # This boundary cannot be issued to a caller. A later contiguous
            # boundary can still be representable, so keep scanning prefixes.
            continue
        cursor_bytes = len(_wire_json_bytes(cursor)) if cursor is not None else 4
        response_bytes = (
            envelope_bytes
            + prefix_sizes[count]
            + count
            - 1  # Commas between entries.
            + cursor_bytes
            - 4  # Replace the serialized null cursor.
        )
        if count == 1:
            first_item_response_bytes = response_bytes
        if response_bytes <= MAX_CONTROL_DOCUMENT_BYTES:
            selected_count = count
            selected_cursor = cursor
            break

    if selected_count == 0:
        if first_cursor_too_large:
            raise RequestFault(
                "the first matching library entry cannot be continued because "
                "its signed cursor boundary exceeds the Controller cursor limit; "
                "narrow the library filters"
            )
        assert first_item_response_bytes is not None
        raise RequestFault(
            "the first matching library entry requires "
            f"{first_item_response_bytes} bytes; document limit is "
            f"{MAX_CONTROL_DOCUMENT_BYTES} bytes; narrow filters to exclude it"
        )
    return list(candidates[:selected_count]), selected_cursor


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
        runtime_archive_available: Callable[[str, int], bool] | None = None,
        assessment: Callable[
            [Sequence[LibraryRecipeProjection]], list[LibraryRecipeProjection]
        ]
        | None = None,
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
        self._runtime_archive_available = runtime_archive_available
        self._assessment = assessment

    def _assessed(
        self, recipes: Sequence[LibraryRecipeProjection]
    ) -> list[LibraryRecipeProjection]:
        if self._assessment is not None:
            return self._assessment(recipes)
        return [
            item.model_copy(
                update={
                    "assessment": unassessed(
                        self._clock,
                        "The Controller placement and cache assessment is unavailable.",
                    )
                }
            )
            for item in recipes
        ]

    @staticmethod
    def selector(publisher: str, slug: str) -> str:
        return f"{publisher}/{slug}"

    def _local_state_snapshot(self) -> Mapping[str, Mapping[str, object]]:
        snapshot = self._local_state()
        if not isinstance(snapshot, Mapping):
            raise LibraryProjectionError(
                "local state provider did not return a mapping"
            )
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
        if not isinstance(running, list) or not all(
            isinstance(item, str) for item in running
        ):
            raise LibraryProjectionError(f"{kind} running state is invalid")
        preparation_value = raw.get("preparation")
        preparation = None
        if preparation_value is not None:
            if not isinstance(preparation_value, Mapping):
                raise LibraryProjectionError(f"{kind} preparation state is invalid")
            preparation = LibraryLocalProgress.model_validate(preparation_value)
        return LibraryLocalState(
            controller=controller, running_on=running, preparation=preparation
        )

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
        current = result.setdefault(digest, {"controller": "unknown", "running_on": []})
        if (
            _LOCAL_STATE_PRIORITY[controller]
            > _LOCAL_STATE_PRIORITY[
                cast(LibraryControllerState, str(current["controller"]))
            ]
        ):
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
            if (
                previous is None
                or str(preparation.get("operation_id", "")) >= previous_operation
            ):
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
        completed = measurement.get(
            "completed_bytes", progress.get("downloaded_bytes", 0)
        )
        total = measurement.get("total_bytes", progress.get("expected_bytes"))
        if type(completed) is not int or completed < 0:
            raise LibraryProjectionError("persisted cache progress bytes are invalid")
        if total is not None and (type(total) is not int or total < 0):
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
            runtime_authorizations = list(
                session.scalars(
                    select(RuntimeImageAuthorization).where(
                        RuntimeImageAuthorization.state == "authorized"
                    )
                )
            )

        revision_digests = {
            revision.id: revision.content_digest for revision in revisions
        }
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
            controller = (
                "preparing"
                if operation.state in {"queued", "running", "partial"}
                else ("cached" if operation.state == "succeeded" else "failed")
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
                running_nodes_by_installation.setdefault(
                    node.installation_id, []
                ).append(node.node_id)
        installations_by_id = {
            installation.id: installation for installation in installations
        }
        for run in runs:
            if run.state not in {
                "planned",
                "starting",
                "running",
                "stopping",
                "stopped",
                "failed",
                "lost",
            }:
                raise LibraryProjectionError("persisted recipe run state is invalid")
            if run.state in {"planned", "starting", "running", "stopping"}:
                installation = installations_by_id.get(run.installation_id)
                plan = run.plan
                if not isinstance(plan, Mapping):
                    raise LibraryProjectionError(
                        "persisted recipe run plan is not a mapping"
                    )
                model_digest = plan.get("model_content_sha256")
                if model_digest is not None and not isinstance(model_digest, str):
                    raise LibraryProjectionError(
                        "persisted recipe run model digest is invalid"
                    )
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
        if self._runtime_archive_available is not None:
            available_recipe_digests: set[str] = set()
            for build in builds:
                digest = revision_digests.get(build.recipe_revision_id)
                if (
                    build.state == "succeeded"
                    and isinstance(build.oci_layout_sha256, str)
                    and type(build.image_bytes) is int
                    and self._runtime_archive_available(
                        build.oci_layout_sha256, build.image_bytes
                    )
                    and digest is not None
                ):
                    available_recipe_digests.add(digest)
            for authorization in runtime_authorizations:
                if (
                    isinstance(authorization.oci_archive_sha256, str)
                    and type(authorization.image_bytes) is int
                    and self._runtime_archive_available(
                        authorization.oci_archive_sha256, authorization.image_bytes
                    )
                ):
                    available_recipe_digests.add(authorization.original_content_digest)
            for revision in revisions:
                if revision.kind != "recipe":
                    continue
                local = result.get(revision.content_digest)
                if (
                    local is not None
                    and local.get("controller") == "cached"
                    and revision.content_digest not in available_recipe_digests
                ):
                    local["controller"] = "not_cached"
        return result

    @staticmethod
    def _model_usage(document: ModelDefinition) -> list[str]:
        return sorted(
            fact.capability
            for fact in document.capabilities.facts
            if fact.support == "supported"
        )

    @staticmethod
    def _recipe_resources(document: RecipeDefinition) -> LibraryResourceProjection:
        roles = document.topology.roles
        memory = max(
            (role.resources.memory.startup_peak_bytes for role in roles), default=None
        )
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
        return not selected or bool(
            {value.casefold() for value in values}
            & {value.casefold() for value in selected}
        )

    @staticmethod
    def _resolve_selector[T](
        items: Sequence[T], selector: str, getter: Callable[[T], tuple[str, str]]
    ) -> T:
        wanted = selector.casefold()
        matches = [
            item
            for item in items
            if wanted in {"/".join(getter(item)).casefold(), getter(item)[1].casefold()}
        ]
        if not matches:
            raise KeyError(selector)
        if len(matches) > 1:
            raise LibrarySelectorAmbiguous(
                selector, ["/".join(getter(item)) for item in matches]
            )
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
                    "local_only": local_only,
                    # Bind a continuation to the same matching identities and
                    # immutable documents. A changed catalog restarts the read;
                    # the Controller does not retain another snapshot store.
                    "collection": [
                        (
                            item.selector,
                            item.identity.content_sha256,
                            item.updated_at.isoformat(),
                        )
                        for item in filtered
                    ],
                }
            ),
        }
        if cursor is not None:
            try:
                boundary = self._cursors.decode(
                    cursor, resource="models", order=_LIBRARY_ORDER, context=context
                )
            except CursorError:
                raise CursorError(
                    "library cursor is invalid or the selection changed; restart without a cursor"
                ) from None
            expected_length = 2 if sort == "updated" else 1
            if not isinstance(boundary, list) or len(boundary) != expected_length:
                raise CursorError("model library cursor is invalid")
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
            if not boundary_items:
                raise CursorError("model library cursor boundary is invalid")
            boundary_key = key(boundary_items[0])
            filtered = [
                item
                for item in filtered
                if (
                    key(item) < boundary_key
                    if sort == "updated"
                    else key(item) > boundary_key
                )
            ]
        response = ModelLibraryResponse(
            generated_at=_utc(self._clock()),
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
                local_only=local_only,
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

    @staticmethod
    def _model_sort_key(
        sort: str,
    ) -> Callable[[LibraryModelProjection], tuple[str, ...]]:
        if sort == "updated":
            return lambda item: (
                item.updated_at.strftime("%Y%m%dT%H%M%SZ"),
                item.selector.casefold(),
                item.identity.content_sha256,
            )
        return lambda item: (item.selector.casefold(), item.identity.content_sha256)

    def model_detail(self, selector: str) -> ModelDetailResponse:
        selected = selector.strip().casefold()
        if not selected:
            raise RequestFault("model selector is required")
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
        pairs = list(zip(model_rows, entries, strict=True))
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
                and selected
                in {revision.id.casefold(), revision.document_id.casefold()}
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
                    if revision.state == "active"
                    and revision.slug.casefold() == selected
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
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        model_selectors: Sequence[str] = (),
        all_models: bool = False,
        ready: bool | None = None,
        fits_fleet: bool | None = None,
        assess: bool = True,
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
        if not assess and (ready is not None or fits_fleet is not None):
            raise RequestFault("readiness filters require assessment")
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
        elif not all_models:
            selected_keys = {
                (
                    item.identity.publisher,
                    item.identity.slug,
                    item.identity.content_sha256,
                )
                for item in model_entries
                if item.local.controller in {"cached", "preparing"}
                or item.local.running_on
            }
            local_recipe_digests = set(snapshot)
        entries = [
            self._recipe_projection(row, _canonical_recipe(row), model_by_key, snapshot)
            for row in recipe_rows
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
            filtered = self._assessed(filtered)
            for item in filtered:
                for name, expected in readiness_filters.items():
                    if expected is None:
                        continue
                    if (
                        item.assessment is None
                        or getattr(item.assessment, name).state == "unavailable"
                    ):
                        raise LibraryAssessmentUnavailable(
                            "Readiness filtering is unavailable for some candidates; narrow the library filters or remove the readiness filter to inspect their reasons."
                        )
            filtered = [
                item
                for item in filtered
                if item.assessment is not None
                and all(
                    expected is None
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
                    "all_models": all_models,
                    "ready": ready,
                    "fits_fleet": fits_fleet,
                    "assess": assess,
                    "usage": list(usage),
                    "publisher": list(publisher),
                    "alignment": list(alignment),
                    "sparks": list(sparks),
                    "collection": [
                        (
                            item.selector,
                            item.identity.recipe_revision_id,
                            item.identity.content_sha256,
                            item.updated_at.isoformat(),
                        )
                        for item in filtered
                    ],
                    "search": search,
                    "updated_since": None
                    if updated_since is None
                    else _utc(updated_since).isoformat(),
                }
            ),
        }
        if cursor is not None:
            try:
                boundary = self._cursors.decode(
                    cursor, resource="recipes", order=_LIBRARY_ORDER, context=context
                )
            except CursorError:
                raise CursorError(
                    "library cursor is invalid or the selection changed; restart without a cursor"
                ) from None
            if not isinstance(boundary, list) or len(boundary) != (
                2 if sort == "updated" else 1
            ):
                raise CursorError("recipe library cursor is invalid")
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
            if not boundary_items:
                raise CursorError("recipe library cursor boundary is invalid")
            boundary_key = key(boundary_items[0])
            filtered = [
                item
                for item in filtered
                if (
                    key(item) < boundary_key
                    if sort == "updated"
                    else key(item) > boundary_key
                )
            ]
        candidates = filtered[:limit]
        if assess and not filtering_assessment:
            candidates = self._assessed(candidates)
        response = RecipeLibraryResponse(
            generated_at=_utc(self._clock()),
            recipes=[],
            facets=self._recipe_facet_values(model_entries, entries),
            next_cursor=None,
            filters=LibraryFilterValues(
                model=list(model_selectors),
                all_models=all_models,
                ready=ready,
                fits_fleet=fits_fleet,
                usage=list(usage),
                publisher=list(publisher),
                alignment=list(alignment),
                sparks=list(sparks),
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

    @staticmethod
    def _recipe_sort_key(
        sort: str,
    ) -> Callable[[LibraryRecipeProjection], tuple[str, ...]]:
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
            entries,
            selector,
            lambda item: (item.identity.publisher, item.identity.slug),
        )
        assert isinstance(entry, LibraryRecipeProjection)
        entry = self._assessed([entry])[0]
        recipe_row = next(
            row
            for row in recipe_rows
            if row.content_digest == entry.identity.content_sha256
        )
        recipe = _canonical_recipe(recipe_row)
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
                raise LibraryProjectionError(
                    "active recipe references a missing active Model document"
                )
            model_documents.append(
                LibraryRecipeModel(selection=selection, model_document=model)
            )
        return RecipeDetailResponse.model_validate(
            entry.model_dump(mode="python")
            | {
                "model_documents": [
                    model.model_dump(mode="json") for model in model_documents
                ]
            }
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
            operational_state=OperationalState(
                builds=[], mappings=[], installations=[], runs=[]
            ),
            placement=[],
            reasons=[],
            model_documents=model_documents,
            model_capabilities=LibraryCapabilityInventory(),
            recipe_capabilities=LibraryCapabilityInventory(),
        )
