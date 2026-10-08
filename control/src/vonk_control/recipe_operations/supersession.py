"""Supersession for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select

from ..lifecycle import CancelRequested
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.evidence import (
    BookkeepingReason,
    retire_as_unknown,
)
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_intent as _parent_intent,  # noqa: PLC0414 -- shared helper export
)
from ..recovery_policy import FailureKind
from .errors import RecipeRequestInvalid
from .intent import (
    _active_owned_workload_jobs,
    _intent_is_current,
    _job_workload_intent,
    _profile_effect_scope,
    _unissued_workload_children,
    _workload_owner_scope,
)
from .interfaces import IssuedWorkloadReconciliation
from .observation_helpers import _aware

if TYPE_CHECKING:
    from .service import RecipeOperationService


class SupersessionMixin:
    def assess_superseded_unissued(self, kind: str, owner_id: str) -> bool:
        """Read-only: can an exact older operation be retired before a new intent?"""
        service = typing_cast("RecipeOperationService", self)
        with service._sessions() as session:
            scope = _workload_owner_scope(session, kind, owner_id)
            active = _active_owned_workload_jobs(session, kind, owner_id)
            return bool(active) and all(
                tuple(sorted(job.targets)) == scope
                and _unissued_workload_children(session, job) is not None
                for job in active
            )

    def assess_superseded_issued(
        self,
        kind: str,
        owner_id: str,
        workload_intent_ordinal: int | None = None,
        *,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> IssuedWorkloadReconciliation | None:
        """Name exact issued work that needs fresh observation before resumption.

        Bookkeeping that disagrees (an owner scope that changed, an intent or plan
        the job lost, children or attempts that are missing) never refuses the
        assessment: the job's own Sparks and orders are the evidence, the
        disagreement is retired as unknown, and the job is reported for
        observation.  With several such jobs the earliest due one is named; the
        others surface on the next assessment.
        """
        service = typing_cast("RecipeOperationService", self)
        if workload_intent_ordinal is not None and (
            type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1
        ):
            raise RecipeRequestInvalid("workload intent ordinal is invalid")
        now = _aware(service._clock())
        with service._sessions() as session:
            scope = _workload_owner_scope(session, kind, owner_id)
            intent_scope = _profile_effect_scope(scope, profile_target_node_ids)
            if workload_intent_ordinal is not None and not _intent_is_current(
                session, workload_intent_ordinal, intent_scope
            ):
                raise RecipeRequestInvalid("workload intent was superseded")
            pending: list[IssuedWorkloadReconciliation] = []
            for job in _active_owned_workload_jobs(
                session, kind, owner_id, include_waiting_cancellation=True
            ):
                if tuple(sorted(job.targets)) != scope:
                    retire_as_unknown(
                        "recipe.workload-owner-scope",
                        job.id,
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        "job targets differ from the owner's Sparks",
                    )
                ordinal = _job_workload_intent(session, job) or 0
                if (
                    workload_intent_ordinal is not None
                    and ordinal >= workload_intent_ordinal
                ):
                    continue
                if _unissued_workload_children(session, job) is not None:
                    continue
                children = tuple(
                    session.scalars(
                        select(AgentOperation)
                        .where(AgentOperation.parent_job_id == job.id)
                        .order_by(AgentOperation.id)
                    )
                )
                attempts = (
                    tuple(
                        session.scalars(
                            select(AgentOperationAttempt).where(
                                AgentOperationAttempt.operation_id.in_(
                                    tuple(child.id for child in children)
                                )
                            )
                        )
                    )
                    if children
                    else ()
                )
                if not children or not attempts:
                    retire_as_unknown(
                        "recipe.issued-workload",
                        job.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "issued job has no orders or attempt evidence",
                    )
                latest_lease = max(
                    (_aware(attempt.lease_deadline) for attempt in attempts),
                    default=now,
                )
                # The helper grant is bounded to 300 seconds; stop/cleanup
                # helpers can run for up to 645 seconds after admission.
                # This is a polling budget, never permission to replay.
                observation_deadline = latest_lease + timedelta(seconds=960)
                observe_due_at = min(
                    observation_deadline,
                    max(
                        now + timedelta(seconds=2),
                        min(latest_lease, now + timedelta(seconds=30)),
                    ),
                )
                plan_digest = _parent_identity(job, "plan_digest")
                pending.append(
                    IssuedWorkloadReconciliation(
                        job_id=job.id,
                        kind=kind,
                        owner_id=owner_id,
                        plan_digest=(
                            plan_digest
                            if isinstance(plan_digest, str)
                            else job.payload_digest
                        ),
                        payload_digests=tuple(
                            child.payload_digest for child in children
                        ),
                        failure_kind=FailureKind.UNCERTAIN_EFFECT,
                        observe_due_at=observe_due_at,
                        observation_deadline=observation_deadline,
                    )
                )
            return min(
                pending,
                key=lambda item: (item.observe_due_at, item.job_id),
                default=None,
            )

    def reconcile_superseded_unissued(
        self,
        kind: str,
        owner_id: str,
        workload_intent_ordinal: int,
        *,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> bool:
        """Retire only exact older workload jobs with no issued agent attempt."""
        service = typing_cast("RecipeOperationService", self)
        if type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1:
            raise RecipeRequestInvalid("workload intent ordinal is invalid")
        now = service._clock()
        retired = False
        with service._sessions.begin() as session:
            scope = _workload_owner_scope(session, kind, owner_id)
            intent_scope = _profile_effect_scope(scope, profile_target_node_ids)
            nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(intent_scope))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                )
            )
            if tuple(node.node_id for node in nodes) != intent_scope or any(
                node.workload_intent_ordinal != workload_intent_ordinal
                or node.state != "active"
                or node.revoked_at is not None
                for node in nodes
            ):
                raise RecipeRequestInvalid("workload intent was superseded")
            for job in _active_owned_workload_jobs(session, kind, owner_id, lock=True):
                previous_ordinal = _parent_intent(job)
                if tuple(sorted(job.targets)) != scope:
                    # The job's Sparks and the owner's rows disagree: the job is
                    # not retired on this evidence, and stays for observation.
                    retire_as_unknown(
                        "recipe.workload-owner-scope",
                        job.id,
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        "job targets differ from the owner's Sparks",
                    )
                    continue
                if (
                    type(previous_ordinal) is not int
                    or previous_ordinal < 1
                    or previous_ordinal >= workload_intent_ordinal
                ):
                    continue
                children = _unissued_workload_children(session, job, lock=True)
                if children is None:
                    continue
                RecipeOperationAdapter().cancelled(
                    job, now, reason="superseded before agent issuance", keep=False
                )
                adapter = AgentOperationAdapter(session)
                for child in children:
                    adapter.settle(
                        child,
                        None,
                        job,
                        CancelRequested(reason="superseded before agent issuance"),
                        now,
                    )
                retired = True
        return retired
