"""Bounded canonical Model to Recipe Library projection."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import InstallationState, RunState, adopt_machine_state
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    read_model,
    read_recipe,
)

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from . import model_cache_states
from .auth import CursorCodec, CursorError
from .catalog_queries import active_head_revision
from .library_assessment import unassessed
from .library_contract import (
    _MAX_PAGE_RECIPES,
    FreshnessPolicy,
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
    LibraryRelease,
    LibraryResourceProjection,
    ModelDetailResponse,
    ModelLibraryResponse,
    OperationalState,
    RecipeAlternative,
    RecipeDetailResponse,
    RecipeLibraryResponse,
    _utc,
)
from .library_image_presence import ImageKey, ImagePresenceIndex
from .machine_states import RUN_LIVE
from .model_cache_contract import DIGEST_PATTERN, UUID_PATTERN
from .models import (
    CatalogDocumentRevision,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
    RecipeLibrarySyncRun,
    RecipeRun,
    RunNode,
)
from .request_fault import RequestFault
from .revision_images import revision_images


class LibraryProjectionError(RuntimeError):
    """The active catalog contains a document outside the public authority."""


class LibrarySelectorAmbiguous(ValueError):
    """A short selector names more than one canonical catalog identity."""

    def __init__(self, selector: str, candidates: Sequence[str]) -> None:
        self.selector = selector
        self.candidates = tuple(candidates)
        super().__init__(f"selector is ambiguous: {selector}")


_LIBRARY_ORDER = "catalog"
_ACTIVE_RUN_STATES = RUN_LIVE
_RUN_STATES = tuple(RunState)
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


_LOGGER = logging.getLogger(__name__)
_LOGGED_UNREADABLE: set[str] = set()


def _note_unreadable(kind: str, key: str, detail: str) -> None:
    """Say once that a stored row is unreadable.

    The projection leaves that one row out (or reads it as unknown) and goes on:
    a damaged row is evidence to rebuild, never a reason to fail the whole page.
    """

    marker = f"{kind}:{key}"
    if marker not in _LOGGED_UNREADABLE:
        _LOGGED_UNREADABLE.add(marker)
        _LOGGER.warning("ignoring unreadable %s %s: %s", kind, key, detail)


def _readable(revision: CatalogDocumentRevision) -> bool:
    """Whether this Controller can read the revision; log an unreadable one once."""

    try:
        _canonical_document(
            revision, ModelDefinition if revision.kind == "model" else RecipeDefinition
        )
    except LibraryProjectionError as error:
        if revision.id not in _LOGGED_UNREADABLE:
            _LOGGED_UNREADABLE.add(revision.id)
            _LOGGER.warning(
                "skipping unreadable %s revision %s (%s/%s): %s",
                revision.kind,
                revision.id,
                revision.publisher,
                revision.slug,
                error.__cause__ or error,
            )
        return False
    return True


def _canonical_document(
    revision: CatalogDocumentRevision,
    document_type: type[ModelDefinition | RecipeDefinition],
) -> ModelDefinition | RecipeDefinition:
    try:
        reader = read_model if document_type is ModelDefinition else read_recipe
        return reader(revision.document)
    except (TypeError, ValueError) as error:
        raise LibraryProjectionError(
            f"active {revision.kind} document is not canonical"
        ) from error


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


def _recipe_creator(document: RecipeDefinition) -> str | None:
    """The upstream creator: the owner in the source reference, else attribution.

    The contract has no creator field and every recipe is published by the
    platform, so the owner of the source repository (for example
    ``MiaAI-Lab`` or ``nvidia``) is the creator; attribution is the fallback.
    """

    reference = document.provenance.source_reference
    if reference:
        parts = urlsplit(reference)
        owner = parts.path.strip("/").split("/", 1)[0]
        if parts.scheme in {"http", "https"} and owner:
            return owner[:128]
    for name in document.provenance.attribution:
        if name.strip():
            return name.strip()[:128]
    return None


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

    def _assessed(
        self, recipes: Sequence[LibraryRecipeProjection], *, deadline: float
    ) -> list[LibraryRecipeProjection]:
        """Assess within what is left of this request's budget."""

        if self._assessment is not None:
            return self._assessment(
                recipes, budget_seconds=max(0.0, deadline - time.monotonic())
            )
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
        # No record of the asset in the local state means it is not cached.
        candidate = raw.get("controller", "not_cached")
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

    def _database_local_state(self) -> Mapping[str, Mapping[str, object]]:
        """Read all controller/cache/Spark-local evidence in one bounded batch.

        This is deliberately a projection query, not a per-document callback.  A
        local revision remains visible even when the active catalog pointer has
        moved on; the immutable catalog row supplies its canonical document.

        Every table is read by the few columns the projection uses.  The JSON
        documents (compiled plans, artifact manifests, per-artifact progress)
        are large and grow with the catalog and the operation history; none of
        them is transferred or decoded here.
        """
        operation_progress = ModelCacheOperation.progress
        operation_payload = ModelCacheOperation.payload
        with self._sessions() as session:
            cache_sets = session.execute(
                select(
                    ModelCacheSet.artifact_set_sha256,
                    ModelCacheSet.model_content_sha256,
                    ModelCacheSet.recipe_revision_sha256,
                    ModelCacheSet.state,
                )
            ).all()
            cache_operations = session.execute(
                select(
                    ModelCacheOperation.id,
                    ModelCacheOperation.state,
                    ModelCacheOperation.artifact_set_sha256,
                    operation_payload["model_content_sha256"].as_json(),
                    operation_payload["recipe_revision_sha256"].as_json(),
                    operation_progress[("measurement", "phase")].as_json(),
                    operation_progress[("measurement", "completed_bytes")].as_json(),
                    operation_progress[("measurement", "total_bytes")].as_json(),
                    operation_progress["downloaded_bytes"].as_json(),
                    operation_progress["expected_bytes"].as_json(),
                ).where(ModelCacheOperation.kind.in_(("download", "repair")))
            ).all()
            revisions = session.execute(
                select(
                    CatalogDocumentRevision.id,
                    CatalogDocumentRevision.content_digest,
                    and_(
                        CatalogDocumentRevision.state == "active",
                        active_head_revision(),
                    ),
                ).where(CatalogDocumentRevision.kind == "recipe")
            ).all()
            builds = session.execute(
                select(
                    RecipeBuild.recipe_revision_id,
                    RecipeBuild.state,
                    RecipeBuild.oci_layout_sha256,
                    RecipeBuild.image_bytes,
                )
            ).all()
            installations = session.execute(
                select(
                    RecipeInstallation.id,
                    RecipeInstallation.recipe_revision_id,
                    RecipeInstallation.state,
                )
            ).all()
            invalid_run = session.scalar(
                select(RecipeRun.id).where(RecipeRun.state.not_in(_RUN_STATES)).limit(1)
            )
            runs = session.execute(
                select(
                    RecipeRun.id,
                    RecipeRun.installation_id,
                    RecipeRun.plan["model_content_sha256"].as_json(),
                ).where(RecipeRun.state.in_(_ACTIVE_RUN_STATES))
            ).all()
            run_nodes = session.execute(
                select(RunNode.run_id, RunNode.node_id).where(
                    RunNode.state == RunState.RUNNING
                )
            ).all()
            # A head that reuses a predecessor's build by content has no build
            # row of its own: it runs the images its recipe's builds of the
            # same source produced.
            built = {
                revision_id
                for revision_id, state, _, _ in builds
                if state == "succeeded"
            }
            digest_of = {revision_id: digest for revision_id, digest, _ in revisions}
            inherited_images = revision_images(
                session,
                {
                    revision_id
                    for revision_id, _, is_head in revisions
                    if is_head and revision_id not in built
                },
                same_source=True,
            )
            inherited_claims = [
                (digest_of[revision_id], image.archive_sha256, image.image_bytes)
                for revision_id, images in inherited_images.items()
                for image in images
            ]
        if invalid_run is not None:
            # A run in a state this vocabulary does not know is left out of the
            # local state; it does not take the whole library down with it.
            _note_unreadable("recipe run state", str(invalid_run), "unknown state")

        revision_digests = {revision_id: digest for revision_id, digest, _ in revisions}
        head_digests = {digest for _, digest, is_head in revisions if is_head}
        result: dict[str, dict[str, object]] = {}
        for _set_id, model_digest, recipe_digest, set_state in cache_sets:
            controller = _controller_state(
                {
                    "cached": "cached",
                    "downloading": "preparing",
                    "verifying": "preparing",
                    "incomplete": "preparing",
                    "needs-repair": "failed",
                    "failed": "failed",
                },
                set_state,
                "persisted cache set state is invalid",
            )
            self._merge_local(result, model_digest, controller=controller)
            self._merge_local(result, recipe_digest, controller=controller)
        cache_sets_by_id = {row[0]: row for row in cache_sets}
        for (
            operation_id,
            state,
            artifact_set_sha256,
            payload_model_digest,
            payload_recipe_digest,
            phase,
            measured_completed,
            measured_total,
            downloaded_bytes,
            expected_bytes,
        ) in cache_operations:
            digest_values = {payload_model_digest, payload_recipe_digest}
            if artifact_set_sha256:
                cache_set = cache_sets_by_id.get(artifact_set_sha256)
                if cache_set is not None:
                    digest_values.update(
                        digest for digest in cache_set[1:3] if digest is not None
                    )
            preparation = self._cache_progress(
                operation_id,
                state,
                phase=phase,
                completed=(
                    measured_completed
                    if measured_completed is not None
                    else (downloaded_bytes if downloaded_bytes is not None else 0)
                ),
                total=measured_total if measured_total is not None else expected_bytes,
            )
            controller = (
                "preparing"
                if state in model_cache_states.LIVE
                else ("cached" if state == "succeeded" else "failed")
            )
            for digest in digest_values:
                if digest is not None and not isinstance(digest, str):
                    _note_unreadable("cache digest", operation_id, repr(digest))
                    continue
                self._merge_local(
                    result,
                    digest,
                    controller=controller,
                    preparation=preparation,
                )
        for build_revision_id, build_state, _layout, _bytes in builds:
            controller = _controller_state(
                {
                    "planned": "preparing",
                    "building": "preparing",
                    "succeeded": "cached",
                    "failed": "failed",
                },
                build_state,
                "persisted recipe build state is invalid",
            )
            self._merge_local(
                result,
                revision_digests.get(build_revision_id),
                controller=controller,
            )
        installation_revision: dict[str, str] = {}
        for (
            installation_id,
            installation_revision_id,
            installation_state,
        ) in installations:
            controller = _controller_state(
                {
                    InstallationState.PLANNED: "preparing",
                    InstallationState.INSTALLING: "preparing",
                    InstallationState.INSTALLED: "cached",
                    InstallationState.PARTIAL: "preparing",
                    InstallationState.FAILED: "failed",
                    InstallationState.UNINSTALLED: "unknown",
                },
                adopt_machine_state(InstallationState, installation_state)
                or installation_state,
                "persisted installation state is invalid",
            )
            installation_revision[installation_id] = installation_revision_id
            self._merge_local(
                result,
                revision_digests.get(installation_revision_id),
                controller=controller,
            )
        nodes_by_run: dict[str, list[str]] = {}
        for run_id, node_id in run_nodes:
            nodes_by_run.setdefault(run_id, []).append(node_id)
        for run_id, installation_id, model_digest in runs:
            if model_digest is not None and not isinstance(model_digest, str):
                _note_unreadable("recipe run model digest", run_id, repr(model_digest))
                model_digest = None
            nodes = nodes_by_run.get(run_id, [])
            if model_digest is not None:
                self._merge_local(
                    result,
                    model_digest,
                    controller="cached",
                    running_on=nodes,
                )
            installed_revision_id = installation_revision.get(installation_id)
            self._merge_local(
                result,
                revision_digests.get(installed_revision_id)
                if installed_revision_id is not None
                else None,
                controller="cached",
                running_on=nodes,
            )
        if self._image_presence is not None:
            self._apply_image_presence(
                result,
                head_digests=head_digests,
                builds=[
                    (revision_digests.get(revision_id), layout, size)
                    for revision_id, state, layout, size in builds
                    if state == "succeeded"
                ],
                inherited=inherited_claims,
            )
        return result

    def _apply_image_presence(
        self,
        result: dict[str, dict[str, object]],
        *,
        head_digests: set[str],
        builds: Sequence[tuple[str | None, object, object]],
        inherited: Sequence[tuple[str, object, object]],
    ) -> None:
        """Demote a cached recipe whose stored image is gone; name what is not known.

        Only the images of the recipes the Library lists are asked about, once
        each, within the image budget. A stored image that cannot be read is not
        an absent one: it is named, per recipe, instead of failing the whole
        Library or reading as a cache miss that a download would never repair.
        An image whose answer is not yet in is reported as ``unknown`` rather
        than as stored or absent.
        """
        assert self._image_presence is not None
        claims: list[tuple[str, ImageKey]] = []
        for digest, layout, size in builds:
            if digest in head_digests and isinstance(layout, str) and type(size) is int:
                claims.append((digest, (layout, size)))
        for digest, layout, size in inherited:
            if digest in head_digests and isinstance(layout, str) and type(size) is int:
                claims.append((digest, (layout, size)))
        presence = self._image_presence.lookup(
            {key for _, key in claims}, budget_seconds=self._image_budget
        )
        available: set[str] = set()
        unreadable: dict[str, str] = {}
        unknown: set[str] = set()
        for digest, key in claims:
            answer = presence[key]
            if answer.state == "present":
                available.add(digest)
            elif answer.state == "unreadable":
                unreadable[digest] = f"image unreadable: {answer.code}"[:64]
            elif answer.state == "unknown":
                unknown.add(digest)
        for digest in head_digests:
            if digest in available:
                continue
            local = result.get(digest)
            if digest in unreadable:
                local = result.setdefault(
                    digest, {"controller": "unknown", "running_on": []}
                )
                local["controller"] = "failed"
                local["preparation"] = {
                    "state": "failed",
                    "phase": unreadable[digest],
                }
            elif local is not None and local.get("controller") == "cached":
                local["controller"] = "unknown" if digest in unknown else "not_cached"

    def _library_release(self) -> LibraryRelease | None:
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
            engine=document.runtime.engine,
            creator=_recipe_creator(document),
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
            return [row for row in session.scalars(query) if _readable(row)]

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
            engine=sorted({recipe.engine for recipe in recipes}, key=str.casefold),
            creator=sorted(
                {recipe.creator for recipe in recipes if recipe.creator},
                key=str.casefold,
            ),
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
        cached: bool = False,
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
        if cached:
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
                    "cached": cached,
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
                if item.local.controller in {"cached", "preparing"}
                or item.local.running_on
            }
            if selected_keys is None:
                # A recipe with local state of its own counts as cached too.
                local_recipe_digests = set(snapshot)
                selected_keys = cached_keys
            else:
                selected_keys &= cached_keys
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
        deadline = time.monotonic() + self._request_budget
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
        entry = self._assessed([entry], deadline=deadline)[0]
        recipe_row = next(
            row
            for row in recipe_rows
            if row.content_digest == entry.identity.content_sha256
        )
        recipe = _canonical_recipe(recipe_row)
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
