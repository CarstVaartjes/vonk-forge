"""Library projection: local state concerns."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError
from sqlalchemy import (
    Exists,
    Select,
    String,
    and_,
    case,
    column,
    func,
    literal,
    literal_column,
    or_,
    select,
    values,
)
from sqlalchemy.orm import Session, load_only
from vonk_agent_protocol import (
    AssetAvailability,
    InstallationState,
    LifecycleState,
    RunState,
    SecurityRefusalError,
    adopt_machine_state,
    canonical_message,
)

from .. import model_cache_states
from ..api_availability import AVAILABILITY_ERRORS
from ..catalog_queries import active_head_revision
from ..content_identity import recipe_content_membership
from ..library_assessment import unassessed
from ..library_contract import (
    LibraryCacheState,
    LibraryLocalProgress,
    LibraryLocalState,
    LibraryRecipeProjection,
)
from ..library_image_presence import ImageKey
from ..model_cache_contract import (
    CacheManifest,
    CacheOperationKind,
    ModelCacheOperationProgress,
)
from ..models import (
    CatalogDocumentRevision,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..operator_receipt_contract import ReceiptOwner
from ..revision_images import revision_images
from ..stored_json import read_row_column
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
    digests: Sequence[str],
) -> Mapping[str, LibraryLocalState] | None:
    try:
        candidate = (
            self._local_state()
            if self._local_state is not None
            else self._database_local_state(digests)
        )
    except SecurityRefusalError:
        raise
    except (*AVAILABILITY_ERRORS, RuntimeError, TypeError, ValueError):
        _note_unreadable("local-state", "provider", "observation is unavailable")
        return None
    if not isinstance(candidate, Mapping):
        return None
    snapshot: dict[str, LibraryLocalState] = {}
    for digest in digests:
        raw = candidate.get(digest)
        if raw:
            try:
                snapshot[digest] = (
                    raw
                    if isinstance(raw, LibraryLocalState)
                    else LibraryLocalState.model_validate_json(canonical_message(raw))
                )
            except (TypeError, ValueError):
                snapshot[digest] = LibraryLocalState(
                    controller=cast(
                        LibraryControllerState, AssetAvailability.UNKNOWN.value
                    )
                )
    return snapshot


def _local(
    self: LibraryProjection,
    digest: str,
    *,
    kind: str,
    snapshot: Mapping[str, LibraryLocalState] | None,
) -> LibraryLocalState:
    if snapshot is None:
        return LibraryLocalState(
            controller=cast(LibraryControllerState, AssetAvailability.UNKNOWN.value)
        )
    observed = snapshot.get(digest)
    raw = observed.model_dump(mode="json") if observed is not None else {}
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


def _verifies_requested_model(session: Session, digests: set[str]) -> Exists:
    """Whether a set's manifest lists a requested digest among its verified models.

    Each set's reference list is expanded once and matched by equality, never
    joined against every requested digest, so the cost grows with the sets plus
    the requested digests. A prefilter only: the shared manifest reader still
    decides exact membership, and a damaged list never fails the read.
    """

    verified = ModelCacheSet.manifest["verified_models"]
    if session.get_bind().dialect.name == "postgresql":
        array = case(
            (func.json_typeof(verified) == "array", verified),
            else_=literal_column("'[]'::json"),
        )
        reference = func.json_array_elements(array).table_valued("value")
        content = reference.c.value.op("->>")("content_sha256")
    else:
        reference = func.json_each(
            ModelCacheSet.manifest, "$.verified_models"
        ).table_valued("value", "type")
        content = case(
            (
                reference.c.type == "object",
                func.json_extract(reference.c.value, "$.content_sha256"),
            )
        )
    return (
        select(literal(1)).select_from(reference).where(content.in_(digests)).exists()
    )


def _latest_cache_operations(
    set_members: Sequence[tuple[str, str]],
) -> Select[str]:
    """The latest download or repair request per requested digest.

    ``set_members`` pairs each relevant artifact set with the requested digests
    it serves. The history is matched to them by an equality join on the
    indexed set column, so the cost grows with the requested digests plus the
    retained history, never with their product. UUID is only a deterministic
    tie-break after durable request time.
    """

    if not set_members:
        return select(ModelCacheOperation.id).where(ModelCacheOperation.id.in_(()))
    members = (
        values(
            column("artifact_set_sha256", String),
            column("digest", String),
            name="set_members",
        )
        .data(list(set_members))
        .cte()
    )
    ranked = (
        select(
            ModelCacheOperation.id,
            func.row_number()
            .over(
                partition_by=members.c.digest,
                order_by=(
                    ModelCacheOperation.created_at.desc(),
                    ModelCacheOperation.id.desc(),
                ),
            )
            .label("position"),
        )
        .join(
            members,
            ModelCacheOperation.artifact_set_sha256 == members.c.artifact_set_sha256,
        )
        .where(
            ModelCacheOperation.kind.in_(
                (CacheOperationKind.DOWNLOAD, CacheOperationKind.REPAIR)
            )
        )
        .subquery()
    )
    return select(ranked.c.id).where(ranked.c.position == 1)


def _database_local_state(
    self: LibraryProjection,
    digests: Sequence[str],
) -> Mapping[str, LibraryLocalState]:
    """Read all controller/cache/Spark-local evidence in one bounded batch.

    This is deliberately a projection query, not a per-document callback.  A
    local revision remains visible even when the active catalog pointer has
    moved on; the immutable catalog row supplies its canonical document.

    Read only the scoped rows and columns the projection uses. Cache manifests
    use the shared row reader so damage cannot discard unrelated local state;
    compiled plans and per-artifact progress are not transferred here.
    """
    if not digests:
        return {}
    requested_digests = set(digests)
    with self._sessions() as session:
        # The shared manifest reader decides exact content membership, so damage
        # in one manifest cannot discard unrelated local state.
        cache_sets = [
            (
                row.artifact_set_sha256,
                row.model_content_sha256,
                row.recipe_revision_sha256,
                row.state,
                read_row_column(row, "manifest"),
            )
            for row in session.scalars(
                select(ModelCacheSet)
                .options(
                    load_only(
                        ModelCacheSet.artifact_set_sha256,
                        ModelCacheSet.model_content_sha256,
                        ModelCacheSet.recipe_revision_sha256,
                        ModelCacheSet.state,
                        ModelCacheSet.manifest,
                    )
                )
                .where(
                    or_(
                        ModelCacheSet.model_content_sha256.in_(digests),
                        ModelCacheSet.recipe_revision_sha256.in_(digests),
                        _verifies_requested_model(session, requested_digests),
                    )
                )
            )
        ]
        reference_digests = {
            set_id: {
                reference.content_sha256
                for reference in (
                    manifest.verified_models
                    if isinstance(manifest, CacheManifest)
                    else []
                )
            }
            for set_id, _, _, _, manifest in cache_sets
        }
        set_members = sorted(
            (set_id, digest)
            for set_id, model_digest, recipe_digest, _, _ in cache_sets
            for digest in requested_digests.intersection(
                (model_digest, recipe_digest, *reference_digests[set_id])
            )
        )
        cache_operations = session.execute(
            select(
                ModelCacheOperation.id,
                ModelCacheOperation.state,
                ModelCacheOperation.artifact_set_sha256,
                ModelCacheOperation.progress,
            )
            .where(ModelCacheOperation.id.in_(_latest_cache_operations(set_members)))
            .order_by(ModelCacheOperation.created_at, ModelCacheOperation.id)
        ).all()
        revisions = session.execute(
            select(
                CatalogDocumentRevision.id,
                CatalogDocumentRevision.content_digest,
                and_(
                    CatalogDocumentRevision.state == "active",
                    active_head_revision(),
                ),
            ).where(
                CatalogDocumentRevision.kind == ReceiptOwner.RECIPE,
                CatalogDocumentRevision.content_digest.in_(digests),
            )
        ).all()
        builds = session.execute(
            select(
                RecipeBuild.recipe_revision_id,
                RecipeBuild.state,
                RecipeBuild.oci_layout_sha256,
                RecipeBuild.image_bytes,
            ).where(recipe_content_membership(RecipeBuild.recipe_revision_id, digests))
        ).all()
        installations = session.execute(
            select(
                RecipeInstallation.id,
                RecipeInstallation.recipe_revision_id,
                RecipeInstallation.state,
            ).where(
                recipe_content_membership(
                    RecipeInstallation.recipe_revision_id, digests
                )
            )
        ).all()
        invalid_run = session.scalar(
            select(RecipeRun.id)
            .where(
                RecipeRun.installation_id.in_([row[0] for row in installations]),
                RecipeRun.state.not_in(_RUN_STATES),
            )
            .limit(1)
        )
        runs = session.execute(
            select(
                RecipeRun.id,
                RecipeRun.installation_id,
                RecipeRun.plan["model_content_sha256"].as_json(),
            ).where(
                RecipeRun.state.in_(_ACTIVE_RUN_STATES),
                or_(
                    RecipeRun.installation_id.in_([row[0] for row in installations]),
                    RecipeRun.plan["model_content_sha256"].as_string().in_(digests),
                ),
            )
        ).all()
        run_nodes = session.execute(
            select(RunNode.run_id, RunNode.node_id).where(
                RunNode.state == RunState.RUNNING,
                RunNode.run_id.in_([row[0] for row in runs]),
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
    result: dict[str, LibraryLocalState] = {}
    for set_id, model_digest, recipe_digest, set_state, _manifest in cache_sets:
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
        for digest in reference_digests[set_id] & set(digests):
            self._merge_local(result, digest, controller=controller)
    cache_sets_by_id = {row[0]: row for row in cache_sets}
    for (
        operation_id,
        state,
        artifact_set_sha256,
        stored_progress,
    ) in cache_operations:
        digest_values: set[str] = set()
        if artifact_set_sha256:
            digest_values.update(reference_digests.get(artifact_set_sha256, ()))
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
        for digest in digest_values & requested_digests:
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
    result: dict[str, LibraryLocalState],
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
                digest, LibraryLocalState(controller=AssetAvailability.UNKNOWN.value)
            )
            local.controller = LibraryCacheState.FAILED.value
            local.preparation = LibraryLocalProgress(
                state=LifecycleState.FAILED, phase=unreadable[digest]
            )
        elif local is not None and local.controller == LibraryCacheState.CACHED.value:
            local.controller = (
                AssetAvailability.UNKNOWN.value
                if digest in unknown
                else LibraryCacheState.NOT_CACHED.value
            )
