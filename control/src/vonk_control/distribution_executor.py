"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    DistributionObject,
    OperationMemberProgress,
    OperationProgress,
    ProgressPhase,
    canonical_message,
)

from . import model_cache_states
from .agent_jobs import AgentJobService, abandon_idempotent_job_in_session
from .bounded_json import sequence
from .content_identity import ImageContent, same_image
from .distribution import DistributionService
from .distribution_assignment import NodeDistributionAssignment
from .lifecycle.job import JobAdapter
from .logging import redact_text
from .model_cache import ModelCacheNotFound
from .model_cache_contract import ModelCacheDownloadResult
from .models import (
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
)
from .oci_image_store import StoreUnknown
from .operation_progress import aggregate_progress, project_progress
from .run_switch_contract import (
    ArtifactVerificationEvidence,
    ArtifactVerificationResult,
    RunSwitchDistributionChildResult,
    RunSwitchPhase,
    RunSwitchPhaseResult,
    RunSwitchPlan,
)
from .run_switch_operations import (
    PhaseExecution,
    _persist_run_switch_runtime_image_reference,
)
from .runtime_image_preparation import (
    RuntimeImagePreparationError,
    RuntimeImageReceipt,
    RuntimeImageStorage,
)
from .strict_json import read_stored_model
from .worker_memory_contract import WorkerMemoryComponent

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeImagePull:
    """The runtime image a node pulls from the layered store."""

    image_digest: str
    config_digest: str
    address: str


@dataclass(frozen=True, slots=True)
class _ChildView:
    """Small child projection consumed by RunSwitchOperationService."""

    state: str
    result: Mapping[str, object]

    @property
    def progress(self) -> Mapping[str, object]:
        value = self.result.get("progress")
        return value if isinstance(value, Mapping) else {}


_PHASE_RECEIPT_ADAPTER = TypeAdapter(RunSwitchPhaseResult)


def _phase_receipt(
    value: Mapping[str, object],
    *,
    phase: RunSwitchPhase | None = None,
) -> dict[str, object]:
    """Validate the closed phase receipt before returning or persisting it."""

    try:
        normalized = dict(value)
        if phase is not None:
            if "phase" in normalized and normalized["phase"] != phase.kind:
                raise ValueError("phase receipt belongs to a different phase")
            normalized.setdefault("phase", phase.kind)
            subphase = getattr(phase, "subphase", None)
            if subphase is None and phase.kind in {"transfer", "verify"}:
                subphase = "target-copy"
            if "subphase" in normalized and normalized["subphase"] != subphase:
                raise ValueError("phase receipt belongs to a different subphase")
            normalized.setdefault("subphase", subphase)
        assignments = normalized.get("assignments")
        if isinstance(assignments, Mapping):
            normalized["assignments"] = {
                node_id: NodeDistributionAssignment.parse(raw)
                if isinstance(raw, Mapping)
                else raw
                for node_id, raw in assignments.items()
            }
        receipt = _PHASE_RECEIPT_ADAPTER.validate_python(normalized, strict=True)
    except (TypeError, ValueError) as error:
        raise RuntimeError("run-switch phase receipt is invalid") from error
    return receipt.model_dump(mode="json", exclude_unset=True)


def _child_receipt(
    value: Mapping[str, object],
    *,
    phase: RunSwitchPhase | None = None,
) -> dict[str, object]:
    """Validate the persisted projection for a target-copy child Job."""

    normalized = dict(value)
    if phase is not None:
        normalized.setdefault("phase", phase.kind)
        normalized.setdefault("subphase", "target-copy")
    else:
        normalized.setdefault("phase", "transfer")
        normalized.setdefault("subphase", "target-copy")
    try:
        receipt = read_stored_model(
            RunSwitchDistributionChildResult, normalized, strict=True
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("distribution child receipt is invalid") from error
    return receipt.model_dump(mode="json", exclude_unset=True)


_FAILURE_TEXT = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")


def _typed_failure(value: Mapping[str, object]) -> dict[str, str]:
    """The agent's typed failure fields, bounded to the member contract."""

    typed: dict[str, str] = {}
    for key in ("failure_kind", "error_code"):
        item = value.get(key)
        if isinstance(item, str) and _FAILURE_TEXT.fullmatch(item):
            typed[key] = item
    diagnostic = value.get("diagnostic")
    if isinstance(diagnostic, str) and diagnostic:
        typed["diagnostic"] = diagnostic[:512]
    return typed


def _evidence_projection(
    node_id: str, value: Mapping[str, object]
) -> ArtifactVerificationEvidence:
    """Keep the high-level evidence fields from an agent handoff receipt."""

    return read_stored_model(
        ArtifactVerificationEvidence,
        {
            "node_id": node_id,
            **{
                key: value[key]
                for key in (
                    "downloaded_bytes",
                    "copied_bytes",
                    "error",
                    "reason",
                    "uncertain",
                )
                if key in value
            },
            **_typed_failure(value),
        },
    )


class DurableDistributionPhaseExecutor:
    """Create one durable child Job and node operations for a transfer phase."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        operations: AgentJobService,
        distribution: DistributionService,
        *,
        clock: Any,
    ) -> None:
        self._sessions = sessions
        self._operations = operations
        self._distribution = distribution
        self._clock = clock

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: Mapping[str, object],
    ) -> PhaseExecution:
        if phase.kind not in {"transfer", "verify"}:
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
        if phase.kind == "verify":
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
        intent_ordinal = progress.get("workload_intent_ordinal")
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
                        "verified": phase.kind == "verify",
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

    def get(self, operation_id: str) -> Any:
        with self._sessions() as session:
            child = session.get(Job, operation_id)
            if child is None or child.kind != "artifact-distribution":
                raise KeyError(operation_id)
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
                        "state": "succeeded",
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
                if member_state in {"pending", "unknown"} and attempt is not None:
                    # A terminal result is the durable handoff even when a
                    # restart observed the parent operation row before its
                    # aggregate state was reconciled.
                    member_state = self._member_state(attempt.state)
                expected_total = target_total(operation.node_id)
                terminal_downloaded = self._int(result.get("downloaded_bytes"))
                evidence_error = None
                if member_state == "succeeded" and (
                    expected_total is None or terminal_downloaded != expected_total
                ):
                    member_state = "failed"
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
                                if member_state != "succeeded"
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
                            if member_state == "failed" and isinstance(result, Mapping)
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
                    if member_state not in {"running", "pending"}:
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
                            if member_state == "pending"
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
            if state == "succeeded" and any(
                item.get("state") == "failed" for item in members
            ):
                state = "failed"
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
                        state="succeeded",
                        completed_bytes=item["completed_bytes"],
                        total_bytes=item["total_bytes"],
                    )
                )
            payload["progress"]["operation"] = aggregate_progress(
                measured_members
            ).model_dump(mode="json", exclude_none=True)
            if child.status_reason or projection_reason:
                payload["reason"] = child.status_reason or projection_reason
            failed = [item for item in members if item.get("state") == "failed"]
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
            child.result = payload
            child.updated_at = self._clock()
            session.commit()
            return _ChildView(state=state, result=payload)

    @staticmethod
    def _verification_result(
        plan: RunSwitchPlan,
        progress: Mapping[str, object],
        *,
        skipped: bool = False,
        cached_nodes: Sequence[str] = (),
        cached_target_totals: Mapping[str, int] | None = None,
        verified_image_digest: str | None = None,
        verified_oci_layout_sha256: str | None = None,
        evidence: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, object]:
        resolved_image_digest, resolved_layout_digest, _image_bytes, build_id = (
            DurableDistributionPhaseExecutor._runtime_identity(plan, progress)
        )
        result = ArtifactVerificationResult(
            skipped=skipped,
            verified=True,
            verified_digests=list(plan.storage.artifact_digests),
            verified_build_id=build_id,
            verified_image_digest=(
                verified_image_digest
                if verified_image_digest is not None
                else resolved_image_digest
            ),
            verified_oci_layout_sha256=(
                verified_oci_layout_sha256
                if verified_oci_layout_sha256 is not None
                else resolved_layout_digest
            ),
            cached_nodes=list(cached_nodes),
            cached_target_totals=dict(cached_target_totals or {}),
            evidence=[
                _evidence_projection(str(item["node_id"]), item)
                for item in evidence
                if isinstance(item, Mapping) and isinstance(item.get("node_id"), str)
            ],
        )
        return result.model_dump(mode="json")

    def _adopt_child(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        actor: str,
        request_key: str,
        workload_intent_ordinal: int,
    ) -> PhaseExecution | None:
        """Adopt persisted work before consulting mutable cache availability."""
        child_request = str(
            uuid.uuid5(
                uuid.UUID(request_key),
                f"artifact-distribution:{phase.kind}:{phase.index}",
            )
        )
        with self._sessions() as session:
            child = session.scalar(select(Job).where(Job.request_id == child_request))
            if child is None:
                return None
            if (
                child.kind != "artifact-distribution"
                or child.actor != actor
                or child.authority_revision != plan.plan_digest
                or child.payload.get("plan_digest") != plan.plan_digest
                or child.payload.get("phase") != phase.kind
                or child.payload.get("target_order") != list(phase.node_ids)
                or child.payload.get("workload_intent_ordinal")
                != workload_intent_ordinal
            ):
                raise RuntimeError("distribution child request key was reused")
            receipt = _phase_receipt(
                {
                    "cached_nodes": child.payload.get("cached_nodes"),
                    "assignments": child.payload.get("assignments"),
                },
                phase=phase,
            )
            assignments = child.payload.get("assignments")
            cached = child.payload.get("cached_nodes")
            if (
                not isinstance(assignments, dict)
                or not isinstance(cached, list)
                or child.targets != list(assignments)
                or set(assignments).intersection(cached)
                or set(assignments).union(cached) != set(phase.node_ids)
                or any(
                    NodeDistributionAssignment.parse(raw).node_id != node_id
                    for node_id, raw in assignments.items()
                )
            ):
                raise RuntimeError("distribution child target scope changed")
            return PhaseExecution(operation_id=child.id, result=receipt)

    def _ensure_child(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        actor: str,
        request_key: str,
        cached: tuple[str, ...],
        assignments: Mapping[str, NodeDistributionAssignment],
        target_order: tuple[str, ...],
        workload_intent_ordinal: int,
        target_bytes: int | None = None,
    ) -> str:
        child_request = str(
            uuid.uuid5(
                uuid.UUID(request_key),
                f"artifact-distribution:{phase.kind}:{phase.index}",
            )
        )
        now = self._clock()
        with self._sessions() as session:
            existing = session.scalar(
                select(Job).where(Job.request_id == child_request)
            )
            if existing is not None:
                if (
                    existing.kind != "artifact-distribution"
                    or existing.payload.get("plan_digest") != plan.plan_digest
                    or existing.payload.get("workload_intent_ordinal")
                    != workload_intent_ordinal
                ):
                    raise RuntimeError("distribution child request key was reused")
                return existing.id
        # Register immutable assignments before opening the child transaction;
        # this avoids nested session transactions while retaining replay safety.
        for assignment in assignments.values():
            self._distribution.register(assignment)
        with self._sessions.begin() as session:
            target_totals = {
                node_id: target_bytes
                if target_bytes is not None
                else self._target_bytes(plan, node_id)
                for node_id in (*cached, *assignments)
            }
            total_bytes = sum(value for value in target_totals.values())
            cached_bytes = sum(target_totals[node_id] for node_id in cached)
            progress = {
                "phase": phase.kind,
                "completed_bytes": cached_bytes,
                "total_bytes": total_bytes,
                "total_bytes_known": True,
                "members": [
                    {
                        "node_id": node_id,
                        "state": "succeeded" if node_id in cached else "pending",
                        "completed_bytes": target_totals[node_id]
                        if node_id in cached
                        else 0,
                        "total_bytes": target_totals[node_id],
                        "error": None,
                        "cached": node_id in cached,
                    }
                    for node_id in (*cached, *assignments)
                ],
            }
            child = JobAdapter.new_job(
                # The request key provides replay identity. Job/operation IDs
                # are persisted once and follow the shared helper UUIDv4
                # contract when this transfer requests a runtime image pull.
                id=str(uuid.uuid4()),
                request_id=child_request,
                kind="artifact-distribution",
                actor=actor,
                authority_revision=plan.plan_digest,
                targets=list(assignments),
                payload_digest=self._digest(
                    {
                        "plan_digest": plan.plan_digest,
                        "phase": phase.kind,
                        "workload_intent_ordinal": workload_intent_ordinal,
                    }
                ),
                payload={
                    "plan_digest": plan.plan_digest,
                    "workload_intent_ordinal": workload_intent_ordinal,
                    "phase": phase.kind,
                    "progress": progress,
                    "cached_nodes": list(cached),
                    "target_order": list(target_order),
                    "target_totals": target_totals,
                    "assignments": {
                        node_id: assignment.to_mapping()
                        for node_id, assignment in assignments.items()
                    },
                },
                result=_child_receipt(
                    {
                        "phase": "transfer",
                        "subphase": "target-copy",
                        "progress": progress,
                        "members": progress["members"],
                        "evidence": [],
                    }
                ),
                created_at=now,
                updated_at=now,
            )
            session.add(child)
            session.flush()
            for node_id, assignment in assignments.items():
                self._operations.enqueue_in_session(
                    session,
                    child.id,
                    node_id,
                    "artifact.distribution.v1",
                    plan.plan_digest,
                    {"plan_digest": plan.plan_digest},
                    operation_id=str(uuid.uuid4()),
                )
            return child.id

    def _model_objects(
        self,
        plan: RunSwitchPlan,
        progress: Mapping[str, object],
    ) -> tuple[tuple[DistributionObject, ...], str, int]:
        preparation = plan.preparation
        model_set_digest = (
            preparation.model.artifact_set_sha256 if preparation is not None else None
        )
        model_set_bytes = (
            preparation.model.artifact_set_bytes if preparation is not None else None
        )
        if model_set_digest is None:
            phase_results = progress.get("phase_results")
            if isinstance(phase_results, list):
                for raw in reversed(phase_results):
                    if not isinstance(raw, Mapping):
                        continue
                    value = raw.get("model_artifact_set_sha256")
                    if isinstance(value, str):
                        model_set_digest = value
                        candidate_bytes = raw.get("model_artifact_set_bytes")
                        if type(candidate_bytes) is int and candidate_bytes > 0:
                            model_set_bytes = candidate_bytes
                        break
        if not isinstance(model_set_digest, str):
            raise TypeError("exact model preparation identity is unavailable")
        source = getattr(
            self._distribution.source, "model_source", self._distribution.source
        )
        getter = getattr(source, "objects_for_set", None)
        if not isinstance(getter, Callable):
            raise TypeError("verified model cache manifest provider is unavailable")
        objects = tuple(getter(model_set_digest))
        if not objects or any(item.kind != "model" for item in objects):
            raise RuntimeError("verified model cache manifest is incomplete")
        expected_digests = set(plan.storage.artifact_digests)
        if expected_digests and {item.sha256 for item in objects} != expected_digests:
            raise RuntimeError("verified model cache manifest does not match the plan")
        actual_bytes = sum(item.bytes for item in objects)
        if model_set_bytes is not None and actual_bytes != model_set_bytes:
            raise RuntimeError(
                "verified model cache byte total does not match the plan"
            )
        return objects, model_set_digest, actual_bytes

    @staticmethod
    def _runtime_identity(
        plan: RunSwitchPlan, progress: Mapping[str, object]
    ) -> tuple[str, str, int, str | None]:
        image_digest = plan.image_digest
        layout_digest = plan.build.oci_layout_sha256
        image_bytes = plan.build.image_bytes
        build_id = plan.recipe_build_id
        preparation = plan.preparation
        if preparation is not None:
            runtime = preparation.runtime_image
            image_digest = image_digest or getattr(runtime, "image_digest", None)
            layout_digest = layout_digest or getattr(runtime, "oci_layout_sha256", None)
            image_bytes = image_bytes or getattr(runtime, "image_bytes", None)
            build_id = build_id or getattr(runtime, "build_id", None)
        phase_results = progress.get("phase_results", [])
        if isinstance(phase_results, list):
            for raw in reversed(phase_results):
                if not isinstance(raw, Mapping):
                    continue
                candidate = (
                    raw.get("result") if isinstance(raw.get("result"), Mapping) else raw
                )
                if not isinstance(candidate, Mapping):
                    continue
                runtime_receipt = candidate.get("runtime_image")
                if isinstance(runtime_receipt, Mapping):
                    candidate = {**candidate, **runtime_receipt}
                image_digest = image_digest or candidate.get("image_digest")
                layout_digest = layout_digest or candidate.get(
                    "oci_layout_sha256", candidate.get("oci_archive_sha256")
                )
                image_bytes = image_bytes or candidate.get("image_bytes")
                build_id = build_id or candidate.get("build_id")
                if image_digest and layout_digest and image_bytes is not None:
                    break
        if (
            (build_id is not None and not isinstance(build_id, str))
            or not isinstance(image_digest, str)
            or not isinstance(layout_digest, str)
            or type(image_bytes) is not int
        ):
            raise RuntimeError("verified OCI runtime image identity is unavailable")
        return image_digest, layout_digest, image_bytes, build_id

    @staticmethod
    def _source_runtime_storage(source: object) -> RuntimeImageStorage | None:
        """Return the runtime-image storage behind a distribution source."""

        for candidate in (source, getattr(source, "oci_source", None)):
            storage = getattr(candidate, "_runtime_storage", None)
            if storage is not None:
                return storage
        return None

    def _archive(
        self,
        *,
        build_id: str | None,
        image_digest: str,
        layout_digest: str,
        image_bytes: int,
    ) -> RuntimeImagePull:
        """The stored archive of a succeeded build, identified by its content.

        The bytes were verified at ingress and managed storage holds the image.
        The plan names the build, and the build's recorded result must equal
        the archive being copied; which recipe revision asks for it does not
        matter.
        """

        if not image_digest or not layout_digest or image_bytes < 1:
            raise RuntimeError("verified OCI runtime image identity is unavailable")
        if build_id is None:
            raise RuntimeError("verified OCI runtime image build is unavailable")
        with self._sessions() as session:
            build = session.get(RecipeBuild, build_id)
            if (
                build is None
                or build.state != "succeeded"
                or build.image_bytes is None
                or not same_image(
                    build,
                    ImageContent(
                        image_digest=image_digest,
                        archive_sha256=layout_digest,
                        image_bytes=image_bytes,
                    ),
                )
            ):
                raise RuntimeError("OCI build authority changed")
        return RuntimeImagePull(
            image_digest=image_digest,
            config_digest=self._stored_config_digest(layout_digest),
            address=layout_digest,
        )

    def _stored_config_digest(self, address: str) -> str:
        storage = self._source_runtime_storage(self._distribution.source)
        layout = getattr(storage, "layout", None)
        image = layout.read(f"sha256:{address}") if layout is not None else None
        if image is None or isinstance(image, StoreUnknown):
            raise RuntimeError("verified OCI runtime image identity is unavailable")
        return image.config_digest

    def _assignment(
        self,
        plan: RunSwitchPlan,
        node_id: str,
        model_objects: tuple[DistributionObject, ...],
        image: RuntimeImagePull,
        *,
        model_set_digest: str,
    ) -> NodeDistributionAssignment:
        generation = getattr(getattr(plan, "mapping", None), "mapping_generation", None)
        if type(generation) is not int or generation < 1:
            generation = 1
        # UUID v4 is part of the wire contract, while the digest-derived bytes
        # make replay after a Controller restart yield the same assignment.
        seed = f"{plan.plan_digest}:{generation}:{node_id}:{model_set_digest}:{image.address}"
        assignment_bytes = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
        assignment_bytes[6] = (assignment_bytes[6] & 0x0F) | 0x40
        assignment_bytes[8] = (assignment_bytes[8] & 0x3F) | 0x80
        return NodeDistributionAssignment.parse(
            {
                "assignment_id": str(uuid.UUID(bytes=bytes(assignment_bytes))),
                "plan_digest": plan.plan_digest,
                "generation": generation,
                "node_id": node_id,
                # The grant lives from this registration; authenticated agent
                # progress renews it while the copy runs (the
                # artifact-distribution renewal in AgentJobService.heartbeat).
                # A plan may be accepted hours before its copy starts, so its
                # age must not expire the grant.
                "expires_at": (
                    self._clock().astimezone(UTC) + timedelta(hours=1)
                ).isoformat(),
                "model_artifact_set_sha256": model_set_digest,
                "objects": [item.to_mapping() for item in model_objects],
                "oci_image_digest": image.image_digest,
                "oci_image_config_digest": image.config_digest,
                "oci_archive_sha256": image.address,
            }
        )

    @staticmethod
    def _cached_targets(
        plan: RunSwitchPlan, targets: tuple[str, ...]
    ) -> tuple[str, ...]:
        preparation = plan.preparation
        if preparation is None:
            return ()
        model = {item.node_id: item for item in preparation.model.targets}
        image = {item.node_id: item for item in preparation.runtime_image.targets}
        return tuple(
            node_id
            for node_id in targets
            if (
                model.get(node_id) is not None
                and model[node_id].state == "ready"
                and getattr(model[node_id], "verified_at", None) is not None
                and model[node_id].verified_sha256
                == preparation.model.artifact_set_sha256
                and image.get(node_id) is not None
                and image[node_id].state == "ready"
                and getattr(image[node_id], "verified_at", None) is not None
                and same_image(
                    ImageContent(
                        image_digest=image[node_id].imported_image_digest,
                        archive_sha256=image[node_id].verified_sha256,
                    ),
                    preparation.runtime_image,
                )
            )
        )

    @staticmethod
    def _target_bytes(plan: RunSwitchPlan, node_id: str) -> int:
        preparation = plan.preparation
        if preparation is None:
            return 0
        model_bytes = getattr(preparation.model, "artifact_set_bytes", 0)
        image_bytes = getattr(preparation.runtime_image, "image_bytes", 0)
        return model_bytes + image_bytes

    def _verify_evidence(
        self,
        plan: RunSwitchPlan,
        progress: Mapping[str, object],
        targets: tuple[str, ...],
        cached: tuple[str, ...],
    ) -> Mapping[str, object]:
        """Require a terminal agent result from every non-cached target."""
        preparation = plan.preparation
        expected_image = plan.image_digest or (
            preparation.runtime_image.image_digest if preparation is not None else None
        )
        expected_layout = plan.build.oci_layout_sha256 or (
            preparation.runtime_image.oci_layout_sha256
            if preparation is not None
            else None
        )
        phase_results = progress.get("phase_results", [])
        if not isinstance(phase_results, list):
            phase_results = []
        for raw in reversed(phase_results):
            if not isinstance(raw, Mapping):
                continue
            runtime_receipt = raw.get("runtime_image")
            if not isinstance(runtime_receipt, Mapping):
                continue
            candidate_image = runtime_receipt.get("image_digest")
            candidate_layout = runtime_receipt.get(
                "oci_layout_sha256", runtime_receipt.get("oci_archive_sha256")
            )
            if isinstance(candidate_image, str) and isinstance(candidate_layout, str):
                expected_image = candidate_image
                expected_layout = candidate_layout
                break
        # A preview may have planned the build and left plan image fields
        # empty. Only a strict assignment emitted by this executor can supply
        # the effective post-build identity for verification.
        for raw in reversed(phase_results):
            if not isinstance(raw, Mapping):
                continue
            assignments = raw.get("assignments")
            if not isinstance(assignments, Mapping):
                continue
            for node_id, assignment_raw in assignments.items():
                if not isinstance(node_id, str) or not isinstance(
                    assignment_raw, Mapping
                ):
                    continue
                try:
                    assignment = NodeDistributionAssignment.parse(assignment_raw)
                except (TypeError, ValueError):
                    continue
                if (
                    assignment.plan_digest == plan.plan_digest
                    and assignment.node_id in targets
                    and assignment.model_artifact_set_sha256
                    == (
                        preparation.model.artifact_set_sha256
                        if preparation is not None
                        else None
                    )
                ):
                    expected_image = assignment.oci_image_digest
                    expected_layout = assignment.oci_archive_sha256
                    break
            if expected_image is not None and expected_layout is not None:
                break
        candidates: list[object] = list(phase_results)
        prior_evidence = progress.get("evidence", [])
        if isinstance(prior_evidence, list):
            candidates.extend(prior_evidence)
        receipts: dict[str, Mapping[str, object]] = {}
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            node_id = candidate.get("node_id")
            evidence = candidate.get("evidence", candidate)
            if isinstance(evidence, Mapping):
                if isinstance(node_id, str) and node_id in targets:
                    receipts[node_id] = evidence
            elif isinstance(evidence, Sequence) and not isinstance(
                evidence, (str, bytes, bytearray)
            ):
                for item in evidence:
                    if not isinstance(item, Mapping):
                        continue
                    item_node_id = item.get("node_id")
                    if isinstance(item_node_id, str) and item_node_id in targets:
                        receipts[item_node_id] = item
        cached_nodes = set(cached)
        cached_nodes.update(
            {
                value
                for value in sequence(progress.get("cached_nodes")) or ()
                if isinstance(value, str)
            }
        )
        for candidate in phase_results:
            if isinstance(candidate, Mapping):
                raw_cached = candidate.get("cached_nodes")
                if isinstance(raw_cached, list):
                    cached_nodes.update(
                        value for value in raw_cached if isinstance(value, str)
                    )
        missing = set(targets) - cached_nodes
        if missing - receipts.keys():
            raise RuntimeError(
                "verification requires terminal evidence from every target"
            )
        return self._verification_result(
            plan,
            progress,
            cached_nodes=sorted(cached_nodes),
            verified_image_digest=expected_image,
            verified_oci_layout_sha256=expected_layout,
            evidence=[dict(receipts[node_id]) for node_id in sorted(receipts)],
        )

    @staticmethod
    def _member_state(value: str) -> str:
        return {
            "queued": "pending",
            "running": "running",
            "succeeded": "succeeded",
            "failed": "failed",
        }.get(value, "unknown")

    @staticmethod
    def _int(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    @staticmethod
    def _digest(value: Mapping[str, object]) -> str:
        return hashlib.sha256(canonical_message(value)).hexdigest()


class CompositeDistributionPhaseExecutor(DurableDistributionPhaseExecutor):
    """Run the Controller cache child before Spark target distribution."""

    def __init__(
        self,
        *args: Any,
        model_cache: object,
        runtime_image_preparer: Callable[..., object] | None = None,
        async_runtime_image_preparation: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._model_cache = model_cache
        self._runtime_image_preparer = runtime_image_preparer
        self._async_runtime_image_preparation = async_runtime_image_preparation
        self._runtime_image_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="runtime-image-preparation"
        )
        self._runtime_image_futures: dict[
            tuple[str, int, int], tuple[Future[Mapping[str, object] | None], str]
        ] = {}

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        return {
            WorkerMemoryComponent.RUNTIME_IMAGE_FUTURES: len(
                self._runtime_image_futures
            )
        }

    def close(self) -> None:
        """Leave image preparation checkpoints resumable during shutdown."""

        self._runtime_image_pool.shutdown(wait=False, cancel_futures=True)

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: Mapping[str, object],
    ) -> PhaseExecution:
        if phase.kind == "prepare" and phase.subphase == "runtime-image":
            # This is the only mutating image boundary.  It runs as its own
            # durable high-level phase so install admission cannot compile a
            # schema-2 payload until the Controller archive and receipt are
            # present.  Target-copy only consumes the persisted evidence.
            if self._async_runtime_image_preparation:
                key = (request_key, phase.index, item_index)
                submitted = self._runtime_image_futures.get(key)
                if submitted is None:
                    self._runtime_image_futures[key] = (
                        self._runtime_image_pool.submit(
                            self._prepare_runtime_image,
                            plan,
                            phase,
                            item_index=item_index,
                            actor=actor,
                            request_key=request_key,
                            progress=progress,
                            wait_for_busy_owner=True,
                        ),
                        self._clock().isoformat(),
                    )
                    return PhaseExecution(
                        waiting=True,
                        status_reason=(
                            "Runtime image preparation is running in the background; "
                            "the durable checkpoint will be resumed after a restart."
                        ),
                    )
                future, started_at = submitted
                if not future.done():
                    # Bounded by the transport's subprocess timeout; the start
                    # time makes a slow or stuck preparation visible.
                    return PhaseExecution(
                        waiting=True,
                        status_reason=(
                            "Runtime image preparation is still running in the "
                            f"background (started {started_at})."
                        ),
                    )
                del self._runtime_image_futures[key]
                error = future.exception()
                if error is not None:
                    # The tick decides retry or failure; record every cause here
                    # so no background failure is ever silent.
                    _LOGGER.warning(
                        "runtime image preparation for run/switch phase %s failed: %s: %s",
                        request_key,
                        getattr(error, "code", type(error).__name__),
                        redact_text(getattr(error, "detail", error)),
                    )
                runtime_result = future.result()
            else:
                runtime_result = self._prepare_runtime_image(
                    plan,
                    phase,
                    item_index=item_index,
                    actor=actor,
                    request_key=request_key,
                    progress=progress,
                )
            if runtime_result is None:
                raise RuntimeError("runtime image preparation returned no evidence")
            return PhaseExecution(result=_phase_receipt(runtime_result, phase=phase))
        if phase.subphase != "model-download":
            return super().execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
        if phase.kind != "transfer" or item_index != 0:
            raise RuntimeError("invalid model-download phase")
        preparation = plan.preparation
        model = preparation.model if preparation is not None else None
        artifact_set_sha256 = (
            model.artifact_set_sha256
            if model is not None
            else plan.storage.artifact_set_sha256
        )
        model_content_sha256 = (
            model.model_content_sha256
            if model is not None
            else plan.model_content_sha256
        )
        recipe_revision_sha256 = (
            model.recipe_revision_sha256
            if model is not None
            else plan.recipe_content_sha256
        )
        artifact_count = (
            model.artifact_count
            if model is not None
            else len(plan.storage.artifact_digests)
        )
        artifact_set_bytes = (
            model.artifact_set_bytes
            if model is not None
            else plan.storage.artifact_set_bytes
        )
        if not artifact_set_sha256 or not artifact_count or not artifact_set_bytes:
            raise RuntimeError("exact model preparation is unavailable")
        preview_method = getattr(self._model_cache, "download_preview", None)
        start_method = getattr(self._model_cache, "start_download", None)
        if not isinstance(preview_method, Callable) or not isinstance(
            start_method, Callable
        ):
            raise TypeError("model-cache download provider is unavailable")
        # An exact persisted set is sufficient to resolve the opaque manifest.
        # When a recipe revision ID is available, include the model pin as an
        # additional cross-check; never send both recipe ID and recipe digest.
        pins: dict[str, object] = {
            "artifact_set_sha256": artifact_set_sha256,
        }
        if plan.recipe_revision_id is not None:
            pins.update(
                model_content_sha256=model_content_sha256,
                recipe_revision_id=plan.recipe_revision_id,
            )
        preview = preview_method(**pins)
        if (
            not isinstance(preview, Mapping)
            or preview.get("artifact_set_sha256") != artifact_set_sha256
            or not isinstance(preview.get("plan_digest"), str)
            or len(preview["plan_digest"]) != 64
            or preview.get("artifact_count") != artifact_count
            or preview.get("expected_bytes") != artifact_set_bytes
        ):
            raise RuntimeError("model-cache download preview is not exact")
        # A first download has no cache-set row yet: preview resolves the
        # immutable catalog manifest without mutating storage. start_download
        # persists that same manifest with the durable child operation.
        manifest = preview.get("_manifest")
        if manifest is None:
            manifest_getter = getattr(
                self._model_cache, "manifest_for_artifact_set", None
            )
            manifest = (
                manifest_getter(artifact_set_sha256)
                if callable(manifest_getter)
                else None
            )
        if (
            getattr(manifest, "digest", None) != artifact_set_sha256
            or getattr(manifest, "recipe_revision_sha256", None)
            != recipe_revision_sha256
        ):
            raise RuntimeError("model-cache manifest recipe identity is not exact")
        blockers = preview.get("blockers", [])
        if (
            isinstance(blockers, Sequence)
            and not isinstance(blockers, (str, bytes, bytearray))
            and blockers
        ):
            raise RuntimeError(
                "model-cache download is blocked: " + "; ".join(map(str, blockers))
            )
        expected_bytes = preview.get("expected_bytes")
        if type(expected_bytes) is not int or expected_bytes < 1:
            raise RuntimeError("model-cache download total is unavailable")
        if preview.get("new_bytes") == 0:
            # Every object is already verified (perhaps cached by another
            # revision's set): record this set as cached for every consumer,
            # without a download or a re-hash.
            adopt = getattr(self._model_cache, "adopt_verified_set", None)
            if callable(adopt):
                adopt(manifest)
            return PhaseExecution(
                result=_phase_receipt(
                    {
                        "schema_version": 2,
                        "skipped": True,
                        "coverage": "complete",
                        "artifact_set_sha256": artifact_set_sha256,
                        # The set is already covered, so this phase transferred no
                        # bytes.  ``expected_bytes`` is the immutable set size while
                        # ``new_bytes`` is the operation's transfer envelope.
                        "downloaded_bytes": 0,
                        "total_bytes": 0,
                        "progress": {
                            "phase": ProgressPhase.MODEL_DOWNLOAD,
                            "completed_bytes": 0,
                            "total_bytes": 0,
                            "total_bytes_known": True,
                        },
                    },
                    phase=phase,
                )
            )
        cache_request_key = str(
            uuid.uuid5(
                uuid.UUID(request_key),
                f"model-download:{phase.index}:{artifact_set_sha256}",
            )
        )
        view = start_method(
            actor=actor,
            request_key=cache_request_key,
            plan_digest=preview["plan_digest"],
            **pins,
        )
        return PhaseExecution(
            operation_id=view.id,
            result=_phase_receipt(self._cache_result(view), phase=phase),
        )

    def _prepare_runtime_image(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: Mapping[str, object],
        wait_for_busy_owner: bool = False,
    ) -> Mapping[str, object] | None:
        """Prepare one Controller image and authorize every target execution.

        This callback is deliberately supplied only to the durable worker
        executor.  API preview, admission, and agent spec reads use the
        read-only receipt resolver in ``ControllerExecutionPlanService``.
        """

        if self._runtime_image_preparer is None:
            return None
        if plan.recipe_revision_id is None or not plan.spark_group.nodes:
            raise RuntimeError("runtime image preparation identity is unavailable")
        from .execution_plan_service import (
            ExecutionPlanCompilationError,
            _bind_runtime_artifacts,
        )
        from .recipe_runtime_specs import compile_runtime_spec, resolve_recipe_entities

        with self._sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == plan.recipe_revision_id,
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
            if revision is None:
                raise RuntimeError(
                    "runtime image preparation recipe revision is unavailable"
                )
            build = (
                session.get(RecipeBuild, plan.recipe_build_id)
                if plan.recipe_build_id is not None
                else None
            )
            if build is None or build.state != "succeeded":
                raise RuntimeError(
                    "runtime image preparation build receipt is unavailable"
                )
            package_handle = {
                "image_digest": build.image_digest,
                "image_reference": f"localhost/vonk/recipe-build@{build.image_digest}",
                "build_input_sha256": build.build_input_sha256,
                "platform": "linux/arm64",
            }
            entities = resolve_recipe_entities(session, revision.document)
            parameters = (
                dict(plan.mapping.parameters) if plan.mapping is not None else {}
            )
            resolved_models = sequence(entities["models"])
            if resolved_models is None:
                raise ExecutionPlanCompilationError(
                    "canonical model projection is invalid"
                )
            runtime_specs = {}
            for node in sorted(
                plan.spark_group.nodes, key=lambda item: (item.rank, item.node_id)
            ):
                runtime_spec = compile_runtime_spec(
                    revision.document,
                    resolved_entities=entities,
                    parameters=parameters,
                    role=node.role,
                    rank=node.rank,
                    package_handle=package_handle,
                )
                runtime_spec = _bind_runtime_artifacts(runtime_spec, resolved_models)
                identity = runtime_spec.get("identity")
                execution_key = (
                    identity.get("execution_sha256")
                    if isinstance(identity, Mapping)
                    else None
                )
                if not isinstance(execution_key, str):
                    raise TypeError(
                        "runtime image preparation execution identity is unavailable"
                    )
                runtime_specs[execution_key] = runtime_spec
        # Each role/rank has its own execution identity. Persist all of them
        # before install admission compiles the group. The preparer reuses the
        # immutable archive; close the read session before its receipt writes.
        execution_keys = tuple(sorted(runtime_specs))

        def before_publish(receipt: RuntimeImageReceipt) -> None:
            # Background preparation runs beside the tick that owns the
            # operation row, which may still hold it when a reused image is
            # ready at once. Off the tick thread, wait for that short
            # transaction instead of failing the whole preparation.
            attempts = 150 if wait_for_busy_owner else 1
            for attempt in range(attempts):
                try:
                    persist_reference(receipt)
                    return
                except RuntimeImagePreparationError as error:
                    if (
                        error.code != ArtifactLifecycleCode.REFERENCE_BUSY
                        or attempt + 1 >= attempts
                    ):
                        raise
                    time.sleep(0.2)

        def persist_reference(receipt: RuntimeImageReceipt) -> None:
            _persist_run_switch_runtime_image_reference(
                self._sessions,
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
                execution_keys=execution_keys,
                receipt=receipt,
                clock=self._clock,
            )

        raw = None
        for runtime_spec in runtime_specs.values():
            receipt = self._runtime_image_preparer(
                revision.document,
                runtime_spec,
                build,
                before_publish=before_publish,
            )
            to_mapping = getattr(receipt, "to_mapping", None)
            prepared = to_mapping() if callable(to_mapping) else receipt
            if not isinstance(prepared, Mapping):
                raise TypeError("runtime image preparation returned invalid evidence")
            if raw is not None and prepared != raw:
                raise RuntimeError(
                    "runtime image preparation returned different images for the group"
                )
            raw = prepared
        if raw is None:
            raise RuntimeError("runtime image preparation returned no evidence")
        effective_execution_key = next(iter(runtime_specs))
        image_digest = raw.get("image_digest")
        layout_digest = raw.get("oci_layout_sha256", raw.get("oci_archive_sha256"))
        image_bytes = raw.get("image_bytes")
        if (
            not isinstance(image_digest, str)
            or not isinstance(layout_digest, str)
            or type(image_bytes) is not int
            or image_bytes < 1
        ):
            raise RuntimeError("runtime image preparation returned incomplete evidence")
        return {
            "runtime_image": dict(raw),
            "effective_execution_key": effective_execution_key,
            "image_digest": image_digest,
            "oci_layout_sha256": layout_digest,
            "image_bytes": image_bytes,
            "build_id": raw.get("build_id"),
        }

    def get(self, operation_id: str) -> Any:
        getter = getattr(self._model_cache, "get_operation", None)
        if isinstance(getter, Callable):
            try:
                view = getter(operation_id)
                return _ChildView(
                    state=self._cache_state(view.state),
                    result=_phase_receipt(self._cache_result(view)),
                )
            except ModelCacheNotFound:
                pass
        return super().get(operation_id)

    @staticmethod
    def _cache_state(state: object) -> str:
        if model_cache_states.operation_is_backoff(
            state if isinstance(state, str) else None
        ):
            return "running"
        if state == "cancelled":
            return "failed"
        if state in {"queued", "running", "succeeded", "failed"}:
            return str(state)
        return "unknown"

    @staticmethod
    def _cache_result(view: Any) -> Mapping[str, object]:
        from .model_cache_contract import ModelCacheOperationProgress
        from .model_cache_progress import project_cache_progress

        cache = read_stored_model(ModelCacheOperationProgress, view.progress)
        progress = project_cache_progress(view.progress)
        downloaded, expected = cache.downloaded_bytes, cache.expected_bytes
        result: dict[str, object] = {
            "schema_version": 2,
            "phase": "transfer",
            "subphase": "model-download",
            "progress": {
                "phase": ProgressPhase.MODEL_DOWNLOAD,
                "completed_bytes": downloaded,
                "total_bytes": expected,
                "total_bytes_known": expected is not None,
                "operation": progress,
            },
            "artifact_set_sha256": view.artifact_set_sha256,
            "downloaded_bytes": downloaded,
            "total_bytes": expected,
        }
        if view.last_error:
            result["reason"] = view.last_error
        if view.result is not None:
            if isinstance(view.result, Mapping):
                evidence = dict(view.result)
            else:
                dump = getattr(view.result, "model_dump", None)
                evidence = dump(mode="json") if callable(dump) else {}
            parsed_evidence = read_stored_model(
                ModelCacheDownloadResult, evidence, strict=True
            )
            if parsed_evidence.artifact_set_sha256 != view.artifact_set_sha256:
                raise RuntimeError("model-cache completion identity is not exact")
            if parsed_evidence.coverage == "complete":
                result["coverage"] = "complete"
            result["evidence"] = parsed_evidence
        return result


__all__ = ["CompositeDistributionPhaseExecutor", "DurableDistributionPhaseExecutor"]
