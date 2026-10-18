"""Bounded retention of completed coordination history, never artifact bytes.

Live and recently used exact identities are retained. Failed scans prove nothing
unused. One reference inventory per pass judges every row; each row's own
consumers are rechecked in a short transaction, and database foreign keys remain
a final safety boundary rather than being bypassed for collection. A pass is
bounded; the next one continues from its cursor an interval later, or after
a short pause while a backlog remains.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, exists, func, or_, select, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    ControllerErrorCode,
    DistributionAssignmentState,
    InstallationState,
    LifecycleState,
    ModelFileState,
    ReservationState,
    RoutePublicationState,
    RunState,
    StateAlias,
)

from .catalog_revision_collection import GRACE, INTERVAL, tokens
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
from .owner_liveness import builds_in_flight
from .stored_json import read_row_column

_LOGGER = logging.getLogger(__name__)
_TERMINAL = (
    LifecycleState.SUCCEEDED.value,
    LifecycleState.FAILED.value,
    LifecycleState.CANCELLED.value,
    LifecycleState.SUPERSEDED.value,
)
#: The pause before a pass that ran out of budget with rows left continues.
#: Each pass stays bounded (two seconds), but a backlog drains at about a batch
#: a minute instead of one per hour: an hourly pass removed ~36 rows an hour,
#: below what a busy fleet ends, so history outlived its grace by days.
BACKLOG_PAUSE = timedelta(minutes=1)
#: The one attempt state that still owns its row's effect. An attempt kept
#: ``observing`` (a lapsed lease or an unconfirmed report) on an ended owner is
#: its retained receipt, never a live claim: the owner's end lapses it so
#: (``lifecycle/agent_operation.py``), and nothing advances it afterwards.
_LIVE_ATTEMPT = LifecycleState.RUNNING.value


type HistoryRow = (
    Job
    | AgentOperation
    | ModelCacheOperation
    | FleetProfileApplication
    | RecipeLibrarySyncRun
    | ArtifactDistributionAssignment
    | ResourceReservation
    | NodeArtifact
    | RecipeRun
)


def _history_specs() -> tuple[
    tuple[
        type[HistoryRow],
        InstrumentedAttribute[datetime] | InstrumentedAttribute[datetime | None],
        ColumnElement[bool],
    ],
    ...,
]:
    # Children precede parents. Never delete live descendants by cascade.
    return (
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
            ResourceReservation.state == ReservationState.RELEASED.value,
        ),
        # Inventory history is removed only after an authenticated inventory
        # said missing and there are no remaining consumers; never infer absence.
        (
            NodeArtifact,
            NodeArtifact.updated_at,
            (NodeArtifact.state == ModelFileState.MISSING.value)
            & (NodeArtifact.ref_count == 0),
        ),
        (
            RecipeRun,
            RecipeRun.updated_at,
            RecipeRun.state.in_((RunState.STOPPED.value, RunState.FAILED.value)),
        ),
    )


def history_rows_past_grace(session: Session, now: datetime) -> int:
    """Count ended history independently of collector health, without reading JSON.

    Includes protected rows: their continuing retention is visible too. This is
    an observation, never an admission decision or permission to remove them.
    """
    cutoff = now - GRACE
    sources = [
        select(model.id).where(stamp <= cutoff, ended)
        for model, stamp, ended in _history_specs()
    ]
    sources.append(
        select(RoutePublication.authority_id)
        .join(RecipeRouteAuthority)
        .where(
            RecipeRouteAuthority.updated_at <= cutoff,
            RoutePublication.state == RoutePublicationState.COMPLETED.value,
        )
    )
    return (
        session.scalar(select(func.count()).select_from(union_all(*sources).subquery()))
        or 0
    )


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
        # Disposable observations only: never retain a Session, row lock or
        # permission to delete across passes. A restart inventories them again.
        self._table_index = 0
        self._pending: tuple[tuple[str, datetime], ...] = ()
        # A full inventory batch means the cycle may have left rows behind.
        self._more = False
        # The last pass ran out of budget before it reached the end.
        self._backlog = False

    def tick(self) -> bool:
        now = self._clock()
        if self._due_at is not None and now < self._due_at:
            return False
        self._due_at = now + INTERVAL
        try:
            counts = self.collect()
            if self._backlog:
                self._due_at = now + BACKLOG_PAUSE
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
        # One reference inventory per pass, never per candidate. Damaged rows
        # keep their recognizable references without blocking unrelated work.
        with self._sessions() as session:
            protected = _protected_tokens(session, now)
        if protected is None:
            # An unknown inventory is retried at the interval, never sooner.
            self._backlog = False
            return removed
        # The budget starts after the inventory: a slow inventory must not
        # spend every pass before its first candidate.
        deadline = time.monotonic() + 2.0
        self._backlog = True
        specs = _history_specs()
        while self._table_index < len(specs):
            if time.monotonic() >= deadline:
                return +removed
            model, stamp, ended = specs[self._table_index]
            statement = select(model.id, stamp).where(stamp <= cutoff, ended)
            boundary = self._after.get(model.__tablename__)
            if boundary is not None:
                at, identity = boundary
                statement = statement.where(
                    or_(stamp > at, and_(stamp == at, model.id > identity))
                )
            if not self._pending:
                with self._sessions() as session:
                    candidates = tuple(
                        session.execute(
                            statement.order_by(stamp, model.id).limit(self._batch)
                        )
                    )
                # Save successful inventory before checking the elapsed budget.
                # Otherwise a slow query can consume every pass without ever
                # reaching its first candidate. No absence is inferred on error.
                self._pending = tuple(
                    (identity, observed_at)
                    for identity, observed_at in candidates
                    if observed_at is not None
                )
                if not candidates:
                    self._after.pop(model.__tablename__, None)
                elif len(candidates) >= self._batch:
                    self._more = True
            for identity, observed_at in self._pending:
                if time.monotonic() >= deadline:
                    return +removed
                self._pending = self._pending[1:]
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
                        if row is None or not self._unused(session, row, protected):
                            continue
                        if model is Job:
                            from .models import (
                                RunSwitchJournalRepair,
                                RunSwitchJournalRepairPending,
                            )

                            session.execute(
                                delete(RunSwitchJournalRepair).where(
                                    RunSwitchJournalRepair.job_id == identity
                                )
                            )
                            session.execute(
                                delete(RunSwitchJournalRepairPending).where(
                                    RunSwitchJournalRepairPending.job_id == identity
                                )
                            )
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
            self._table_index += 1
            if self._table_index == len(specs) and self._more:
                # A backlog drains within this pass while its budget lasts,
                # one batch per table per cycle so no table starves another.
                self._table_index = 0
                self._more = False
        if time.monotonic() >= deadline:
            return +removed
        self._table_index = 0
        self._backlog = False
        removed["route_publications"] += self._publications(cutoff, protected)
        return +removed

    @staticmethod
    def _unused(
        session: Session,
        row: HistoryRow,
        protected: frozenset[str],
    ) -> bool:
        identity = row.id
        if identity in protected:
            return False
        if session.scalar(
            select(
                exists().where(
                    ResourceReservation.owner_id == identity,
                    ResourceReservation.state != ReservationState.RELEASED.value,
                )
            )
        ):
            return False
        if isinstance(row, Job):
            if session.scalar(
                select(
                    exists().where(
                        JobAttempt.job_id == identity,
                        JobAttempt.state == _LIVE_ATTEMPT,
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
            # A stopped or failed run holds no claims and is not wanted on any
            # Spark: its row and a missing row get the same `unowned`
            # disposition and the same rejected observation, so no absence
            # proof can arrive for it and keeping it changes nothing there.
            if session.scalar(select(exists().where(ArtifactJob.run_id == identity))):
                return False
            if session.scalar(
                select(
                    exists().where(
                        ResourceReservation.owner_kind == "run",
                        ResourceReservation.owner_id == identity,
                        ResourceReservation.state != ReservationState.RELEASED.value,
                    )
                )
            ):
                return False
        if isinstance(row, AgentOperation):
            # A retained artifact job still advertises this operation tree.
            # Keep the actual receipt for its owner's entire grace, even if
            # this child ended earlier than its parent.
            if session.scalar(
                select(exists().where(ArtifactJob.operation_id == row.parent_job_id))
            ):
                return False
            parent = session.get(Job, row.parent_job_id)
            if parent is not None and parent.state not in _TERMINAL:
                return False
            if session.scalar(
                select(
                    exists().where(
                        AgentOperationAttempt.operation_id == identity,
                        AgentOperationAttempt.state == _LIVE_ATTEMPT,
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

    def _publications(self, cutoff: datetime, protected: frozenset[str]) -> int:
        # Publication rows have no clock; their owning authority supplies it.
        statement = (
            select(RoutePublication.authority_id)
            .join(RecipeRouteAuthority)
            .where(
                RecipeRouteAuthority.updated_at <= cutoff,
                RoutePublication.state == RoutePublicationState.COMPLETED.value,
                ~exists().where(
                    RoutePublicationOwner.authority_id == RoutePublication.authority_id
                ),
            )
            .limit(self._batch)
            .with_for_update(of=RoutePublication, skip_locked=True)
        )
        with self._sessions.begin() as session:
            ids = tuple(session.scalars(statement))
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
    """Keep damaged owners and recognizable references; unknown is not unused."""
    cutoff = now - GRACE
    selected = session.scalar(select(FleetProfileSelection.application_id))
    if selected is not None and session.get(FleetProfileApplication, selected) is None:
        log_event(
            _LOGGER,
            "history.references_unavailable",
            service="control-worker",
            table=FleetProfileApplication.__tablename__,
            row_id=selected,
            code=ControllerErrorCode.UNAVAILABLE.value,
            detail="selected application is missing",
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
                ArtifactJob.state.not_in((*_TERMINAL, StateAlias.EXPIRED.value)),
                ArtifactJob.updated_at >= cutoff,
            ),
        ),
        (
            RecipeInstallation,
            "plan",
            RecipeInstallation.state != InstallationState.UNINSTALLED.value,
        ),
        (
            RecipeRun,
            "plan",
            RecipeRun.state.not_in((RunState.STOPPED.value, RunState.FAILED.value)),
        ),
        (RecipeBuild, "plan", builds_in_flight(now)),
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
                # Retain this owner even if its state changes before deletion.
                # Raw tokens are conservative evidence only: validation failure
                # cannot prove that a recognizable reference is unused.
                extra.add(owner.id)
            extra.update(tokens((getattr(owner, column),)))
            if isinstance(owner, ArtifactDistributionAssignment):
                extra.update(
                    tokens(
                        (
                            owner.model_artifact_set_sha256,
                            owner.oci_archive_sha256,
                        )
                    )
                )
    return frozenset(extra)
