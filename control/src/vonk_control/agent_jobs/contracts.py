"""Contracts for the node-scoped agent queue."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProgress,
    AgentResult,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    SecurityRefusalError,
    UnknownOutcomeError,
)

from .. import agent_operation_states, job_states
from ..content_identity import same_build_units
from ..lifecycle.agent_operation import AGGREGATE_FINAL_STATES
from ..lifecycle.evidence import Residue
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt
from ..recovery_policy import FailureKind
from .stored import column_field

_LOGGER = logging.getLogger(__name__)


AgentFence = str | AgentClaim | AgentProgress | AgentResult


ResultConsumer = Callable[
    [Session, StoredOperation, AgentOperationAttempt, AgentResult], None
]


@dataclass(frozen=True, slots=True)
class SupersededAgentEffect:
    parent_job_id: str
    operation_id: str
    node_id: str
    kind: str
    failure_kind: FailureKind
    observe_due_at: datetime
    observation_deadline: datetime


_RECIPE_CAPABILITIES = frozenset(
    {
        AgentOperation.RECIPE_BUILD.value,
        AgentOperation.RECIPE_BUILD_CLEANUP.value,
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_JOB_RUN.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
        AgentOperation.RECIPE_RECONCILE.value,
    }
)


_MUTATING_OPERATIONS = frozenset(
    {
        AgentOperation.AGENT_UPGRADE.value,
        AgentOperation.RECIPE_BUILD.value,
        AgentOperation.RECIPE_BUILD_CLEANUP.value,
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_JOB_RUN.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
        AgentOperation.RECIPE_RECONCILE.value,
    }
)


#: Orders the agent runs beside its claim lane (``executor::runs_off_lane``):
#: a Spark keeps claiming other work while one of them runs (owner decision
#: 2026-10-26, "builds run off the claim lane"). Shared vector:
#: ``agent_protocol/fixtures/off-lane-operations.json``.
OFF_LANE_OPERATIONS = frozenset({AgentOperation.RECIPE_BUILD.value})


class ClaimOrder(NamedTuple):
    """The facts of a claimable order the conflict rule reads, without its row."""

    id: str
    kind: str
    build_id: str | None


def _build_id(order: StoredOperation | ClaimOrder) -> str | None:
    if isinstance(order, ClaimOrder):
        return order.build_id
    value = column_field(order, "payload", "build_id")
    return None if value is None or isinstance(value, Residue) else str(value)


def mutations_conflict(
    claimed: StoredOperation | ClaimOrder, holder: StoredOperation | ClaimOrder
) -> bool:
    """Whether a live ``holder`` forbids ``claimed`` from overlapping it on its Spark.

    Mutations on one Spark stay serial, except a build: it compiles in its own
    storage under its own units, and its memory and disk reservations already
    decide what fits beside it. It conflicts only with work that would end it
    under its feet: its own build identity (a cleanup or a second attempt
    stops the same units) and an agent upgrade (which restarts its supervisor).
    """

    if (
        claimed.kind not in _MUTATING_OPERATIONS
        or holder.kind not in _MUTATING_OPERATIONS
    ):
        return False
    build = AgentOperation.RECIPE_BUILD.value
    if build not in (claimed.kind, holder.kind):
        return True
    other = holder if claimed.kind == build else claimed
    if other.kind == AgentOperation.AGENT_UPGRADE.value:
        return True
    return other.kind in {
        build,
        AgentOperation.RECIPE_BUILD_CLEANUP.value,
    } and same_build_units(_build_id(claimed), _build_id(holder))


def holds_claim(queued: StoredOperation, holder: StoredOperation) -> bool:
    """Whether a live ``holder`` on the same Spark keeps ``queued`` from its claim.

    The agent claims the next order once its lane is free, so any lane order
    holds every queued one; an off-lane order holds only what it conflicts with.
    """

    return holder.kind not in OFF_LANE_OPERATIONS or mutations_conflict(queued, holder)


_WORKLOAD_INTENT_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_JOB_RUN.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
        AgentOperation.RECIPE_RECONCILE.value,
    }
)


_TERMINAL_PARENT_STATES = frozenset(
    job_states.words(
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.NEEDS_OPERATOR,
        LifecycleState.CANCELLED,
    )
)


_AGGREGATE_FINAL_STATES = AGGREGATE_FINAL_STATES


_CONCLUDED_OUTCOMES = _AGGREGATE_FINAL_STATES - set(agent_operation_states.PARKED)


_ABANDONABLE_OPERATIONS = frozenset(
    {
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RUNTIME_PREFLIGHT.value,
    }
)


_ENDED_PARENT_STATES = _CONCLUDED_OUTCOMES | set(
    job_states.words(LifecycleState.FAILED)
)


_DATABASE_REPOLL_SECONDS = 0.25


_CONTROL_OPERATIONS = frozenset(operation.value for operation in AgentOperation)


CLAIM_LEASE_SECONDS = 30


#: The lease a heartbeat earns when its progress advanced. The agent renews
#: every ten seconds, far inside half of it, so a Controller restart or a
#: network gap shorter than the rest is a wait its live owner rides out, not
#: a lost claim. A liveness bound, never a budget: advancing work renews it on
#: every heartbeat, a renewal without progress adds only CLAIM_LEASE_SECONDS,
#: and only an owner that stops renewing reaches it.
PROGRESSING_LEASE = timedelta(minutes=5)


_GRANT_LIFETIME = timedelta(hours=1)


class StaleAgentAttempt(RuntimeError):
    """An agent attempted to update an operation it no longer owns.

    Raise one of the categorized subclasses.
    """


class StaleAgentFence(SecurityRefusalError, StaleAgentAttempt):
    """The lease, certificate or fence presented is not the operation's own."""


class StaleAgentLease(UnknownOutcomeError, StaleAgentAttempt):
    """The attempt's lease or authority lapsed or its bookkeeping is damaged."""


class StaleAgentRequest(InvalidRequestError, StaleAgentAttempt):
    """A late report that is premature or contradicts the stored evidence."""


class AgentConfigurationConflict(InvalidRequestError, RuntimeError):
    """A consumer is bound twice or after the service started."""


class AgentContactIdentityMismatch(SecurityRefusalError, ValueError):
    """The contact source is not the locked identity of the node."""


class AgentResultRefused(InvalidRequestError, ValueError):
    """An authenticated attempt's result that its operation's contract refuses.

    The same bytes are refused on every delivery, so the attempt is settled
    failed with this detail instead of being left to lapse into a reissue.
    """


class OperatorRetirementRefused(InvalidRequestError, ValueError):
    """An operator asked to retire parked work that is still live.

    Retirement is the terminal counterpart of ``resume``: it fails a parked
    order, retaining uncertain effects for exact cleanup.  This typed refusal
    names the one condition that still makes the operation live.
    """

    def __init__(self, operation_id: str, reason: str) -> None:
        self.operation_id = operation_id
        self.reason = reason
        super().__init__(
            f"operation {operation_id} cannot be retired: {reason}",
            reason=InvalidRequestReason.NOT_READY,
        )


_CLAIM_REFUSAL_PREFIX = "claim refused: "


_CLAIM_NOTE_PREFIX = "claim note: "


_BOUNDARY_REFUSAL_PREFIXES = ("heartbeat refused: ", "result refused: ")


_REFUSAL_PREFIXES = (
    _CLAIM_REFUSAL_PREFIX,
    _CLAIM_NOTE_PREFIX,
    *_BOUNDARY_REFUSAL_PREFIXES,
)


_MAX_CLAIM_REFUSAL_REASON = 512
