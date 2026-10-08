"""Library projection: local state concerns."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError
from sqlalchemy import and_, select
from vonk_agent_protocol import (
    AssetAvailability,
    InstallationState,
    RunState,
    SecurityRefusalError,
    adopt_machine_state,
    canonical_message,
)

from .. import model_cache_states
from ..catalog_queries import active_head_revision
from ..library_assessment import unassessed
from ..library_contract import (
    LibraryLocalState,
    LibraryRecipeProjection,
)
from ..library_image_presence import ImageKey
from ..model_cache_contract import ModelCacheOperationProgress
from ..models import (
    CatalogDocumentRevision,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..revision_images import revision_images
from .common import (
    _ACTIVE_RUN_STATES,
    _RUN_STATES,
    LibraryControllerState,
    _controller_state,
    _note_unreadable,
)

if TYPE_CHECKING:
    from .service import LibraryProjection


def _assessed(
    self: LibraryProjection,
    recipes: Sequence[LibraryRecipeProjection],
    *,
    deadline: float,
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


def _local_state_snapshot(
    self: LibraryProjection,
) -> Mapping[str, Mapping[str, object]] | None:
    snapshot = None
    deadline = time.monotonic() + self._request_budget
    for _attempt in range(3):
        try:
            candidate = self._local_state()
            if not isinstance(candidate, Mapping):
                continue
            snapshot = candidate
            for raw in candidate.values():
                if raw:
                    LibraryLocalState.model_validate_json(canonical_message(raw))
            return snapshot
        except SecurityRefusalError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            if time.monotonic() >= deadline:
                break
    _note_unreadable("local-state", "provider", "observation is unavailable")
    return snapshot


def _local(
    self: LibraryProjection,
    digest: str,
    *,
    kind: str,
    snapshot: Mapping[str, Mapping[str, object]] | None,
) -> LibraryLocalState:
    if snapshot is None:
        return LibraryLocalState(
            controller=cast(LibraryControllerState, AssetAvailability.UNKNOWN.value)
        )
    raw = snapshot.get(digest, {})
    try:
        if not isinstance(raw, Mapping):
            raise TypeError("local observation is not a mapping")
        return LibraryLocalState.model_validate_json(
            canonical_message(raw or {"controller": "not_cached"})
        )
    except (TypeError, ValueError):
        _note_unreadable(kind, digest, "local observation is unavailable")
        return LibraryLocalState(
            controller=cast(LibraryControllerState, AssetAvailability.UNKNOWN.value)
        )


def _database_local_state(
    self: LibraryProjection,
) -> Mapping[str, Mapping[str, object]]:
    """Read all controller/cache/Spark-local evidence in one bounded batch.

    This is deliberately a projection query, not a per-document callback.  A
    local revision remains visible even when the active catalog pointer has
    moved on; the immutable catalog row supplies its canonical document.

    Every table is read by the few columns the projection uses.  The JSON
    documents (compiled plans, artifact manifests, per-artifact progress)
    are large and grow with the catalog and the operation history; none of
    them is transferred or decoded here.
    """
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
                ModelCacheOperation.progress,
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
            revision_id for revision_id, state, _, _ in builds if state == "succeeded"
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
        stored_progress,
    ) in cache_operations:
        digest_values = {payload_model_digest, payload_recipe_digest}
        if artifact_set_sha256:
            cache_set = cache_sets_by_id.get(artifact_set_sha256)
            if cache_set is not None:
                digest_values.update(
                    digest for digest in cache_set[1:3] if digest is not None
                )
        # Read complete JSON through the database JSON decoder. Numeric
        # subpath extraction on SQLite converts wide integers to floats
        # before Python can validate their canonical meaning.
        try:
            progress = ModelCacheOperationProgress.model_validate_json(
                canonical_message(stored_progress), strict=True
            )
        except ValidationError as error:
            _note_unreadable(
                "cache progress",
                operation_id,
                "; ".join(
                    f"{issue['loc']}: {issue['type']}"
                    for issue in error.errors(
                        include_input=False,
                        include_context=False,
                        include_url=False,
                    )
                ),
            )
            preparation = None
        except (TypeError, ValueError) as error:
            _note_unreadable("cache progress", operation_id, type(error).__name__)
            preparation = None
        else:
            preparation = self._cache_progress(
                operation_id,
                state,
                phase=progress.measurement.phase,
                completed=progress.measurement.completed_bytes,
                total=progress.measurement.total_bytes,
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
    self: LibraryProjection,
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
