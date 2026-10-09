"""Cancellation retry."""

from __future__ import annotations

import uuid
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RunSwitchCode,
    WaitReason,
)

from .. import job_states
from ..admission_locking import admission_attempts
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from ..artifact_reference_scan import (
    _run_switch_runtime_image_intent,
)
from ..bounded_json import require_integer
from ..job_documents import (
    RecipeStartParent,
    RecipeStopParent,
)
from ..lifecycle.run_switch import (
    LIVE_STATES as _LIVE_STATES,
)
from ..models import (
    AgentNode,
    Job,
)
from ..recipe_build_cancellation import (
    BuildConsumerError,
    lock_run_switch_build_dependency,
)
from ..run_switch_contract import (
    RunSwitchCancellation,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchRuntimeImageReferenceIntent,
    RunSwitchStopApplyRequest,
)
from ..stored_json import read_row_column
from ..strict_json import (
    read_stored_model,
    serialize_json_value,
)
from .constants import _OPERATION_KINDS
from .errors import RunSwitchRequestInvalid, RunSwitchRetryLater
from .identity_helpers import _string_or_none
from .ownership import _complete_cancellation
from .plan_persistence import _reserve_run_switch_assets
from .planning_helpers import _digest, _now, _run_switch_payload, _stored_job_plan
from .provider import _ADAPTER
from .result_helpers import _parse_persisted_result, _persisted_result, _read_progress

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class CancellationRetryMixin:
    def cancel(
        self, operation_id: str, *, actor: str, request_key: str, reason: str
    ) -> RunSwitchOperation:
        """Commit cancellation intent and retry contention in fresh transactions.

        The first attempt persists intent before the contested boundary. When
        admission backoff ends, report the typed contention. The durable intent
        remains pending; the ordinary tick resumes it under its stop deadline.
        """
        service = typing_cast("RunSwitchOperationService", self)
        contention: RunSwitchRetryLater | None = None
        for _attempt in admission_attempts():
            try:
                result = service._cancel_once(
                    operation_id, actor=actor, request_key=request_key, reason=reason
                )
                end = getattr(service._artifact_phase_executor, "end_background", None)
                if callable(end):
                    end(service.get(operation_id).request_key)
                return result
            except RunSwitchRetryLater as error:
                contention = error
                continue
        if contention is not None:
            raise contention
        return service.get(operation_id)

    def _cancel_once(
        self, operation_id: str, *, actor: str, request_key: str, reason: str
    ) -> RunSwitchOperation:
        """Stop at the next safe phase boundary, keeping shared immutable work."""
        service = typing_cast("RunSwitchOperationService", self)
        from ..run_switch_journal_repair import (
            is_zero_transfer_journal_fault,
            record_repair_cancellation,
            try_repair_zero_transfer_journal,
        )

        stop_run_id: str | None = None
        profile_application_id: str | None = None
        cancellation = RunSwitchCancellation(
            request_key=request_key,
            actor=actor,
            reason=" ".join(reason.split()),
            requested_at=_now(service._clock),
        )
        with service._sessions() as session:
            snapshot = session.get(Job, operation_id)
            if (
                snapshot is not None
                and snapshot.kind in _OPERATION_KINDS
                and snapshot.state not in _LIVE_STATES
            ):
                return service._operation_view(snapshot)
            damaged = snapshot is not None and is_zero_transfer_journal_fault(snapshot)
        if damaged:
            record_repair_cancellation(service._sessions, operation_id, cancellation)
            try_repair_zero_transfer_journal(
                service._sessions, operation_id, _now(service._clock)
            )
            return service.get(operation_id)
        with service._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise RunSwitchRetryLater(
                    "run-switch cancellation owner observation is unavailable"
                )
            progress = _read_progress(read_row_column(job, "result"))
            if job.state not in _LIVE_STATES:
                return service._operation_view(job)
            if progress.cancellation is None:
                progress.cancellation = cancellation
                job.result = _persisted_result(progress)
                job.updated_at = cancellation.requested_at
        # The intent above is committed before a build lock, child observation
        # or Stop admission can fail. Repeated cancels retain that first intent.
        with service._sessions.begin() as session:
            # Read identities first, then acquire the build before its parent.
            # Detachment and last-consumer cleanup must share this NOWAIT fence.
            snapshot = session.get(Job, operation_id)
            if snapshot is None:
                return service.get(operation_id)
            snapshot_plan = _stored_job_plan(snapshot)
            snapshot_index = _read_progress(
                read_row_column(snapshot, "result")
            ).phase_index
            if snapshot_plan is not None:
                try:
                    lock_run_switch_build_dependency(
                        session,
                        snapshot_plan,
                        phase_index=snapshot_index,
                        allow_cancelling=True,
                    )
                except BuildConsumerError as error:
                    raise RunSwitchRetryLater(
                        f"{error.code}: {error}",
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    ) from error
            try:
                job = session.scalar(
                    select(Job)
                    .where(Job.id == operation_id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "55P03":
                    raise
                raise RunSwitchRetryLater(
                    "run-switch cancellation owner is busy",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            if job is None:
                return service.get(operation_id)
            if job.state not in _LIVE_STATES:
                return service._operation_view(job)
            progress = _read_progress(read_row_column(job, "result"))
            if (
                _stored_job_plan(job) != snapshot_plan
                or progress.phase_index != snapshot_index
            ):
                # Replan in a fresh transaction; never append an earlier lock.
                raise RunSwitchRetryLater(
                    "run-switch cancellation boundary changed",
                    reason=WaitReason.SCOPE_CHANGED,
                )
            cancellation = progress.cancellation or cancellation
            # A recorded cancel is retried: an unreadable plan is unknown,
            # so the cancel treats the operation as possibly started and Stops
            # through the child's run, without the plan's build-dependency lock.
            plan = _stored_job_plan(job)
            profile_application_id = _string_or_none(progress.profile_application_id)
            phase = None
            if plan is not None:
                phase = plan.phases[
                    min(
                        require_integer(progress.phase_index, "phase index"),
                        len(plan.phases) - 1,
                    )
                ]
            if "start" in progress.completed_phases or (
                job.state
                in job_states.words(LifecycleState.RUNNING, LifecycleState.OBSERVING)
                and (phase is None or phase.kind in {"start", "final_verify"})
            ):
                child_id = _string_or_none(progress.child_operation_id)
                child = session.get(Job, child_id) if child_id is not None else None
                child_parent = (
                    read_row_column(child, "payload") if child is not None else None
                )
                owner_id = (
                    child_parent.owner_id
                    if isinstance(child_parent, RecipeStartParent | RecipeStopParent)
                    and child_parent.owner_kind == "run"
                    else None
                )
                stop_run_id = (
                    plan.run_id if plan is not None else None
                ) or _string_or_none(owner_id)
                # The operation is marked cancelled only after its Stop is
                # durably accepted below; a failed Stop leaves it cancellable.
                # An intent with no run identity to Stop is a bookkeeping gap,
                # not a refusal: the cancel is recorded and driven like any
                # other, so it observes the child and completes (rule 4).
            if stop_run_id is None:
                progress.cancellation = cancellation
                progress.retry_attempt = None
                if job.state in job_states.words(
                    LifecycleState.OBSERVING, LifecycleState.NEEDS_OPERATOR
                ):
                    progress.observation_due_at = cancellation.requested_at
                job.status_reason = (
                    "Cancellation requested; finishing the current preparation safely."
                )
                if phase is not None and phase.subphase == "container-build":
                    _complete_cancellation(job, progress, cancellation.requested_at)
                else:
                    # Nothing issued ends at once; an issued child is stopped and
                    # observed up to the core's budget, then the cancel ends with
                    # its effect recorded unknown.
                    service._cancel_with_session(
                        session, job, progress, cancellation.requested_at
                    )
                    job.result = _persisted_result(progress)
                    job.updated_at = cancellation.requested_at
        if stop_run_id is not None:
            stop = service.apply_stop(
                RunSwitchStopApplyRequest(
                    run_id=stop_run_id, request_key=cancellation.request_key
                ),
                actor=actor,
                profile_application_id=profile_application_id,
            )
            with service._sessions.begin() as session:
                job = session.get(Job, operation_id, with_for_update=True)
                if job is not None and job.state in _LIVE_STATES:
                    progress = _read_progress(read_row_column(job, "result"))
                    progress.cancellation = cancellation
                    _ADAPTER.cancelled(
                        job,
                        progress,
                        cancellation.requested_at,
                        reason=(
                            "Cancellation translated into Run/Switch Stop "
                            f"operation {stop.operation_id}"
                        ),
                    )
                    job.result = _persisted_result(progress)
                    job.updated_at = cancellation.requested_at
            return stop
        return service.get(operation_id)

    def retry(
        self,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
    ) -> RunSwitchOperation:
        """Re-observe stored evidence and owners in bounded fresh transactions."""
        service = typing_cast("RunSwitchOperationService", self)
        pending: RunSwitchRetryLater | None = None
        for _attempt in admission_attempts():
            try:
                return service._retry_once(
                    operation_id, actor=actor, request_key=request_key
                )
            except RunSwitchRetryLater as error:
                pending = error
        assert pending is not None
        raise pending

    def _retry_once(
        self,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
    ) -> RunSwitchOperation:
        """One observation; rollback frees reference/build locks before retry."""
        service = typing_cast("RunSwitchOperationService", self)

        try:
            uuid.UUID(request_key)
        except (TypeError, ValueError, AttributeError) as error:
            raise RunSwitchRequestInvalid(
                "run-switch retry request key is invalid"
            ) from error
        with service._sessions.begin() as session:
            previous = session.get(Job, operation_id, with_for_update=True)
            if previous is None or previous.kind not in _OPERATION_KINDS:
                raise RunSwitchRetryLater(
                    "run-switch operation observation is unavailable"
                )
            existing = session.scalar(select(Job).where(Job.request_id == request_key))
            if existing is not None:
                if existing.kind != previous.kind:
                    raise RunSwitchRequestInvalid(
                        "run-switch request key was already used",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                return service._operation_view(existing)
            current_progress = _parse_persisted_result(
                read_row_column(previous, "result")
            )
            progress = (
                current_progress.model_copy(deep=True)
                if current_progress is not None
                else RunSwitchOperationResult()
            )
            if current_progress is None:
                parent = _run_switch_payload(previous)
                current_progress = parent.progress if parent is not None else None
                if current_progress is None:
                    raise RunSwitchRetryLater(
                        "run-switch retry evidence is unavailable"
                    )
                progress = current_progress.model_copy(deep=True)
            if previous.state != LifecycleState.FAILED.value:
                raise RunSwitchRetryLater(
                    "run-switch operation observation is unavailable"
                )
            nodes = list(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(previous.targets))
                    .order_by(AgentNode.node_id)
                    .with_for_update()
                )
            )
            previous_parent = _run_switch_payload(previous)
            if previous_parent is None or len(nodes) != len(previous.targets):
                raise RunSwitchRetryLater(
                    "run-switch retry ownership observation is unavailable"
                )
            if any(
                node.workload_intent_ordinal != previous_parent.workload_intent_ordinal
                for node in nodes
            ):
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.SUPERSEDED}: retry no longer owns the accepted workload intent",
                    reason=WaitReason.SCOPE_CHANGED,
                )
            retry_plan = _stored_job_plan(previous)
            if retry_plan is None:
                raise RunSwitchRetryLater(
                    "accepted run-switch plan observation is unavailable"
                )
            prior_image_intent = current_progress.runtime_image_reference_intent
            if prior_image_intent is not None:
                try:
                    validated_intent = _run_switch_runtime_image_intent(
                        previous, retry_plan
                    )
                except ArtifactLifecycleError as error:
                    raise RunSwitchRetryLater(
                        "run-switch retry runtime image reference is unavailable"
                    ) from error
                if validated_intent != prior_image_intent:
                    raise RunSwitchRetryLater(
                        "run-switch retry runtime image reference is unavailable"
                    )
            now = _now(service._clock)
            _reserve_run_switch_assets(session, retry_plan, now=now)
            if prior_image_intent is not None:
                try:
                    require_reference_open(
                        session,
                        (
                            ArtifactIdentity(
                                "runtime-image", prior_image_intent.archive_sha256
                            ),
                        ),
                        now=now,
                    )
                except ArtifactLifecycleError as error:
                    raise RunSwitchRetryLater(
                        f"{error.code}: {error.detail}"
                    ) from error
            try:
                lock_run_switch_build_dependency(
                    session,
                    retry_plan,
                    phase_index=current_progress.phase_index,
                )
            except BuildConsumerError as error:
                raise RunSwitchRetryLater(f"{error.code}: {error}") from error
            ordinal = max(node.workload_intent_ordinal for node in nodes) + 1
            for node in nodes:
                node.workload_intent_ordinal = ordinal
            service.request_superseded_workload_cancellation_in_session(
                session, tuple(previous.targets), ordinal, now
            )
            retry_job_id = str(uuid.uuid4())
            if prior_image_intent is not None:
                rebound_intent = read_stored_model(
                    RunSwitchRuntimeImageReferenceIntent,
                    {
                        **prior_image_intent.model_dump(mode="json"),
                        "operation_id": retry_job_id,
                        "request_key": request_key,
                        "actor": actor,
                        "workload_intent_ordinal": ordinal,
                    },
                    strict=True,
                )
                progress.runtime_image_reference_intent = rebound_intent
            progress.child_operation_id = None
            progress.retryable = False
            progress.failure_code = None
            # A new explicit request owns a new observation lifetime. Retain
            # verified phase effects, never its predecessor's spent clocks.
            progress.retry_attempt = None
            progress.retry_reason = None
            progress.observation_due_at = None
            progress.observation_deadline_at = None
            progress.recovery_deadline_at = None
            progress.start_deadline = None
            progress.final_verify_started_at = None
            progress.failed_phase = None
            progress.final_observation = None
            progress.blockers = []
            progress.workload_intent_ordinal = ordinal
            payload = serialize_json_value(
                previous_parent.model_copy(
                    update={
                        "workload_intent_ordinal": ordinal,
                        "progress": progress,
                        "retry_of": previous.id,
                    }
                )
            )
            job = _ADAPTER.new_operation(
                allowed=True,
                id=retry_job_id,
                request_id=request_key,
                kind=previous.kind,
                actor=actor,
                authority_revision=previous.authority_revision,
                targets=list(previous.targets),
                payload_digest=_digest(payload),
                payload=payload,
                result=_persisted_result(progress),
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            return service._operation_view(job)
