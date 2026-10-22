"""Claim for the node-scoped agent queue."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, select
from vonk_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProgress,
    Disposition,
    InvalidRequestReason,
    LifecycleState,
    RecipeBuildRequest,
    WaitReason,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import canonical_payload

from .. import agent_operation_states, job_states
from ..agent_operation_facts import (
    RESTART_REISSUE_OPERATIONS as _RESTART_REISSUE_OPERATIONS,
)
from ..agent_operation_facts import (
    attempt_holds_open_launch_budget as _attempt_holds_open_launch_budget,
)
from ..agent_operation_facts import attempt_holds_order as _attempt_holds_order
from ..agent_operation_facts import aware as _aware
from ..auth import AgentSource
from ..categorized_errors import InvalidValue
from ..inventory_items import inventory_items, runtime_value
from ..lifecycle import CancelRequested, Outcome, Reported
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.evidence import Residue
from ..models import AgentNode, AgentOperationAttempt, Job
from ..models import AgentOperation as StoredOperation
from ..operation_progress import stored_progress
from ..own_cleanup import CLEANUP_KINDS
from ..persisted_waits import schedule_wait
from ..recovery_policy import WAIT_STEP
from .claim_authority import recipe_build_disk_wait
from .contracts import _MUTATING_OPERATIONS, CLAIM_LEASE_SECONDS
from .endings import end_unobserved_order
from .evidence import (
    _exact_service_stop_receipt_covers_start,
    _lease_expiry_reason,
    _profile_stop_covers_jobrun_mutations,
)
from .predicates import _anchor_start_budget, _claim_predicate, _claim_refusal_reason
from .retry import (
    _parked_retry_evidence,
    _renew_distribution_grant,
    _retry_not_authorized_for_current_attempt,
    agent_upgrade_in_flight_on_spark,
)
from .stored import column_field, column_is_document, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def _claim_once(
    self: AgentJobService,
    node_id: str,
    certificate_serial: str,
    runtime_identity: AgentRuntimeIdentity,
    preflight_fingerprint: str | None,
    hostname: str | None,
    source: AgentSource | None,
    claim_fence: str | None = None,
) -> AgentClaim | None:
    with self._claim_lock, self._sessions.begin() as session:
        now = self._clock()
        if (
            claim_fence is not None
            and session.scalar(
                select(AgentOperationAttempt.id).where(
                    AgentOperationAttempt.fence == claim_fence
                )
            )
            is not None
        ):
            operation, attempt = self._active(
                session, AgentProgress(fence=claim_fence), source=source
            )
            node = session.get(AgentNode, node_id)
            if (
                node is None
                or operation.node_id != node_id
                or attempt.agent_certificate_serial != certificate_serial
                or runtime_value(node, lambda identity: identity.binary_digest)
                != runtime_identity.binary_digest
                or runtime_value(node, lambda identity: identity.build_digest)
                != runtime_identity.build_digest
                or runtime_value(node, lambda identity: identity.architecture)
                != runtime_identity.architecture
                or runtime_value(node, lambda identity: identity.semantic_version)
                != runtime_identity.semantic_version
                or inventory_items(node).preflight_fingerprint != preflight_fingerprint
            ):
                raise InvalidValue(
                    "claim request identity changed",
                    reason=InvalidRequestReason.CONFLICT,
                )
            return AgentClaim.model_validate(
                {
                    "fence": attempt.fence,
                    "operation": AgentOperation(operation.kind),
                    "payload": column_value(operation, "payload"),
                    "deadline": _aware(attempt.lease_deadline),
                }
            )
        self._fail_spent_start_budgets(session, node_id, now)
        candidate_id = session.scalar(
            self._claimable_operations(node_id, now).with_only_columns(
                StoredOperation.id
            )
        )
        recovery_id = None
        if candidate_id is None:
            predicate = _claim_predicate(now)
            for parked, attempt in session.execute(
                select(StoredOperation, AgentOperationAttempt)
                .join(Job, Job.id == StoredOperation.parent_job_id)
                .join(AgentNode, AgentNode.node_id == StoredOperation.node_id)
                .join(
                    AgentOperationAttempt,
                    and_(
                        AgentOperationAttempt.operation_id == StoredOperation.id,
                        AgentOperationAttempt.attempt
                        == StoredOperation.current_attempt,
                    ),
                )
                .where(
                    StoredOperation.node_id == node_id,
                    StoredOperation.state.in_(agent_operation_states.PARKED),
                    StoredOperation.kind.in_(_RESTART_REISSUE_OPERATIONS),
                    _retry_not_authorized_for_current_attempt(),
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    *(condition.expression for condition in predicate.common),
                    *(condition.expression for condition in predicate.diagnostics),
                )
                .order_by(StoredOperation.created_at, StoredOperation.id)
            ):
                if _parked_retry_evidence(parked, attempt, now):
                    recovery_id = parked.id
                    break
        upgrade_id = None
        if runtime_identity.package_activation is not None:
            receipt = runtime_identity.package_activation
            upgrade_id = session.scalar(
                select(StoredOperation.id)
                .where(
                    StoredOperation.node_id == node_id,
                    StoredOperation.kind == AgentOperation.AGENT_UPGRADE.value,
                    StoredOperation.payload["rollback"]["attempt_nonce"].as_string()
                    == receipt.attempt_nonce,
                    StoredOperation.state.in_(agent_operation_states.LIVE),
                )
                .order_by(StoredOperation.created_at, StoredOperation.id)
                .limit(1)
            )
        scopes = self._lock_operation_scopes(
            session,
            tuple(
                dict.fromkeys(
                    value
                    for value in (candidate_id, upgrade_id, recovery_id)
                    if value is not None
                )
            ),
            node_id,
        )
        if scopes is None:
            refused_id = candidate_id if candidate_id is not None else upgrade_id
            if refused_id is not None:
                self._record_claim_refusal(
                    session,
                    operation=session.get(StoredOperation, refused_id),
                    job_id=None,
                    check="operation-scope-lock",
                )
            return None
        identity = self._lock_identity(session, node_id, certificate_serial)
        now = self._clock()
        if identity is None or not self._identity_is_active(*identity, now):
            if candidate_id is not None:
                self._record_claim_refusal(
                    session,
                    operation=session.get(StoredOperation, candidate_id),
                    job_id=None,
                    check="node-identity-inactive",
                )
            return None
        node, certificate = identity
        self._consume_contact(session, source, node, certificate)
        self._record_contact(
            session,
            node,
            certificate,
            now,
            runtime_identity,
            preflight_fingerprint,
            hostname,
        )
        self._reconcile_agent_upgrade(
            session,
            node_id,
            certificate.serial,
            now,
            runtime_identity,
            operation_id=upgrade_id,
            parent_job_id=None if upgrade_id is None else scopes[upgrade_id][0],
        )
        if recovery_id is not None:
            parked = session.scalar(
                select(StoredOperation)
                .where(StoredOperation.id == recovery_id)
                .with_for_update(of=StoredOperation)
                .execution_options(populate_existing=True)
            )
            attempt = (
                session.scalar(
                    select(AgentOperationAttempt)
                    .where(
                        AgentOperationAttempt.operation_id == recovery_id,
                        AgentOperationAttempt.attempt == parked.current_attempt,
                    )
                    .with_for_update(of=AgentOperationAttempt)
                )
                if parked is not None
                else None
            )
            if (
                parked is not None
                and attempt is not None
                and _parked_retry_evidence(parked, attempt, now)
            ):
                retry_after = (
                    column_field(attempt, "result", "retry_after_seconds")
                    if column_is_document(attempt, "result")
                    else None
                )
                previous_reason = parked.status_reason
                # Evidence that the order is restart-safe makes its parked
                # attempt an uncertain effect: the core retries it.
                AgentOperationAdapter(session).settle(
                    parked,
                    attempt,
                    session.get(Job, parked.parent_job_id),
                    Reported(
                        Outcome.UNKNOWN,
                        retry_after=(
                            _aware(now) + timedelta(seconds=retry_after)
                            if type(retry_after) is int
                            else None
                        ),
                    ),
                    now,
                )
                if self._claim_has_authority(
                    session,
                    parked,
                    now,
                    node=node,
                    locked_targets=scopes[recovery_id][1],
                ):
                    parked.updated_at = now
                    self._aggregate_parent(session, parked.parent_job_id)
                else:
                    AgentOperationAdapter.withdraw_retry(parked)
                    parked.status_reason = previous_reason
            # Recovery schedules a future exact claim. It never reissues
            # an effect inside this observation of an old parked attempt.
            return None
        if candidate_id is None:
            excluded = self._excluded_work_refusal(session, node, now)
            if excluded is not None:
                excluded_operation, refusal_check, refusal_facts = excluded
                self._record_claim_refusal(
                    session,
                    operation=excluded_operation,
                    job_id=excluded_operation.parent_job_id,
                    check=refusal_check,
                    **refusal_facts.rendered(),
                )
            return None
        statement = (
            self._claimable_operations(node_id, now)
            .where(StoredOperation.id == candidate_id)
            .with_for_update(of=StoredOperation, skip_locked=True)
            .execution_options(populate_existing=True)
        )
        operation = session.scalar(statement)
        if operation is None or operation.parent_job_id != scopes[candidate_id][0]:
            if operation is not None:
                self._record_claim_refusal(
                    session,
                    operation=operation,
                    job_id=operation.parent_job_id,
                    check="parent-scope-changed",
                    kind=operation.kind,
                )
            return None
        payload_document = column_value(operation, "payload")
        if isinstance(payload_document, Residue):
            end_unobserved_order(
                self,
                session,
                operation,
                AgentOperationAdapter.attempt_of(session, operation),
                session.get(Job, operation.parent_job_id),
                now,
                reason=WaitReason.JOB_STATE_UNCERTAIN,
                note=f"stored agent payload damaged: {payload_document.reason.value}",
            )
            return None
        if not self._claim_has_authority(
            session,
            operation,
            now,
            node=node,
            locked_targets=scopes[candidate_id][1],
        ):
            return None
        if (
            operation.kind == AgentOperation.RECIPE_BUILD.value
            and not self._recipe_build_runtime_matches(
                session, operation, runtime_identity
            )
        ):
            self._reject_recipe_build_claim(session, operation, certificate_serial, now)
            self._record_claim_refusal(
                session,
                operation=operation,
                job_id=operation.parent_job_id,
                check="builder-runtime-changed",
                kind=operation.kind,
            )
            return None
        if operation.kind in _MUTATING_OPERATIONS:
            candidates = tuple(
                session.scalars(
                    select(StoredOperation)
                    .where(
                        StoredOperation.node_id == node_id,
                        StoredOperation.id != operation.id,
                        StoredOperation.kind.in_(_MUTATING_OPERATIONS),
                        StoredOperation.state.in_(
                            agent_operation_states.RUNNING_OR_PARKED
                        ),
                    )
                    .order_by(StoredOperation.id)
                    .with_for_update(of=StoredOperation)
                )
            )
            active_mutations_list = []
            reconciled_dead_running = False
            for old in candidates:
                if (
                    operation.workload_intent_ordinal is not None
                    and _exact_service_stop_receipt_covers_start(
                        session, old, operation.workload_intent_ordinal
                    )
                ):
                    continue
                if old.state == "running":
                    attempt = session.scalar(
                        select(AgentOperationAttempt)
                        .where(
                            AgentOperationAttempt.operation_id == old.id,
                            AgentOperationAttempt.attempt == old.current_attempt,
                        )
                        .with_for_update(of=AgentOperationAttempt)
                    )
                    superseded_by = (
                        operation.workload_intent_ordinal
                        if (
                            old.kind not in CLEANUP_KINDS
                            and old.workload_intent_ordinal is not None
                            and operation.workload_intent_ordinal is not None
                            and old.workload_intent_ordinal
                            < operation.workload_intent_ordinal
                        )
                        else None
                    )
                    if _attempt_holds_order(
                        old, attempt, now, superseded=superseded_by is not None
                    ):
                        # A live lease -- or a current-intent attempt inside
                        # its own launch budget -- can still renew and report
                        # its exact effect; nothing may overlap it.
                        active_mutations_list.append(old)
                        continue
                    # The order is `running` but its attempt can no longer
                    # renew or report.  It can never deliver the receipt a
                    # wait needs, so leaving it as a blocker wedges every
                    # later mutation on this node forever (#811).  A
                    # superseded cancellation is reconciled first; anything
                    # else becomes a durable operator-visible wait.  The
                    # effect is never re-issued.
                    supersession = self._superseded_cancellation_state(
                        session, old, operation, now
                    )
                    if supersession == "block":
                        active_mutations_list.append(old)
                        continue
                    if supersession == "cancelled":
                        continue
                    self._reconcile_dead_running_operation(
                        session,
                        old,
                        attempt,
                        node,
                        now,
                        superseded_by=superseded_by,
                    )
                    reconciled_dead_running = True
                    continue
                # A waiting-for-operator order is decided by the
                # supersession state alone: #810 reconciles an authorised
                # cancellation that can no longer arrive, and a plain wait
                # never blocks later work.
                if (
                    self._superseded_cancellation_state(session, old, operation, now)
                    == "block"
                ):
                    active_mutations_list.append(old)
            active_mutations = tuple(active_mutations_list)
            if reconciled_dead_running and not active_mutations:
                self._record_claim_refusal(
                    session,
                    operation=operation,
                    job_id=operation.parent_job_id,
                    check="dead-mutation-reconciled",
                    kind=operation.kind,
                    operation_intent=operation.workload_intent_ordinal,
                )
                return None
            # A current exact STOP is the cleanup action for an older
            # cancelled workload. Do not let the old order's bookkeeping
            # prevent that STOP from reaching the agent; every other
            # mutation still waits for its prior effect to cease.
            current_ordinal = operation.workload_intent_ordinal
            stop_cleans_superseded = (
                operation.kind == AgentOperation.RECIPE_STOP.value
                and current_ordinal is not None
            )
            if (
                operation.kind == AgentOperation.RECIPE_STOP.value
                and current_ordinal is not None
            ):
                current_parent = session.get(Job, operation.parent_job_id)
                has_jobrun_blocker = any(
                    old.kind == AgentOperation.RECIPE_JOB_RUN.value
                    for old in active_mutations
                )
                profile_jobrun_stop_authorized = (
                    current_parent is not None
                    and _profile_stop_covers_jobrun_mutations(
                        operation, current_parent, active_mutations
                    )
                    if has_jobrun_blocker
                    else False
                )
                if has_jobrun_blocker and not profile_jobrun_stop_authorized:
                    stop_cleans_superseded = False
                for old in active_mutations:
                    if old.kind == AgentOperation.RECIPE_JOB_RUN.value:
                        if not profile_jobrun_stop_authorized:
                            stop_cleans_superseded = False
                            break
                        continue
                    old_parent = session.get(Job, old.parent_job_id)
                    if (
                        old.kind
                        not in {
                            AgentOperation.RECIPE_START.value,
                            AgentOperation.RECIPE_STOP.value,
                        }
                        or old.workload_intent_ordinal is None
                        or old.workload_intent_ordinal >= current_ordinal
                        or column_field(old, "payload", "run_id")
                        != column_field(operation, "payload", "run_id")
                        or old_parent is None
                        or not column_is_document(old_parent, "result")
                        or column_field(old_parent, "result", "cancel_requested")
                        is not True
                    ):
                        stop_cleans_superseded = False
                        break
            if active_mutations and not stop_cleans_superseded:
                blocking = active_mutations[0]
                self._record_claim_refusal(
                    session,
                    operation=operation,
                    job_id=operation.parent_job_id,
                    check="live-mutation-in-progress",
                    kind=operation.kind,
                    operation_intent=operation.workload_intent_ordinal,
                    blocking_kind=blocking.kind,
                    blocking_state=blocking.state,
                    blocking_operation=blocking.id,
                    blocking_intent=blocking.workload_intent_ordinal,
                )
                return None
        resumable_progress = None
        if operation.current_attempt:
            previous = session.scalar(
                select(AgentOperationAttempt)
                .where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
                .with_for_update(of=AgentOperationAttempt)
            )
            if (
                previous is not None
                and operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value
            ):
                resumable_progress = stored_progress(previous)
            if previous is not None and (
                previous.state == "running"
                or agent_operation_states.attempt_reported_unknown(previous)
            ):
                if operation.state == "running" and (
                    _attempt_holds_open_launch_budget(operation, previous, now)
                ):
                    # The attempt is the launch its own immutable budget
                    # declares live.  A poll for work cannot expire or park
                    # it, and no new attempt may be issued over it; the exact
                    # fence keeps the operation until it reports or the
                    # budget elapses.
                    return None
                AgentOperationAdapter.expire_attempt(previous)
        if operation.state == "running":
            # An expiry decides the operation without an attempt result, so
            # record why it stopped and the last facts describing the
            # interruption.  Without this an uncertain operation carries no
            # evidence at all and no operator can tell a lost connection
            # from an effect that may already have happened.
            self._settle_lapsed(
                session,
                operation,
                previous,
                _lease_expiry_reason(operation, previous, node, now),
                now,
            )
            self._aggregate_parent(session, operation.parent_job_id)
            return None
        installing = (
            agent_upgrade_in_flight_on_spark(session, operation, now)
            if operation.kind == AgentOperation.AGENT_UPGRADE.value
            else None
        )
        if installing is not None:
            # A Spark installs one package at a time, including a superseded
            # rollout's order still finishing: this order waits for it.
            self._record_claim_refusal(
                session,
                operation=operation,
                job_id=operation.parent_job_id,
                check="agent-upgrade-in-flight",
                kind=operation.kind,
                blocking_kind=installing.kind,
                blocking_state=installing.state,
                blocking_operation=installing.id,
            )
            return None
        if (
            operation.kind == AgentOperation.RECIPE_BUILD
            and operation.current_attempt > 0
            and isinstance(payload_document, RecipeBuildRequest)
            # An expired retry must reach the core's expiry reconciliation,
            # even when no disk is available. It will not issue an attempt.
            and (
                operation.recovery_deadline is None
                or _aware(now) < _aware(operation.recovery_deadline)
            )
        ):
            disk_wait = recipe_build_disk_wait(
                self, session, node_id, payload_document, now
            )
            if disk_wait is not None:
                next_check = now + timedelta(seconds=WAIT_STEP)
                if operation.recovery_deadline is not None:
                    next_check = min(next_check, _aware(operation.recovery_deadline))
                schedule_wait(operation, next_check)
                operation.status_reason = _claim_refusal_reason(
                    disk_wait.value,
                    kind=operation.kind,
                    node_id=node_id,
                    retry_due_at=next_check.isoformat(),
                    recovery_deadline=operation.recovery_deadline,
                )
                parent = session.get(Job, operation.parent_job_id)
                if parent is not None:
                    parent.status_reason = operation.status_reason
                return None
        if operation.kind == AgentOperation.AGENT_UPGRADE.value:
            import secrets

            from vonk_agent_protocol.contracts import AgentUpgradePayload

            payload = column_value(operation, "payload")
            if not isinstance(payload, AgentUpgradePayload):
                return None  # the damaged-payload ending above already owns this case
            if runtime_identity.binary_digest != payload.rollback.source.binary_sha256:
                # Never reinstall over a build other than the exact rollback
                # source.  Settle the order so it cannot starve this
                # Spark's queue: authenticated contact already reports the
                # target, or a newer request must plan from the new build.
                at_target = bool(
                    runtime_identity.binary_digest == payload.target_binary_digest
                    and runtime_identity.build_digest == payload.target_build_digest
                )
                # ``cancelled`` when this order never attempted an install:
                # no package receipt may be derived from it.  A previously
                # attempted install is proven by the target build.
                settled_reason = (
                    "Spark already runs the requested agent build"
                    if at_target
                    else "Spark runs a different agent build than this "
                    "rollout's rollback source"
                )
                settle_event: Reported | CancelRequested
                if not at_target:
                    settle_event = Reported.failed(
                        Disposition.FAIL, reason=settled_reason
                    )
                elif operation.current_attempt >= 1:
                    settle_event = Reported(Outcome.DONE, reason=settled_reason)
                else:
                    settle_event = CancelRequested(reason=settled_reason)
                AgentOperationAdapter(session).settle(
                    operation, None, None, settle_event, now
                )
                self._aggregate_parent(session, operation.parent_job_id)
                return None
            # A new claim owns a fresh watchdog authority. The retry query
            # already enforced the full previous rollback safety fence.
            document = payload.model_dump(mode="json")
            document["rollback"].update(
                attempt_nonce=secrets.token_hex(32),
                activation_deadline=int(now.timestamp()) + 900,
            )
            payload_bytes = canonical_payload(AgentOperation.AGENT_UPGRADE, document)
            operation.payload = json.loads(payload_bytes)
            operation.payload_digest = hashlib.sha256(payload_bytes).hexdigest()
        if (
            operation.kind == AgentOperation.RECIPE_START.value
            and operation.current_attempt == 0
        ):
            _anchor_start_budget(session, operation, now)
        fence = claim_fence or str(uuid.uuid4())
        deadline = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
        # The claim consumes the schedule the previous attempt carried
        # (leaving it would make the attempt that follows look like it still
        # had one) and clears the interrupted reason: a live attempt has none.
        attempt = AgentOperationAdapter(session).start_attempt(
            operation,
            session.get(Job, operation.parent_job_id),
            certificate_serial,
            fence,
            deadline,
            now,
            progress=resumable_progress,
        )
        if attempt is None:
            self._aggregate_parent(session, operation.parent_job_id)
            return None  # the core refuses a claim the predicate let through
        self._refresh_parent_claim_refusal(session, operation, now)
        if operation.kind in {
            AgentOperation.ARTIFACT_DISTRIBUTION.value,
            AgentOperation.RECIPE_INSTALL.value,
            AgentOperation.RECIPE_START.value,
            AgentOperation.RECIPE_JOB_RUN.value,
        }:
            # Every attempt is issued with a fresh grant, as the first one
            # was: a retry that waited out the grant's hour (a parked
            # operation, a backoff) must not be refused by its own plan.
            _renew_distribution_grant(session, operation, now)
        self._note_malformed_cancel_flag(session, operation)
        return AgentClaim.model_validate(
            {
                "fence": attempt.fence,
                "operation": AgentOperation(operation.kind),
                "payload": column_value(operation, "payload"),
                "deadline": deadline,
            }
        )
