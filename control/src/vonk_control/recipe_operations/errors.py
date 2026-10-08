"""Errors for digest-bound recipe operations."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from vonk_agent_protocol import (
    InvalidRequestError,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
)

if TYPE_CHECKING:
    from .interfaces import RecipeOperationView


class RecipeOperationConflict(RuntimeError):
    """A lifecycle request is stale, conflicting, or unsafe to execute."""


class RecipeRequestInvalid(InvalidRequestError, RecipeOperationConflict):
    """The request names an entity, argument or operation that cannot be acted on.

    It is refused at submit time, before anything is persisted, and the caller
    can change the request.  It is never raised for damaged stored state: that is
    rebuilt from evidence or retired as unknown (``lifecycle.evidence``).
    """


class RecipeStopAuthorityRefused(SecurityRefusalError, RecipeOperationConflict):
    """A destructive Stop or workload-intent fence lacks the exact authority.

    A Stop is built only from the run's exact durable Start authority, and a job
    joins only the workload intent it was admitted under.  Without that proof the
    Controller must not invent a destructive payload or take a newer intent.
    """


class RecipeRetryLater(UnknownOutcomeError, RecipeOperationConflict):
    """Unsettled facts observed by the request's bounded owner retry."""

    def __init__(
        self, message: str, *, reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE
    ) -> None:
        super().__init__(message, reason=reason)


class RecipeBuildOwnershipBusy(RecipeRetryLater):
    """A build's owner rows are locked by another writer: the cancellation that
    met it repeats its transaction, and the requester hears of it only after the
    attempts are spent."""


class _RouteNotWithdrawn(UnknownOutcomeError):
    """The run's route is listed again; withdraw it again before dispatching."""


class _ServiceStopReplay(UnknownOutcomeError):
    """A competing continuation already committed the request's exact receipt.

    Unwind the withdrawal transaction before returning the observed operation;
    this reconciled outcome never enters the admission retry loop.
    """

    def __init__(self, operation: RecipeOperationView) -> None:
        self.operation = operation
        super().__init__()


class RecipeReconciliationBlocked(UnknownOutcomeError, RecipeOperationConflict):
    """A corrupt installation lacks exact, current cleanup authority."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class RecipeArtifactJobCancellationPending(RecipeOperationConflict):
    """An issued one-shot job still needs its exact cancellation receipt."""

    def __init__(
        self, *, job_id: str, observe_due_at: datetime, observation_deadline: datetime
    ) -> None:
        super().__init__(f"artifact job cancellation is pending: {job_id}")
        self.job_id = job_id
        self.observe_due_at = observe_due_at
        self.observation_deadline = observation_deadline


class RecipeInstallPreflightExpired(RecipeOperationConflict):
    """Acceptance refused an identical plan on runtime preflight evidence.

    The receipt aged out, the host fingerprint moved, or it does not cover this
    recipe's requirements.  Nothing was persisted, so a caller that owns a
    bounded runtime preflight gate may rerun its ordinary probe and re-present
    the same plan; every other caller keeps treating this as the conflict it is.
    """
