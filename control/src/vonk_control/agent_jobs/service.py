"""Service for the node-scoped agent queue."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentClaim,
    AgentDirective,
    AgentOperation,
    AgentProgress,
    AgentResult,
    InvalidRequestReason,
    OperationProgress,
    OutcomeDone,
    OutcomeFailed,
    OutcomeUnknown,
    ProgressPhase,
    WaitReason,
    canonical_message,
    validate_result_for_operation,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import AgentResultPayload, ArtifactDistributionResult

from .. import agent_operation_states
from ..agent_job_contract import ClaimFacts
from ..agent_operation_facts import aware as _aware
from ..agent_outcome import stored_report
from ..auth import AgentSource
from ..categorized_errors import InvalidType, InvalidValue
from ..distribution import record_distributed_runtime_image
from ..failure_evidence import safe_text, sanitize_diagnostics
from ..lifecycle import (
    Reconciler,
    Reported,
)
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..models import (
    AgentCertificate,
    AgentNode,
    AgentOperationAttempt,
    Job,
)
from ..models import AgentOperation as StoredOperation
from ..operation_contract import sanitize_failure_evidence, validate_progress_update
from ..operation_progress import progress_document, sample_progress, stored_progress
from .aggregation import _aggregate_parent, _aggregate_parent_state
from .claim import _claim_once
from .claim_authority import (
    _claim_has_authority,
    _recipe_build_runtime_matches,
    _reconcile_agent_upgrade,
    _reject_recipe_build_claim,
)
from .claim_notes import (
    _excluded_work_refusal,
    _note_malformed_cancel_flag,
    _record_claim_refusal,
    _refresh_parent_claim_refusal,
    _write_refusal_note,
    record_boundary_refusal,
)
from .claim_notes import (
    claim_next_operation as claim,
)
from .claim_reconcile import (
    _cancel_superseded_operation,
    _claimable_operations,
    _fail_spent_start_budgets,
    _reconcile_dead_running_operation,
    _settle_lapsed,
    _superseded_cancellation_state,
)
from .completion import _report_event
from .contracts import (
    AgentConfigurationConflict,
    AgentFence,
    ContactConsumer,
    ResultConsumer,
    SupersededAgentEffect,
)
from .endings import end_unobserved_order
from .heartbeat import heartbeat, known_superseded_cancellation
from .identity import (
    _active,
    _consume_contact,
    _fence_token,
    _identity_is_active,
    _lock_identity,
    _reason,
    _record_contact,
    _runtime_identity,
)
from .orchestration import enqueue, enqueue_in_session, notify_available
from .predicates import _document
from .reconcile import (
    _order_reconciler,
    _sweep_lapsed_attempts,
    _sweep_would_act,
    reconcile_orders,
)
from .results import fail, record_late_result, record_result, succeed
from .retirement import operator_resume_candidates_in_session
from .retry import superseded_cancellation_deadline
from .scope import _lock_operation_scopes, _lock_target_scopes, _target_scope
from .stored import column_value
from .supersession import (
    abandon_superseded_idempotent_operations_in_session,
    assess_superseded_agent_effects_in_session,
    request_superseded_workload_cancellation_in_session,
)


class AgentJobService:
    """Transactional queue with focused persistence and orchestration modules."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        result_consumer: ResultConsumer | None = None,
        contact_consumer: ContactConsumer | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if result_consumer is not None and not callable(result_consumer):
            raise InvalidType(
                "agent result consumer must be callable",
                reason=InvalidRequestReason.MALFORMED,
            )
        if contact_consumer is not None and not callable(contact_consumer):
            raise InvalidType(
                "agent contact consumer must be callable",
                reason=InvalidRequestReason.MALFORMED,
            )
        self._sessions = sessions
        self._clock = clock
        self._monotonic = monotonic
        self._result_consumer = result_consumer
        self._contact_consumer = contact_consumer
        self._advance_rollout: Callable[[Session, Job], None] | None = None
        self._reconcile_rollouts: Callable[[int], bool] | None = None
        self._advance_node: Callable[[str], None] | None = None
        self._configuration_lock = threading.Lock()
        self._started = False
        self._reconciler: Reconciler | None = None
        # SQLite ignores row locks. This only prevents same-service test races;
        # PostgreSQL correctness is provided by the database locks below.
        self._claim_lock = threading.RLock()
        self._available = threading.Condition()

    def enqueue(
        self,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: object,
    ) -> StoredOperation:
        return enqueue(
            self, parent_job_id, node_id, operation, authority_revision, payload
        )

    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: object,
        *,
        operation_id: str,
    ) -> StoredOperation:
        return enqueue_in_session(
            self,
            session,
            parent_job_id,
            node_id,
            operation,
            authority_revision,
            payload,
            operation_id=operation_id,
        )

    def notify_available(self) -> None:
        return notify_available(self)

    def reconcile_orders(self, limit: int = 100) -> bool:
        return reconcile_orders(self, limit)

    def _order_reconciler(self) -> Reconciler:
        return _order_reconciler(self)

    def _sweep_lapsed_attempts(self, limit: int) -> bool:
        return _sweep_lapsed_attempts(self, limit)

    def _sweep_would_act(self, operation_id: str, node_id: str) -> bool:
        return _sweep_would_act(self, operation_id, node_id)

    @staticmethod
    def request_superseded_workload_cancellation_in_session(
        session: Session, targets: Sequence[str], ordinal: int, now: datetime
    ) -> None:
        return request_superseded_workload_cancellation_in_session(
            session, targets, ordinal, now
        )

    @staticmethod
    def abandon_superseded_idempotent_operations_in_session(
        session: Session, targets: Sequence[str], ordinal: int, now: datetime
    ) -> int:
        return abandon_superseded_idempotent_operations_in_session(
            session, targets, ordinal, now
        )

    @staticmethod
    def assess_superseded_agent_effects_in_session(
        session: Session, targets: Sequence[str], current_ordinal: int, now: datetime
    ) -> tuple[SupersededAgentEffect, ...]:
        return assess_superseded_agent_effects_in_session(
            session, targets, current_ordinal, now
        )

    def set_result_consumer(self, consumer: ResultConsumer) -> None:
        """Bind projection consumption once, before the queue serves any work."""
        if not callable(consumer):
            raise InvalidType(
                "agent result consumer must be callable",
                reason=InvalidRequestReason.MALFORMED,
            )
        with self._configuration_lock:
            if self._result_consumer is not None:
                raise AgentConfigurationConflict(
                    "agent result consumer is already configured",
                    reason=InvalidRequestReason.IMMUTABLE,
                )
            if self._started:
                raise AgentConfigurationConflict(
                    "agent job service has already started",
                    reason=InvalidRequestReason.IMMUTABLE,
                )
            self._result_consumer = consumer

    def set_contact_consumer(self, consumer: ContactConsumer) -> None:
        """Bind atomic authenticated contact persistence before serving work."""

        if not callable(consumer):
            raise InvalidType(
                "agent contact consumer must be callable",
                reason=InvalidRequestReason.MALFORMED,
            )
        with self._configuration_lock:
            if self._contact_consumer is not None:
                raise AgentConfigurationConflict(
                    "agent contact consumer is already configured",
                    reason=InvalidRequestReason.IMMUTABLE,
                )
            if self._started:
                raise AgentConfigurationConflict(
                    "agent job service has already started",
                    reason=InvalidRequestReason.IMMUTABLE,
                )
            self._contact_consumer = consumer

    def set_rollout_owner(
        self,
        advance: Callable[[Session, Job], None],
        advance_node: Callable[[str], None],
        *,
        reconcile: Callable[[int], bool] | None = None,
    ) -> None:
        """Bind the agent-upgrade rollout owner.

        ``advance`` owns an agent-upgrade parent's projection inside the
        caller's transaction; ``advance_node`` resumes deferred rollouts when a
        Spark polls.  The rollout never waits for an operator.
        """

        with self._configuration_lock:
            self._advance_rollout = advance
            self._advance_node = advance_node
            self._reconcile_rollouts = reconcile

    def _mark_started(self) -> None:
        with self._configuration_lock:
            self._started = True

    def claim(
        self,
        node_id: str,
        certificate_serial: str,
        wait_seconds: float = 0,
        *,
        runtime_identity: object,
        preflight_fingerprint: str | None = None,
        hostname: str | None = None,
        source: AgentSource | None = None,
    ) -> AgentClaim | None:
        return claim(
            self,
            node_id,
            certificate_serial,
            wait_seconds,
            runtime_identity=runtime_identity,
            preflight_fingerprint=preflight_fingerprint,
            hostname=hostname,
            source=source,
        )

    def _record_claim_refusal(
        self,
        session: Session,
        *,
        operation: StoredOperation | None,
        job_id: str | None,
        check: str,
        **facts: object,
    ) -> None:
        return _record_claim_refusal(
            self, session, operation=operation, job_id=job_id, check=check, **facts
        )

    def _write_refusal_note(
        self,
        session: Session,
        *,
        operation: StoredOperation | None,
        job_id: str | None,
        reason: str,
    ) -> None:
        return _write_refusal_note(
            self, session, operation=operation, job_id=job_id, reason=reason
        )

    def _note_malformed_cancel_flag(
        self, session: Session, operation: StoredOperation
    ) -> None:
        return _note_malformed_cancel_flag(self, session, operation)

    @staticmethod
    def _refresh_parent_claim_refusal(
        session: Session, operation: StoredOperation, now: datetime
    ) -> None:
        return _refresh_parent_claim_refusal(session, operation, now)

    def record_boundary_refusal(
        self,
        fence: str,
        *,
        boundary: str,
        check: str,
        **facts: object,
    ) -> bool:
        return record_boundary_refusal(
            self, fence, boundary=boundary, check=check, **facts
        )

    def _excluded_work_refusal(
        self,
        session: Session,
        node: AgentNode,
        now: datetime,
    ) -> tuple[StoredOperation, str, ClaimFacts] | None:
        return _excluded_work_refusal(self, session, node, now)

    def _cancel_superseded_operation(
        self,
        session: Session,
        operation: StoredOperation,
        parent: Job,
        now: datetime,
        *,
        superseded_by: int | None,
        disarmed: bool,
    ) -> None:
        return _cancel_superseded_operation(
            self,
            session,
            operation,
            parent,
            now,
            superseded_by=superseded_by,
            disarmed=disarmed,
        )

    def _superseded_cancellation_state(
        self,
        session: Session,
        old: StoredOperation,
        operation: StoredOperation,
        now: datetime,
    ) -> str:
        return _superseded_cancellation_state(self, session, old, operation, now)

    def _reconcile_dead_running_operation(
        self,
        session: Session,
        operation: StoredOperation,
        attempt: AgentOperationAttempt | None,
        node: AgentNode,
        now: datetime,
        *,
        superseded_by: int | None,
    ) -> None:
        return _reconcile_dead_running_operation(
            self, session, operation, attempt, node, now, superseded_by=superseded_by
        )

    def _settle_lapsed(
        self,
        session: Session,
        operation: StoredOperation,
        attempt: AgentOperationAttempt | None,
        reason: str,
        now: datetime,
    ) -> None:
        return _settle_lapsed(self, session, operation, attempt, reason, now)

    @staticmethod
    def _claimable_operations(node_id: str, now: datetime):
        return _claimable_operations(node_id, now)

    def _fail_spent_start_budgets(
        self, session: Session, node_id: str, now: datetime
    ) -> int:
        return _fail_spent_start_budgets(self, session, node_id, now)

    def _claim_once(
        self,
        node_id: str,
        certificate_serial: str,
        runtime_identity: AgentRuntimeIdentity,
        preflight_fingerprint: str | None,
        hostname: str | None,
        source: AgentSource | None,
    ) -> AgentClaim | None:
        return _claim_once(
            self,
            node_id,
            certificate_serial,
            runtime_identity,
            preflight_fingerprint,
            hostname,
            source,
        )

    def _reconcile_agent_upgrade(
        self,
        session: Session,
        node_id: str,
        certificate_serial: str,
        now: datetime,
        runtime_identity: AgentRuntimeIdentity,
        *,
        operation_id: str | None,
        parent_job_id: str | None,
    ) -> None:
        return _reconcile_agent_upgrade(
            self,
            session,
            node_id,
            certificate_serial,
            now,
            runtime_identity,
            operation_id=operation_id,
            parent_job_id=parent_job_id,
        )

    @staticmethod
    def _recipe_build_runtime_matches(
        session: Session,
        operation: StoredOperation,
        runtime_identity: AgentRuntimeIdentity,
    ) -> bool:
        return _recipe_build_runtime_matches(session, operation, runtime_identity)

    def _reject_recipe_build_claim(
        self,
        session: Session,
        operation: StoredOperation,
        certificate_serial: str,
        now: datetime,
    ) -> None:
        return _reject_recipe_build_claim(
            self, session, operation, certificate_serial, now
        )

    def _claim_has_authority(
        self,
        session: Session,
        operation: StoredOperation,
        now: datetime,
        *,
        node: AgentNode,
        locked_targets: tuple[str, ...],
    ) -> bool:
        return _claim_has_authority(
            self, session, operation, now, node=node, locked_targets=locked_targets
        )

    @staticmethod
    def _target_scope(targets: object) -> tuple[str, ...] | None:
        return _target_scope(targets)

    @classmethod
    def _lock_operation_scopes(
        cls,
        session: Session,
        operation_ids: tuple[str, ...],
        node_id: str,
        *,
        nowait: bool = False,
    ) -> dict[str, tuple[str, tuple[str, ...]]] | None:
        return _lock_operation_scopes(
            cls, session, operation_ids, node_id, nowait=nowait
        )

    @classmethod
    def _lock_target_scopes(
        cls,
        session: Session,
        scopes: dict[str, tuple[str, tuple[str, ...]]],
        node_id: str,
        *,
        nowait: bool = False,
    ) -> bool:
        return _lock_target_scopes(cls, session, scopes, node_id, nowait=nowait)

    def heartbeat(
        self,
        fence: AgentFence,
        progress: OperationProgress | None,
        lease_seconds: int,
        *,
        source: AgentSource | None = None,
    ) -> AgentDirective:
        return heartbeat(self, fence, progress, lease_seconds, source=source)

    def known_superseded_cancellation(
        self, fence: AgentProgress, *, source: AgentSource | None = None
    ) -> bool:
        return known_superseded_cancellation(self, fence, source=source)

    def succeed(self, fence: AgentFence, result: AgentResultPayload) -> None:
        return succeed(self, fence, result)

    def fail(self, fence: AgentFence, reason: str) -> None:
        return fail(self, fence, reason)

    def record_result(
        self, message: AgentResult, *, source: AgentSource | None = None
    ) -> None:
        return record_result(self, message, source=source)

    def record_late_result(
        self, message: AgentResult, *, source: AgentSource | None = None
    ) -> bool:
        return record_late_result(self, message, source=source)

    def _finish(
        self,
        fence: AgentFence,
        state: str,
        *,
        result: AgentResultPayload | None,
        reason: str | None,
        source: AgentSource | None = None,
    ) -> None:
        self._mark_started()
        with self._sessions.begin() as session:
            operation, attempt = self._active(
                session,
                fence,
                source=source,
                allow_superseded_cancellation=state == "cancelled",
                # An outcome is the same launch decision the heartbeat renewal
                # already allows: an attempt that is still the operation's own
                # current attempt inside its declared readiness budget may report
                # what it observed.  A newer attempt, a stopped attempt, and a
                # spent budget all still refuse it, so this never re-blesses an
                # abandoned effect.
                allow_lapsed_renewal=True,
            )
            now = self._clock()
            node = session.get(AgentNode, operation.node_id)
            parent = session.get(Job, operation.parent_job_id)
            superseded = bool(
                node is not None
                and operation.workload_intent_ordinal is not None
                and operation.workload_intent_ordinal != node.workload_intent_ordinal
            )
            if superseded:
                cancellation_deadline = superseded_cancellation_deadline(
                    None if parent is None else column_value(parent, "result")
                )
                if (
                    state != "cancelled"
                    or cancellation_deadline is None
                    or _aware(now) >= cancellation_deadline
                ):
                    end_unobserved_order(
                        self,
                        session,
                        operation,
                        attempt,
                        parent,
                        now,
                        reason=WaitReason.LEASE_LAPSED,
                        note="superseded operation has no completion authority",
                    )
                    return
            if isinstance(fence, AgentResult):
                if fence.state != state or (
                    result is not None and _document(fence.result) != _document(result)
                ):
                    raise InvalidValue(
                        "agent result does not match requested completion",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                message = fence
            else:
                canonical_result = (
                    result if result is not None else {"reason": self._reason(reason)}
                )
                message = AgentResult.model_validate_json(
                    canonical_message(
                        {
                            "fence": attempt.fence,
                            "state": state,
                            "result": canonical_result,
                        }
                    )
                )
            # One reading of the report: a typed outcome is projected to the
            # stored body shape, a legacy body goes through the legacy adapter.
            message, outcome = stored_report(operation.kind, message)
            validate_result_for_operation(
                operation.kind,
                message.result,
                state=message.state,
            )
            if state in {"failed", agent_operation_states.WIRE_UNKNOWN}:
                try:
                    raw_result = _document(message.result)
                    diagnostics = raw_result.pop("diagnostics", None)
                    if (
                        operation.kind == AgentOperation.RECIPE_JOB_RUN.value
                        and "exit_code" in raw_result
                    ):
                        # Preserve the typed output manifest and process receipt.
                        # Generic log truncation must not rewrite their structure.
                        message_result = raw_result
                        raw_reason = message_result.get("reason")
                        if isinstance(raw_reason, str):
                            message_result["reason"] = safe_text(raw_reason)
                    else:
                        message_result = sanitize_failure_evidence(raw_result)
                    if diagnostics is not None:
                        message_result["diagnostics"] = sanitize_diagnostics(
                            diagnostics
                        ).model_dump(mode="json")
                    message = AgentResult.model_validate_json(
                        canonical_message(
                            {
                                **message.model_dump(mode="json"),
                                "result": message_result,
                            }
                        )
                    )
                except (TypeError, ValueError) as error:
                    raise InvalidValue(
                        f"operation failure evidence is invalid: {error}",
                        reason=InvalidRequestReason.MALFORMED,
                    ) from error
            if (
                state == "succeeded"
                and operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value
                and isinstance(message.result, ArtifactDistributionResult)
            ):
                # Final authoritative evidence closes a last sample that may
                # have been coalesced immediately before result publication.
                retained = stored_progress(attempt)
                final_progress = OperationProgress(
                    phase=ProgressPhase.COMPLETED,
                    completed_bytes=message.result.downloaded_bytes,
                    completed_items=None if retained is None else retained.total_items,
                )
                final_progress = validate_progress_update(retained, final_progress)
                attempt.progress = progress_document(
                    sample_progress(retained, final_progress, _aware(now))
                )
                record_distributed_runtime_image(
                    session,
                    node_id=operation.node_id,
                    plan_digest=operation.authority_revision,
                    now=now,
                )
            adapter = AgentOperationAdapter(
                session, resume_candidates=operator_resume_candidates_in_session
            )
            adapter.record_report(attempt, state, message.result)
            adapter.settle(
                operation,
                attempt,
                parent,
                self._report_event(
                    operation, attempt, parent, outcome, message.result, now
                ),
                now,
            )
            if self._result_consumer is not None:
                self._result_consumer(session, operation, attempt, message)
            self._aggregate_parent(session, operation.parent_job_id)
        # A result consumer can atomically make the next durable recipe phase
        # queueable; wake long-polling agents only after that transaction commits.
        self.notify_available()

    @staticmethod
    def _report_event(
        operation: StoredOperation,
        attempt: AgentOperationAttempt,
        parent: Job | None,
        outcome: OutcomeDone | OutcomeFailed | OutcomeUnknown,
        result: AgentResultPayload,
        now: datetime,
    ) -> Reported:
        return _report_event(operation, attempt, parent, outcome, result, now)

    def _active(
        self,
        session: Session,
        fence: AgentFence,
        *,
        source: AgentSource | None = None,
        allow_superseded_cancellation: bool = False,
        allow_lapsed_renewal: bool = False,
    ) -> tuple[StoredOperation, AgentOperationAttempt]:
        return _active(
            self,
            session,
            fence,
            source=source,
            allow_superseded_cancellation=allow_superseded_cancellation,
            allow_lapsed_renewal=allow_lapsed_renewal,
        )

    @staticmethod
    def _record_contact(
        session: Session,
        node: AgentNode,
        certificate: AgentCertificate,
        now: datetime,
        runtime_identity: AgentRuntimeIdentity | None,
        preflight_fingerprint: str | None,
        hostname: str | None,
    ) -> None:
        return _record_contact(
            session,
            node,
            certificate,
            now,
            runtime_identity,
            preflight_fingerprint,
            hostname,
        )

    @staticmethod
    def _runtime_identity(
        value: object,
    ) -> AgentRuntimeIdentity:
        return _runtime_identity(value)

    def _consume_contact(
        self,
        session: Session,
        source: AgentSource | None,
        node: AgentNode,
        certificate: AgentCertificate,
    ) -> None:
        return _consume_contact(self, session, source, node, certificate)

    @staticmethod
    def _fence_token(fence: AgentFence) -> str:
        return _fence_token(fence)

    @staticmethod
    def _reason(reason: str | None) -> str:
        return _reason(reason)

    @staticmethod
    def _lock_identity(
        session: Session,
        node_id: str,
        certificate_serial: str,
    ) -> tuple[AgentNode, AgentCertificate] | None:
        return _lock_identity(session, node_id, certificate_serial)

    @staticmethod
    def _identity_is_active(
        node: AgentNode,
        certificate: AgentCertificate,
        now: datetime,
    ) -> bool:
        return _identity_is_active(node, certificate, now)

    def _aggregate_parent(self, session: Session, parent_job_id: str) -> None:
        return _aggregate_parent(self, session, parent_job_id)

    def _aggregate_parent_state(self, session: Session, parent_job_id: str) -> None:
        return _aggregate_parent_state(self, session, parent_job_id)
