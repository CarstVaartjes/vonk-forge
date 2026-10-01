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
  ``lost`` or otherwise active (a ``failed`` run holds nothing and counts as
  stopped), a build of it is in flight, or a recipe job still names one of its
  runs;
* it was superseded, or an installation, run, build, mapping or image
  authorization of it was last touched, less than a grace period ago;
* a live or recently finished operation names it: a Job, agent operation,
  profile application (and the selected one always), model-cache operation or
  recipe job payload is searched for its id, so a reference kept in JSON is
  found without knowing which contract wrote it;
* an image authorization of another revision was built from it or names its
  content (an editorial successor reusing the image receipt);
* a kept recipe revision still pins it as its model, by binding or because a
  head or candidate recipe document names its digest.

Everything else is removed together with the dead rows only it owned: its
uninstalled installations, stopped runs, mappings, finished builds, image
authorizations and model bindings. Audit history of those is not kept.

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

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .agent_jobs import release_owned_reservations_in_session
from .logging import log_event
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
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RecipeSourceBundle,
    RunNode,
    RuntimeImageAuthorization,
    SourceBundleArchive,
)

_LOGGER = logging.getLogger(__name__)
GRACE = timedelta(hours=24)
INTERVAL = timedelta(hours=1)
# One sweep stops here and the rest continues on the next worker pass, so a
# first sweep over a large catalog never holds the worker loop for long.
SWEEP_BUDGET_SECONDS = 20.0
_FINISHED = ("succeeded", "failed", "cancelled")
_FINISHED_BUILDS = ("succeeded", "failed")
# Neither holds ports, memory or a place on a Spark (see STOPPABLE_RUN_STATES),
# and nothing ever moves a failed run on to stopped.
_DEAD_RUNS = ("stopped", "failed")
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
            live = _live_tokens(session, now) | _pinned_by_heads(session)
        removed = 0
        kept: Counter[str] = Counter()
        for candidate in candidates:
            if time.monotonic() > deadline:
                self._due_at = now  # continue on the next worker pass
                break
            try:
                with self._sessions.begin() as session:
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
        bundles = self._collect_bundles(cutoff, now)
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
            session, revision, installations, runs, builds, live, cutoff
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
                    RecipeInstallation.state != "uninstalled",
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
                    RecipeBuild.state.in_(_IN_FLIGHT_BUILDS),
                )
            )
        ):
            raise _Kept("build in flight")
        recent = (
            (RecipeInstallation, RecipeInstallation.updated_at),
            (RecipeBuild, RecipeBuild.updated_at),
            (ClusterMapping, ClusterMapping.updated_at),
            (RuntimeImageAuthorization, RuntimeImageAuthorization.authorized_at),
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
        # An editorial successor reuses an image receipt through the original
        # revision, which must stay resolvable while that authorization lives.
        reuse = [
            RuntimeImageAuthorization.original_content_digest == revision.content_digest
        ]
        if builds:
            reuse.append(RuntimeImageAuthorization.build_id.in_(builds))
        if session.scalar(
            select(
                exists().where(
                    RuntimeImageAuthorization.recipe_revision_id != revision.id,
                    or_(*reuse),
                )
            )
        ):
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
        session.execute(
            delete(RuntimeImageAuthorization).where(
                RuntimeImageAuthorization.recipe_revision_id == revision.id
            )
        )
        # A build or mapping another installation still uses stays, and keeps
        # the revision through the remaining-reference check below.
        session.execute(
            delete(RecipeBuild).where(
                RecipeBuild.recipe_revision_id == revision.id,
                RecipeBuild.state.in_(_FINISHED_BUILDS),
                ~exists().where(RecipeInstallation.recipe_build_id == RecipeBuild.id),
                ~exists().where(RuntimeImageAuthorization.build_id == RecipeBuild.id),
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
            (
                "authorization",
                RuntimeImageAuthorization.recipe_revision_id == revision.id,
            ),
        ):
            if session.scalar(select(exists().where(reference))):
                raise _Kept(name)

    # -- source bundles -----------------------------------------------------

    def _collect_bundles(self, cutoff: datetime, now: datetime) -> int:
        """Remove source bundles no revision, build or live payload names."""

        try:
            with self._sessions() as session:
                stored = list(
                    session.execute(
                        select(
                            RecipeSourceBundle.sha256, RecipeSourceBundle.verified_at
                        )
                    )
                )
                old = {sha for sha, at in stored if _utc(at) <= cutoff}
                if not old:
                    return 0
                named = _live_tokens(session, now)
                named = named | _tokens(
                    session.scalars(select(CatalogDocumentRevision.projected))
                )
                named = named | frozenset(
                    session.scalars(select(RecipeBuild.source_bundle_sha256))
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
        for sha in sorted(old - named):
            try:
                with self._sessions.begin() as session:
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
            except SQLAlchemyError as error:
                log_event(
                    _LOGGER,
                    "catalog_revision.bundle_refused",
                    service="control-worker",
                    sha256=sha,
                    code=type(error).__name__,
                )
                continue
            removed += 1
        return removed


def _live_tokens(session: Session, now: datetime) -> frozenset[str]:
    """Every id and digest named by a live or recently finished record.

    Searching the stored JSON text, rather than reading each contract's fields,
    finds a reference whichever producer wrote it and whichever contract it was
    written under.
    """

    recent = now - GRACE
    selected = session.scalar(select(FleetProfileSelection.application_id))
    sources = (
        select(Job.payload).where(
            or_(Job.state.not_in(_FINISHED), Job.updated_at >= recent)
        ),
        select(AgentOperation.payload).where(
            or_(
                AgentOperation.state.not_in(_FINISHED),
                AgentOperation.updated_at >= recent,
            )
        ),
        select(FleetProfileApplication.plan).where(
            or_(
                FleetProfileApplication.state.not_in(_FINISHED),
                FleetProfileApplication.updated_at >= recent,
                FleetProfileApplication.id == selected,
            )
        ),
        select(ModelCacheOperation.payload).where(
            or_(
                ModelCacheOperation.state.not_in(_FINISHED),
                ModelCacheOperation.updated_at >= recent,
            )
        ),
        select(ArtifactJob.compiled_contract).where(
            or_(ArtifactJob.state.not_in(_FINISHED), ArtifactJob.updated_at >= recent)
        ),
        select(RecipeInstallation.plan).where(
            RecipeInstallation.state != "uninstalled"
        ),
        select(RecipeRun.plan).where(RecipeRun.state.not_in(_DEAD_RUNS)),
        select(RecipeBuild.plan).where(RecipeBuild.state.in_(_IN_FLIGHT_BUILDS)),
    )
    found: set[str] = set()
    for statement in sources:
        found |= _tokens(session.scalars(statement))
    return frozenset(found)


def _pinned_by_heads(session: Session) -> frozenset[str]:
    """Digests the head and candidate recipe documents name (their models).

    A recipe binds its model revision when it is activated, so a pending
    candidate has no binding yet; the digest in its own document is what it
    will bind to.
    """

    heads = select(CatalogDocumentHead.active_revision_id).union(
        select(CatalogDocumentHead.candidate_revision_id)
    )
    return _tokens(
        session.scalars(
            select(CatalogDocumentRevision.document).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.id.in_(heads),
            )
        )
    )


def _tokens(values: Iterable[object]) -> frozenset[str]:
    found: set[str] = set()
    for value in values:
        found.update(_TOKEN.findall(json.dumps(value, default=str)))
    return frozenset(found)


def _ids(session: Session, column, *conditions) -> list[str]:
    return list(session.scalars(select(column).where(*conditions)))


def _utc(value: datetime) -> datetime:
    # SQLite returns naive timestamps for timezone-aware columns.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


__all__ = ["GRACE", "INTERVAL", "CatalogRevisionCollector", "Collected"]
