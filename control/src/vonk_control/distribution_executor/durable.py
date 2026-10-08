"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    DistributionCode,
    LifecycleState,
    OperationMemberProgress,
    OperationProgress,
    ProgressPhase,
    WaitReason,
)
from vonk_agent_protocol.agent_words import ProfileChildPhase, ProfileEffectState

from ..agent_jobs import abandon_idempotent_job_in_session
from ..bounded_json import sequence
from ..distribution_assignment import NodeDistributionAssignment
from ..lifecycle.job import JobAdapter
from ..models import (
    AgentOperation,
    AgentOperationAttempt,
    Job,
)
from ..operation_progress import aggregate_progress, project_progress
from ..run_switch_contract import (
    RunSwitchDistributionEndedResult,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
)
from ..run_switch_operations import PhaseExecution
from ..strict_json import read_stored_model, serialize_json_value
from .children import DistributionChildren
from .receipts import (
    _child_receipt,
    _ChildView,
    _evidence_projection,
    _phase_receipt,
    _typed_failure,
)


class DurableDistributionPhaseExecutor(DistributionChildren):
    """Durable boundary for durable artifact distribution."""

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution:
        if phase.kind not in {
            ProfileChildPhase.TRANSFER.value,
            ProfileChildPhase.VERIFY.value,
        }:
            return PhaseExecution(
                result=_phase_receipt(
                    {
                        "scope": "spark-local",
                        "reclaimed_bytes": 0,
                        "protected_referenced_bytes": 0,
                        "reclaimed_digests": [],
                        "protected_digests": [],
                        "nas_evicted": False,
                    },
                    phase=phase,
                )
            )
        if item_index != 0:
            raise RuntimeError(f"unexpected {phase.kind} item index {item_index}")
        if phase.kind == ProfileChildPhase.VERIFY.value:
            targets = tuple(phase.node_ids)
            cached = self._cached_targets(plan, targets)
            if len(cached) == len(targets):
                return PhaseExecution(
                    result=_phase_receipt(
                        self._verification_result(
                            plan,
                            progress,
                            skipped=True,
                            cached_nodes=cached,
                            cached_target_totals={
                                node_id: self._target_bytes(plan, node_id)
                                for node_id in cached
                            },
                        ),
                        phase=phase,
                    )
                )
            return PhaseExecution(
                result=_phase_receipt(
                    self._verify_evidence(plan, progress, targets, cached), phase=phase
                )
            )
        targets = tuple(phase.node_ids)
        intent_ordinal = progress.workload_intent_ordinal
        if type(intent_ordinal) is int and intent_ordinal > 0:
            adopted = self._adopt_child(
                plan,
                phase,
                actor=actor,
                request_key=request_key,
                workload_intent_ordinal=intent_ordinal,
            )
            if adopted is not None:
                return adopted
        cached = self._cached_targets(plan, targets)
        missing = tuple(node_id for node_id in targets if node_id not in cached)
        if not missing:
            return PhaseExecution(
                result=_phase_receipt(
                    {
                        "skipped": True,
                        "verified": phase.kind == ProfileChildPhase.VERIFY.value,
                        "verified_digests": list(plan.storage.artifact_digests),
                        "verified_build_id": self._runtime_identity(plan, progress)[3],
                        "verified_image_digest": (
                            plan.preparation.runtime_image.image_digest
                            if plan.preparation is not None
                            else plan.image_digest
                        ),
                        "verified_oci_layout_sha256": (
                            plan.preparation.runtime_image.oci_layout_sha256
                            if plan.preparation is not None
                            else plan.build.oci_layout_sha256
                        ),
                        "cached_nodes": list(targets),
                        "cached_target_totals": {
                            node_id: self._target_bytes(plan, node_id)
                            for node_id in targets
                        },
                    },
                    phase=phase,
                )
            )
        if type(intent_ordinal) is not int or intent_ordinal < 1:
            raise RuntimeError("distribution requires its authorized workload intent")
        model_objects, model_set_digest, model_set_bytes = self._model_objects(
            plan, progress
        )
        image_digest, layout_digest, image_bytes, build_id = self._runtime_identity(
            plan, progress
        )
        image = self._archive(
            build_id=build_id,
            image_digest=image_digest,
            layout_digest=layout_digest,
            image_bytes=image_bytes,
        )
        assignments = {
            node_id: self._assignment(
                plan,
                node_id,
                model_objects,
                image,
                model_set_digest=model_set_digest,
            )
            for node_id in missing
        }
        child_id = self._ensure_child(
            plan,
            phase,
            actor=actor,
            request_key=request_key,
            cached=cached,
            assignments=assignments,
            target_order=targets,
            # The image is pulled, not downloaded as an object; Docker fetches
            # only the layers the node lacks.
            target_bytes=model_set_bytes,
            workload_intent_ordinal=intent_ordinal,
        )
        return PhaseExecution(
            operation_id=child_id,
            result=_phase_receipt(
                {
                    "cached_nodes": list(cached),
                    # Persist the exact assignment already verified against the
                    # succeeded build and cache manifest for the verify phase.
                    "assignments": {
                        node_id: assignment.to_mapping()
                        for node_id, assignment in assignments.items()
                    },
                },
                phase=phase,
            ),
        )

    def abandon(
        self, session: Session, operation_id: str, now: datetime, *, reason: str
    ) -> bool:
        """Close a parked distribution child its owner cancelled.

        Idempotent and content-addressed: finished objects and partial files
        stay on the Spark for the next transfer.
        """

        child = session.get(Job, operation_id)
        if child is None or child.kind != "artifact-distribution":
            return False
        return abandon_idempotent_job_in_session(session, child.id, now, reason=reason)

    def get(self, operation_id: str) -> _ChildView:
        with self._sessions() as session:
            child = session.get(Job, operation_id)
            if child is None or child.kind != "artifact-distribution":
                return _ChildView(
                    state=LifecycleState.FAILED,
                    result=RunSwitchDistributionEndedResult(
                        phase=ProfileChildPhase.TRANSFER,
                        subphase=ProfileChildPhase.TARGET_COPY,
                        reason=WaitReason.RECEIPT_MISSING,
                        error_code=DistributionCode.UNASSIGNED,
                    ),
                )
            # AgentJobService owns the parent state transition. Reconcile the
            # durable child before projecting it so a restart cannot leave a
            # completed set of node operations looking queued.
            self._operations._aggregate_parent(session, child.id)
            operations = list(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == child.id)
                    .order_by(AgentOperation.node_id)
                )
            )
            members = []
            measured_members = []
            evidence = []
            cached_nodes = tuple(
                value
                for value in sequence(child.payload.get("cached_nodes")) or ()
                if isinstance(value, str)
            )
            cached_totals = child.payload.get("target_totals", {})
            if not isinstance(cached_totals, Mapping):
                cached_totals = {}
            assignments = child.payload.get("assignments", {})
            if not isinstance(assignments, Mapping):
                assignments = {}

            def target_total(node_id: str) -> int | None:
                declared = self._int(cached_totals.get(node_id))
                assignment = assignments.get(node_id)
                if not isinstance(assignment, Mapping):
                    return declared
                try:
                    parsed = NodeDistributionAssignment.parse(assignment)
                except (TypeError, ValueError):
                    return None
                if parsed.node_id != node_id:
                    return None
                assigned = sum(item.bytes for item in parsed.objects)
                if declared is not None and assigned != declared:
                    return None
                return declared if declared is not None else assigned

            for node_id in cached_nodes:
                total = self._int(cached_totals.get(node_id))
                members.append(
                    {
                        "node_id": node_id,
                        "phase": ProgressPhase.TRANSFER,
                        "state": LifecycleState.SUCCEEDED.value,
                        "completed_bytes": total or 0,
                        "total_bytes": total,
                        "error": None,
                        "cached": True,
                    }
                )
            for operation in operations:
                attempt = session.scalar(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id == operation.id,
                        AgentOperationAttempt.attempt == operation.current_attempt,
                    )
                )
                raw = (attempt.progress if attempt is not None else None) or {}
                result = (attempt.result if attempt is not None else None) or {}
                member_state = self._member_state(operation.state)
                if (
                    member_state
                    in {
                        ProfileEffectState.PENDING.value,
                        ProfileEffectState.UNKNOWN.value,
                    }
                    and attempt is not None
                ):
                    # A terminal result is the durable handoff even when a
                    # restart observed the parent operation row before its
                    # aggregate state was reconciled.
                    member_state = self._member_state(attempt.state)
                expected_total = target_total(operation.node_id)
                terminal_downloaded = self._int(result.get("downloaded_bytes"))
                evidence_error = None
                if member_state == LifecycleState.SUCCEEDED.value and (
                    expected_total is None or terminal_downloaded != expected_total
                ):
                    member_state = LifecycleState.FAILED.value
                    evidence_error = "distributed transfer byte evidence mismatch"
                members.append(
                    {
                        "node_id": operation.node_id,
                        "phase": ProgressPhase.TRANSFER,
                        "state": member_state,
                        # The agent's terminal distribution evidence reports the
                        # aggregate payload under ``downloaded_bytes``. Preserve
                        # that exact handoff in the durable child projection;
                        # otherwise a successful import appears pending with zero
                        # bytes even though its result body is complete.
                        "completed_bytes": (
                            (
                                self._int(raw.get("bytes"))
                                or self._int(raw.get("completed_bytes"))
                                if member_state != LifecycleState.SUCCEEDED.value
                                else terminal_downloaded
                            )
                            or 0
                        ),
                        "total_bytes": self._int(raw.get("total_bytes"))
                        or expected_total,
                        "error": evidence_error
                        or (
                            result.get("reason")
                            if isinstance(result, Mapping)
                            else None
                        ),
                        **(
                            _typed_failure(result)
                            if member_state == LifecycleState.FAILED.value
                            and isinstance(result, Mapping)
                            else {}
                        ),
                    }
                )
                if raw:
                    measured = project_progress(
                        read_stored_model(OperationProgress, raw), self._clock()
                    )
                    values = {
                        key: value
                        for key, value in measured.model_dump(mode="python").items()
                        if key in OperationMemberProgress.model_fields
                    }
                    if member_state not in {
                        LifecycleState.RUNNING.value,
                        ProfileEffectState.PENDING.value,
                    }:
                        values.update(
                            bytes_per_second=None,
                            smoothed_bytes_per_second=None,
                            eta_seconds=None,
                            activity=None,
                        )
                    values.update(
                        member_id=operation.node_id,
                        state=member_state,
                        completed_bytes=members[-1]["completed_bytes"],
                        total_bytes=members[-1]["total_bytes"],
                    )
                    measured_members.append(
                        read_stored_model(OperationMemberProgress, values)
                    )
                else:
                    measured_members.append(
                        OperationMemberProgress(
                            member_id=operation.node_id,
                            phase=ProgressPhase.PENDING
                            if member_state == ProfileEffectState.PENDING.value
                            else ProgressPhase.TRANSFER,
                            state=member_state,
                            completed_bytes=members[-1]["completed_bytes"],
                            total_bytes=members[-1]["total_bytes"],
                        )
                    )
                if isinstance(result, Mapping) and result:
                    evidence.append(_evidence_projection(operation.node_id, result))
            by_node = {str(item["node_id"]): item for item in members}
            target_order = child.payload.get("target_order", list(by_node))
            if isinstance(target_order, list):
                members = [
                    by_node[node_id]
                    for node_id in target_order
                    if isinstance(node_id, str) and node_id in by_node
                ]
            state = child.state
            if state == LifecycleState.SUCCEEDED.value and any(
                item.get("state") == LifecycleState.FAILED.value for item in members
            ):
                state = LifecycleState.FAILED.value
            projection_reason = next(
                (
                    item.get("error")
                    for item in members
                    if isinstance(item.get("error"), str) and item.get("error")
                ),
                None,
            )
            completed = sum(
                self._int(item.get("completed_bytes")) or 0 for item in members
            )
            totals = [self._int(item.get("total_bytes")) for item in members]
            total = (
                sum(value for value in totals if value is not None)
                if all(value is not None for value in totals)
                else None
            )
            payload = {
                "progress": {
                    "phase": ProgressPhase.TRANSFER,
                    "completed_bytes": completed,
                    "total_bytes": total,
                    "total_bytes_known": total is not None,
                    "members": members,
                },
                "members": members,
                "evidence": evidence,
            }
            for node_id in cached_nodes:
                item = by_node[node_id]
                measured_members.append(
                    OperationMemberProgress(
                        member_id=node_id,
                        phase=ProgressPhase.TRANSFER,
                        state=LifecycleState.SUCCEEDED.value,
                        completed_bytes=item["completed_bytes"],
                        total_bytes=item["total_bytes"],
                    )
                )
            payload["progress"]["operation"] = aggregate_progress(
                measured_members
            ).model_dump(mode="json", exclude_none=True)
            if child.status_reason or projection_reason:
                payload["reason"] = child.status_reason or projection_reason
            failed = [
                item
                for item in members
                if item.get("state") == LifecycleState.FAILED.value
            ]
            if failed:
                kinds = {item.get("failure_kind") for item in failed}
                # One kind for all failed members is that kind; anything mixed
                # or unknown stays for the parent's fail-closed classifier.
                if len(kinds) == 1 and isinstance(next(iter(kinds)), str):
                    payload["failure_kind"] = next(iter(kinds))
                code = next(
                    (
                        item["error_code"]
                        for item in failed
                        if isinstance(item.get("error_code"), str)
                    ),
                    None,
                )
                if code is not None:
                    payload["error_code"] = code
            if state != child.state:
                JobAdapter.amend_ended(child, payload.get("reason"), self._clock())
            payload = _child_receipt(payload)
            child.result = serialize_json_value(payload)
            child.updated_at = self._clock()
            session.commit()
            return _ChildView(state=state, result=payload)
