"""Bounded retention of completed coordination history, never artifact bytes.

Live and recently used exact identities are retained. Failed scans prove nothing
unused. Every row is rechecked in a short transaction, and database foreign keys
remain a final safety boundary rather than being bypassed for collection.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import DistributionAssignmentState, RunState

from .catalog_revision_collection import GRACE, INTERVAL, live_tokens, tokens
from .lifecycle.evidence import Residue
from .logging import log_event
from .models import (
    AgentOperation,
    AgentOperationAttempt,
    ArtifactDistributionAssignment,
    ArtifactJob,
    ArtifactLifecycleGate,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    JobAttempt,
    ModelCacheOperation,
    NodeArtifact,
    RecipeBuild,
    RecipeInstallation,
    RecipeLibrarySyncRun,
    RecipeRouteAuthority,
    RecipeRun,
    ResourceReservation,
    RoutePublication,
    RoutePublicationOwner,
    RunNode,
)
from .stored_json import read_row_column

_LOGGER = logging.getLogger(__name__)
_TERMINAL = ("succeeded", "failed", "cancelled", "superseded")


class TerminalHistoryCollector:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        batch: int = 32,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._batch = batch
        self._due_at: datetime | None = None
        self._after: dict[str, tuple[datetime, str]] = {}

    def tick(self) -> bool:
        now = self._clock()
        if self._due_at is not None and now < self._due_at:
            return False
        self._due_at = now + INTERVAL
        try:
            counts = self.collect()
        except SQLAlchemyError as error:
            log_event(
                _LOGGER,
                "history.scan_deferred",
                service="control-worker",
                code=type(error).__name__,
            )
            return False
        if counts:
            log_event(
                _LOGGER,
                "history.pruned",
                service="control-worker",
                removed=dict(counts),
            )
        return bool(counts)

    def collect(self) -> Counter[str]:
        now = self._clock()
        cutoff = now - GRACE
        removed: Counter[str] = Counter()
        deadline = time.monotonic() + 2.0
        # Children precede parents. Never delete live descendants by cascade.
        specs = (
            (
                AgentOperation,
                AgentOperation.updated_at,
                AgentOperation.state.in_(_TERMINAL),
            ),
            (Job, Job.updated_at, Job.state.in_(_TERMINAL)),
            (
                ModelCacheOperation,
                ModelCacheOperation.updated_at,
                ModelCacheOperation.state.in_(_TERMINAL),
            ),
            (
                FleetProfileApplication,
                FleetProfileApplication.updated_at,
                FleetProfileApplication.state.in_(_TERMINAL),
            ),
            (
                RecipeLibrarySyncRun,
                RecipeLibrarySyncRun.completed_at,
                RecipeLibrarySyncRun.state.in_(_TERMINAL),
            ),
            (
                ArtifactDistributionAssignment,
                ArtifactDistributionAssignment.updated_at,
                ArtifactDistributionAssignment.state.in_(
                    (
                        DistributionAssignmentState.REVOKED.value,
                        DistributionAssignmentState.EXPIRED.value,
                    )
                ),
            ),
            (
                ResourceReservation,
                ResourceReservation.released_at,
                ResourceReservation.state == "released",
            ),
            # Inventory history is removed only after an authenticated inventory
            # said missing and there are no remaining consumers; never infer absence.
            (
                NodeArtifact,
                NodeArtifact.updated_at,
                (NodeArtifact.state == "missing") & (NodeArtifact.ref_count == 0),
            ),
            (
                RecipeRun,
                RecipeRun.updated_at,
                RecipeRun.state.in_((RunState.STOPPED.value, RunState.FAILED.value)),
            ),
        )
        for model, stamp, ended in specs:
            statement = select(model.id, stamp).where(stamp <= cutoff, ended)
            boundary = self._after.get(model.__tablename__)
            if boundary is not None:
                at, identity = boundary
                statement = statement.where(
                    or_(stamp > at, and_(stamp == at, model.id > identity))
                )
            with self._sessions() as session:
                candidates = tuple(
                    session.execute(
                        statement.order_by(stamp, model.id).limit(self._batch)
                    )
                )
            if not candidates:
                self._after.pop(model.__tablename__, None)
            elif len(candidates) >= self._batch:
                self._due_at = now
            for identity, observed_at in candidates:
                if time.monotonic() >= deadline:
                    self._due_at = now
                    return +removed
                # Retained and contended rows cannot starve unrelated history.
                # Restarting loses only this observation cursor, not authority.
                self._after[model.__tablename__] = (observed_at, identity)
                try:
                    with self._sessions.begin() as session:
                        row = session.scalar(
                            select(model)
                            .where(model.id == identity, stamp <= cutoff, ended)
                            .with_for_update(skip_locked=True)
                        )
                        if row is None or not self._unused(session, row, now):
                            continue
                        if model is Job:
                            session.execute(
                                delete(JobAttempt).where(JobAttempt.job_id == identity)
                            )
                            session.execute(delete(Job).where(Job.id == identity))
                        elif model is AgentOperation:
                            session.execute(
                                delete(AgentOperationAttempt).where(
                                    AgentOperationAttempt.operation_id == identity
                                )
                            )
                            session.execute(
                                delete(AgentOperation).where(
                                    AgentOperation.id == identity
                                )
                            )
                        elif model is ModelCacheOperation:
                            session.execute(
                                delete(ModelCacheOperation).where(
                                    ModelCacheOperation.id == identity
                                )
                            )
                        elif model is FleetProfileApplication:
                            session.execute(
                                delete(FleetProfileApplication).where(
                                    FleetProfileApplication.id == identity
                                )
                            )
                        elif model is RecipeLibrarySyncRun:
                            session.execute(
                                delete(RecipeLibrarySyncRun).where(
                                    RecipeLibrarySyncRun.id == identity
                                )
                            )
                        elif model is ArtifactDistributionAssignment:
                            session.execute(
                                delete(ArtifactDistributionAssignment).where(
                                    ArtifactDistributionAssignment.id == identity
                                )
                            )
                        elif model is ResourceReservation:
                            session.execute(
                                delete(ResourceReservation).where(
                                    ResourceReservation.id == identity
                                )
                            )
                        elif model is NodeArtifact:
                            session.execute(
                                delete(NodeArtifact).where(NodeArtifact.id == identity)
                            )
                        elif model is RecipeRun:
                            session.execute(
                                delete(RunNode).where(RunNode.run_id == identity)
                            )
                            session.execute(
                                delete(RecipeRun).where(RecipeRun.id == identity)
                            )
                    removed[model.__tablename__] += 1
                except SQLAlchemyError as error:
                    log_event(
                        _LOGGER,
                        "history.row_deferred",
                        service="control-worker",
                        table=model.__tablename__,
                        row_id=identity,
                        code=type(error).__name__,
                    )
        removed["route_publications"] += self._publications(cutoff)
        return +removed

    @staticmethod
    def _unused(
        session: Session,
        row: Job
        | AgentOperation
        | ModelCacheOperation
        | FleetProfileApplication
        | RecipeLibrarySyncRun
        | ArtifactDistributionAssignment
        | ResourceReservation
        | NodeArtifact
        | RecipeRun,
        now: datetime,
    ) -> bool:
        protected = _protected_tokens(session, now)
        if protected is None:
            return False
        identity = row.id
        if identity in protected:
            return False
        if session.scalar(
            select(
                exists().where(
                    ResourceReservation.owner_id == identity,
                    ResourceReservation.state != "released",
                )
            )
        ):
            return False
        if isinstance(row, Job):
            if session.scalar(
                select(
                    exists().where(
                        JobAttempt.job_id == identity,
                        JobAttempt.state.not_in(_TERMINAL),
                    )
                )
            ):
                return False
            if session.scalar(
                select(exists().where(AgentOperation.parent_job_id == identity))
            ):
                return False
            if session.scalar(
                select(exists().where(ArtifactJob.operation_id == identity))
            ):
                return False
        if isinstance(row, (Job, ModelCacheOperation)) and session.scalar(
            select(exists().where(ArtifactLifecycleGate.removal_owner_id == identity))
        ):
            return False
        if isinstance(row, FleetProfileApplication) and session.scalar(
            select(exists().where(FleetProfileSelection.application_id == identity))
        ):
            return False
        if isinstance(row, RecipeRun):
            # A terminal SQL state alone never proves the process stopped.
            if session.scalar(
                select(
                    exists().where(
                        RunNode.run_id == identity,
                        RunNode.observation_process_running.is_not(False),
                    )
                )
            ):
                return False
            if session.scalar(select(exists().where(ArtifactJob.run_id == identity))):
                return False
            if session.scalar(
                select(
                    exists().where(
                        ResourceReservation.owner_kind == "run",
                        ResourceReservation.owner_id == identity,
                        ResourceReservation.state != "released",
                    )
                )
            ):
                return False
        if isinstance(row, AgentOperation):
            parent = session.get(Job, row.parent_job_id)
            if parent is not None and parent.state not in _TERMINAL:
                return False
            if session.scalar(
                select(
                    exists().where(
                        AgentOperationAttempt.operation_id == identity,
                        AgentOperationAttempt.state.not_in(_TERMINAL),
                    )
                )
            ):
                return False
        # Digest-named objects are retained while current accepted intent names them.
        if isinstance(row, NodeArtifact) and row.digest in protected:
            return False
        return not (
            isinstance(row, ArtifactDistributionAssignment)
            and not tokens(
                (row.plan_digest, row.model_artifact_set_sha256, row.oci_archive_sha256)
            ).isdisjoint(protected)
        )

    def _publications(self, cutoff: datetime) -> int:
        # Publication rows have no clock; their owning authority supplies it.
        statement = (
            select(RoutePublication.authority_id)
            .join(RecipeRouteAuthority)
            .where(
                RecipeRouteAuthority.updated_at <= cutoff,
                RoutePublication.state == "completed",
                ~exists().where(
                    RoutePublicationOwner.authority_id == RoutePublication.authority_id
                ),
            )
            .limit(self._batch)
            .with_for_update(of=RoutePublication, skip_locked=True)
        )
        with self._sessions.begin() as session:
            ids = tuple(session.scalars(statement))
            protected = _protected_tokens(session, self._clock())
            if protected is None:
                return 0
            removed = 0
            for identity in ids:
                if identity in protected:
                    continue
                session.execute(
                    delete(RoutePublication).where(
                        RoutePublication.authority_id == identity
                    )
                )
                removed += 1
            return removed


def _protected_tokens(session: Session, now: datetime) -> frozenset[str] | None:
    """Unknown accepted references defer retention instead of implying unused."""
    cutoff = now - GRACE
    selected = session.scalar(select(FleetProfileSelection.application_id))
    if selected is not None and session.get(FleetProfileApplication, selected) is None:
        log_event(
            _LOGGER,
            "history.references_unavailable",
            service="control-worker",
            table=FleetProfileApplication.__tablename__,
            row_id=selected,
            code="selected-application-missing",
        )
        return None
    specs = (
        (Job, "payload", or_(Job.state.not_in(_TERMINAL), Job.updated_at >= cutoff)),
        (
            AgentOperation,
            "payload",
            or_(
                AgentOperation.state.not_in(_TERMINAL),
                AgentOperation.updated_at >= cutoff,
            ),
        ),
        (
            ModelCacheOperation,
            "payload",
            or_(
                ModelCacheOperation.state.not_in(_TERMINAL),
                ModelCacheOperation.updated_at >= cutoff,
            ),
        ),
        (
            FleetProfileApplication,
            "plan",
            or_(
                FleetProfileApplication.state.not_in(_TERMINAL),
                FleetProfileApplication.updated_at >= cutoff,
                FleetProfileApplication.id == selected,
            ),
        ),
        (
            ArtifactJob,
            "compiled_contract",
            or_(
                ArtifactJob.state.is_(None),
                ArtifactJob.state.not_in((*_TERMINAL, "expired")),
                ArtifactJob.updated_at >= cutoff,
            ),
        ),
        (RecipeInstallation, "plan", RecipeInstallation.state != "uninstalled"),
        (
            RecipeRun,
            "plan",
            RecipeRun.state.not_in((RunState.STOPPED.value, RunState.FAILED.value)),
        ),
        (RecipeBuild, "plan", RecipeBuild.state.in_(("planned", "building"))),
        (
            ArtifactDistributionAssignment,
            "objects",
            ArtifactDistributionAssignment.state
            == DistributionAssignmentState.ACTIVE.value,
        ),
    )
    extra: set[str] = set()
    for model, column, condition in specs:
        for owner in session.scalars(
            select(model).where(condition).execution_options(yield_per=20)
        ):
            decoded = read_row_column(owner, column)
            if isinstance(decoded, Residue):
                log_event(
                    _LOGGER,
                    "history.references_unavailable",
                    service="control-worker",
                    table=model.__tablename__,
                    row_id=owner.id,
                    code=decoded.reason.value,
                )
                return None
            if isinstance(owner, ArtifactDistributionAssignment):
                extra.update(
                    tokens(
                        (
                            owner.objects,
                            owner.model_artifact_set_sha256,
                            owner.oci_archive_sha256,
                        )
                    )
                )
    return live_tokens(session, now) | frozenset(extra)
