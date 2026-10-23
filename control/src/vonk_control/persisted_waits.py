"""Atomic wait writers and read-only projections for the existing durable owners."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, get_args

from pydantic import BaseModel, TypeAdapter, ValidationError
from vonk_agent_protocol import (
    LifecycleState,
    OperationReason,
    OperatorActionName,
    ProgressAlarmCode,
    WaitOwnerKind,
    WaitReason,
    WaitResumeCondition,
    canonical_message,
)

from .agent_operation_facts import aware
from .models.waits import ScheduledWaitOwner
from .operation_blockers import BlockingOperation, OperationBlocker, make_blocker
from .strict_json import warn_unreadable_once
from .wait_contract import QueueWait, WaitEvidence

_REASON = TypeAdapter(OperationReason)


def typed_wait_reason(value: str | None, fallback: OperationReason) -> OperationReason:
    """Adopt a declared code, never classify the human-readable detail."""
    try:
        return _REASON.validate_json(canonical_message(value))
    except ValidationError:
        return fallback


def read_wait(owner: ScheduledWaitOwner) -> QueueWait | None:
    """Corrupt historical evidence is isolated; reading never advances its owner."""
    if owner.wait_evidence is None:
        return None
    try:
        if not isinstance(owner.wait_evidence, WaitEvidence):
            raise TypeError("unreadable wait evidence")
        evidence = owner.wait_evidence
        if owner.next_action_at is None:
            raise ValueError("wait has no due field")
        return QueueWait(**dict(evidence), next_check=aware(owner.next_action_at))
    except (ValueError, TypeError):
        warn_unreadable_once(
            "wait",
            getattr(owner, "id", None)
            or getattr(owner, "serial", None)
            or type(owner).__name__,
        )
        return None


def clear_wait(owner: ScheduledWaitOwner) -> bool:
    changed = owner.wait_evidence is not None or owner.next_action_at is not None
    owner.wait_evidence = None
    owner.next_action_at = None
    return changed


def write_wait(
    owner: ScheduledWaitOwner,
    *,
    identity: BlockingOperation,
    kind: WaitOwnerKind,
    reason: OperationReason,
    now: datetime,
    next_check: datetime,
    blockers: list[OperationBlocker],
    resume_condition: WaitResumeCondition = WaitResumeCondition.DEPENDENCY_CHANGED,
    dependency_deadline: datetime | None = None,
    last_progress_at: datetime | None = None,
    operator_action: OperatorActionName | None = None,
) -> bool:
    """Write validated evidence and its due field in the caller's transaction.

    Preserving the same dependency keeps first-blocked and real-progress clocks;
    a later retry, refreshed prose or changed next-check is not real progress.
    """
    previous = read_wait(owner)
    same = (
        previous is not None
        and previous.reason == reason
        and [
            (b.code, b.operation, b.node_ids, b.run_id, b.identity_conflict)
            for b in previous.blockers
            if b.code is not ProgressAlarmCode.STALLED
        ]
        == [
            (b.code, b.operation, b.node_ids, b.run_id, b.identity_conflict)
            for b in blockers
            if b.code is not ProgressAlarmCode.STALLED
        ]
    )
    evidence = WaitEvidence(
        owner=identity,
        owner_kind=kind,
        reason=reason,
        blockers=blockers,
        first_blocked_at=previous.first_blocked_at if same and previous else aware(now),
        last_progress_at=aware(last_progress_at)
        if last_progress_at is not None
        else (previous.last_progress_at if previous else None),
        dependency_deadline=min(
            (
                bound
                for bound in (
                    previous.dependency_deadline if same and previous else None,
                    aware(dependency_deadline)
                    if dependency_deadline is not None
                    else None,
                )
                if bound is not None
            ),
            default=None,
        ),
        resume_condition=resume_condition,
        operator_action=operator_action,
    )
    due = aware(next_check)
    changed = owner.wait_evidence != evidence or owner.next_action_at != due
    owner.wait_evidence = evidence
    owner.next_action_at = due
    return changed


def lifecycle_wait(
    owner: ScheduledWaitOwner,
    *,
    identity: str,
    kind: WaitOwnerKind,
    state: LifecycleState,
    now: datetime,
    due: datetime | None,
    detail: str | None,
    reason: OperationReason | None = None,
    blockers: list[OperationBlocker] | None = None,
    deadline: datetime | None = None,
    cancelling: bool = False,
    lease_deadline: datetime | None = None,
    operator_action: OperatorActionName | None = None,
) -> bool:
    """The shared adapter write, without duplicating lifecycle decisions."""
    waiting = (
        state
        in {
            LifecycleState.BACKOFF,
            LifecycleState.OBSERVING,
            LifecycleState.NEEDS_OPERATOR,
        }
        or (state is LifecycleState.QUEUED and due is not None)
        or (
            state is LifecycleState.RUNNING
            and due is not None
            and lease_deadline is None
        )
    )
    if not waiting:
        return clear_wait(owner)
    previous = read_wait(owner)
    fallback = (
        WaitReason.STOP_UNCONFIRMED
        if cancelling
        else WaitReason.OBSERVATION_UNAVAILABLE
    )
    code = reason or typed_wait_reason(
        blockers[0].code if blockers else detail,
        fallback,
    )
    if reason is None and not blockers and code == fallback and previous is not None:
        code = previous.reason
        blockers = list(previous.blockers)
    owner_ref = BlockingOperation(id=identity, kind=kind.value)
    return write_wait(
        owner,
        identity=owner_ref,
        kind=kind,
        reason=code,
        now=now,
        next_check=due if due is not None else now,
        blockers=blockers
        or [
            make_blocker(code, detail or code.value).model_copy(
                update={"operation": owner_ref}
            )
        ],
        dependency_deadline=deadline,
        operator_action=operator_action,
        resume_condition=(
            WaitResumeCondition.EFFECT_OBSERVED
            if cancelling or state is LifecycleState.OBSERVING
            else WaitResumeCondition.OPERATOR_ACTION
            if state is LifecycleState.NEEDS_OPERATOR
            else WaitResumeCondition.RETRY_DUE
            if state in {LifecycleState.BACKOFF, LifecycleState.QUEUED}
            else WaitResumeCondition.DEPENDENCY_CHANGED
        ),
    )


if TYPE_CHECKING:
    from .models import Job


def _job_owns_contract(owner: Job, contract: type[BaseModel]) -> bool:
    # Opaque engine content is never interpreted as Controller scheduling data.
    from .stored_json import binding_for

    schema = binding_for("jobs", "payload").contract_for(owner.kind)
    return schema is contract or contract in get_args(schema)


def _adopt_wait_instant(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.utcoffset() is not None:
                return parsed
        except ValueError:
            return None
    return None


def wait_due(owner: ScheduledWaitOwner) -> datetime | None:
    """Read the SQL due field, adopting only pre-contract progress clocks.

    Older deployed rows have no wait evidence. They retain issued work and their
    due instant until their next owner transition writes the current contract.
    Current evidence never falls back to a retired progress timestamp.
    """
    if owner.next_action_at is not None:
        return aware(owner.next_action_at)
    if owner.wait_evidence is not None:
        return None
    from .models import FleetProfileApplication, Job

    paths: tuple[tuple[str, ...], ...] = ()
    document: object = None
    if isinstance(owner, Job):
        from .job_documents import AvailabilityJobPayload, RunSwitchJobPayload
        from .recipe_image_removal_contract import RecipeCacheRemovalOwner
        from .recipe_update_contract import RecipeUpdateDocument

        if _job_owns_contract(owner, RunSwitchJobPayload):
            document = owner.result
            paths = (("observation_due_at",),)
        elif _job_owns_contract(owner, AvailabilityJobPayload):
            document = owner.payload
            paths = (("retry_after_at",),)
        elif _job_owns_contract(owner, RecipeUpdateDocument):
            document = owner.payload
            paths = (("next_attempt_at",),)
        elif _job_owns_contract(owner, RecipeCacheRemovalOwner):
            document = owner.payload
            paths = (("checkpoint", "failure", "retry_time"),)
        else:
            return None
    elif isinstance(owner, FleetProfileApplication):
        document = owner.progress
        cancellation = (
            document.get("cancellation") if isinstance(document, dict) else None
        )
        if isinstance(cancellation, dict):
            due = _adopt_wait_instant(cancellation.get("observation_due_at"))
            if due is not None:
                return due
        paths = (
            ("admission_retry_at",),
            ("retry_due_at",),
            ("switch_adapter", "observation_due_at"),
        )
    for path in paths:
        value = document
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        due = _adopt_wait_instant(value)
        if due is not None:
            return due
        if isinstance(value, str):
            warn_unreadable_once("wait-clock", getattr(owner, "id", None))
    return None


def owner_identity(
    owner: ScheduledWaitOwner,
) -> tuple[BlockingOperation, WaitOwnerKind]:
    """Identify the existing owner, never infer a wait reason from its prose."""
    from .models import (
        AgentCertificate,
        AgentIssuedCertificateRevocation,
        AgentOperation,
        ArtifactJob,
        ArtifactLifecycleGate,
        CoordinatorFailure,
        FleetProfileApplication,
        FleetProfileSelection,
        Job,
        ModelCacheOperation,
        RecipeInstallation,
        RecipeLibrarySyncRun,
        RecipeRun,
        RoutePublicationOwner,
    )

    if isinstance(owner, Job):
        previous = read_wait(owner)
        kind = previous.owner_kind if previous else WaitOwnerKind.JOB
        if previous is None:
            from .agent_upgrade_contract import AgentUpgradeRolloutPayload
            from .job_documents import (
                AvailabilityJobPayload,
                RecipeBuildCleanupParent,
                RecipeBuildParent,
                RecipeInstallParent,
                RecipeJobActivateParent,
                RecipeJobRunParent,
                RecipeReconcileParent,
                RecipeStartParent,
                RecipeStopParent,
                RecipeUninstallParent,
                RunSwitchJobPayload,
            )
            from .recipe_image_removal_contract import RecipeCacheRemovalOwner
            from .recipe_update_contract import RecipeUpdateDocument

            for model, candidate in (
                (AgentUpgradeRolloutPayload, WaitOwnerKind.AGENT_UPGRADE),
                *(
                    (contract, WaitOwnerKind.RECIPE_OPERATION)
                    for contract in (
                        RecipeBuildCleanupParent,
                        RecipeBuildParent,
                        RecipeInstallParent,
                        RecipeStartParent,
                        RecipeStopParent,
                        RecipeUninstallParent,
                        RecipeReconcileParent,
                        RecipeJobRunParent,
                        RecipeJobActivateParent,
                    )
                ),
                (RunSwitchJobPayload, WaitOwnerKind.RUN_SWITCH),
                (AvailabilityJobPayload, WaitOwnerKind.IMAGE_AVAILABILITY),
                (RecipeCacheRemovalOwner, WaitOwnerKind.IMAGE_AVAILABILITY),
                (RecipeUpdateDocument, WaitOwnerKind.RECIPE_UPDATE_BATCH),
            ):
                if _job_owns_contract(owner, model):
                    kind = candidate
                    break
        return BlockingOperation(id=owner.id, kind=owner.kind), kind
    for model, kind in (
        (CoordinatorFailure, WaitOwnerKind.COORDINATOR),
        (AgentOperation, WaitOwnerKind.AGENT_OPERATION),
        (RecipeInstallation, WaitOwnerKind.CLEANUP),
        (ArtifactLifecycleGate, WaitOwnerKind.STORAGE_RELIEF),
        (ArtifactJob, WaitOwnerKind.ARTIFACT_JOB),
        (FleetProfileApplication, WaitOwnerKind.FLEET_PROFILE),
        (FleetProfileSelection, WaitOwnerKind.SELECTED_PROFILE),
        (ModelCacheOperation, WaitOwnerKind.MODEL_CACHE),
        (RecipeLibrarySyncRun, WaitOwnerKind.CATALOG_SYNC),
        (RecipeRun, WaitOwnerKind.ROUTE_RECOVERY),
        (RoutePublicationOwner, WaitOwnerKind.ROUTE_PUBLICATION),
        (AgentCertificate, WaitOwnerKind.CERTIFICATE),
        (AgentIssuedCertificateRevocation, WaitOwnerKind.CERTIFICATE_REVOCATION),
    ):
        if isinstance(owner, model):
            identity = (
                getattr(owner, "id", None)
                or getattr(owner, "serial", None)
                or getattr(owner, "artifact_sha256", None)
                or str(getattr(owner, "singleton_id", 1))
            )
            return BlockingOperation(id=identity, kind=kind.value), kind
    raise TypeError(f"unregistered wait owner: {type(owner).__name__}")


def schedule_wait(
    owner: ScheduledWaitOwner,
    next_check: datetime | None,
    *,
    now: datetime | None = None,
    reason: OperationReason | None = None,
    blockers: list[OperationBlocker] | None = None,
    deadline: datetime | None = None,
    detail: str | None = None,
) -> bool:
    """Change the owner's schedule and evidence together, never a JSON clock."""
    if next_check is None:
        return clear_wait(owner)
    previous = read_wait(owner)
    identity, kind = owner_identity(owner)
    code = reason or (
        previous.reason if previous else WaitReason.OBSERVATION_UNAVAILABLE
    )
    observed_at = now or getattr(owner, "updated_at", None) or next_check
    return write_wait(
        owner,
        identity=identity,
        kind=kind,
        reason=code,
        now=observed_at,
        next_check=next_check,
        blockers=blockers
        or (
            previous.blockers
            if previous
            and detail is None
            and (reason is None or reason == previous.reason)
            else [
                make_blocker(code, detail or code.value).model_copy(
                    update={"operation": identity}
                )
            ]
        ),
        dependency_deadline=deadline
        if deadline is not None
        else previous.dependency_deadline
        if previous
        else None,
        operator_action=previous.operator_action if previous else None,
        resume_condition=previous.resume_condition
        if previous
        else WaitResumeCondition.DEPENDENCY_CHANGED,
    )


def initialize_wait[T: ScheduledWaitOwner](owner: T, kind: WaitOwnerKind) -> T:
    """Give a new queued owner its initial wait, before it is flushed."""
    from uuid import uuid4

    from . import job_states
    from .models import (
        AgentOperation,
        FleetProfileApplication,
        Job,
        ModelCacheOperation,
    )

    identity = getattr(owner, "id", None)
    if identity is None:
        identity = str(uuid4())
        if isinstance(
            owner, Job | AgentOperation | ModelCacheOperation | FleetProfileApplication
        ):
            owner.id = identity
        else:
            raise TypeError("wait owner requires an explicit identity")
    state = job_states.core(getattr(owner, "state", None))
    now = getattr(owner, "created_at", None) or getattr(owner, "updated_at", None)
    if state is not None and now is not None:
        lifecycle_wait(
            owner,
            identity=identity,
            kind=kind,
            state=state,
            now=now,
            due=owner.next_action_at,
            detail=None,
        )
    return owner
