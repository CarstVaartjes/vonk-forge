"""Collect catalog revisions nothing uses any more.

A recipe refresh replaces a recipe in place: the head pointer moves to the new
revision and the old one stays behind as a ``superseded`` row, forever unless
something removes it. Nothing offers a superseded revision any more, so this
collector removes it once it is proven unused, hourly and without a schema
change. A model revision follows as soon as no recipe revision pins it.

A revision is **kept** while anything still points at it:

* it is the head's active or candidate revision, or the newest revision number
  of its document (so revision numbers never repeat);
* an installation of it is anything but ``uninstalled`` (``failed`` and
  ``partial`` may have left files), one of its runs is still ``stopping`` or
  ``lost`` or otherwise active, its canonical planned nodes lack reconciled
  current-generation process absence, a build is in flight, or a job names its
  runs;
* it was superseded, or an installation, run, build or mapping of it was last
  touched, less than a grace period ago;
* a live or recently finished operation names it: a Job, agent operation,
  profile application (and the selected one always), model-cache operation or
  recipe job payload is searched for its id, so a reference kept in JSON is
  found without knowing which contract wrote it;
* a build of it is the newest image its recipe's head would run (an editorial
  successor that reuses the image by content has no build of its own);
* a kept recipe revision still pins it as its model, by binding or because a
  head or candidate recipe document names its digest.

Everything else is removed together with the dead rows only it owned: its
uninstalled installations, stopped runs, mappings, finished builds and model
bindings. Audit history of those is not kept.

A source bundle (archive bytes stored in PostgreSQL) is removed once no
remaining revision, build or live payload names its digest and it is older than
the grace period.

Revisions are removed in bounded batches walked by a cursor, so every pass
moves on from where the last one stopped and nothing ever waits for a global
fence. Each revision is rechecked and deleted under its own savepoint: one that
cannot be removed (a reference appeared, a constraint refused, a row is locked)
is skipped and retried at the next sweep, so a failure never stops the rest and
never touches a workload. References the database keeps (heads, installations,
builds, mappings, model bindings) refuse the delete through their ``RESTRICT``
foreign keys, and a writer still holding one wins after a short lock wait. The
references kept in JSON are read once per pass to skip what is named, and read
again inside the batch transaction after its deletes: anything named there by
a committed write rolls the batch back and is kept, and the rest is retried.
A JSON reference still uncommitted at that recheck is not seen: it can only
name a revision superseded longer than the grace period, and the request that
wrote it finds the revision gone (its foreign-key writes are refused).
Cached model files and runtime-image receipts are not owned by a revision (they
are content-addressed and shared) and are left to their own request-led removal.

Revisions are read as plain columns and deleted with Core statements. Loading
one as an ORM object would run the immutability guards on an old-contract
document that can no longer be read, and these rows are exactly the ones that
may be unreadable.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any

from sqlalchemy import (
    Select,
    and_,
    delete,
    exists,
    false,
    or_,
    select,
    text,
    union_all,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker
from vonk_agent_protocol import (
    TERMINAL_LIFECYCLE_STATES,
    InstallationState,
    LifecycleSubject,
    RunState,
    WaitReason,
    canonical_message,
    stored_words,
)
from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName
from vonk_forge_contracts import ModelDefinition

from .agent_jobs import release_owned_reservations_in_session
from .catalog_revision_contract import UnprojectedRevision, cached_catalog_document
from .logging import log_event
from .model_cache_contract import CacheManifest, CacheOperationKind
from .models import (
    AgentOperation,
    ArtifactJob,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfileApplication,
    FleetProfileSelection,
    InstallationNode,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RecipeSourceBundle,
    RunNode,
    SourceBundleArchive,
)
from .owner_liveness import builds_in_flight
from .revision_images import revision_images
from .stored_json import binding_for
from .wait_contract import WaitOwnerKind

_LOGGER = logging.getLogger(__name__)
GRACE = timedelta(hours=24)
INTERVAL = timedelta(hours=1)
# Rows fetched per round trip when scanning stored documents; bounds a scan by
# the largest few documents instead of the whole table.
_SCAN_BATCH = 20
# A pass starts no new batch or phase after this. A batch takes rows for this
# long (always at least one) and its one recheck gets this long again; the rest
# continues from the cursor on the next worker pass. One pass is bounded by its
# inventory reads, this budget and one more batch, so a large backlog never
# holds the worker loop for long.
SWEEP_BUDGET_SECONDS = 20.0
# Candidates removed in one transaction; the JSON references are read once per
# batch, so the recheck costs one scan per batch, not one per candidate.
_BATCH = 50
# A row a writer holds is skipped after this wait, never queued for: the writer
# wins and the row is retried at the next sweep.
_LOCK_TIMEOUT_MS = 250
_FINISHED_BUILDS = ("succeeded", "failed")
# Terminal states are candidates only; exact current-generation absence is
# independently required before releasing claims and discarding recovery rows.
_DEAD_RUNS = (RunState.STOPPED, RunState.FAILED)
# A uuid (a revision, installation or run id) or a sha256 (a source bundle).
_TOKEN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{64}"
)


class _Phase(StrEnum):
    """What the sweep cursor walks, in this order.

    Recipe revisions go first, so a model revision only a collected recipe
    revision pinned follows in the same sweep; source bundles go last, once no
    removed revision names them.
    """

    RECIPE = RunSwitchReasonScopeName.RECIPE.value
    MODEL = RunSwitchReasonScopeName.MODEL.value
    BUNDLE = "bundle"


class _Kept(Exception):
    """The revision is still referenced; leave it for a later sweep."""


class _Unfinished(Exception):
    """The batch recheck could not finish (damaged row, scan over budget).

    Nothing of the batch was removed. Exactly like an inventory that cannot
    finish, it proves nothing unused: the sweep ends and starts over at its
    next interval, not on the next worker pass.
    """


class _Rescinded(Exception):
    """The batch recheck found removed rows named since the inventory.

    The batch rolls back and the pass ends; the named rows are skipped for
    the rest of the sweep, so the batch is retried smaller on the next pass.
    """

    def __init__(self, named: frozenset[str]) -> None:
        super().__init__(len(named))
        self.named = named


@dataclass(frozen=True, slots=True)
class _Candidate:
    id: str
    kind: str
    publisher: str
    slug: str
    content_digest: str

    @property
    def names(self) -> frozenset[str]:
        return frozenset({self.id, self.content_digest})


@dataclass(frozen=True, slots=True)
class _Bundle:
    id: str

    @property
    def names(self) -> frozenset[str]:
        return frozenset({self.id})


@dataclass(frozen=True, slots=True)
class Collected:
    revisions: int
    bundles: int
    kept: dict[str, int]


class CatalogRevisionCollector:
    """Hourly sweep of unused superseded catalog revisions."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        budget_seconds: float = SWEEP_BUDGET_SECONDS,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._budget_seconds = budget_seconds
        self._due_at: datetime | None = None
        # Where the next pass continues (phase, last id passed); None starts a
        # new sweep. Held in memory: a restarted worker simply starts over.
        self._cursor: tuple[str, str] | None = None
        # Rows a batch recheck found named during this sweep; skipped until
        # the sweep starts over, so a rescinded batch comes back smaller.
        self._named: set[str] = set()

    def tick(self) -> bool:
        """Collect at most once per interval; True when anything was removed."""

        now = self._clock()
        if self._due_at is not None and now < self._due_at:
            return False
        self._due_at = now + INTERVAL
        try:
            result = self.collect()
        except SQLAlchemyError as error:
            # A database fault proves nothing unused; try again next interval.
            log_event(
                _LOGGER,
                "catalog_revision.sweep_failed",
                service="control-worker",
                code=type(error).__name__,
            )
            return False
        return result.revisions > 0 or result.bundles > 0

    def collect(self) -> Collected:
        """One pass: batches from the cursor until the sweep ends or the budget.

        Never waits for another writer: a held row is skipped, and the pass
        continues with the next batch.
        """

        now = self._clock()
        cutoff = now - GRACE
        if self._cursor is None:
            self._named = set()
        start, after = self._cursor or (_Phase.RECIPE, "")
        removed = bundles = 0
        kept: Counter[str] = Counter()
        deadline: float | None = None
        for phase in tuple(_Phase)[tuple(_Phase).index(start) :]:
            if deadline is not None and time.monotonic() > deadline:
                return self._resume(phase, after, now, removed, bundles, kept)
            # Bundle refusals are logged, not reported as kept revisions.
            counts = Counter[str]() if phase == _Phase.BUNDLE else kept
            try:
                for batch, named in self._batches(phase, after, now, cutoff):
                    if deadline is None:
                        # The budget starts after the inventory, so every pass
                        # reaches a batch.
                        deadline = time.monotonic() + self._budget_seconds
                    pending = []
                    for item in batch:
                        if named.isdisjoint(item.names) and item.id not in self._named:
                            pending.append(item)
                        else:
                            counts["live operation"] += 1
                    try:
                        gone, stopped = self._remove_batch(
                            phase, pending, now, cutoff, counts
                        )
                    except _Rescinded as rescinded:
                        # A conflict re-plans: the next pass retries this batch
                        # without the rows named since the inventory.
                        counts["live operation"] += len(rescinded.named)
                        self._named |= rescinded.named
                        return self._resume(phase, after, now, removed, bundles, kept)
                    except _Unfinished:
                        # As an inventory that cannot finish: start over at the
                        # interval tick already set.
                        self._cursor = None
                        return self._swept(removed, bundles, kept)
                    if phase == _Phase.BUNDLE:
                        bundles += gone
                    else:
                        removed += gone
                    if stopped is not None:
                        return self._resume(phase, stopped, now, removed, bundles, kept)
                    after = batch[-1].id
                    if time.monotonic() > deadline:
                        return self._resume(phase, after, now, removed, bundles, kept)
            except _Kept as reason:
                # A damaged or unfinished reference scan proves nothing unused.
                kept[str(reason)] += 1
                break
            after = ""
        self._cursor = None
        return self._swept(removed, bundles, kept)

    def _resume(
        self,
        phase: _Phase,
        after: str,
        now: datetime,
        removed: int,
        bundles: int,
        kept: Counter[str],
    ) -> Collected:
        """End the pass; the next worker pass continues after ``after``."""

        self._cursor = (phase, after)
        self._due_at = now
        return self._swept(removed, bundles, kept)

    @staticmethod
    def _swept(removed: int, bundles: int, kept: Counter[str]) -> Collected:
        if removed or bundles or kept:
            log_event(
                _LOGGER,
                "catalog_revision.swept",
                service="control-worker",
                removed=removed,
                bundles=bundles,
                kept=dict(kept),
            )
        return Collected(removed, bundles, dict(kept))

    def _batches(
        self, phase: _Phase, after: str, now: datetime, cutoff: datetime
    ) -> Iterator[tuple[list[Any], frozenset[str]]]:
        """Each next batch after ``after``, with what live rows name beside it.

        The inventory is read once per phase, after its first batch, only to
        skip what is named now; the batch recheck decides.
        """

        batch = self._next_batch(phase, after, cutoff)
        if not batch:
            return
        with self._sessions() as session:
            deadline = time.monotonic() + self._budget_seconds
            _bound_statements(session, deadline)
            named = self._references(session, phase, now, deadline)
        while batch:
            yield batch, named
            batch = self._next_batch(phase, batch[-1].id, cutoff)

    def _next_batch(self, phase: _Phase, after: str, cutoff: datetime) -> list[Any]:
        with self._sessions() as session:
            if phase == _Phase.BUNDLE:
                return self._bundle_candidates(session, cutoff, after=after)
            return self._candidates(session, cutoff, kind=phase, after=after)

    def _references(
        self,
        session: Session,
        phase: _Phase,
        now: datetime,
        deadline: float | None = None,
        *,
        since: datetime | None = None,
    ) -> frozenset[str]:
        """Every id and digest a live row names that keeps a ``phase`` row.

        For Model revisions that includes the digests a cached set holds (NAS
        models stay until space is short) and the ones another Model declares
        as a dependency; ``since`` limits the latter to Models created since
        then (the recheck only needs those the inventory cannot have seen).
        """

        if deadline is None:
            deadline = time.monotonic() + self._budget_seconds
        found = live_tokens(session, now, require_complete=True, deadline=deadline)
        if phase == _Phase.MODEL:
            found |= _model_dependencies(session, since=since) | (
                self._cached_model_digests(session) or frozenset()
            )
        if phase != _Phase.BUNDLE:
            return found | pinned_by_heads(
                session, require_complete=True, deadline=deadline
            )
        return (
            found
            | streamed_tokens(
                session,
                select(CatalogDocumentRevision.projected),
                require_complete=True,
                deadline=deadline,
            )
            | frozenset(session.scalars(select(RecipeBuild.source_bundle_sha256)))
            | frozenset(
                digest
                for digest in session.scalars(
                    select(CatalogDocumentRevision.source_bundle_sha256)
                )
                if digest is not None
            )
        )

    def _remove_batch(
        self,
        phase: _Phase,
        pending: list[Any],
        now: datetime,
        cutoff: datetime,
        kept: Counter[str],
    ) -> tuple[int, str | None]:
        """Remove ``pending`` in one transaction, each under its own savepoint.

        Rows are taken for one budget, and always at least one; the recheck
        then reads the references again in the same transaction, with a
        budget of its own (the same scan as the inventory, so it fails only
        where the inventory would). Returns how many were removed and, when
        the row budget ended the batch early, the last row tried.

        Raises ``_Rescinded`` when a write committed since the inventory names
        a removed row, and ``_Unfinished`` when the recheck cannot finish;
        either way nothing of the batch is removed.
        """

        removed: list[tuple[Any, frozenset[str]]] = []
        stopped_after: str | None = None
        try:
            with self._sessions.begin() as session:
                rows_until = time.monotonic() + self._budget_seconds
                _bound_lock_waits(session, rows_until)
                for index, item in enumerate(pending):
                    if index and time.monotonic() > rows_until:
                        stopped_after = pending[index - 1].id
                        break
                    try:
                        with session.begin_nested():
                            names = (
                                self._remove_bundle(session, item, cutoff)
                                if phase == _Phase.BUNDLE
                                else self._remove(session, item, cutoff, now)
                            )
                    except _Kept as reason:
                        kept[str(reason)] += 1
                        continue
                    except SQLAlchemyError as error:
                        # A constraint refused or a writer holds a row.
                        kept[f"refused {type(error).__name__}"] += 1
                        log_event(
                            _LOGGER,
                            "catalog_revision.refused",
                            service="control-worker",
                            phase=phase.value,
                            id=item.id,
                            code=type(error).__name__,
                        )
                        continue
                    removed.append((item, names))
                if removed:
                    recheck_until = time.monotonic() + self._budget_seconds
                    _bound_statements(session, recheck_until)
                    fresh = self._references(
                        session, phase, now, recheck_until, since=cutoff
                    )
                    named = frozenset(
                        item.id
                        for item, names in removed
                        if not fresh.isdisjoint(names)
                    )
                    if named:
                        raise _Rescinded(named)
        except (_Kept, SQLAlchemyError) as error:
            # The recheck could not finish; nothing of the batch is removed.
            kept[
                str(error)
                if isinstance(error, _Kept)
                else f"refused {type(error).__name__}"
            ] += len(removed)
            raise _Unfinished from error
        for item, _ in removed:
            log_event(
                _LOGGER,
                "catalog_revision.removed",
                service="control-worker",
                phase=phase.value,
                id=item.id,
            )
        return len(removed), stopped_after

    # -- candidates ---------------------------------------------------------

    @staticmethod
    def _candidates(
        session: Session,
        cutoff: datetime,
        *,
        kind: str = _Phase.RECIPE,
        after: str = "",
        limit: int = _BATCH,
    ) -> list[_Candidate]:
        """The next superseded revisions of ``kind`` past the grace period.

        Keyset over the primary key from ``after``. A revision is a candidate
        when no head points at it and a newer revision of its document exists,
        and it and every newer one are older than the cutoff (no row records
        when a revision was superseded; the one that replaced it was created
        at that moment).
        """

        revision = CatalogDocumentRevision
        newer = aliased(CatalogDocumentRevision)
        pointed = union_all(
            select(CatalogDocumentHead.active_revision_id.label("id")),
            select(CatalogDocumentHead.candidate_revision_id.label("id")),
        ).subquery()
        # Revision numbers are the catalog allocator's (max + 1): the newest one
        # of each document stays, so a number is never handed out twice.
        later = and_(
            newer.document_id == revision.document_id,
            newer.revision_number > revision.revision_number,
        )
        rows = session.execute(
            select(
                revision.id,
                revision.kind,
                revision.publisher,
                revision.slug,
                revision.content_digest,
            )
            .where(
                revision.kind == kind,
                revision.id > after,
                revision.created_at <= cutoff,
                ~exists().where(pointed.c.id == revision.id),
                exists().where(later),
                ~exists().where(later, newer.created_at > cutoff),
            )
            .order_by(revision.id)
            .limit(limit)
        ).all()
        return [_Candidate(*row) for row in rows]

    @staticmethod
    def _bundle_candidates(
        session: Session, cutoff: datetime, *, after: str = "", limit: int = _BATCH
    ) -> list[_Bundle]:
        """The next source bundles past the grace period, keyset by digest."""

        return [
            _Bundle(sha)
            for sha in session.scalars(
                select(RecipeSourceBundle.sha256)
                .where(
                    RecipeSourceBundle.sha256 > after,
                    RecipeSourceBundle.verified_at <= cutoff,
                )
                .order_by(RecipeSourceBundle.sha256)
                .limit(limit)
            )
        ]

    # -- one revision -------------------------------------------------------

    def _remove(
        self,
        session: Session,
        revision: _Candidate,
        cutoff: datetime,
        now: datetime,
    ) -> frozenset[str]:
        """Delete one revision and what only it owned, or raise ``_Kept``.

        Returns every id the deletes removed, for the batch recheck.
        """

        # A read: the heads' RESTRICT keys refuse deleting a revision a head
        # points at, and a head moving onto it holds its key until it commits.
        head = session.execute(
            select(
                CatalogDocumentHead.active_revision_id,
                CatalogDocumentHead.candidate_revision_id,
            ).where(
                CatalogDocumentHead.kind == revision.kind,
                CatalogDocumentHead.publisher == revision.publisher,
                CatalogDocumentHead.slug == revision.slug,
            )
        ).one_or_none()
        if head is not None and revision.id in tuple(head):
            raise _Kept("head")
        installations = _ids(
            session,
            RecipeInstallation.id,
            RecipeInstallation.recipe_revision_id == revision.id,
        )
        runs = (
            _ids(session, RecipeRun.id, RecipeRun.installation_id.in_(installations))
            if installations
            else []
        )
        builds = _ids(
            session, RecipeBuild.id, RecipeBuild.recipe_revision_id == revision.id
        )
        self._require_unused(
            session,
            revision,
            installations,
            runs,
            builds,
            cutoff,
            now,
            heads=tuple(value for value in (head or ()) if value is not None),
        )
        self._delete_dead_rows(session, revision, installations, runs, now, cutoff)
        self._require_no_remaining_reference(session, revision, cutoff)
        session.execute(
            delete(CatalogDocumentRevision).where(
                CatalogDocumentRevision.id == revision.id
            )
        )
        return revision.names | frozenset(installations) | frozenset(runs)

    @staticmethod
    def _require_unused(
        session: Session,
        revision: _Candidate,
        installations: list[str],
        runs: list[str],
        builds: list[str],
        cutoff: datetime,
        now: datetime,
        *,
        heads: tuple[str, ...] = (),
    ) -> None:
        """Raise ``_Kept`` when a live or recent row still names the revision."""

        if session.scalar(
            select(
                exists().where(
                    RecipeInstallation.recipe_revision_id == revision.id,
                    RecipeInstallation.state != InstallationState.UNINSTALLED,
                )
            )
        ):
            raise _Kept("installation")
        if runs and session.scalar(
            select(
                exists().where(
                    RecipeRun.id.in_(runs), RecipeRun.state.not_in(_DEAD_RUNS)
                )
            )
        ):
            raise _Kept("run")
        if runs and session.scalar(
            select(exists().where(ArtifactJob.run_id.in_(runs)))
        ):
            raise _Kept("recipe job")
        if session.scalar(
            select(
                exists().where(
                    RecipeBuild.recipe_revision_id == revision.id,
                    builds_in_flight(now),
                )
            )
        ):
            raise _Kept("build in flight")
        recent = (
            (RecipeInstallation, RecipeInstallation.updated_at),
            (RecipeBuild, RecipeBuild.updated_at),
            (ClusterMapping, ClusterMapping.updated_at),
        )
        for model, stamp in recent:
            if session.scalar(
                select(
                    exists().where(
                        model.recipe_revision_id == revision.id,
                        stamp > cutoff,
                    )
                )
            ):
                raise _Kept("recent")
        if runs and session.scalar(
            select(
                exists().where(RecipeRun.id.in_(runs), RecipeRun.updated_at > cutoff)
            )
        ):
            raise _Kept("recent")
        # An editorial successor runs its predecessor's build by content, with
        # no build row of its own. The newest build of the recipe is the image
        # the head would run, so the revision that built it stays until the
        # head has built a newer one (unknown means keep).
        if builds and heads:
            in_use = {
                images[0].build_id
                for images in revision_images(session, heads).values()
                if images
            }
            if in_use.intersection(builds):
                raise _Kept("image reuse")

    @staticmethod
    def _delete_dead_rows(
        session: Session,
        revision: _Candidate,
        installations: list[str],
        runs: list[str],
        now: datetime,
        cutoff: datetime,
    ) -> None:
        """Delete the dead rows only this revision owned, or raise ``_Kept``.

        The state each was checked in is part of its delete: a run reopened or
        an installation reinstalled since the check no longer matches, and the
        savepoint rolls the revision's deletes back.
        """

        for run_id in runs:
            release_owned_reservations_in_session(session, "run", run_id, now)
        for installation_id in installations:
            release_owned_reservations_in_session(
                session, "installation", installation_id, now
            )
        if runs:
            session.execute(delete(RunNode).where(RunNode.run_id.in_(runs)))
            deleted = session.execute(
                delete(RecipeRun).where(
                    RecipeRun.id.in_(runs),
                    RecipeRun.state.in_(_DEAD_RUNS),
                    RecipeRun.updated_at <= cutoff,
                )
            )
            if _rowcount(deleted) != len(runs):
                raise _Kept("run reopened")
        if installations:
            session.execute(
                delete(InstallationNode).where(
                    InstallationNode.installation_id.in_(installations)
                )
            )
            deleted = session.execute(
                delete(RecipeInstallation).where(
                    RecipeInstallation.id.in_(installations),
                    RecipeInstallation.state == InstallationState.UNINSTALLED,
                    RecipeInstallation.updated_at <= cutoff,
                )
            )
            if _rowcount(deleted) != len(installations):
                raise _Kept("installation reopened")
        # A build or mapping another installation still uses stays, and keeps
        # the revision through the remaining-reference check below.
        session.execute(
            delete(RecipeBuild).where(
                RecipeBuild.recipe_revision_id == revision.id,
                RecipeBuild.state.in_(_FINISHED_BUILDS),
                ~exists().where(RecipeInstallation.recipe_build_id == RecipeBuild.id),
            )
        )
        unused_mappings = [
            ClusterMapping.recipe_revision_id == revision.id,
            ~exists().where(RecipeInstallation.mapping_id == ClusterMapping.id),
            ~exists().where(RecipeRun.mapping_id == ClusterMapping.id),
        ]
        mapping_ids = _ids(session, ClusterMapping.id, *unused_mappings)
        if mapping_ids:
            session.execute(
                delete(ClusterMappingNode).where(
                    ClusterMappingNode.mapping_id.in_(mapping_ids)
                )
            )
            session.execute(
                delete(ClusterMapping).where(ClusterMapping.id.in_(mapping_ids))
            )
        session.execute(
            delete(CatalogRecipeModelReference).where(
                CatalogRecipeModelReference.recipe_revision_id == revision.id
            )
        )

    @staticmethod
    def _cached_model_digests(session: Session) -> frozenset[str] | None:
        """Model digests any cached set verifies; ``None`` when a manifest is
        unreadable, so each revision is decided by its own recheck instead."""

        found: set[str] = set()
        for value in session.scalars(
            select(ModelCacheSet.manifest).execution_options(yield_per=_SCAN_BATCH)
        ):
            try:
                manifest = CacheManifest.model_validate_json(canonical_message(value))
            except (TypeError, ValueError):
                return None
            found.update(item.content_sha256 for item in manifest.verified_models)
        return frozenset(found)

    @staticmethod
    def _require_no_remaining_reference(
        session: Session,
        revision: _Candidate,
        cutoff: datetime,
    ) -> None:
        """The database's own foreign keys, checked first-hand before the delete.

        Revisions are immutable, so a dependent imported during the pass is the
        only thing the inventory can have missed. An importer stamps
        ``created_at`` before it commits, so the rows read here are those
        created since ``cutoff`` (the grace window), not since the pass.
        """

        if revision.kind == RunSwitchReasonScopeName.MODEL:
            for value in session.scalars(
                select(ModelCacheSet.manifest).execution_options(yield_per=_SCAN_BATCH)
            ):
                try:
                    manifest = CacheManifest.model_validate_json(
                        canonical_message(value)
                    )
                except (TypeError, ValueError):
                    raise _Kept(WaitReason.OBSERVATION_UNAVAILABLE.value) from None
                if any(
                    reference.content_sha256 == revision.content_digest
                    for reference in manifest.verified_models
                ):
                    raise _Kept(WaitOwnerKind.MODEL_CACHE.value)
            if revision.content_digest in _model_dependencies(session, since=cutoff):
                raise _Kept("model dependency")
        for name, reference in (
            (
                "recipe binding",
                CatalogRecipeModelReference.model_revision_id == revision.id,
            ),
            ("installation", RecipeInstallation.recipe_revision_id == revision.id),
            ("mapping", ClusterMapping.recipe_revision_id == revision.id),
            ("build", RecipeBuild.recipe_revision_id == revision.id),
        ):
            if session.scalar(select(exists().where(reference))):
                raise _Kept(name)

    # -- source bundles -----------------------------------------------------

    @staticmethod
    def _remove_bundle(
        session: Session, bundle: _Bundle, cutoff: datetime
    ) -> frozenset[str]:
        """Delete one bundle unless a put refreshed it since the inventory.

        Archive bytes are in this same database, so deletion has no external
        storage wait. The grace condition is part of the delete itself, so a
        concurrent put (which refreshes ``verified_at``) wins: committed, it no
        longer matches; in flight, it holds the row and the delete is refused.
        """

        deleted = session.execute(
            delete(RecipeSourceBundle).where(
                RecipeSourceBundle.sha256 == bundle.id,
                RecipeSourceBundle.verified_at <= cutoff,
            )
        )
        if _rowcount(deleted) != 1:
            raise _Kept("stored again")
        session.execute(
            delete(SourceBundleArchive).where(SourceBundleArchive.sha256 == bundle.id)
        )
        return bundle.names


def _model_dependencies(
    session: Session, since: datetime | None = None
) -> frozenset[str]:
    """Model digests any readable Model revision declares as a dependency.

    ``since`` limits the read to revisions created at or after it.

    A document this contract cannot read resolves no set (the model cache
    reports it missing), so it protects nothing and never holds the sweep.
    """

    found: set[str] = set()
    for digest, document in session.execute(
        select(CatalogDocumentRevision.content_digest, CatalogDocumentRevision.document)
        .where(
            CatalogDocumentRevision.kind == RunSwitchReasonScopeName.MODEL,
            *(
                (CatalogDocumentRevision.created_at >= since,)
                if since is not None
                else ()
            ),
        )
        .execution_options(yield_per=_SCAN_BATCH)
    ):
        try:
            model = cached_catalog_document(
                RunSwitchReasonScopeName.MODEL, digest, document
            )
        except (TypeError, ValueError):
            continue
        if isinstance(model, ModelDefinition):
            found.update(item.content_sha256 for item in model.dependencies)
    return frozenset(found)


def not_among(column, words: Collection[str]):
    """``column NOT IN words`` spelled as ranges between the sorted words.

    The same rows (a word outside ``words`` is still selected), but the state
    index can answer it. Ranges follow ``sorted`` only while every word is
    lowercase letters, whatever the database collation orders; the vocabulary
    test holds the ended words to that.
    """

    ordered = sorted(set(words))
    return or_(
        column < ordered[0],
        *(and_(column > low, column < high) for low, high in pairwise(ordered)),
        column > ordered[-1],
    )


def operation_tokens(
    session: Session,
    now: datetime,
    *,
    recent: bool = True,
    require_complete: bool = False,
    include_removals: bool = True,
    deadline: float | None = None,
    superseded_applications: Collection[str] = (),
) -> frozenset[str]:
    """Every id and digest named by a live or recently finished operation.

    Tokens include references nested inside engine-owned extension fields.
    Collection requires the current owning contract to validate first; a raw
    token observation alone cannot establish absence. With ``recent=False``
    only operations that have not ended count (an expired job has ended).
    ``superseded_applications`` are profile loads a reviewed load replaces:
    they name nothing for it. Selected desired state is protected by its
    profile references, not by historical operation effects.
    """

    window = now - GRACE if recent else None

    def live(state, updated, subject=LifecycleSubject.JOB):
        ended = stored_words(subject, TERMINAL_LIFECYCLE_STATES)
        unended = not_among(state, ended)
        return unended if window is None else or_(unended, updated >= window)

    selected = session.scalar(select(FleetProfileSelection.application_id))
    sources = (
        # Runtime preparation records its exact installation in durable results
        # before the next child exists. Those effects are references too.
        *(
            select(column).where(live(Job.state, Job.updated_at), column.is_not(None))
            for column in (Job.payload, Job.result)
        ),
        select(AgentOperation.payload).where(
            live(
                AgentOperation.state,
                AgentOperation.updated_at,
                LifecycleSubject.AGENT_OPERATION,
            )
        ),
        *(
            select(column).where(
                or_(
                    and_(
                        live(
                            FleetProfileApplication.state,
                            FleetProfileApplication.updated_at,
                            LifecycleSubject.FLEET_PROFILE_APPLICATION,
                        ),
                        FleetProfileApplication.id.not_in(
                            tuple(superseded_applications)
                        ),
                    ),
                    FleetProfileApplication.id == selected if recent else false(),
                ),
                column.is_not(None),
            )
            for column in (
                FleetProfileApplication.plan,
                FleetProfileApplication.progress,
            )
        ),
        select(ModelCacheOperation.payload).where(
            live(
                ModelCacheOperation.state,
                ModelCacheOperation.updated_at,
                LifecycleSubject.MODEL_CACHE_OPERATION,
            ),
            ModelCacheOperation.kind.is_not(None)
            if include_removals
            else ModelCacheOperation.kind != CacheOperationKind.REMOVE,
        ),
        select(ArtifactJob.compiled_contract).where(
            # A job still being prepared has no lifecycle state yet.
            or_(
                ArtifactJob.state.is_(None),
                live(
                    ArtifactJob.state,
                    ArtifactJob.updated_at,
                    LifecycleSubject.ARTIFACT_JOB,
                ),
            )
        ),
    )
    found: set[str] = set()
    for statement in sources:
        found |= streamed_tokens(
            session, statement, require_complete=require_complete, deadline=deadline
        )
    return frozenset(found)


def live_tokens(
    session: Session,
    now: datetime,
    *,
    require_complete: bool = False,
    include_removals: bool = True,
    deadline: float | None = None,
) -> frozenset[str]:
    """Operation tokens plus what every installation, run and build still names."""

    found: set[str] = set(
        operation_tokens(
            session,
            now,
            require_complete=require_complete,
            deadline=deadline,
            include_removals=include_removals,
        )
    )
    found |= plan_tokens(
        session, now, require_complete=require_complete, deadline=deadline
    )
    return frozenset(found)


def plan_tokens(
    session: Session,
    now: datetime,
    *,
    require_complete: bool = False,
    deadline: float | None = None,
) -> frozenset[str]:
    """What every installation, run and build still names (no operations)."""

    sources = (
        select(RecipeInstallation.plan).where(
            RecipeInstallation.state != InstallationState.UNINSTALLED
        ),
        select(RecipeRun.plan).where(RecipeRun.state.not_in(_DEAD_RUNS)),
        select(RecipeBuild.plan).where(builds_in_flight(now)),
    )
    found: set[str] = set()
    for statement in sources:
        found |= streamed_tokens(
            session, statement, require_complete=require_complete, deadline=deadline
        )
    return frozenset(found)


def pinned_by_heads(
    session: Session,
    *,
    require_complete: bool = False,
    deadline: float | None = None,
) -> frozenset[str]:
    """Digests the head and candidate recipe documents name (their models).

    A recipe binds its model revision when it is activated, so a pending
    candidate has no binding yet; the digest in its own document is what it
    will bind to.
    """

    heads = select(CatalogDocumentHead.active_revision_id).union(
        select(CatalogDocumentHead.candidate_revision_id)
    )
    return streamed_tokens(
        session,
        select(CatalogDocumentRevision.document).where(
            CatalogDocumentRevision.kind == "recipe",
            CatalogDocumentRevision.id.in_(heads),
        ),
        require_complete=require_complete,
        deadline=deadline,
    )


def streamed_tokens(
    session: Session,
    statement: Select[Any],
    *,
    require_complete: bool = False,
    deadline: float | None = None,
) -> frozenset[str]:
    """Scan JSON references, validating the owning contract before collection.

    The public token helpers retain their observation contract for other
    consumers. Destructive collection requests a complete scan; a damaged row
    ends that attempt without effects, and the next hourly sweep re-observes it.
    """
    if not require_complete:
        rows = session.execute(statement.execution_options(yield_per=_SCAN_BATCH))
        return tokens(value for value in rows.scalars())
    column = next(iter(statement.selected_columns))
    binding = binding_for(column.table.name, column.name)
    if binding.discriminator is not None:
        statement = statement.add_columns(column.table.c[binding.discriminator])
    found: set[str] = set()
    for row in session.execute(statement.execution_options(yield_per=_SCAN_BATCH)):
        if deadline is not None and time.monotonic() >= deadline:
            raise _Kept(WaitReason.OBSERVATION_UNAVAILABLE.value)
        kind = (
            row._mapping[binding.discriminator]
            if binding.discriminator is not None
            else None
        )
        adapter = binding.adapter_for(kind)
        # Generic passthrough is not evidence that a controlled plan was scanned.
        if adapter is None or (
            binding.discriminator is not None and kind not in binding.contracts
        ):
            raise _Kept(WaitReason.OBSERVATION_UNAVAILABLE.value)
        try:
            observed = adapter.validate_json(canonical_message(row[0]))
            if isinstance(observed, UnprojectedRevision):
                raise _Kept(WaitReason.OBSERVATION_UNAVAILABLE.value)
            found.update(_TOKEN.findall(canonical_message(row[0]).decode()))
        except (TypeError, ValueError):
            raise _Kept(WaitReason.OBSERVATION_UNAVAILABLE.value) from None
    return frozenset(found)


def _bound_lock_waits(session: Session, deadline: float) -> None:
    """Bound every wait of one batch transaction; it never takes a fence.

    A row a writer holds (a head moving, an installation being written, a
    bundle being stored) refuses its statement after a short lock wait, and
    the whole transaction shares the batch budget.
    """
    _bound_statements(session, deadline, lock_timeout_ms=_LOCK_TIMEOUT_MS)


def _bound_statements(
    session: Session, deadline: float, *, lock_timeout_ms: int | None = None
) -> None:
    """Bound each following statement by the time left until ``deadline``.

    ``statement_timeout`` is per statement: the whole transaction is bounded
    by the deadline checks of the loops that use it, not by this setting.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
    settings = [("statement_timeout", remaining_ms)]
    if lock_timeout_ms is not None:
        settings.append(("lock_timeout", min(lock_timeout_ms, remaining_ms)))
    for setting, value in settings:
        session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": setting, "value": f"{value}ms"},
        )


def tokens(values: Iterable[object]) -> frozenset[str]:
    found: set[str] = set()
    for value in values:
        found.update(_TOKEN.findall(json.dumps(value, default=str)))
    return frozenset(found)


def _rowcount(result: object) -> int:
    """Rows a conditional delete matched."""
    return int(getattr(result, "rowcount", 0) or 0)


def _ids(session: Session, column, *conditions) -> list[str]:
    return list(session.scalars(select(column).where(*conditions)))


def _utc(value: datetime) -> datetime:
    # SQLite returns naive timestamps for timezone-aware columns.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


__all__ = [
    "GRACE",
    "INTERVAL",
    "CatalogRevisionCollector",
    "Collected",
    "live_tokens",
    "operation_tokens",
    "pinned_by_heads",
    "plan_tokens",
    "streamed_tokens",
    "tokens",
]
