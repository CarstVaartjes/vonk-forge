"""Errors."""

from __future__ import annotations

from datetime import datetime

from vonk_agent_protocol import (
    InvalidRequestError,
    RunSwitchCode,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    run_switch_code,
)

from ..categorized_faults import security_reason
from ..recipe_runtime_specs import (
    RecipeRuntimeSpecError,
)
from ..runtime_image_preparation import (
    RuntimeImagePreparationRefused,
    RuntimeImagePreparationUnknown,
)
from .constants import _RUNTIME_IMAGE_IDENTITY_MISMATCH, _RUNTIME_IMAGE_OWNER_CHANGED


class RunSwitchOperationConflict(RuntimeError):
    """The selected outcome is stale, unsupported, or unsafe to execute.

    ``definite`` marks an outcome its raiser *reports* rather than a failure to
    observe: the phase did its work and the answer is "this is how it ended" (the
    profile Stop below), which the parent reads as a typed result.  It ends the
    operation; every other conflict is an unknown that is observed again.
    """

    definite = False


class _RunSwitchDefiniteConflict(InvalidRequestError, RunSwitchOperationConflict):
    """A refusal of the accepted request itself, not a failure to observe.

    The accepted image identity changed, or the plan names a phase this executor
    cannot do: nothing observed again will change it, and the person (or the
    profile above) must review and apply again.  The allowlist keeps these in its
    ``input-validation`` family; ``test_run_switch_lifecycle`` ties the two together.
    """

    definite = True


class _RunSwitchIncompleteProfileGroupConflict(_RunSwitchDefiniteConflict):
    """Reachable ranks stopped, but the profile group remains incomplete."""

    code = RunSwitchCode.PROFILE_INCOMPLETE_MULTI_SPARK_MODEL


class _RunSwitchBuildParentChanged(UnknownOutcomeError, RunSwitchOperationConflict):
    """This out-of-transaction executor no longer owns the build checkpoint."""


class RunSwitchIssuedWorkloadPending(UnknownOutcomeError, RunSwitchOperationConflict):
    """An older issued lifecycle effect needs observation, never blind replay."""

    def __init__(
        self,
        *,
        kind: str,
        owner_id: str,
        job_id: str,
        observe_due_at: datetime,
        observation_deadline: datetime,
    ) -> None:
        code = run_switch_code(f"{kind}-issued-pending")
        super().__init__(f"{code}: {owner_id} ({job_id})")
        self.kind = kind
        self.job_id = job_id
        self.observe_due_at = observe_due_at
        self.observation_deadline = observation_deadline


class RunSwitchPostStopEvidencePending(UnknownOutcomeError, RunSwitchOperationConflict):
    """A released claim is not evidence that its physical bytes are free.

    ``collected_after`` is the instant evidence must be collected after (strictly):
    a retry before it cannot find any, so the retry clock never schedules one.
    """

    code = RunSwitchCode.POST_STOP_INVENTORY_PENDING

    def __init__(self, message: str, *, collected_after: datetime | None = None):
        super().__init__(message)
        self.collected_after = collected_after


class RunSwitchInstallPreflightExpired(UnknownOutcomeError, RunSwitchOperationConflict):
    """A compile needs a fresh runtime preflight probe before it is accepted.

    The preflight window it was admitted on may have passed, or the host
    fingerprint or requirements moved while it compiled.  Nothing was accepted.
    ``_advance`` holds the runtime-plan checkpoint so the next tick re-enters
    ``LifecyclePreflight.ensure`` for a bounded refresh; any handler that does
    not know this subclass keeps failing the phase, which is the safe reading.
    """


class RunSwitchRefused(SecurityRefusalError, RunSwitchOperationConflict):
    """A refusal at a security boundary: an artifact digest, a reclaim not planned
    or an eviction that would delete another person's bytes."""

    def __init__(
        self, *args: object, reason: SecurityRefusalReason | None = None
    ) -> None:
        super().__init__(
            *args,
            reason=reason
            if reason is not None
            else security_reason(args[0] if args else None),
        )


class RunSwitchRequestInvalid(InvalidRequestError, RunSwitchOperationConflict):
    """The selected outcome is stale, unsupported or not allowed for this request:
    refused at submit time or at the phase that owns the request."""


class RunSwitchRetryLater(UnknownOutcomeError, RunSwitchOperationConflict):
    """Evidence, capacity or an owner that is not settled yet: the phase observes
    it again on the next tick; nothing is refused and nothing waits for a person."""


class RunSwitchRuntimeSpecInvalid(InvalidRequestError, RecipeRuntimeSpecError):
    """A recipe whose canonical document cannot produce a runtime projection."""


class _RuntimeImageOwnerChanged(RuntimeImagePreparationUnknown):
    """The phase, claim or progress that owned an image publication moved on."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            _RUNTIME_IMAGE_OWNER_CHANGED,
            detail,
            reason=WaitReason.SCOPE_CHANGED,
        )


class _RuntimeImageIdentityMismatch(RuntimeImagePreparationRefused):
    """A published image or receipt that differs from the approved identity."""

    def __init__(self, detail: str) -> None:
        super().__init__(_RUNTIME_IMAGE_IDENTITY_MISMATCH, detail)


class _RuntimeImageIdentityUnknown(RuntimeImagePreparationUnknown):
    """An image reference whose stored identity cannot be settled here."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            _RUNTIME_IMAGE_IDENTITY_MISMATCH,
            detail,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
