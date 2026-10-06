"""Free disk by removing what nothing uses, and only when disk is short.

Installations, runtime image receipts and cached models are always refetchable,
so they are removed only to make room: when work was refused for lack of disk
(a profile load, an install, a build, a model download asked for free space on
a Spark or the NAS, see :mod:`storage_demands`), or when a Spark or the NAS
runs low on free space on its own (below a share of its capacity, or below the
largest install or model set it has held). Time unused is never a reason, and a
profile change alone removes nothing.

A pass measures free space (a Spark's latest inventory, the NAS filesystem) and
removes the least recently used unused items until the shortfall plus a small
reserve is covered, then stops. Items used in the last 24 hours go only after
everything older. Which items exist:

1. **Spark installations** (state ``installed``). An installation a saved
   profile points to (but that is not the loaded profile's) may go when space is
   needed, after every installation no profile points to, least recently used
   first within each group: its models stay on the NAS, so loading that profile
   again reinstalls it. Removal is a real uninstall on
   the Sparks, queued through the same lifecycle as ``vonkctl recipe
   uninstall``; its model files go with it unless another installation on that
   Spark still needs them. Installations that share one model are removed
   together or not at all, and each is told which others go with it so the
   Spark reclaims the shared files with the last. Model files are hard-linked
   between installations (and between models that share a file), so what an
   eviction frees is counted by file: a shared file is freed only when the last
   group linking it goes, and a group whose files a staying one still links
   frees nothing and is not offered.
2. **Runtime image receipts** in the NAS ``image-cache``; the image store then
   reclaims the blobs nothing else names.
3. **Model files** in the NAS model cache, through the model cache's durable,
   fenced removal.

Nothing is removed while it is

* an installation of the loaded profile (the selected profile's recipe selector
  resolving to its revision on its Sparks), or an image or model the newest
  revision of every recipe a saved profile names;
* running, or installed for a workload that has not stopped;
* named by a live or recently finished operation (searched in the stored JSON,
  as the catalog revision collector does).

A removal queued on a Spark is given time to land and show in a fresh inventory
before the next one, so the Spark's own free space (not a promise about how much
a removal frees, which hard links make uncertain) decides whether more is
needed. A round that frees far less than it promised pauses eviction there for
a while. If everything that may go would still not cover a refused request,
nothing is removed and the waiting load says so
(``storage.insufficient_after_eviction``).

A load waiting to be admitted has issued nothing, so its plan does not keep the
installations on its Sparks (the space it waits for is theirs to give).

Every removal is re-proven while the Sparks (or artifact gates) are locked, so
a load that starts after the pass looked is never raced: the uninstall takes
no new workload intent (it supersedes nothing and leaves recovery of a running
workload alone) and is refused when an operation targets its Sparks. One item
that cannot be removed is kept for the next pass and never stops the rest.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InstallationState,
    LifecycleState,
    RunState,
    RunSwitchCode,
    canonical_message,
)

from . import job_states, model_cache_states
from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    lock_reference_gates,
)
from .artifact_reference_scan import (
    model_set_reference_findings,
    runtime_image_reference_findings,
)
from .attempt_residues import _OWNER_JOB_KINDS
from .catalog_revision_collection import (
    GRACE,
    live_tokens,
    operation_tokens,
    tokens,
)
from .fleet_profile_contract import FleetProfileAssignmentInput, FleetProfilePreview
from .logging import log_event
from .models import (
    AgentNode,
    ArtifactDistributionAssignment,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    InstallationNode,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
)
from .oci_image_store import (
    DAMAGED_MANIFEST_CODES,
    IMAGE_CACHE_DIRECTORY,
    OciImageStoreError,
)
from .recipe_action_plans import UninstallPlan
from .revision_images import revision_archives
from .runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
)
from .settings import (
    STORAGE_EVICTION_RESERVE_FLOOR_BYTES,
    STORAGE_EVICTION_RESERVE_FRACTION,
    STORAGE_INEFFECTIVE_COOLDOWN_SECONDS,
    STORAGE_LOW_FREE_CAP_FRACTION,
    STORAGE_LOW_FREE_FRACTION,
    STORAGE_SCAN_INTERVAL_SECONDS,
)
from .storage_demands import (
    NAS_IMAGES,
    NAS_MODELS,
    STORAGE_EVICTING,
    STORAGE_EVICTION_TIMED_OUT,
    STORAGE_INSUFFICIENT,
    StorageDemand,
    StorageDemands,
    StorageRelief,
    spark_scope,
)
from .worker_memory_contract import WorkerMemoryComponent

_LOGGER = logging.getLogger(__name__)
ACTOR = "system:storage-sweep"
# One pass stops here and the rest continues on the next worker pass.
SWEEP_BUDGET_SECONDS = 30.0
# A Spark's inventory older than this proves nothing about its free space.
INVENTORY_MAX_AGE = timedelta(seconds=300)
# A failed run holds nothing; any other state but stopped may still be on a Spark.
_DEAD_RUNS = (RunState.STOPPED, RunState.FAILED)
# Installations that may hold files on their Sparks (a plan holds none).
_HOLDING_STATES = (
    InstallationState.INSTALLED,
    InstallationState.INSTALLING,
    InstallationState.PARTIAL,
    InstallationState.FAILED,
)
_FINISHED_JOBS = job_states.words(
    LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
)
_RECEIPT_SUFFIX = ".receipt.json"
_ASSIGNMENTS = TypeAdapter(list[FleetProfileAssignmentInput])


class _Kept(Exception):
    """Something still uses it (or proof is missing); try again next pass."""


class UnusedModelRemoval(Protocol):
    """The model cache's unattended, fenced removal seam."""

    def accept_unused_removal(
        self,
        model_content_sha256: str,
        *,
        actor: str,
        request_key: str,
        verify: Callable[[Session, tuple[str, ...]], None],
    ) -> object: ...


class InstallationRemoval(Protocol):
    """The recipe lifecycle's uninstall, as the collector uses it."""

    def preview_uninstall(
        self, installation_id: str, *, also_removing: Collection[str] = ()
    ) -> UninstallPlan: ...

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        unattended_guard: Callable[[Session], None] | None = None,
        also_removing: Collection[str] = (),
    ) -> object: ...


class ImageBlobReclaimer(Protocol):
    """The image store's reclaim of blobs no receipt names any more."""

    def collect(self) -> object: ...


@dataclass(frozen=True, slots=True)
class Swept:
    installations: int
    images: int
    models: int
    kept: dict[str, int]
    #: What the pass expected to free, per scope, with why it ran.
    scopes: tuple[dict[str, object], ...] = ()


#: How a keeping reason reads in the sentence a waiting load shows.
_KEPT_WORDS = {
    "profile": "the loaded profile uses them",
    "running": "its workload is running",
    "run not stopped": "its run was never stopped",
    "live operation": "a live operation names it",
    "not installed": "not installed",
}

_STORAGE_WAIT_CODES = frozenset(
    {
        RunSwitchCode.INSUFFICIENT_DISK,
        RunSwitchCode.DISK_EVICTION_PLANNED,
        STORAGE_EVICTING,
        STORAGE_EVICTION_TIMED_OUT,
        STORAGE_INSUFFICIENT,
    }
)


def _kept_sentence(kept_bytes: Mapping[str, int]) -> str:
    """What stays on the Spark, by reason, so a refusal says why nothing goes."""

    parts = [
        f"{size} bytes of installations stay because {reason}"
        for reason, size in sorted(
            kept_bytes.items(), key=lambda item: (-item[1], item[0])
        )
        if size > 0
    ]
    return ("; ".join(parts[:2]) + ". ") if parts else ""


@dataclass(frozen=True, slots=True)
class EvictionCapacity:
    """What a Spark's unused installations could free, and what stays."""

    freeable: int
    kept: str
    items: tuple[_Item, ...]

    def plan(self, shortfall: int) -> str:
        """What would be evicted to cover ``shortfall``, in eviction order."""

        return _eviction_sentence(self.items, shortfall)


def _eviction_order(items: Iterable[_Item]) -> list[_Item]:
    """Installations no saved profile points to first, then pointed ones; the
    least recently used first within each group."""

    return sorted(
        items,
        key=lambda item: (
            bool(item.profiles),
            item.recent,
            item.last_used,
            item.kind,
            item.key,
        ),
    )


def _freeable_in_order(
    candidates: list[_Item],
    holders: Mapping[str, set[str]],
    kept: Counter[str],
    kept_bytes: Counter[str] | None,
) -> list[_Item]:
    """Give each item the bytes its removal really frees, in eviction order.

    Installations link shared model objects instead of copying them, so a
    shared object is freed only when the last group that links it goes. An item
    whose objects a group that stays still links, and that holds nothing of its
    own, frees nothing ever and is not offered. The others carry the bytes they
    free after everything ahead of them (an object two removable groups share
    counts for the later one), so the sum is what removing them all frees.
    """

    removable = {item.key for item in candidates}
    useful = []
    for item in candidates:
        reachable = item.private + sum(
            size for digest, size in item.objects if holders[digest] <= removable
        )
        if reachable <= 0:
            kept["installation: shares its files with one that stays"] += len(
                item.members
            )
            if kept_bytes is not None:
                kept_bytes["installations that stay share its model files"] += (
                    item.freeable
                )
            continue
        useful.append(item)
    remaining = {digest: set(keys) for digest, keys in holders.items()}
    ordered: list[_Item] = []
    for item in _eviction_order(useful):
        freed = item.private
        for digest, size in item.objects:
            remaining[digest].discard(item.key)
            if not remaining[digest]:
                freed += size
        ordered.append(replace(item, freeable=freed))
    return ordered


def _model_objects(
    session: Session, model_digests: Collection[str]
) -> dict[str, dict[str, int]]:
    """The model files (file digest and bytes) each model is made of, from the
    model cache's recorded manifests. A model with no manifest is absent: all
    of its installed bytes are then treated as the installation's own."""

    found: dict[str, dict[str, int]] = {}
    if not model_digests:
        return found
    for model, manifest in session.execute(
        select(ModelCacheSet.model_content_sha256, ModelCacheSet.manifest).where(
            ModelCacheSet.model_content_sha256.in_(tuple(model_digests))
        )
    ):
        artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
        if model is None or not isinstance(artifacts, list):
            continue
        objects = found.setdefault(model, {})
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            digest, size = artifact.get("sha256"), artifact.get("download_bytes")
            owner = artifact.get("model_content_sha256")
            if (
                isinstance(digest, str)
                and isinstance(size, int)
                and not isinstance(size, bool)
                and size >= 0
                and owner in (None, model)
            ):
                objects[digest] = size
    return found


def _eviction_sentence(items: Iterable[_Item], shortfall: int) -> str:
    """Name what eviction removes to free ``shortfall`` bytes: unpointed
    installations first, then the bytes taken from each saved profile."""

    unpointed = 0
    by_profile: dict[str, int] = {}
    remaining = shortfall
    for item in _eviction_order(items):
        if remaining <= 0:
            break
        remaining -= item.freeable
        if item.profiles:
            by_profile[", ".join(item.profiles)] = (
                by_profile.get(", ".join(item.profiles), 0) + item.freeable
            )
        else:
            unpointed += item.freeable
    parts = []
    if unpointed:
        parts.append(f"evicting {unpointed} bytes from unused installations")
    parts.extend(
        f"evicting {size} bytes from saved profile {names}"
        for names, size in by_profile.items()
    )
    return "; ".join(parts)


def spark_eviction_capacity(
    session: Session, node_id: str, now: datetime
) -> EvictionCapacity:
    """What unused installations on a Spark could free, read-only, and what stays.

    A review asks this to plan the eviction a load needs instead of refusing it:
    the installations the collector can remove (no saved profile points to them
    first, then pointed ones, least recently used first) and a sentence naming
    what it must keep, so a refusal names what blocks. It registers no demand and
    removes nothing.
    """

    kept_bytes: Counter[str] = Counter()
    items = UnusedStorageCollector._spark_items(
        session, node_id, _Evidence.read(session, now), Counter(), kept_bytes
    )
    return EvictionCapacity(
        sum(item.freeable for item in items), _kept_sentence(kept_bytes), tuple(items)
    )


def _waits_for_storage(application: FleetProfileApplication) -> bool:
    """A load that issued nothing and waits to be admitted holds nothing.

    It must not keep the installations on its own Sparks: its admission waits
    for disk that only those installations can free, so keeping them would
    make the space it waits for impossible to free for ever. The selected
    profile's own installations stay kept by the loaded profile. Anything it
    already issued (a current operation) keeps them as before.
    """

    if application.current_operation_id is not None:
        return False
    progress = application.progress if isinstance(application.progress, dict) else {}
    if progress.get("admission_pending") is True:
        return True
    blockers = progress.get("blockers")
    return isinstance(blockers, list) and any(
        isinstance(item, dict) and item.get("code") in _STORAGE_WAIT_CODES
        for item in blockers
    )


@dataclass(frozen=True, slots=True)
class _Evidence:
    """Everything that can keep something, read in one snapshot."""

    now: datetime
    cutoff: datetime
    live: frozenset[str]
    #: Named by an operation that has not ended; one that just finished only
    #: makes its installation recently used (removed last), never in use.
    operations: frozenset[str]
    #: Digests the newest revisions of the recipes a saved profile names mention.
    pinned: frozenset[str]
    pointed_digests: frozenset[str]
    pointed_models: frozenset[str]
    pointed_archives: frozenset[str]
    # Newest active recipe revision of each document: id, creation time, number
    # and content digest (what an installation is compared on).
    newest: Mapping[str, tuple[str, datetime, int, str]]
    # (publisher, slug) -> Spark sets of the saved profile assignments naming it.
    # None when a profile cannot be read: nothing can then be proven unused.
    pointers: Mapping[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]] | None
    # The same for the loaded (selected) profile alone: its installations never go.
    loaded_pointers: (
        Mapping[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]] | None
    )
    owned_nodes: frozenset[str]
    active_scopes: tuple[str, ...]
    recent_sets: frozenset[str]
    recent_archives: frozenset[str]
    application_references_readable: bool

    @classmethod
    def read(cls, session: Session, now: datetime) -> _Evidence:
        cutoff = now - GRACE
        revisions = session.execute(
            select(
                CatalogDocumentRevision.id,
                CatalogDocumentRevision.document_id,
                CatalogDocumentRevision.revision_number,
                CatalogDocumentRevision.created_at,
                CatalogDocumentRevision.content_digest,
            ).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        newest: dict[str, tuple[str, datetime, int, str]] = {}
        for revision_id, document_id, number, created, digest in revisions:
            known = newest.get(document_id)
            if known is None or number > known[2]:
                newest[document_id] = (revision_id, _utc(created), number, digest)
        pointers = _profile_pointers(session)
        selected = session.scalar(select(FleetProfileSelection.profile_id))
        loaded_pointers = (
            {}
            if selected is None
            else _profile_pointers(session, only_profile_id=selected)
        )
        head_ids = {item[0] for item in newest.values()}
        # A load that is queued, admission-waiting or running needs the exact
        # revisions its plan resolved, whatever the saved profiles say now (a
        # sweep rewrites its profile between steps).  Unlike the Spark scopes
        # below, a load waiting for storage still needs its assets.
        applied_ids = _applied_revision_ids(session)
        application_references_readable = applied_ids is not None
        applied_ids = applied_ids or frozenset()
        head_ids.update(applied_ids)
        for column in (
            CatalogDocumentHead.active_revision_id,
            CatalogDocumentHead.candidate_revision_id,
        ):
            head_ids.update(
                value
                for value in session.scalars(
                    select(column).where(CatalogDocumentHead.kind == "recipe")
                )
                if value is not None
            )
        # What a profile points to is the newest revision of the recipes it
        # names. A recipe nobody named is offered by the catalog but is no
        # more than a download away, so it can go when space is short.
        pointed = [
            row
            for row in session.execute(
                select(
                    CatalogDocumentRevision.id,
                    CatalogDocumentRevision.publisher,
                    CatalogDocumentRevision.slug,
                    CatalogDocumentRevision.content_digest,
                    CatalogDocumentRevision.document,
                ).where(CatalogDocumentRevision.id.in_(head_ids))
            )
            if pointers is None
            or row.id in applied_ids
            or (row.publisher.casefold(), row.slug.casefold()) in pointers
        ]
        pointed_ids = frozenset(row.id for row in pointed)
        pinned = tokens(row.document for row in pointed)
        bound = frozenset(
            session.scalars(
                select(CatalogRecipeModelReference.model_content_digest).where(
                    CatalogRecipeModelReference.recipe_revision_id.in_(pointed_ids)
                )
            )
        )
        owned = frozenset(
            node_id
            for targets in session.scalars(
                select(Job.targets).where(
                    Job.kind.in_(_OWNER_JOB_KINDS),
                    Job.state.in_(("queued", "running")),
                )
            )
            if isinstance(targets, list)
            for node_id in targets
            if isinstance(node_id, str)
        )
        scopes = tuple(
            canonical_message(application.plan).decode()
            for application in session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.state.in_(_UNFINISHED_LOADS)
                )
            )
            if not _waits_for_storage(application)
        )
        return cls(
            now=now,
            application_references_readable=application_references_readable,
            cutoff=cutoff,
            live=live_tokens(session, now),
            operations=operation_tokens(session, now, recent=False),
            pinned=pinned,
            pointed_digests=frozenset(row.content_digest for row in pointed),
            pointed_models=bound | pinned,
            # What a pointed recipe can run is every image its document built,
            # including a predecessor's build that a successor reuses.
            pointed_archives=revision_archives(session, pointed_ids)
            | frozenset(
                value
                for value in session.scalars(
                    select(RecipeBuild.oci_layout_sha256).where(
                        RecipeBuild.recipe_revision_id.in_(pointed_ids)
                    )
                )
                if value is not None
            )
            | pinned,
            newest=newest,
            pointers=pointers,
            loaded_pointers=loaded_pointers,
            owned_nodes=owned,
            active_scopes=scopes,
            recent_sets=frozenset(
                session.scalars(
                    select(ArtifactDistributionAssignment.model_artifact_set_sha256)
                    .where(ArtifactDistributionAssignment.updated_at > cutoff)
                    .distinct()
                )
            ),
            recent_archives=frozenset(
                session.scalars(
                    select(ArtifactDistributionAssignment.oci_archive_sha256)
                    .where(ArtifactDistributionAssignment.updated_at > cutoff)
                    .distinct()
                )
            )
            | frozenset(
                value
                for value in session.scalars(
                    select(RecipeBuild.oci_layout_sha256).where(
                        or_(
                            RecipeBuild.updated_at > cutoff,
                            RecipeBuild.state.in_(("planned", "building")),
                        )
                    )
                )
                if value is not None
            ),
        )


@dataclass(frozen=True, slots=True)
class _Pressure:
    """One Spark or filesystem that is short of free space."""

    scope: str
    node_id: str | None
    free: int
    total: int
    observed_at: datetime | None
    #: Bytes missing for the refused work (or the low-space line) to be met.
    shortfall: int
    #: The shortfall plus a reserve, so the next request does not refuse again.
    need: int
    why: str
    #: Work was refused for this space, so removing too little would not help it.
    demanded: bool


@dataclass(frozen=True, slots=True)
class _Item:
    """Something unused that can be removed, and what that is expected to free."""

    kind: str  # "installation", "model" or "image"
    key: str
    #: Installations sharing one model on a Spark are removed together.
    members: tuple[str, ...]
    last_used: datetime
    recent: bool
    freeable: int
    #: Saved profiles pointing at it: evicted after everything no profile points to.
    profiles: tuple[str, ...] = ()
    #: The shared model objects (file digest, bytes) its installations link, and
    #: the bytes only they hold (runtime files, private copies). ``freeable`` is
    #: what removing it frees *after the items ahead of it in eviction order*: a
    #: shared object frees only with the last installation that links it.
    objects: tuple[tuple[str, int], ...] = ()
    private: int = 0


@dataclass(slots=True)
class _Outcome:
    scope: str
    why: str
    shortfall: int
    outcome: str
    estimated_freed: int = 0
    removed: Counter[str] = field(default_factory=Counter)
    freeable: int = 0

    def line(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "why": self.why,
            "needed_bytes": self.shortfall,
            "freeable_bytes": self.freeable,
            "estimated_freed_bytes": self.estimated_freed,
            "outcome": self.outcome,
            "installations_removed": self.removed["installation"],
            "image_receipts_removed": self.removed["image"],
            "models_removed": self.removed["model"],
        }


@dataclass(frozen=True, slots=True)
class _Round:
    """What a pass promised to free, to compare with what the next one measures."""

    estimated: int
    free_before: int


def _disk_usage(path: Path) -> tuple[int, int]:
    usage = shutil.disk_usage(path)
    return usage.total, usage.free


class UnusedStorageCollector:
    """Frees disk on a Spark or the NAS, by removing unused items, when it is short."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        lifecycle: InstallationRemoval,
        image_cache_root: Path | None = None,
        model_cache_root: Path | None = None,
        model_cache: UnusedModelRemoval | None = None,
        image_blobs: ImageBlobReclaimer | None = None,
        demands: StorageDemands | None = None,
        budget_seconds: float = SWEEP_BUDGET_SECONDS,
        disk_usage: Callable[[Path], tuple[int, int]] = _disk_usage,
        low_free_fraction: float = STORAGE_LOW_FREE_FRACTION,
        low_free_cap_fraction: float = STORAGE_LOW_FREE_CAP_FRACTION,
        reserve_fraction: float = STORAGE_EVICTION_RESERVE_FRACTION,
        reserve_floor_bytes: int = STORAGE_EVICTION_RESERVE_FLOOR_BYTES,
        scan_interval: timedelta = timedelta(seconds=STORAGE_SCAN_INTERVAL_SECONDS),
        cooldown: timedelta = timedelta(seconds=STORAGE_INEFFECTIVE_COOLDOWN_SECONDS),
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._lifecycle = lifecycle
        self._images = (
            FilesystemRuntimeImageStorage(image_cache_root)
            if image_cache_root is not None
            else None
        )
        self._image_cache = (
            image_cache_root / IMAGE_CACHE_DIRECTORY
            if image_cache_root is not None
            else None
        )
        self._model_cache_root = model_cache_root
        self._model_cache = model_cache
        self._image_blobs = image_blobs
        self._demands = demands
        self._budget_seconds = budget_seconds
        self._disk_usage = disk_usage
        self._low_free_fraction = low_free_fraction
        self._low_free_cap_fraction = low_free_cap_fraction
        self._reserve_fraction = reserve_fraction
        self._reserve_floor_bytes = reserve_floor_bytes
        self._scan_interval = scan_interval
        self._cooldown = cooldown
        self._due_at: datetime | None = None
        self._rounds: dict[str, _Round] = {}
        self._paused_until: dict[str, datetime] = {}
        self._last_outcome: dict[str, str] = {}

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        return {WorkerMemoryComponent.STORAGE_COLLECTION_ROUNDS: len(self._rounds)}

    def tick(self) -> bool:
        """Look at free space at most once per scan; True when anything was removed."""

        now = self._clock()
        if self._due_at is not None and now < self._due_at:
            return False
        self._due_at = now + self._scan_interval
        try:
            result = self.collect()
        except SQLAlchemyError as error:
            # A database fault proves nothing unused; try again next scan.
            log_event(
                _LOGGER,
                "unused_storage.sweep_failed",
                service="control-worker",
                code=type(error).__name__,
            )
            return False
        return bool(result.installations or result.images or result.models)

    def collect(self) -> Swept:
        """One pass: free what the short Sparks and the NAS need, and no more."""

        now = self._clock()
        deadline = time.monotonic() + self._budget_seconds
        pressures = self._pressures(now)
        if not pressures:
            self._last_outcome.clear()
            return Swept(0, 0, 0, {})
        with self._sessions() as session:
            evidence = _Evidence.read(session, now)
        kept: Counter[str] = Counter()
        # An installation on two Sparks is one removal for both of them.
        attempted: dict[tuple[str, str], bool] = {}
        outcomes = [
            self._relieve(pressure, evidence, deadline, kept, attempted)
            for pressure in pressures
        ]
        if any("sweep budget" in reason for reason in kept):
            self._due_at = now  # continue on the next worker pass
        removed: Counter[str] = Counter()
        for outcome in outcomes:
            removed.update(outcome.removed)
        # One line per pass that did something or whose answer changed, so a
        # Spark that stays short and waiting does not repeat itself every scan.
        changed = [
            outcome
            for outcome in outcomes
            if outcome.removed.total()
            or self._last_outcome.get(outcome.scope) != outcome.outcome
        ]
        self._last_outcome = {outcome.scope: outcome.outcome for outcome in outcomes}
        if changed:
            log_event(
                _LOGGER,
                "unused_storage.eviction_pass",
                service="control-worker",
                scopes=[outcome.line() for outcome in changed],
                kept=dict(kept),
            )
        return Swept(
            removed["installation"],
            removed["image"],
            removed["model"],
            dict(kept),
            tuple(outcome.line() for outcome in outcomes),
        )

    def relief_for_spark(
        self,
        node_id: str,
        required_free_bytes: int,
        *,
        source: str,
        subject: str,
        reason: str,
    ) -> StorageRelief | None:
        """Ask for free space on a Spark for work that was refused for lack of it.

        Returns what to tell the waiting work: that unused installations are
        being removed (``storage.evicting``), or that all of them together would
        not be enough (``storage.insufficient_after_eviction``). ``None`` when
        the Spark's free space cannot be read or already covers the request.
        """

        scope = spark_scope(node_id)
        if self._demands is not None:
            self._demands.request(
                scope,
                required_free_bytes,
                source=source,
                subject=subject,
                reason=reason,
            )
        now = self._clock()
        try:
            with self._sessions() as session:
                snapshot = _latest_snapshots(session, node_id).get(node_id)
                if snapshot is None or now - snapshot[2] > INVENTORY_MAX_AGE:
                    return None
                shortfall = required_free_bytes - snapshot[0]
                if shortfall <= 0:
                    return None
                evidence = _Evidence.read(session, now)
                kept_bytes: Counter[str] = Counter()
                items = self._spark_items(
                    session, node_id, evidence, Counter(), kept_bytes
                )
                settling = _spark_settling(session, node_id, snapshot[2])
        except SQLAlchemyError:
            return None
        freeable = sum(item.freeable for item in items)
        paused = self._paused_until.get(scope)
        paused = paused if paused is not None and now < paused else None
        if freeable >= shortfall and paused is None:
            code = STORAGE_EVICTING
            detail = (
                f"Needs {shortfall} more free bytes on this Spark; "
                f"{freeable} bytes of unused installations can be removed: "
                + _eviction_sentence(items, shortfall)
                + ", least recently used first"
                + (" (waiting for the last removal to finish)." if settling else ".")
            )
        else:
            code = STORAGE_INSUFFICIENT
            detail = (
                f"Needs {shortfall} more free bytes on this Spark; only "
                f"{freeable} bytes of unused installations can be removed"
                + (
                    "; the last removals freed far less than expected, so "
                    "removal is paused"
                    if paused is not None
                    else ""
                )
                + ". "
                + _kept_sentence(kept_bytes)
                + "Stop or remove something on this Spark to make room."
            )
        return StorageRelief(
            code, shortfall, freeable, detail, paused=paused is not None
        )

    # -- pressure ---------------------------------------------------------------

    def _pressures(self, now: datetime) -> list[_Pressure]:
        demands = self._demands.active() if self._demands is not None else []
        pressures: list[_Pressure] = []
        with self._sessions() as session:
            largest_install = (
                session.scalar(select(func.max(InstallationNode.required_bytes))) or 0
            )
            largest_set = (
                session.scalar(select(func.max(ModelCacheSet.expected_bytes))) or 0
            )
            snapshots = _latest_snapshots(session)
        for node_id, (free, total, observed) in sorted(snapshots.items()):
            if now - observed > INVENTORY_MAX_AGE:
                continue
            scope = spark_scope(node_id)
            pressure = self._pressure(
                scope,
                node_id,
                free,
                total,
                observed,
                largest_install,
                [demand for demand in demands if demand.scope == scope],
            )
            if pressure is not None:
                pressures.append(pressure)
        for scope, path, kinds in self._nas_filesystems():
            try:
                total, free = self._disk_usage(path)
            except OSError:
                continue
            wanted = {
                *([NAS_MODELS] if "model" in kinds else []),
                *([NAS_IMAGES] if "image" in kinds else []),
            }
            pressure = self._pressure(
                scope,
                None,
                free,
                total,
                None,
                largest_set,
                [demand for demand in demands if demand.scope in wanted],
            )
            if pressure is not None:
                pressures.append(pressure)
        return pressures

    def _pressure(
        self,
        scope: str,
        node_id: str | None,
        free: int,
        total: int,
        observed: datetime | None,
        largest: int,
        demands: list[StorageDemand],
    ) -> _Pressure | None:
        mark = min(
            max(math.ceil(total * self._low_free_fraction), largest),
            math.ceil(total * self._low_free_cap_fraction),
        )
        reserve = max(
            self._reserve_floor_bytes, math.ceil(total * self._reserve_fraction)
        )
        if self._demands is not None:
            self._demands.settle(scope, free)
        wanted = max((demand.required_free_bytes for demand in demands), default=0)
        if wanted > free:
            required = max(wanted, mark if free < mark else 0)
            why = "refused: " + "; ".join(
                sorted({f"{demand.source} {demand.reason}" for demand in demands})
            )
            demanded = True
        elif free < mark:
            required = mark
            why = f"free space {free} is below the {mark}-byte low-space line"
            demanded = False
        else:
            return None
        shortfall = required - free
        return _Pressure(
            scope,
            node_id,
            free,
            total,
            observed,
            shortfall,
            shortfall + reserve,
            why,
            demanded,
        )

    def _nas_filesystems(self) -> list[tuple[str, Path, frozenset[str]]]:
        roots: list[tuple[Path, str]] = []
        if self._model_cache is not None and self._model_cache_root is not None:
            roots.append((self._model_cache_root, "model"))
        if self._images is not None and self._image_cache is not None:
            roots.append((self._image_cache, "image"))
        by_device: dict[int, list[tuple[Path, str]]] = defaultdict(list)
        for root, kind in roots:
            try:
                by_device[os.stat(root).st_dev].append((root, kind))
            except OSError:
                continue
        result: list[tuple[str, Path, frozenset[str]]] = []
        for members in by_device.values():
            kinds = frozenset(kind for _root, kind in members)
            scope = "nas" if len(by_device) == 1 else f"nas:{min(kinds)}"
            result.append((scope, members[0][0], kinds))
        return result

    # -- one scope ----------------------------------------------------------------

    def _relieve(
        self,
        pressure: _Pressure,
        evidence: _Evidence,
        deadline: float,
        kept: Counter[str],
        attempted: dict[tuple[str, str], bool],
    ) -> _Outcome:
        now = self._clock()
        outcome = _Outcome(pressure.scope, pressure.why, pressure.shortfall, "evicting")
        with self._sessions() as session:
            if pressure.node_id is not None and pressure.observed_at is not None:
                settling = _spark_settling(
                    session, pressure.node_id, pressure.observed_at
                )
                items = self._spark_items(session, pressure.node_id, evidence, kept)
            else:
                settling = _model_removal_in_flight(session)
                items = self._nas_items(session, evidence, kept)
        outcome.freeable = sum(item.freeable for item in items)
        if settling:
            outcome.outcome = "waiting"
            return outcome
        self._check_round(pressure, now)
        paused = self._paused_until.get(pressure.scope)
        if paused is not None and now < paused:
            outcome.outcome = "paused"
            return outcome
        if pressure.demanded and outcome.freeable < pressure.shortfall:
            # Removing part of it would cost cached items and still not admit
            # the work that asked.
            outcome.outcome = "insufficient_after_eviction"
            return outcome
        ordered = _eviction_order(items)
        queued = 0
        for item in ordered:
            if queued >= pressure.need:
                break
            done = attempted.get((item.kind, item.key))
            if done is None:
                if time.monotonic() > deadline:
                    kept[f"{item.kind}: sweep budget"] += 1
                    continue
                done = attempted[(item.kind, item.key)] = self._remove(
                    item, kept, outcome
                )
            if done:
                queued += item.freeable
        outcome.estimated_freed = queued
        if outcome.removed["image"]:
            self._reclaim_blobs()
        if queued:
            self._rounds[pressure.scope] = _Round(queued, pressure.free)
        elif pressure.demanded:
            outcome.outcome = "insufficient_after_eviction"
        else:
            outcome.outcome = "nothing_removable"
        return outcome

    def _check_round(self, pressure: _Pressure, now: datetime) -> None:
        """Compare what the last round promised with the free space now measured."""

        round_ = self._rounds.pop(pressure.scope, None)
        if round_ is None or round_.estimated <= 0:
            return
        freed = pressure.free - round_.free_before
        if freed * 2 >= round_.estimated:
            return
        self._paused_until[pressure.scope] = now + self._cooldown
        log_event(
            _LOGGER,
            "unused_storage.ineffective",
            service="control-worker",
            scope=pressure.scope,
            estimated_freed_bytes=round_.estimated,
            freed_bytes=max(0, freed),
            paused_until=self._paused_until[pressure.scope].isoformat(),
        )

    def _remove(self, item: _Item, kept: Counter[str], outcome: _Outcome) -> bool:
        """Remove one item; False when anything still keeps it."""

        label = "image receipt" if item.kind == "image" else item.kind
        done = 0
        for member in item.members:
            try:
                if item.kind == "installation":
                    self._uninstall(member, item.members)
                elif item.kind == "image":
                    assert self._image_cache is not None
                    self._remove_receipt(
                        member, self._image_cache / f"{member}{_RECEIPT_SUFFIX}"
                    )
                else:
                    self._remove_model(member)
            except _Kept as why:
                kept[f"{label}: {why}"] += 1
                break
            except (
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                SQLAlchemyError,
                ArtifactLifecycleError,
                RuntimeImagePreparationError,
            ) as error:
                kept[f"{label}: refused {_code(error)}"] += 1
                log_event(
                    _LOGGER,
                    "unused_storage.refused",
                    service="control-worker",
                    kind=item.kind,
                    record_id=member,
                    code=_code(error),
                    detail=str(error)[:256],
                )
                break
            done += 1
            outcome.removed[item.kind] += 1
            log_event(
                _LOGGER,
                "unused_storage.removing",
                service="control-worker",
                kind=item.kind,
                record_id=member,
            )
        return done == len(item.members)

    def _reclaim_blobs(self) -> None:
        if self._image_blobs is None:
            return
        try:
            self._image_blobs.collect()
        except (OSError, RuntimeError, SQLAlchemyError) as error:
            log_event(
                _LOGGER,
                "unused_storage.refused",
                service="control-worker",
                kind="image-blobs",
                code=_code(error),
                detail=str(error)[:256],
            )

    # -- installations ------------------------------------------------------------

    @staticmethod
    def _spark_items(
        session: Session,
        node_id: str,
        evidence: _Evidence,
        kept: Counter[str],
        kept_bytes: Counter[str] | None = None,
    ) -> list[_Item]:
        rows = session.execute(
            select(
                RecipeInstallation.id,
                RecipeInstallation.state,
                RecipeInstallation.model_content_sha256,
                InstallationNode.installed_bytes,
            )
            .join(
                InstallationNode,
                InstallationNode.installation_id == RecipeInstallation.id,
            )
            .where(
                InstallationNode.node_id == node_id,
                RecipeInstallation.state.in_(_HOLDING_STATES),
            )
        ).all()
        groups: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
        for installation_id, state, model, installed in rows:
            # One model's files are shared by every installation of it on a
            # Spark, so only removing all of them frees them.
            groups[model or f"installation:{installation_id}"].append(
                (installation_id, state, installed)
            )
        objects = _model_objects(
            session, [key for key in groups if not key.startswith("installation:")]
        )
        # Which groups link each shared object, kept groups included: an object
        # a group that stays still links is not freed by removing the others.
        holders: dict[str, set[str]] = defaultdict(set)
        for key in groups:
            for digest in objects.get(key, {}):
                holders[digest].add(key)
        candidates: list[_Item] = []
        for key, members in sorted(groups.items()):
            reasons = {
                installation_id: (
                    _installation_kept(session, installation_id, evidence)
                    if state == InstallationState.INSTALLED
                    else "not installed"
                )
                for installation_id, state, _installed in members
            }
            if any(reason is not None for reason in reasons.values()):
                for reason in reasons.values():
                    kept[
                        f"installation: {reason or 'model shared with one in use'}"
                    ] += 1
                if kept_bytes is not None:
                    why = next(
                        reason for reason in reasons.values() if reason is not None
                    )
                    kept_bytes[_KEPT_WORDS.get(why, why)] += max(
                        installed for _id, _state, installed in members
                    )
                continue
            installed = max(installed for _id, _state, installed in members)
            if installed <= 0:
                kept["installation: holds no bytes"] += len(members)
                continue
            shared = objects.get(key, {})
            last_used = max(
                _installation_last_used(session, installation_id)
                for installation_id, _state, _installed in members
            )
            profiles = sorted(
                {
                    name
                    for installation_id, _state, _installed in members
                    for name in _installation_pointing(
                        session, installation_id, evidence, evidence.pointers
                    )
                }
            )
            candidates.append(
                _Item(
                    "installation",
                    key,
                    tuple(sorted(installation_id for installation_id, *_ in members)),
                    last_used,
                    last_used > evidence.cutoff,
                    installed,
                    tuple(profiles),
                    tuple(sorted(shared.items())),
                    # What is not a model object (runtime files, private copies)
                    # goes with the installation alone.
                    max(0, installed - sum(shared.values())),
                )
            )
        return _freeable_in_order(candidates, holders, kept, kept_bytes)

    def _uninstall(self, installation_id: str, group: Collection[str] = ()) -> None:
        # The installations of one model go together: none of them keeps the
        # shared files, so the uninstall asks the Spark to reclaim them (it
        # removes only what no other installation still links).
        others = tuple(item for item in group if item != installation_id)
        plan = self._lifecycle.preview_uninstall(installation_id, also_removing=others)
        if not plan.allowed:
            code = plan.blockers[0].code if plan.blockers else "not allowed"
            raise _Kept(f"uninstall blocked ({code})")

        def still_unused(session: Session) -> None:
            reason = _installation_kept(
                session,
                installation_id,
                _Evidence.read(session, self._clock()),
            )
            if reason is not None:
                raise _Kept(reason)

        self._lifecycle.uninstall(
            installation_id,
            plan_digest=plan.plan_digest,
            actor=ACTOR,
            request_id=str(uuid.uuid4()),
            unattended_guard=still_unused,
            also_removing=others,
        )

    # -- NAS: runtime image receipts and model files ----------------------------

    def _nas_items(
        self, session: Session, evidence: _Evidence, kept: Counter[str]
    ) -> list[_Item]:
        return [
            *self._image_items(evidence, kept),
            *self._model_items(session, evidence, kept),
        ]

    def _image_items(self, evidence: _Evidence, kept: Counter[str]) -> list[_Item]:
        if self._images is None or self._image_cache is None:
            return []
        items: list[_Item] = []
        for path in sorted(self._image_cache.glob(f"*{_RECEIPT_SUFFIX}")):
            archive = path.name.removesuffix(_RECEIPT_SUFFIX)
            if len(archive) != 64 or any(c not in "0123456789abcdef" for c in archive):
                continue
            modified = _mtime(path)
            reason = _image_kept(archive, evidence, modified)
            if reason is not None:
                kept[f"image receipt: {reason}"] += 1
                continue
            assert modified is not None
            try:
                size = self._images.published_archive_bytes(archive)
            except (OSError, RuntimeImagePreparationError, ValueError):
                size = 0
            if size <= 0:
                # A damaged manifest is removable: its receipt names an image
                # that cannot be served, and its files go with the next sweep.
                size = self._damaged_manifest_bytes(archive)
            if size <= 0:
                kept["image receipt: holds no bytes"] += 1
                continue
            items.append(
                _Item(
                    "image",
                    archive,
                    (archive,),
                    modified,
                    modified > evidence.cutoff or archive in evidence.recent_archives,
                    size,
                )
            )
        return items

    def _damaged_manifest_bytes(self, archive: str) -> int:
        """The on-disk size of a damaged manifest, or 0 when it is not damaged."""

        assert self._images is not None
        try:
            self._images.layout.read(f"sha256:{archive}")
        except OciImageStoreError as error:
            if error.code not in DAMAGED_MANIFEST_CODES:
                return 0
            try:
                return max(
                    1, self._images.layout.blob_path(f"sha256:{archive}").stat().st_size
                )
            except OSError:
                return 0
        return 0

    def _remove_receipt(self, archive: str, path: Path) -> None:
        """Remove one receipt under its publication lock and reference gate.

        The artifact lock is taken first and never waited for; inside it, one
        short transaction holds the image's reference gate (a consumer that
        wants the image meanwhile gets a busy answer and retries), proves the
        image unused again and unlinks the receipt. A load that wants the
        image afterwards prepares it again, as for any cache miss.
        """

        assert self._images is not None
        now = self._clock()
        with (
            self._images.publication_lock(archive),
            self._sessions.begin() as session,
        ):
            rows = lock_reference_gates(
                session, (ArtifactIdentity("runtime-image", archive),), now=now
            )
            if any(row.removal_owner_id is not None for row in rows):
                raise _Kept("another removal owns it")
            evidence = _Evidence.read(session, now)
            reason = _image_kept(archive, evidence, _mtime(path))
            if reason is None and runtime_image_reference_findings(
                session, (archive,)
            ).get(archive):
                reason = "referenced"
            if reason is not None:
                raise _Kept(reason)
            self._images.remove_published(archive)

    def _model_items(
        self, session: Session, evidence: _Evidence, kept: Counter[str]
    ) -> list[_Item]:
        if self._model_cache is None:
            return []
        rows = session.execute(
            select(
                ModelCacheSet.artifact_set_sha256,
                ModelCacheSet.model_content_sha256,
                ModelCacheSet.verified_bytes,
                ModelCacheSet.last_accessed_at,
                ModelCacheSet.updated_at,
            )
        ).all()
        by_model: dict[str, list[tuple[str, int, datetime, datetime]]] = defaultdict(
            list
        )
        unidentified = 0
        for set_digest, model_digest, verified, accessed, updated in rows:
            if model_digest is None:
                unidentified += 1
            else:
                by_model[model_digest].append(
                    (set_digest, verified, _utc(accessed), _utc(updated))
                )
        if unidentified:
            kept["model: no model identity"] += unidentified
        items: list[_Item] = []
        for digest, sets in sorted(by_model.items()):
            reason = _model_kept(session, digest, evidence)
            if reason is not None:
                kept[f"model: {reason}"] += 1
                continue
            size = sum(verified for _set, verified, _a, _u in sets)
            if size <= 0:
                kept["model: holds no bytes"] += 1
                continue
            last_used = max(
                max(accessed, updated) for _set, _v, accessed, updated in sets
            )
            items.append(
                _Item(
                    "model",
                    digest,
                    (digest,),
                    last_used,
                    last_used > evidence.cutoff
                    or any(
                        set_digest in evidence.recent_sets for set_digest, *_ in sets
                    ),
                    size,
                )
            )
        return items

    def _remove_model(self, digest: str) -> None:
        assert self._model_cache is not None

        def verify(session: Session, sets: tuple[str, ...]) -> None:
            reason = _model_kept(
                session, digest, _Evidence.read(session, self._clock())
            )
            if reason is None and any(
                model_set_reference_findings(session, sets).values()
            ):
                reason = "referenced"
            if reason is not None:
                raise _Kept(reason)

        self._model_cache.accept_unused_removal(
            digest, actor=ACTOR, request_key=str(uuid.uuid4()), verify=verify
        )


# -- the rules, one function per kind -----------------------------------------


def _installation_kept(
    session: Session, installation_id: str, evidence: _Evidence
) -> str | None:
    """Why an installation stays, or ``None`` when nothing uses it."""

    installation = session.get(RecipeInstallation, installation_id)
    if installation is None or installation.state != InstallationState.INSTALLED:
        return "not installed"
    revision = session.execute(
        select(
            CatalogDocumentRevision.document_id,
            CatalogDocumentRevision.publisher,
            CatalogDocumentRevision.slug,
        ).where(CatalogDocumentRevision.id == installation.recipe_revision_id)
    ).one_or_none()
    newest = evidence.newest.get(revision.document_id) if revision else None
    if revision is None or newest is None:
        return "recipe unavailable"
    nodes = set(
        session.scalars(
            select(InstallationNode.node_id).where(
                InstallationNode.installation_id == installation_id
            )
        )
    )
    if evidence.pointers is None or evidence.loaded_pointers is None:
        return "profiles unreadable"
    if newest[0] == installation.recipe_revision_id and _points_at(
        evidence.loaded_pointers, revision.publisher, revision.slug, nodes
    ):
        return "profile"
    runs = session.execute(
        select(RecipeRun.id, RecipeRun.state).where(
            RecipeRun.installation_id == installation_id
        )
    ).all()
    if any(state not in _DEAD_RUNS for _id, state in runs):
        return "running"
    if any(state == "failed" for _id, state in runs):
        # Uninstall refuses a run that was never stopped; nothing here stops one.
        return "run not stopped"
    if (
        installation_id in evidence.operations
        or any(run_id in evidence.operations for run_id, _state in runs)
        or not evidence.owned_nodes.isdisjoint(nodes)
        or any(
            node_id in scope for scope in evidence.active_scopes for node_id in nodes
        )
    ):
        return "live operation"
    if not evidence.application_references_readable:
        return "profile application unreadable"
    return None


def _points_at(
    pointers: Mapping[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]],
    publisher: str,
    slug: str,
    nodes: set[str],
) -> tuple[str, ...]:
    """The profiles in ``pointers`` that name this recipe on any of ``nodes``."""

    return tuple(
        name
        for name, sparks in pointers.get((publisher.casefold(), slug.casefold()), ())
        if nodes & sparks
    )


def _installation_pointing(
    session: Session,
    installation_id: str,
    evidence: _Evidence,
    pointers: Mapping[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]] | None,
) -> tuple[str, ...]:
    """The saved profiles that point at this installation (newest revision of
    their recipe, on its Sparks); empty when none or when unreadable."""

    installation = session.get(RecipeInstallation, installation_id)
    if installation is None or pointers is None:
        return ()
    revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
    newest = evidence.newest.get(revision.document_id) if revision else None
    if revision is None or newest is None or newest[3] != revision.content_digest:
        return ()
    nodes = set(
        session.scalars(
            select(InstallationNode.node_id).where(
                InstallationNode.installation_id == installation_id
            )
        )
    )
    return _points_at(pointers, revision.publisher, revision.slug, nodes)


def _installation_last_used(session: Session, installation_id: str) -> datetime:
    """When the installation, one of its Sparks or one of its runs last changed."""

    installation = session.get(RecipeInstallation, installation_id)
    assert installation is not None
    stamps = [_utc(installation.updated_at)]
    stamps.extend(
        _utc(updated)
        for updated in session.scalars(
            select(InstallationNode.updated_at).where(
                InstallationNode.installation_id == installation_id
            )
        )
    )
    stamps.extend(
        _utc(updated)
        for updated in session.scalars(
            select(RecipeRun.updated_at).where(
                RecipeRun.installation_id == installation_id
            )
        )
    )
    return max(stamps)


#: Profile applications that still hold what their plans resolved.
_UNFINISHED_LOADS = ("queued", "running")


def _applied_revision_ids(session: Session) -> frozenset[str] | None:
    """Read exact pending references; damage cannot prove any artifact unused."""

    found: set[str] = set()
    for application in session.scalars(
        select(FleetProfileApplication).where(
            FleetProfileApplication.state.in_(_UNFINISHED_LOADS)
        )
    ):
        try:
            plan = FleetProfilePreview.model_validate_json(
                json.dumps(application.plan), strict=True
            )
        except (TypeError, ValueError):
            return None
        if (
            plan.profile_id != application.profile_id
            or plan.profile_digest != application.profile_digest
            or plan.plan_digest != application.plan_digest
        ):
            return None
        found.update(item.recipe_revision_id for item in plan.resolved_assignments)
    return frozenset(found)


def _model_kept(session: Session, digest: str, evidence: _Evidence) -> str | None:
    """Why a model's cached files stay, or ``None`` when no profile needs them."""

    sets = session.execute(
        select(
            ModelCacheSet.artifact_set_sha256,
            ModelCacheSet.recipe_revision_sha256,
        ).where(ModelCacheSet.model_content_sha256 == digest)
    ).all()
    if digest in evidence.pointed_models or any(
        recipe in evidence.pointed_digests for _s, recipe in sets
    ):
        return "profile"
    names = {digest} | {set_digest for set_digest, _r in sets}
    names.update(
        session.scalars(
            select(ModelCacheSetArtifact.artifact_sha256).where(
                ModelCacheSetArtifact.artifact_set_sha256.in_(names)
            )
        )
    )
    if not evidence.live.isdisjoint(names):
        return "live operation"
    if not evidence.application_references_readable:
        return "profile application unreadable"
    return None


def _image_kept(
    archive: str, evidence: _Evidence, modified: datetime | None
) -> str | None:
    """Why an image receipt stays, or ``None`` when no profile needs the image."""

    if archive in evidence.pointed_archives:
        return "profile"
    if modified is None:
        return "receipt unreadable"
    if archive in evidence.live:
        return "live operation"
    if not evidence.application_references_readable:
        return "profile application unreadable"
    return None


# -- helpers --------------------------------------------------------------------


def _latest_snapshots(
    session: Session, node_id: str | None = None
) -> dict[str, tuple[int, int, datetime]]:
    """Each live Spark's latest reported (free, total, observed at) disk."""

    latest = select(
        NodeInventorySnapshot.node_id,
        func.max(NodeInventorySnapshot.observed_at).label("observed_at"),
    ).group_by(NodeInventorySnapshot.node_id)
    if node_id is not None:
        latest = latest.where(NodeInventorySnapshot.node_id == node_id)
    newest = latest.subquery()
    rows = session.execute(
        select(
            NodeInventorySnapshot.node_id,
            NodeInventorySnapshot.disk_free_bytes,
            NodeInventorySnapshot.disk_total_bytes,
            NodeInventorySnapshot.observed_at,
        )
        .join(
            newest,
            and_(
                NodeInventorySnapshot.node_id == newest.c.node_id,
                NodeInventorySnapshot.observed_at == newest.c.observed_at,
            ),
        )
        .join(AgentNode, AgentNode.node_id == NodeInventorySnapshot.node_id)
        .where(AgentNode.revoked_at.is_(None))
    )
    return {
        found: (free, total, _utc(observed)) for found, free, total, observed in rows
    }


def _spark_settling(session: Session, node_id: str, observed_at: datetime) -> bool:
    """A removal this collector queued is still running, or finished after the
    Spark last reported its free space, so that report does not show it yet."""

    for targets in session.scalars(
        select(Job.targets).where(
            Job.kind == "recipe.uninstall",
            Job.actor == ACTOR,
            or_(Job.state.not_in(_FINISHED_JOBS), Job.updated_at >= observed_at),
        )
    ):
        if isinstance(targets, list) and node_id in targets:
            return True
    return False


def _model_removal_in_flight(session: Session) -> bool:
    return (
        session.scalar(
            select(ModelCacheOperation.id)
            .where(
                ModelCacheOperation.kind == "remove",
                ModelCacheOperation.state.in_(model_cache_states.LIVE),
            )
            .limit(1)
        )
        is not None
    )


def _profile_pointers(
    session: Session, only_profile_id: str | None = None
) -> dict[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]] | None:
    """The profile name and Spark set each saved profile assigns to each recipe.

    ``None`` when a profile cannot be read as the current contract: nothing is
    then provably unpointed, so the caller keeps everything an assignment might
    name.  ``only_profile_id`` restricts it to one profile (the loaded one).
    """

    found: dict[tuple[str, str], list[tuple[str, frozenset[str]]]] = {}
    statement = select(FleetProfile.name, FleetProfile.assignments)
    if only_profile_id is not None:
        statement = statement.where(FleetProfile.id == only_profile_id)
    try:
        for name, assignments in session.execute(statement):
            for assignment in _ASSIGNMENTS.validate_json(
                canonical_message(assignments), strict=True
            ):
                publisher, separator, slug = assignment.recipe_selector.partition("/")
                if not separator:
                    return None
                found.setdefault((publisher.casefold(), slug.casefold()), []).append(
                    (name, frozenset(assignment.spark_ids))
                )
    except (TypeError, ValueError):
        return None
    return {key: tuple(value) for key, value in found.items()}


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def _code(error: Exception) -> str:
    return str(getattr(error, "code", type(error).__name__))


def _utc(value: datetime) -> datetime:
    # SQLite returns naive timestamps for timezone-aware columns.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


__all__ = ["ACTOR", "GRACE", "Swept", "UnusedStorageCollector"]
