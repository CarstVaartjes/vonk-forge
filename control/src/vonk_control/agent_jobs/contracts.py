"""Contracts for the node-scoped agent queue."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

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
from ..auth import AgentSource
from ..lifecycle.agent_operation import AGGREGATE_FINAL_STATES
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt
from ..recovery_policy import FailureKind

_LOGGER = logging.getLogger(__name__)


AgentFence = str | AgentClaim | AgentProgress | AgentResult


ResultConsumer = Callable[
    [Session, StoredOperation, AgentOperationAttempt, AgentResult], None
]


ContactConsumer = Callable[[Session, AgentSource], None]


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
