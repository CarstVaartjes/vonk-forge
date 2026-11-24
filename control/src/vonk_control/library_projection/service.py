"""Library projection: service concerns."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import RecipeDefinition
from vonk_forge_contracts.qualification_status import QualificationStatus

from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.state_machines import AssetAvailability
from vonk_control.auth import CursorCodec
from vonk_control.catalog.revision_contract import (
    ModelRevisionProjection,
    RecipeRevisionProjection,
)
from vonk_control.inventory.repository import INVENTORY_MAX_AGE_SECONDS
from vonk_control.library.contract import (
    FreshnessPolicy,
    LibraryFacetValues,
    LibraryLocalProgress,
    LibraryLocalState,
    LibraryModelProjection,
    LibraryRecipeProjection,
    LibraryResourceProjection,
)
from vonk_control.library.image_presence import ImagePresenceIndex
from vonk_control.library.storage_observation import LibraryStorageObserver
from vonk_control.library_projection.catalog import (
    _catalog_documents,
    _document_texts,
    _documents_for_snapshot,
    _library_release,
    _model_projection,
    _recipe_facet_values,
    _recipe_projection,
    library_vocabulary,
    refresh_catalog,
)
from vonk_control.library_projection.common import (
    _LOCAL_STATE_PRIORITY,
    DocumentText,
    LibraryControllerState,
    LibrarySelectorAmbiguous,
    _note_unreadable,
)
from vonk_control.library_projection.local_state import (
    _apply_image_presence,
    _assessed,
    _database_local_state,
    _local,
    _local_state_snapshot,
    _recipe_model_digests,
    _state_digests,
)
from vonk_control.library_projection.views import (
    authoring_recipe_detail,
    model_detail,
    models,
    recipe_detail,
    recipe_library,
)
from vonk_control.model_cache import states as model_cache_states
from vonk_control.model_cache.contract import ModelCacheOperationState, ModelCacheUsage
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.wait_contract import QueueWait

REQUEST_BUDGET_SECONDS = 8.0
IMAGE_BUDGET_SECONDS = 2.0


class LibraryProjection:
    """Read active canonical Model and Recipe revisions only."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        cursors: CursorCodec,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        inventory_fresh_seconds: int = INVENTORY_MAX_AGE_SECONDS,
        telemetry_live_seconds: int = 6,
        telemetry_delayed_seconds: int = 20,
        local_state: Callable[[], Mapping[str, Mapping[str, object]]] | None = None,
        runtime_archive_available: Callable[[str, int], bool] | None = None,
        model_storage: Callable[[], Mapping[str, ModelCacheUsage]] | None = None,
        assessment: Callable[..., list[LibraryRecipeProjection]] | None = None,
        request_budget_seconds: float = REQUEST_BUDGET_SECONDS,
        image_budget_seconds: float = IMAGE_BUDGET_SECONDS,
        image_present_ttl_seconds: float = 0.0,
        image_absent_ttl_seconds: float = 0.0,
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
        self._storage_observer = LibraryStorageObserver(model_storage, clock=clock)
        self._request_budget = request_budget_seconds
        self._image_budget = image_budget_seconds
        self._image_presence = (
            ImagePresenceIndex(
                runtime_archive_available,
                present_ttl_seconds=image_present_ttl_seconds,
                absent_ttl_seconds=image_absent_ttl_seconds,
            )
            if runtime_archive_available is not None
            else None
        )
        self._assessment = assessment
        self._catalog_snapshot: tuple[
            list[tuple[CatalogDocumentRevision, ModelRevisionProjection]],
            list[tuple[CatalogDocumentRevision, RecipeRevisionProjection]],
        ] = ([], [])
        self._catalog_observed_at = self._clock()
        self._catalog_texts: dict[str, DocumentText] = {}
        # The automatic refresh owns the first storage observation too.
        self.refresh_catalog(observe_storage=False)

    @staticmethod
    def selector(publisher: str, slug: str) -> str:
        return f"{publisher}/{slug}"

    @staticmethod
    def _merge_local(
        result: dict[str, LibraryLocalState],
        digest: str | None,
        *,
        controller: LibraryControllerState,
        running_on: Sequence[str] = (),
        preparation: LibraryLocalProgress | None = None,
        installed_on_sparks: bool = False,
    ) -> None:
        if digest is None:
            return
        current = result.setdefault(
            digest, LibraryLocalState(controller=AssetAvailability.UNKNOWN.value)
        )
        if (
            _LOCAL_STATE_PRIORITY[controller]
            > _LOCAL_STATE_PRIORITY[current.controller]
        ):
            current.controller = controller
        current.running_on = sorted(set(current.running_on) | set(running_on))
        current.installed_on_sparks = current.installed_on_sparks or installed_on_sparks
        if preparation is not None:
            # Requests arrive in durable creation order; identity has no recency.
            current.preparation = preparation

    @staticmethod
    def _cache_progress(
        operation_id: str,
        state: str,
        *,
        phase: object,
        completed: object,
        total: object,
        rate: float | None = None,
        eta: float | None = None,
        wait: QueueWait | None = None,
    ) -> LibraryLocalProgress | None:
        """The progress of one cache operation, or ``None`` when its stored
        state does not read: the asset then shows no progress (unknown)."""

        projected_state = {
            "queued": "queued",
            "running": "running",
            model_cache_states.BACKOFF: model_cache_states.BACKOFF,
            "succeeded": "succeeded",
            "failed": "failed",
            LifecycleState.CANCELLED.value: LifecycleState.CANCELLED.value,
        }.get(state)
        if projected_state is None:
            _note_unreadable("cache operation state", operation_id, state)
            return None
        if type(completed) is not int or completed < 0:
            _note_unreadable("cache progress bytes", operation_id, repr(completed))
            return None
        if total is not None and (type(total) is not int or total < 0):
            _note_unreadable("cache progress total", operation_id, repr(total))
            return None
        return LibraryLocalProgress(
            operation_id=operation_id,
            state=cast(ModelCacheOperationState, projected_state),
            phase=phase if isinstance(phase, str) else None,
            completed_bytes=completed,
            total_bytes=total,
            smoothed_bytes_per_second=rate,
            eta_seconds=eta,
            reason=wait.reason if wait else None,
            blockers=list(wait.blockers) if wait else [],
            next_attempt_at=wait.next_check if wait else None,
        )

    @staticmethod
    def _model_usage(text: DocumentText) -> list[str]:
        """The capabilities the Model's display declares; none while unreadable."""
        return [] if text.model is None else sorted(text.model.capabilities)

    @staticmethod
    def _recipe_resources(
        document: RecipeDefinition | RecipeRevisionProjection,
    ) -> LibraryResourceProjection:
        roles = document.topology.roles
        memory = max((role.resources.memory.peak_bytes for role in roles), default=None)
        disk = max(
            (
                role.resources.disk.image_bytes
                + role.resources.disk.artifact_bytes
                + role.resources.disk.working_bytes
                + role.resources.disk.safety_margin_bytes
                for role in roles
            ),
            default=None,
        )
        image = max((role.resources.disk.image_bytes for role in roles), default=None)
        load_peak = max(
            (
                role.resources.memory.load_peak_bytes
                for role in roles
                if role.resources.memory.load_peak_bytes is not None
            ),
            default=None,
        )
        minimum_swap = max(
            (
                role.resources.memory.minimum_swap_bytes
                for role in roles
                if role.resources.memory.minimum_swap_bytes
            ),
            default=None,
        )
        return LibraryResourceProjection(
            memory_bytes=memory,
            load_peak_bytes=load_peak,
            minimum_swap_bytes=minimum_swap,
            disk_bytes=disk,
            runtime_memory_bytes=memory,
            image_bytes=image,
        )

    @staticmethod
    def _alignment_by_model(
        recipes: Iterable[
            tuple[
                CatalogDocumentRevision,
                RecipeDefinition | RecipeRevisionProjection,
            ]
        ],
        texts: Mapping[str, DocumentText],
    ) -> dict[tuple[str, str], set[str]]:
        """Map each model publisher/slug to the alignments of recipes serving it."""

        alignments: dict[tuple[str, str], set[str]] = {}
        for row, document in recipes:
            display = texts[row.document_id].recipe
            alignment = None if display is None else display.alignment
            if alignment is None:
                continue
            references = (
                document.models
                if isinstance(document, RecipeRevisionProjection)
                else [selection.model for selection in document.models]
            )
            for reference in references:
                alignments.setdefault((reference.publisher, reference.slug), set()).add(
                    alignment
                )
        return alignments

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
            family=sorted({model.family for model in models if model.family}),
            line=sorted({model.line for model in models if model.line}),
            version=sorted({model.version for model in models}),
            quantization=sorted({model.quantization for model in models}),
            publisher=sorted({model.identity.publisher for model in models}),
            alignment=sorted({value for model in models for value in model.alignment}),
        )

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

    @staticmethod
    def _recipe_sort_key(
        sort: str, *, working_first: bool = False
    ) -> Callable[[LibraryRecipeProjection], tuple[str, ...]]:
        """The recency order; ``working_first`` ranks Working recipes ahead of it
        (the list is read newest first, so the flag leads the timestamp)."""
        if sort == "updated":
            return lambda item: (
                (
                    "1"
                    if working_first
                    and item.qualification is not None
                    and item.qualification.status == QualificationStatus.WORKING
                    else "0"
                )
                + item.updated_at.strftime("%Y%m%dT%H%M%SZ"),
                item.selector.casefold(),
                item.identity.content_sha256,
            )
        return lambda item: (item.selector.casefold(), item.identity.content_sha256, "")

    _assessed = _assessed
    _local_state_snapshot = _local_state_snapshot
    _recipe_model_digests = _recipe_model_digests
    _state_digests = _state_digests
    _local = staticmethod(_local)
    _database_local_state = _database_local_state
    _apply_image_presence = staticmethod(_apply_image_presence)
    _library_release = _library_release
    library_vocabulary = library_vocabulary
    _model_projection = _model_projection
    _recipe_projection = _recipe_projection
    refresh_catalog = refresh_catalog
    _catalog_documents = _catalog_documents
    _documents_for_snapshot = _documents_for_snapshot
    _document_texts = _document_texts
    _recipe_facet_values = _recipe_facet_values
    models = models
    model_detail = model_detail
    recipe_library = recipe_library
    recipe_detail = recipe_detail
    authoring_recipe_detail = authoring_recipe_detail
