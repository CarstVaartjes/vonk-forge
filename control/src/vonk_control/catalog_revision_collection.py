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

Every revision is removed in its own transaction, and one that cannot be
removed (a reference appeared, a constraint refused) is skipped and retried at
the next sweep: a failure never stops the rest and never touches a workload.
The head is re-checked under its row lock inside that transaction. Cached
model files and runtime-image receipts are not owned by a revision (they are
content-addressed and shared) and are left to their own request-led removal.

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, delete, exists, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InstallationState,
    RunState,
    WaitReason,
    canonical_message,
)

from .agent_jobs import release_owned_reservations_in_session
from .catalog_revision_contract import UnprojectedRevision
from .logging import log_event
from .models import (
    AgentOperation,
    ArtifactJob,
    CatalogDocument,
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
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RecipeSourceBundle,
    ResourceReservation,
    RunNode,
    SourceBundleArchive,
)
from .revision_images import revision_images
from .run_history_retention import run_absence_reconciled
from .stored_json import binding_for

_LOGGER = logging.getLogger(__name__)
GRACE = timedelta(hours=24)
INTERVAL = timedelta(hours=1)
# Rows fetched per round trip when scanning stored documents; bounds a scan by
# the largest few documents instead of the whole table.
_SCAN_BATCH = 20
# One sweep stops here and the rest continues on the next worker pass, so a
# first sweep over a large catalog never holds the worker loop for long.
SWEEP_BUDGET_SECONDS = 20.0
_FINISHED = ("succeeded", "failed", "cancelled", "superseded")
_FINISHED_BUILDS = ("succeeded", "failed")
# Terminal states are candidates only; exact current-generation absence is
# independently required before releasing claims and discarding recovery rows.
_DEAD_RUNS = (RunState.STOPPED, RunState.FAILED)
_IN_FLIGHT_BUILDS = ("planned", "building")
# A uuid (a revision, installation or run id) or a sha256 (a source bundle).
_TOKEN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{64}"
)


class _Kept(Exception):
    """The revision is still referenced; leave it for a later sweep."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    id: str
    kind: str
    publisher: str
    slug: str
    content_digest: str


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
        now = self._clock()
        cutoff = now - GRACE
        deadline = time.monotonic() + self._budget_seconds
        with self._sessions() as session:
            candidates = self._candidates(session, cutoff)
        removed = 0
        kept: Counter[str] = Counter()
        for candidate in candidates:
            if time.monotonic() > deadline:
                self._due_at = now  # continue on the next worker pass
                break
            try:
                with self._sessions.begin() as session:
                    _fence_references(session, deadline)
                    live = live_tokens(
                        session, now, require_complete=True, deadline=deadline
                    ) | pinned_by_heads(
                        session, require_complete=True, deadline=deadline
                    )
                    self._remove(session, candidate, live, cutoff, now)
            except _Kept as reason:
                kept[str(reason)] += 1
                continue
            except SQLAlchemyError as error:
                kept[f"refused {type(error).__name__}"] += 1
                log_event(
                    _LOGGER,
                    "catalog_revision.refused",
                    service="control-worker",
                    revision_id=candidate.id,
                    code=type(error).__name__,
                )
                continue
            removed += 1
        bundles = self._collect_bundles(cutoff, now, deadline)
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

    # -- candidates ---------------------------------------------------------

    @staticmethod
    def _candidates(session: Session, cutoff: datetime) -> list[_Candidate]:
        """Superseded revisions past the grace period, recipes before models."""

        heads = {
            (kind, publisher, slug): (active, candidate)
            for kind, publisher, slug, active, candidate in session.execute(
                select(
                    CatalogDocumentHead.kind,
                    CatalogDocumentHead.publisher,
                    CatalogDocumentHead.slug,
                    CatalogDocumentHead.active_revision_id,
                    CatalogDocumentHead.candidate_revision_id,
                )
            )
        }
        rows = session.execute(
            select(
                CatalogDocumentRevision.id,
                CatalogDocumentRevision.document_id,
                CatalogDocumentRevision.kind,
                CatalogDocumentRevision.publisher,
                CatalogDocumentRevision.slug,
                CatalogDocumentRevision.revision_number,
                CatalogDocumentRevision.content_digest,
                CatalogDocumentRevision.created_at,
            )
        ).all()
        created = {row.id: _utc(row.created_at) for row in rows}
        newest: dict[str, int] = {}
        for row in rows:
            newest[row.document_id] = max(
                newest.get(row.document_id, 0), row.revision_number
            )
        found: list[_Candidate] = []
        for row in rows:
            active, candidate = heads.get(
                (row.kind, row.publisher, row.slug), (None, None)
            )
            if row.id in (active, candidate) or row.revision_number == newest.get(
                row.document_id
            ):
                continue
            # No row records when a revision was superseded; the head that
            # replaced it was created at that moment.
            superseded_at = max(created[row.id], created.get(active, created[row.id]))
            if superseded_at <= cutoff:
                found.append(
                    _Candidate(
                        row.id, row.kind, row.publisher, row.slug, row.content_digest
                    )
                )
        found.sort(key=lambda item: (item.kind != "recipe", item.id))
        return found

    # -- one revision -------------------------------------------------------

    def _remove(
        self,
        session: Session,
        revision: _Candidate,
        live: frozenset[str],
        cutoff: datetime,
        now: datetime,
    ) -> None:
        """Delete one revision and what only it owned, or raise ``_Kept``."""

        head = session.execute(
            select(
                CatalogDocumentHead.active_revision_id,
                CatalogDocumentHead.candidate_revision_id,
            )
            .where(
                CatalogDocumentHead.kind == revision.kind,
                CatalogDocumentHead.publisher == revision.publisher,
                CatalogDocumentHead.slug == revision.slug,
            )
            .with_for_update()
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
            live,
            cutoff,
            heads=tuple(value for value in (head or ()) if value is not None),
        )
        self._delete_dead_rows(session, revision, installations, runs, now)
        self._require_no_remaining_reference(session, revision)
        session.execute(
            delete(CatalogDocumentRevision).where(
                CatalogDocumentRevision.id == revision.id
            )
        )
        log_event(
            _LOGGER,
            "catalog_revision.removed",
            service="control-worker",
            revision_id=revision.id,
            kind=revision.kind,
        )

    @staticmethod
    def _require_unused(
        session: Session,
        revision: _Candidate,
        installations: list[str],
        runs: list[str],
        builds: list[str],
        live: frozenset[str],
        cutoff: datetime,
        *,
        heads: tuple[str, ...] = (),
    ) -> None:
        """Raise ``_Kept`` when anything live or recent still names the revision."""

        if (
            revision.id in live
            or revision.content_digest in live
            or not live.isdisjoint(installations)
            or not live.isdisjoint(runs)
        ):
            raise _Kept("live operation")
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
        if runs and any(
            not run_absence_reconciled(session, run)
            for run in session.scalars(select(RecipeRun).where(RecipeRun.id.in_(runs)))
        ):
            raise _Kept("run absence unreconciled")
        if session.scalar(
            select(
                exists().where(
                    RecipeBuild.recipe_revision_id == revision.id,
                    RecipeBuild.state.in_(_IN_FLIGHT_BUILDS),
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
    ) -> None:
        for run_id in runs:
            release_owned_reservations_in_session(session, "run", run_id, now)
        for installation_id in installations:
            release_owned_reservations_in_session(
                session, "installation", installation_id, now
            )
        if runs:
            session.execute(delete(RunNode).where(RunNode.run_id.in_(runs)))
            session.execute(delete(RecipeRun).where(RecipeRun.id.in_(runs)))
        if installations:
            session.execute(
                delete(InstallationNode).where(
                    InstallationNode.installation_id.in_(installations)
                )
            )
            session.execute(
                delete(RecipeInstallation).where(
                    RecipeInstallation.id.in_(installations)
                )
            )
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
    def _require_no_remaining_reference(session: Session, revision: _Candidate) -> None:
        """The database's own foreign keys, checked first-hand before the delete."""

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

    def _collect_bundles(self, cutoff: datetime, now: datetime, deadline: float) -> int:
        """Recheck complete references under a DML fence before each deletion.

        Archive bytes are in this same database, so deletion has no external
        storage wait. NOWAIT contention retains the bundle for the next sweep.
        """
        try:
            with self._sessions() as session:
                old = list(
                    session.scalars(
                        select(RecipeSourceBundle.sha256).where(
                            RecipeSourceBundle.verified_at <= cutoff
                        )
                    )
                )
        except SQLAlchemyError as error:
            log_event(
                _LOGGER,
                "catalog_revision.bundle_scan_failed",
                service="control-worker",
                code=type(error).__name__,
            )
            return 0
        removed = 0
        for sha in sorted(old):
            if time.monotonic() >= deadline:
                self._due_at = now
                break
            try:
                with self._sessions.begin() as session:
                    _fence_references(session, deadline)
                    # A concurrent put refreshes verified_at; the old candidate
                    # list is never proof that this bundle is still collectible.
                    eligible = session.scalar(
                        select(RecipeSourceBundle.sha256).where(
                            RecipeSourceBundle.sha256 == sha,
                            RecipeSourceBundle.verified_at <= cutoff,
                        )
                    )
                    if eligible is None:
                        continue
                    named = (
                        live_tokens(
                            session, now, require_complete=True, deadline=deadline
                        )
                        | streamed_tokens(
                            session,
                            select(CatalogDocumentRevision.projected),
                            require_complete=True,
                            deadline=deadline,
                        )
                        | frozenset(
                            session.scalars(select(RecipeBuild.source_bundle_sha256))
                        )
                    )
                    if sha in named:
                        continue
                    session.execute(
                        delete(SourceBundleArchive).where(
                            SourceBundleArchive.sha256 == sha
                        )
                    )
                    session.execute(
                        delete(RecipeSourceBundle).where(
                            RecipeSourceBundle.sha256 == sha
                        )
                    )
            except (_Kept, SQLAlchemyError) as error:
                # Unknown references and concurrent writers prove nothing unused.
                log_event(
                    _LOGGER,
                    "catalog_revision.bundle_refused",
                    service="control-worker",
                    sha256=sha,
                    code=str(error)
                    if isinstance(error, _Kept)
                    else type(error).__name__,
                )
                continue
            removed += 1
        return removed


def operation_tokens(
    session: Session,
    now: datetime,
    *,
    recent: bool = True,
    require_complete: bool = False,
    deadline: float | None = None,
) -> frozenset[str]:
    """Every id and digest named by a live or recently finished operation.

    Tokens include references nested inside engine-owned extension fields.
    Collection requires the current owning contract to validate first; a raw
    token observation alone cannot establish absence. With ``recent=False``
    only operations that have not ended count (an expired job has ended).
    """

    window = now - GRACE if recent else None
    ended = _FINISHED if recent else (*_FINISHED, "expired")

    def live(state, updated):
        return (
            state.not_in(ended)
            if window is None
            else or_(state.not_in(ended), updated >= window)
        )

    selected = session.scalar(select(FleetProfileSelection.application_id))
    sources = (
        select(Job.payload).where(live(Job.state, Job.updated_at)),
        select(AgentOperation.payload).where(
            live(AgentOperation.state, AgentOperation.updated_at)
        ),
        select(FleetProfileApplication.plan).where(
            or_(
                live(FleetProfileApplication.state, FleetProfileApplication.updated_at),
                FleetProfileApplication.id == selected,
            )
        ),
        select(ModelCacheOperation.payload).where(
            live(ModelCacheOperation.state, ModelCacheOperation.updated_at)
        ),
        select(ArtifactJob.compiled_contract).where(
            # A job still being prepared has no lifecycle state yet.
            or_(
                ArtifactJob.state.is_(None),
                live(ArtifactJob.state, ArtifactJob.updated_at),
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
    deadline: float | None = None,
) -> frozenset[str]:
    """Operation tokens plus what every installation, run and build still names."""

    sources = (
        select(RecipeInstallation.plan).where(
            RecipeInstallation.state != InstallationState.UNINSTALLED
        ),
        select(RecipeRun.plan).where(RecipeRun.state.not_in(_DEAD_RUNS)),
        select(RecipeBuild.plan).where(RecipeBuild.state.in_(_IN_FLIGHT_BUILDS)),
    )
    found: set[str] = set(
        operation_tokens(
            session, now, require_complete=require_complete, deadline=deadline
        )
    )
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
        return tokens(
            session.scalars(statement.execution_options(yield_per=_SCAN_BATCH))
        )
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


def _fence_references(session: Session, deadline: float) -> None:
    """Fence phantom JSON references without changes to any accepting producer.

    PostgreSQL DML automatically conflicts with this short collector fence.
    Readers remain available; a writer already in flight wins immediately.
    EXCLUSIVE also conflicts with existing FOR UPDATE row owners, preventing
    a lock-order cycle while allowing ordinary SELECTs. All tables this sweep
    mutates share that fence. Lock acquisition never waits, and all following
    SQL shares the sweep budget.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
    session.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": f"{remaining_ms}ms"},
    )
    tables = sorted(
        model.__tablename__
        for model in (
            AgentOperation,
            ArtifactJob,
            CatalogDocument,
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
            RecipeBuild,
            RecipeInstallation,
            RecipeRun,
            RecipeSourceBundle,
            ResourceReservation,
            RunNode,
            SourceBundleArchive,
        )
    )
    session.execute(
        text("LOCK TABLE " + ", ".join(tables) + " IN EXCLUSIVE MODE NOWAIT")
    )


def tokens(values: Iterable[object]) -> frozenset[str]:
    found: set[str] = set()
    for value in values:
        found.update(_TOKEN.findall(json.dumps(value, default=str)))
    return frozenset(found)


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
    "streamed_tokens",
    "tokens",
]
