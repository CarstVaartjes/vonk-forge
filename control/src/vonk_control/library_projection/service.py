"""Library projection: service concerns."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from .. import model_cache_states
from ..auth import CursorCodec
from ..library_contract import (
    FreshnessPolicy,
    LibraryFacetValues,
    LibraryModelProjection,
    LibraryRecipeProjection,
    LibraryResourceProjection,
)
from ..library_image_presence import ImagePresenceIndex
from .catalog import (
    _catalog_documents,
    _documents_for_snapshot,
    _library_release,
    _model_projection,
    _recipe_facet_values,
    _recipe_projection,
)
from .common import (
    _LOCAL_STATE_PRIORITY,
    LibraryControllerState,
    LibraryProjectionError,
    LibrarySelectorAmbiguous,
    _note_unreadable,
)
from .local_state import (
    _apply_image_presence,
    _assessed,
    _database_local_state,
    _local,
    _local_state_snapshot,
)
from .views import (
    authoring_recipe_detail,
    model_detail,
    models,
    recipe_detail,
    recipe_library,
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
        assessment: Callable[..., list[LibraryRecipeProjection]] | None = None,
        request_budget_seconds: float = 8.0,
        image_budget_seconds: float = 2.0,
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
        self._local_state = local_state or self._database_local_state
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

    @staticmethod
    def selector(publisher: str, slug: str) -> str:
        return f"{publisher}/{slug}"

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
    def _cache_progress(
        operation_id: str,
        state: str,
        *,
        phase: object,
        completed: object,
        total: object,
    ) -> dict[str, object] | None:
        """The progress of one cache operation, or ``None`` when its stored
        state does not read: the asset then shows no progress (unknown)."""

        projected_state = {
            "queued": "queued",
            "running": "running",
            model_cache_states.BACKOFF: model_cache_states.BACKOFF,
            "succeeded": "succeeded",
            "failed": "failed",
            "cancelled": "failed",
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
        return {
            "operation_id": operation_id,
            "state": projected_state,
            "phase": phase if isinstance(phase, str) else None,
            "completed_bytes": completed,
            "total_bytes": total,
        }

    @staticmethod
    def _model_usage(document: ModelDefinition) -> list[str]:
        return sorted(document.capabilities)

    @staticmethod
    def _recipe_resources(document: RecipeDefinition) -> LibraryResourceProjection:
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
        return LibraryResourceProjection(
            memory_bytes=memory,
            disk_bytes=disk,
            runtime_memory_bytes=memory,
            image_bytes=image,
        )

    @staticmethod
    def _alignment_by_model(
        recipe_documents: Iterable[RecipeDefinition],
    ) -> dict[tuple[str, str], set[str]]:
        """Map each model publisher/slug to the alignments of recipes serving it."""

        alignments: dict[tuple[str, str], set[str]] = {}
        for document in recipe_documents:
            alignment = document.metadata.alignment
            if alignment is None:
                continue
            for selection in document.models:
                alignments.setdefault(
                    (selection.model.publisher, selection.model.slug), set()
                ).add(alignment)
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
            family=sorted({model.family for model in models}),
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
        sort: str,
    ) -> Callable[[LibraryRecipeProjection], tuple[str, ...]]:
        if sort == "updated":
            return lambda item: (
                item.updated_at.strftime("%Y%m%dT%H%M%SZ"),
                item.selector.casefold(),
                item.identity.content_sha256,
            )
        return lambda item: (item.selector.casefold(), item.identity.content_sha256, "")

    _assessed = _assessed
    _local_state_snapshot = _local_state_snapshot
    _local = _local
    _database_local_state = _database_local_state
    _apply_image_presence = _apply_image_presence
    _library_release = _library_release
    _model_projection = _model_projection
    _recipe_projection = _recipe_projection
    _catalog_documents = _catalog_documents
    _documents_for_snapshot = _documents_for_snapshot
    _recipe_facet_values = _recipe_facet_values
    models = models
    model_detail = model_detail
    recipe_library = recipe_library
    recipe_detail = recipe_detail
    authoring_recipe_detail = authoring_recipe_detail
