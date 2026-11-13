"""Bounded retention of ended rows by one rule table, never artifact bytes.

Every collected table has one entry in :func:`retention_rules`: an indexed
terminal predicate, the clock its window runs on, keep-rules that are keyed
``exists`` checks about that one row, and the child rows deleted with it.
Tables kept on purpose are listed in :data:`RETAINED` with the reason.

A pass is an explicit state machine (:class:`PassPhase`). It inventories the
references live rows hold once, then per table selects one indexed batch past
its window, rechecks each row in its own short transaction, deletes it with
its children, and advances that table's cursor. A pass is bounded and takes no
global fence; the next one continues from the cursors an interval later, or
after a short pause while a backlog remains. Database foreign keys remain the
final safety boundary rather than being bypassed.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import (
    DateTime,
    and_,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import (
    InstrumentedAttribute,
    Session,
    aliased,
    load_only,
    sessionmaker,
)
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    ControllerErrorCode,
    DistributionAssignmentState,
    InstallationState,
    LifecycleState,
    ReservationState,
    RoutePublicationState,
    RunState,
)
from vonk_agent_protocol.state_machines import EnrollmentRecordState, NodeIdentityState

from .catalog_revision_collection import GRACE, INTERVAL, tokens
from .lifecycle.evidence import Residue
from .logging import log_event
from .model_cache_contract import ModelCacheSetState
from .models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentIssuedCertificateRevocation,
    AgentNode,
    AgentNodeContact,
    AgentNodeProfile,
    AgentOperation,
    AgentOperationAttempt,
    ArtifactDistributionAssignment,
    ArtifactJob,
    ArtifactLifecycleGate,
    ClusterMapping,
    ClusterMappingNode,
    ControlProcessHeartbeat,
    FleetProfileApplication,
    FleetProfileSelection,
    InstallationNode,
    Job,
    JobAttempt,
    ModelCacheOperation,
    ModelCacheSet,
    NodeSnapshot,
    NodeTelemetryLatest,
    NodeTelemetrySample,
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
from .run_states import ENDED_RUN_STATES
from .step_ca import CERTIFICATE_LIFETIME
from .stored_json import binding_for, read_row_column

PASS_BUDGET_SECONDS = 2.0

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
#: The clock of a heartbeat registered by a process that never completed a
#: loop: such a row is past every window, unless it is this worker's own.
_NEVER = datetime(1970, 1, 1, tzinfo=UTC)

#: A keep-rule that needs the locked row itself: ``(session, row, protected)``.
type RowKeep = Callable[[Session, Any, frozenset[str]], bool]


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """When one table's rows have ended, how long they stay, and what keeps them."""

    model: type[Any]
    #: The primary key; the cursor's tie-break and every keyed check's key.
    key: InstrumentedAttribute[Any]
    #: The indexed column (or, for a small table, expression) the window runs on.
    clock: ColumnElement[datetime] | InstrumentedAttribute[Any]
    #: The indexed terminal predicate, given the window's cutoff.
    ended: Callable[[datetime], ColumnElement[bool]]
    #: Keyed ``exists`` checks over this table's own columns, any of which keeps
    #: the row. They are part of the selection itself: a kept row is never
    #: selected, so rows kept for a long time cost no statement per pass.
    held: tuple[ColumnElement[bool], ...] = ()
    #: Checks that need the locked row in Python (JSON evidence, references).
    keep_row: tuple[RowKeep, ...] = ()
    #: Foreign-key columns of child tables, deleted first by this row's key.
    children: tuple[InstrumentedAttribute[Any], ...] = ()
    window: timedelta = GRACE

    @property
    def table(self) -> str:
        return str(self.model.__tablename__)

    def candidate(self, cutoff: datetime) -> ColumnElement[bool]:
        """Ended, past its window, and no keyed check holds it."""
        return and_(
            self.clock <= cutoff,
            self.ended(cutoff),
            *(~held for held in self.held),
        )


class Retained(StrEnum):
    """Why a mapped table has no rule here."""

    #: Desired state an operator or a request owns; it changes, it never ends.
    DESIRED_STATE = "desired-state"
    #: The current observed truth (one row per subject), replaced in place.
    OBSERVED_TRUTH = "observed-truth"
    #: Ended rows are removed by the named owner below, not by this pass.
    OWNED_ELSEWHERE = "owned-elsewhere"


#: Every mapped table without a rule, and why. The behavioural guard requires
#: each mapped table to be a rule's table, a rule's child, or listed here.
RETAINED: dict[str, tuple[Retained, str]] = {
    "users": (Retained.DESIRED_STATE, "operator accounts"),
    "fleet_profiles": (Retained.DESIRED_STATE, "saved profiles"),
    "fleet_profile_recipes": (
        Retained.DESIRED_STATE,
        "projection of the recipes each saved profile names",
    ),
    "fleet_profile_selection": (Retained.DESIRED_STATE, "the selected application"),
    "catalog_documents": (Retained.DESIRED_STATE, "the recipe library"),
    "catalog_document_heads": (Retained.DESIRED_STATE, "the recipe library heads"),
    "catalog_recipe_model_references": (
        Retained.DESIRED_STATE,
        "projection of each catalog revision's models",
    ),
    "recipe_route_authorities": (Retained.DESIRED_STATE, "published routes"),
    "route_publication_owner": (Retained.DESIRED_STATE, "a route's live owner"),
    "fleet_event_cursor": (Retained.OBSERVED_TRUTH, "the stream's one cursor"),
    "node_telemetry_latest": (Retained.OBSERVED_TRUTH, "each node's newest sample"),
    "measured_role_peaks": (
        Retained.OBSERVED_TRUTH,
        (
            "one raised-in-place row per recipe content and role; bounded by the "
            "recipes ever run, and kept after its runs and installations go"
        ),
    ),
    "coordinator_failures": (
        Retained.OBSERVED_TRUTH,
        "one current failure per source, deleted when it clears",
    ),
    "catalog_document_revisions": (
        Retained.OWNED_ELSEWHERE,
        "catalog_revision_collection.CatalogRevisionCollector",
    ),
    "recipe_source_bundles": (
        Retained.OWNED_ELSEWHERE,
        "catalog_revision_collection.CatalogRevisionCollector",
    ),
    "source_bundle_archives": (
        Retained.OWNED_ELSEWHERE,
        "catalog_revision_collection.CatalogRevisionCollector",
    ),
    # With their catalog revision. Measured peaks live in measured_role_peaks,
    # so no rule needs to keep an installation for them.
    "recipe_installations": (
        Retained.OWNED_ELSEWHERE,
        "catalog_revision_collection.CatalogRevisionCollector",
    ),
    "installation_nodes": (
        Retained.OWNED_ELSEWHERE,
        "catalog_revision_collection.CatalogRevisionCollector",
    ),
    "recipe_builds": (
        Retained.OWNED_ELSEWHERE,
        "catalog_revision_collection.CatalogRevisionCollector",
    ),
    "cluster_mappings": (
        Retained.OWNED_ELSEWHERE,
        "catalog_revision_collection.CatalogRevisionCollector",
    ),
    "cluster_mapping_nodes": (
        Retained.OWNED_ELSEWHERE,
        "catalog_revision_collection.CatalogRevisionCollector",
    ),
    "artifact_jobs": (Retained.OWNED_ELSEWHERE, "artifact_jobs.ArtifactJobService"),
    "artifact_job_files": (
        Retained.OWNED_ELSEWHERE,
        "artifact_jobs.ArtifactJobService",
    ),
    "artifact_job_blobs": (
        Retained.OWNED_ELSEWHERE,
        "artifact_jobs.ArtifactJobService",
    ),
    "artifact_lifecycle_gates": (
        Retained.OWNED_ELSEWHERE,
        "artifact_lifecycle: an open gate is deleted where it is released",
    ),
    "node_telemetry_samples": (
        Retained.OWNED_ELSEWHERE,
        "telemetry_maintenance, after 24 h",
    ),
    "node_engine_samples": (
        Retained.OWNED_ELSEWHERE,
        "telemetry_maintenance, with its sample after 24 h",
    ),
    "node_inventory_snapshots": (
        Retained.OWNED_ELSEWHERE,
        "telemetry_maintenance, after 24 h",
    ),
    "fleet_stream_events": (
        Retained.OWNED_ELSEWHERE,
        "telemetry_maintenance, at expiry",
    ),
    "local_certificate_issuance": (
        Retained.OWNED_ELSEWHERE,
        "telemetry_maintenance, after certificate expiry",
    ),
    "local_certificate_revocations": (
        Retained.OWNED_ELSEWHERE,
        "telemetry_maintenance, at certificate expiry",
    ),
    "route_publications": (
        Retained.OWNED_ELSEWHERE,
        "this pass's last phase, by its authority's clock",
    ),
    "sessions": (Retained.OWNED_ELSEWHERE, "browser_auth: expired login sessions"),
    "model_cache_set_artifacts": (
        Retained.OWNED_ELSEWHERE,
        "deleted with its model cache set by the foreign-key cascade",
    ),
}


def _reserved(owner: InstrumentedAttribute[str]) -> ColumnElement[bool]:
    return exists().where(
        ResourceReservation.owner_id == owner,
        ResourceReservation.state != ReservationState.RELEASED.value,
    )


def _command_evidence(session: Session, row: Any, _protected: frozenset[str]) -> bool:
    from .evidence_repeats import command_evidence_needed

    return command_evidence_needed(session, row)


def _assignment_named(session: Session, row: Any, protected: frozenset[str]) -> bool:
    del session
    return not tokens(
        (row.plan_digest, row.model_artifact_set_sha256, row.oci_archive_sha256)
    ).isdisjoint(protected)


def _newest_sync() -> ColumnElement[bool]:
    """The newest succeeded run, and the newest failed one when it is newer."""

    newer = aliased(RecipeLibrarySyncRun)
    later = or_(
        newer.completed_at > RecipeLibrarySyncRun.completed_at,
        and_(
            newer.completed_at == RecipeLibrarySyncRun.completed_at,
            newer.id > RecipeLibrarySyncRun.id,
        ),
    )
    # Two named outcomes kept by recency, not a live or ended set.
    succeeded = LifecycleState.SUCCEEDED.value
    failed = LifecycleState.FAILED.value
    return or_(
        and_(
            RecipeLibrarySyncRun.state == succeeded,
            ~exists().where(newer.state == succeeded, later),
        ),
        and_(
            RecipeLibrarySyncRun.state == failed,
            ~exists().where(newer.state.in_((succeeded, failed)), later),
        ),
    )


def _stop_unproven() -> ColumnElement[bool]:
    """A failed run stays until each of its Sparks delivered a report after it.

    A Spark reports every run it still holds. A process still running for an
    ended run reopens it as ``lost`` (or is stopped under an orphan stop
    grant), so a report the Controller accepted after the run ended, with the
    run still failed, is the Spark's word that nothing of it runs. Job claims
    and other contact prove nothing about processes. A Spark without a contact
    row (retired) can report nothing.
    """

    status = AgentNodeContact.recipe_observation_http_status
    delivered_at = AgentNodeContact.recipe_observation_status_at
    return and_(
        RecipeRun.state == RunState.FAILED.value,
        exists().where(
            RunNode.run_id == RecipeRun.id,
            AgentNodeContact.node_id == RunNode.node_id,
            or_(
                delivered_at.is_(None),
                delivered_at <= RecipeRun.updated_at,
                status.is_(None),
                status < 200,
                status >= 300,
            ),
        ),
    )


def retention_rules(*, current_worker: str | None = None) -> tuple[RetentionRule, ...]:
    """The single rule table. Children precede parents; the order is the pass's."""

    from .journal_models import RunSwitchJournalRepair, RunSwitchJournalRepairPending
    from .model_cache.inventory import holds_unique_objects

    return (
        RetentionRule(
            AgentOperation,
            AgentOperation.id,
            AgentOperation.updated_at,
            lambda _cutoff: AgentOperation.state.in_(_TERMINAL),
            held=(
                _reserved(AgentOperation.id),
                # A retained artifact job still advertises this operation tree.
                exists().where(
                    ArtifactJob.operation_id == AgentOperation.parent_job_id
                ),
                exists().where(
                    Job.id == AgentOperation.parent_job_id,
                    Job.state.not_in(_TERMINAL),
                ),
                exists().where(
                    AgentOperationAttempt.operation_id == AgentOperation.id,
                    AgentOperationAttempt.state == _LIVE_ATTEMPT,
                ),
            ),
            keep_row=(_command_evidence,),
            children=(AgentOperationAttempt.operation_id,),
        ),
        RetentionRule(
            Job,
            Job.id,
            Job.updated_at,
            lambda _cutoff: Job.state.in_(_TERMINAL),
            held=(
                _reserved(Job.id),
                exists().where(
                    JobAttempt.job_id == Job.id, JobAttempt.state == _LIVE_ATTEMPT
                ),
                exists().where(AgentOperation.parent_job_id == Job.id),
                exists().where(ArtifactJob.operation_id == Job.id),
                exists().where(ArtifactLifecycleGate.removal_owner_id == Job.id),
            ),
            # A job's journal repairs and its pending repair end with it.
            children=(
                RunSwitchJournalRepair.job_id,
                RunSwitchJournalRepairPending.job_id,
                JobAttempt.job_id,
            ),
        ),
        RetentionRule(
            ModelCacheOperation,
            ModelCacheOperation.id,
            ModelCacheOperation.updated_at,
            lambda _cutoff: ModelCacheOperation.state.in_(_TERMINAL),
            held=(
                _reserved(ModelCacheOperation.id),
                exists().where(
                    ArtifactLifecycleGate.removal_owner_id == ModelCacheOperation.id
                ),
            ),
        ),
        RetentionRule(
            FleetProfileApplication,
            FleetProfileApplication.id,
            FleetProfileApplication.updated_at,
            lambda _cutoff: FleetProfileApplication.state.in_(_TERMINAL),
            held=(
                _reserved(FleetProfileApplication.id),
                exists().where(
                    FleetProfileSelection.application_id == FleetProfileApplication.id
                ),
            ),
        ),
        RetentionRule(
            RecipeLibrarySyncRun,
            RecipeLibrarySyncRun.id,
            RecipeLibrarySyncRun.completed_at,
            lambda _cutoff: RecipeLibrarySyncRun.state.in_(_TERMINAL),
            held=(_newest_sync(),),
        ),
        RetentionRule(
            ArtifactDistributionAssignment,
            ArtifactDistributionAssignment.id,
            ArtifactDistributionAssignment.updated_at,
            lambda _cutoff: ArtifactDistributionAssignment.state.in_(
                (
                    DistributionAssignmentState.REVOKED.value,
                    DistributionAssignmentState.EXPIRED.value,
                )
            ),
            held=(_reserved(ArtifactDistributionAssignment.id),),
            keep_row=(_assignment_named,),
        ),
        RetentionRule(
            ResourceReservation,
            ResourceReservation.id,
            ResourceReservation.released_at,
            lambda _cutoff: (
                ResourceReservation.state == ReservationState.RELEASED.value
            ),
        ),
        RetentionRule(
            RecipeRun,
            RecipeRun.id,
            RecipeRun.updated_at,
            lambda _cutoff: RecipeRun.state.in_(ENDED_RUN_STATES),
            held=(
                _reserved(RecipeRun.id),
                exists().where(ArtifactJob.run_id == RecipeRun.id),
                _stop_unproven(),
            ),
            children=(RunNode.run_id,),
        ),
        RetentionRule(
            ControlProcessHeartbeat,
            ControlProcessHeartbeat.id,
            func.coalesce(
                ControlProcessHeartbeat.completed_at,
                literal(_NEVER, DateTime(timezone=True)),
                type_=DateTime(timezone=True),
            ),
            # The running worker's own row is never a candidate; any other
            # worker's row ends 24 h after its last completed loop.
            lambda _cutoff: (
                ControlProcessHeartbeat.process_instance_id != current_worker
                if current_worker is not None
                else literal(True)
            ),
        ),
        RetentionRule(
            # A set that lists no object of its own frees no byte: removing
            # its row removes no model file, only a record that holds nothing.
            # Its membership rows go with it (foreign-key cascade).
            ModelCacheSet,
            ModelCacheSet.artifact_set_sha256,
            ModelCacheSet.last_accessed_at,
            lambda _cutoff: and_(
                ModelCacheSet.protected.is_(False),
                ModelCacheSet.state != ModelCacheSetState.CACHED.value,
            ),
            held=(
                holds_unique_objects(ModelCacheSet.artifact_set_sha256),
                exists().where(
                    ModelCacheOperation.artifact_set_sha256
                    == ModelCacheSet.artifact_set_sha256,
                    ModelCacheOperation.state.not_in(_TERMINAL),
                ),
                exists().where(
                    ArtifactLifecycleGate.artifact_kind == "model-set",
                    ArtifactLifecycleGate.artifact_sha256
                    == ModelCacheSet.artifact_set_sha256,
                    ArtifactLifecycleGate.removal_owner_id.is_not(None),
                ),
            ),
        ),
        RetentionRule(
            # Expired, revoked at the CA, and no attempt or enrollment names it.
            AgentCertificate,
            AgentCertificate.serial,
            AgentCertificate.not_after,
            lambda _cutoff: AgentCertificate.ca_revoked_at.is_not(None),
            held=(
                exists().where(
                    AgentOperationAttempt.agent_certificate_serial
                    == AgentCertificate.serial
                ),
                exists().where(
                    AgentEnrollment.certificate_serial == AgentCertificate.serial
                ),
                exists().where(
                    AgentCertificateRotation.source_serial == AgentCertificate.serial
                ),
            ),
        ),
        RetentionRule(
            # Its certificate was bound before this evidence was recorded, so
            # it has expired one lifetime after; then a day of grace.
            AgentIssuedCertificateRevocation,
            AgentIssuedCertificateRevocation.serial,
            AgentIssuedCertificateRevocation.created_at,
            lambda _cutoff: AgentIssuedCertificateRevocation.ca_revoked_at.is_not(None),
            window=CERTIFICATE_LIFETIME + GRACE,
        ),
        RetentionRule(
            # A grant and its enrollment end 24 h after max(expiry, consumption).
            AgentEnrollmentGrant,
            AgentEnrollmentGrant.id,
            AgentEnrollmentGrant.expires_at,
            lambda cutoff: or_(
                AgentEnrollmentGrant.consumed_at.is_(None),
                AgentEnrollmentGrant.consumed_at <= cutoff,
            ),
            held=(
                exists().where(
                    AgentEnrollment.grant_id == AgentEnrollmentGrant.id,
                    AgentEnrollment.state == EnrollmentRecordState.ISSUING.value,
                ),
            ),
            children=(AgentEnrollment.grant_id,),
        ),
        RetentionRule(
            # A retired node goes after its certificates have expired and gone,
            # once nothing that names it remains.
            AgentNode,
            AgentNode.node_id,
            AgentNode.revoked_at,
            lambda _cutoff: AgentNode.state == NodeIdentityState.RETIRED.value,
            held=tuple(
                exists().where(column == AgentNode.node_id)
                for column in (
                    AgentCertificate.node_id,
                    AgentOperation.node_id,
                    InstallationNode.node_id,
                    RunNode.node_id,
                    RecipeBuild.builder_node_id,
                    ClusterMapping.endpoint_owner_node_id,
                    ClusterMappingNode.node_id,
                    ResourceReservation.node_id,
                )
            ),
            children=(
                AgentNodeContact.node_id,
                AgentNodeProfile.node_id,
                AgentCertificateRotation.node_id,
                NodeTelemetryLatest.node_id,
                NodeTelemetrySample.node_id,
                NodeSnapshot.node_id,
            ),
        ),
    )


def history_rows_past_grace(session: Session, now: datetime) -> int:
    """Count ended history independently of collector health, without reading JSON.

    Includes protected rows: their continuing retention is visible too. This is
    an observation, never an admission decision or permission to remove them.
    """
    sources = []
    for rule in retention_rules():
        cutoff = now - rule.window
        sources.append(select(rule.key).where(rule.clock <= cutoff, rule.ended(cutoff)))
    sources.append(
        select(RoutePublication.authority_id)
        .join(RecipeRouteAuthority)
        .where(
            RecipeRouteAuthority.updated_at <= now - GRACE,
            RoutePublication.state == RoutePublicationState.COMPLETED.value,
        )
    )
    return (
        session.scalar(select(func.count()).select_from(union_all(*sources).subquery()))
        or 0
    )


class PassPhase(StrEnum):
    """Where a collection cycle stands; every pass starts with the inventory."""

    #: Select the next indexed batch of the current table past its window.
    SELECT = "select"
    #: Recheck and delete the pending batch one row at a time.
    DELETE = "delete"
    #: Every table is done: collect completed route publications.
    PUBLICATIONS = "publications"


#: The declared edges; a pass that ran out of budget stays where it stood.
_EDGES: dict[PassPhase, frozenset[PassPhase]] = {
    PassPhase.SELECT: frozenset(
        {PassPhase.SELECT, PassPhase.DELETE, PassPhase.PUBLICATIONS}
    ),
    PassPhase.DELETE: frozenset({PassPhase.SELECT, PassPhase.PUBLICATIONS}),
    PassPhase.PUBLICATIONS: frozenset({PassPhase.SELECT}),
}


class TerminalHistoryCollector:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        batch: int = 32,
        process_instance_id: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._batch = batch
        self._rules = retention_rules(current_worker=process_instance_id)
        self._due_at: datetime | None = None
        self._after: dict[str, tuple[datetime, str]] = {}
        # Disposable observations only: never retain a Session, row lock or
        # permission to delete across passes. A restart inventories them again.
        self._phase = PassPhase.SELECT
        self._table_index = 0
        self._pending: tuple[tuple[str, datetime], ...] = ()
        # A full inventory batch means the cycle may have left rows behind.
        self._more = False
        # The last pass ran out of budget before it reached the end.
        self._backlog = False

    def _move(self, phase: PassPhase) -> None:
        """The one transition; an undeclared edge restarts the cycle."""
        if phase not in _EDGES[self._phase]:
            log_event(
                _LOGGER,
                "history.cycle_restarted",
                service="control-worker",
                phase=self._phase.value,
                wanted=phase.value,
            )
            self._table_index, self._pending, self._more = 0, (), False
            phase = PassPhase.SELECT
        self._phase = phase

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
        deadline = time.monotonic() + PASS_BUDGET_SECONDS
        self._backlog = True
        while self._phase is PassPhase.PUBLICATIONS or _within(deadline):
            if self._phase is PassPhase.PUBLICATIONS:
                removed["route_publications"] += self._publications(
                    now - GRACE, protected
                )
                self._move(PassPhase.SELECT)
                self._backlog = False
                return +removed
            rule = self._rules[self._table_index]
            if self._phase is PassPhase.SELECT:
                self._select(rule, now)
                continue
            for identity, observed_at in self._pending:
                if not _within(deadline):
                    return +removed
                self._pending = self._pending[1:]
                # Retained and contended rows cannot starve unrelated history.
                # Restarting loses only this observation cursor, not authority.
                self._after[rule.table] = (observed_at, identity)
                if self._remove(rule, identity, now, protected):
                    removed[rule.table] += 1
            self._next_table()
        return +removed

    def _select(self, rule: RetentionRule, now: datetime) -> None:
        cutoff = now - rule.window
        statement = select(rule.key, rule.clock).where(rule.candidate(cutoff))
        boundary = self._after.get(rule.table)
        if boundary is not None:
            at, identity = boundary
            statement = statement.where(
                or_(rule.clock > at, and_(rule.clock == at, rule.key > identity))
            )
        with self._sessions() as session:
            candidates = tuple(
                session.execute(
                    statement.order_by(rule.clock, rule.key).limit(self._batch)
                )
            )
        # Save successful inventory before checking the elapsed budget.
        # Otherwise a slow query can consume every pass without ever reaching
        # its first candidate. No absence is inferred on error.
        self._pending = tuple(
            (identity, observed_at)
            for identity, observed_at in candidates
            if observed_at is not None
        )
        if not candidates:
            self._after.pop(rule.table, None)
            self._next_table()
            return
        if len(candidates) >= self._batch:
            self._more = True
        self._move(PassPhase.DELETE)

    def _next_table(self) -> None:
        self._table_index += 1
        if self._table_index < len(self._rules):
            self._move(PassPhase.SELECT)
            return
        self._table_index = 0
        if self._more:
            # A backlog drains within this pass while its budget lasts, one
            # batch per table per cycle so no table starves another.
            self._more = False
            self._move(PassPhase.SELECT)
            return
        self._move(PassPhase.PUBLICATIONS)

    def _remove(
        self,
        rule: RetentionRule,
        identity: Any,
        now: datetime,
        protected: frozenset[str],
    ) -> bool:
        cutoff = now - rule.window
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(rule.model)
                    .where(rule.key == identity, rule.candidate(cutoff))
                    .with_for_update(skip_locked=True)
                )
                if row is None or str(identity) in protected:
                    return False
                if any(keep(session, row, protected) for keep in rule.keep_row):
                    return False
                for child in rule.children:
                    session.execute(
                        delete(cast("type[Any]", child.class_)).where(child == identity)
                    )
                session.execute(delete(rule.model).where(rule.key == identity))
            return True
        except SQLAlchemyError as error:
            log_event(
                _LOGGER,
                "history.row_deferred",
                service="control-worker",
                table=rule.table,
                row_id=str(identity),
                code=type(error).__name__,
            )
            return False

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


def _within(deadline: float) -> bool:
    """Whether this pass's work budget is left; work, never a wait."""
    return time.monotonic() < deadline


def _protected_tokens(session: Session, now: datetime) -> frozenset[str] | None:
    """Keep damaged owners and recognizable references; unknown is not unused.

    Cost: one indexed read of the live and recently ended rows (the retention
    window) and only the columns that can name a reference, never the history
    past its window.
    """
    cutoff = now - GRACE
    selected = session.scalar(select(FleetProfileSelection.application_id))
    retry_root: str | None = None
    if selected is not None:
        application = session.get(
            FleetProfileApplication,
            selected,
            options=(
                load_only(FleetProfileApplication.id, FleetProfileApplication.progress),
            ),
        )
        if application is None:
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
        # The selected application pins the application it retried.
        progress = read_row_column(application, "progress")
        retry_root = getattr(progress, "retry_of_application_id", None)
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
                ArtifactJob.state.not_in(_TERMINAL),
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
            RecipeRun.state.not_in(ENDED_RUN_STATES),
        ),
        (RecipeBuild, "plan", builds_in_flight(now)),
        (
            ArtifactDistributionAssignment,
            "objects",
            ArtifactDistributionAssignment.state
            == DistributionAssignmentState.ACTIVE.value,
        ),
    )
    extra: set[str] = set() if retry_root is None else {str(retry_root)}
    for model, column, condition in specs:
        for owner in session.scalars(
            select(model)
            .where(condition)
            .options(load_only(*_reference_columns(model, column)))
            .execution_options(yield_per=20)
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


def _reference_columns(model: type[Any], column: str) -> tuple[Any, ...]:
    """The id, the referencing column and what reading it needs; no other JSON."""
    names = {"id", column}
    discriminator = binding_for(model.__tablename__, column).discriminator
    if discriminator is not None:
        names.add(discriminator)
    if model is ArtifactDistributionAssignment:
        names |= {"model_artifact_set_sha256", "oci_archive_sha256"}
    return tuple(getattr(model, name) for name in sorted(names))
