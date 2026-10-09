"""Agent upgrades: resume."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    canonical_message,
)
from vonk_agent_protocol.contracts import AgentUpgradePayload

from .. import agent_operation_states, job_states
from ..agent_jobs import (
    schedule_agent_upgrade_retry,
)
from ..agent_upgrade_contract import (
    AgentUpgradeRolloutPayload,
)
from ..categorized_errors import (
    InvalidValue,
    MissingRecord,
)
from ..lifecycle.agent_upgrade import UNSUPPORTED_DISPATCH
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..models import AgentOperation, AgentOperationAttempt, Job, JobAttempt
from ..strict_json import read_stored_model

if TYPE_CHECKING:
    from .service import AgentUpgradeService

from .errors import AgentUpgradeConflict
from .helpers import _aware
from .intent import _plan_digest, _request_intent


class ResumeMixin:
    def _retire_rollout(self, parent: Job, now: datetime, reason: str) -> None:
        """A stored rollout that cannot be read ends as failed, with a residue.

        Nothing re-derives a damaged plan, and dispatching a Spark from a plan
        that does not verify would install an unproven package, so the rollout
        is ended (the operator starts a new one) instead of waiting or raising.
        """
        service = cast("AgentUpgradeService", self)

        retire_as_unknown(
            "agent-upgrade.rollout",
            parent.id,
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            reason,
        )
        service._rollouts.fail(parent, now, reason)

    def resume(self, job_id: str) -> None:
        """Resume only the durable agent-operation side of an upgrade rollout."""
        service = cast("AgentUpgradeService", self)

        now = service._clock()
        with service._sessions.begin() as session:
            parent = session.scalar(
                select(Job).where(Job.id == job_id).with_for_update(of=Job)
            )
            if parent is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if parent.kind != "agent-upgrade":
                raise InvalidValue(
                    "job is not a resumable agent upgrade",
                    reason=InvalidRequestReason.NOT_READY,
                )
            failed_dispatch = (
                parent.state == "failed"
                and parent.status_reason == UNSUPPORTED_DISPATCH
            )
            stale_dispatch = parent.state == "running"
            if (
                parent.state
                not in job_states.words(
                    LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR
                )
                and not failed_dispatch
                and not stale_dispatch
            ):
                raise InvalidValue(
                    "job is not a resumable agent upgrade",
                    reason=InvalidRequestReason.NOT_READY,
                )
            worker_attempt = (
                None
                if parent.current_attempt == 0
                else session.scalar(
                    select(JobAttempt)
                    .where(
                        JobAttempt.job_id == parent.id,
                        JobAttempt.attempt == parent.current_attempt,
                    )
                    .with_for_update(of=JobAttempt)
                )
            )
            if parent.current_attempt > 0 and worker_attempt is None:
                # The dispatch audit row is gone, so no worker can hold a lease
                # on it: it is recorded and read as a lapsed dispatch.
                retire_as_unknown(
                    "agent-upgrade.dispatch-audit",
                    parent.id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "the worker dispatch audit of the rollout is missing",
                )
            if worker_attempt is not None and worker_attempt.state == "running":
                if _aware(worker_attempt.lease_deadline) > _aware(now):
                    raise InvalidValue(
                        "agent upgrade worker dispatch is still active",
                        reason=InvalidRequestReason.NOT_READY,
                    )
                service._rollouts.expire_worker_attempt(worker_attempt)
            if failed_dispatch or stale_dispatch:
                if failed_dispatch and (
                    worker_attempt is None or worker_attempt.state != "failed"
                ):
                    # The failed dispatch is the evidence that reopening is
                    # safe, and it is not there: reopening only projects the
                    # rollout again (nothing is dispatched until the plan below
                    # checks out), so it is recorded and carried on.
                    retire_as_unknown(
                        "agent-upgrade.dispatch-audit",
                        parent.id,
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        "the failed dispatch of the rollout has no failed audit",
                    )
                if (
                    stale_dispatch
                    and worker_attempt is not None
                    and not job_states.attempt_lapsed(worker_attempt)
                ):
                    raise InvalidValue(
                        "agent upgrade worker dispatch is not stale",
                        reason=InvalidRequestReason.NOT_READY,
                    )
            invalid_plan = "stored agent upgrade plan is invalid"
            try:
                stored = AgentUpgradeRolloutPayload.model_validate(parent.payload)
            except (TypeError, ValueError):
                service._retire_rollout(parent, now, invalid_plan)
                return
            order = stored.node_order
            package = stored.package
            if len(order) != len(set(order)) or order != parent.targets:
                service._retire_rollout(parent, now, invalid_plan)
                return
            try:
                normalized_intent = _request_intent(stored.request_intent, None)
                normalized_repair = (
                    None
                    if stored.repair_manifest is None
                    else service._repair_manifest(stored.repair_manifest, package)
                )
            except AgentUpgradeConflict:
                service._retire_rollout(parent, now, invalid_plan)
                return
            if normalized_repair is not None and order != [normalized_repair.node_id]:
                service._retire_rollout(parent, now, invalid_plan)
                return
            if parent.payload_digest != _plan_digest(
                sources=stored.sources,
                authority_revision=parent.authority_revision,
                node_ids=order,
                package=package,
                request_intent=normalized_intent,
                repair_manifest=normalized_repair,
            ):
                service._retire_rollout(parent, now, invalid_plan)
                return
            if set(stored.sources) != set(order):
                service._retire_rollout(
                    parent, now, "stored rollback sources are invalid"
                )
                return
            stored_operations = list(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == parent.id)
                    .order_by(AgentOperation.created_at, AgentOperation.id)
                    .with_for_update(of=AgentOperation)
                )
            )
            waiting = [
                operation
                for operation in stored_operations
                if operation.state in agent_operation_states.PARKED
            ]
            active = [
                operation
                for operation in stored_operations
                if operation.state in {"queued", "running"}
            ]
            if len({operation.node_id for operation in stored_operations}) != len(
                stored_operations
            ):
                service._retire_rollout(
                    parent, now, "stored agent upgrade operation is invalid"
                )
                return
            for operation in stored_operations:
                payload = read_stored_model(AgentUpgradePayload, operation.payload)
                source = stored.sources.get(operation.node_id)
                if (
                    source is None
                    or operation.kind != "agent.upgrade.v1"
                    or operation.node_id not in order
                    or operation.authority_revision != parent.authority_revision
                    or {
                        key: value
                        for key, value in operation.payload.items()
                        if key
                        not in {
                            "rollback",
                            "source_package_bytes",
                            "source_package_url",
                        }
                    }
                    != package.model_dump(mode="json")
                    or payload.rollback.source != source.package
                    or payload.source_package_bytes != source.package_bytes
                    or payload.source_package_url != source.package_url
                    or operation.payload_digest
                    != hashlib.sha256(canonical_message(operation.payload)).hexdigest()
                ):
                    service._retire_rollout(
                        parent, now, "stored agent upgrade operation is invalid"
                    )
                    return
            # Deferred (offline) Sparks are passed over while later ones
            # upgrade, so materialized operations need not form a prefix.
            for operation in active:
                if operation.state == "queued":
                    if operation.current_attempt != 0:
                        service._retire_rollout(
                            parent, now, "stored agent upgrade attempt is invalid"
                        )
                        return
                    continue
                active_attempt = session.scalar(
                    select(AgentOperationAttempt)
                    .where(
                        AgentOperationAttempt.operation_id == operation.id,
                        AgentOperationAttempt.attempt == operation.current_attempt,
                    )
                    .with_for_update(of=AgentOperationAttempt)
                )
                if active_attempt is None or active_attempt.state != "running":
                    service._retire_rollout(
                        parent, now, "stored agent upgrade attempt is invalid"
                    )
                    return
            if failed_dispatch:
                service._rollouts.reopen(parent, now, reason=None)
            else:
                service._rollouts.project(parent, now, reason=None)
            if waiting:
                for operation in waiting:
                    attempt = session.scalar(
                        select(AgentOperationAttempt)
                        .where(
                            AgentOperationAttempt.operation_id == operation.id,
                            AgentOperationAttempt.attempt == operation.current_attempt,
                        )
                        .with_for_update(of=AgentOperationAttempt)
                    )
                    if attempt is None or attempt.state not in {
                        "failed",
                        *agent_operation_states.ATTEMPT_OBSERVING,
                    }:
                        service._retire_rollout(
                            parent, now, "stored agent upgrade attempt is invalid"
                        )
                        return
                    # Operator resume is a new dispatch decision. For an
                    # attempted install it must establish a fresh full safety
                    # fence regardless of the stored helper result. Old agents
                    # can omit or reshape that result, and even a success
                    # acknowledgement is not proof that the new runtime took
                    # over. This prevents the resumed request from overlapping
                    # an orphaned dpkg or maintainer script.
                    schedule_agent_upgrade_retry(operation, attempt, now)
            else:
                service._advance(session, parent)
        service._operations.notify_available()
