"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import DistributionAssignment, DistributionObject, canonical_message

from .agent_jobs import AgentJobService
from .distribution import DistributionError, DistributionService
from .models import AgentOperation, AgentOperationAttempt, Job, RecipeBuild
from .run_switch_contract import RunSwitchPhase, RunSwitchPlan
from .run_switch_operations import PhaseExecution


@dataclass(frozen=True, slots=True)
class _ChildView:
    """Small child projection consumed by RunSwitchOperationService."""

    state: str
    result: Mapping[str, object]

    @property
    def progress(self) -> Mapping[str, object]:
        value = self.result.get("progress")
        return value if isinstance(value, Mapping) else {}


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
            return PhaseExecution(result={"scope": "spark-local", "reclaimed_bytes": 0, "nas_evicted": False})
        if item_index != 0:
            return PhaseExecution(result={"verified": True, "verified_digests": list(plan.storage.artifact_digests), "verified_image_digest": plan.image_digest, "verified_oci_layout_sha256": plan.build.oci_layout_sha256})
        targets = tuple(phase.node_ids)
        cached = self._cached_targets(plan, targets)
        missing = tuple(node_id for node_id in targets if node_id not in cached)
        if not missing:
            return PhaseExecution(result={
                "skipped": True,
                "verified": phase.kind == "verify",
                "verified_digests": list(plan.storage.artifact_digests),
                "verified_image_digest": plan.image_digest,
                "verified_oci_layout_sha256": plan.build.oci_layout_sha256,
                "cached_nodes": list(targets),
            })
        model_objects = self._model_objects(plan)
        archive = self._archive(plan)
        assignments = {
            node_id: self._assignment(plan, node_id, model_objects, archive)
            for node_id in missing
        }
        child_id = self._ensure_child(
            plan,
            phase,
            actor=actor,
            request_key=request_key,
            cached=cached,
            assignments=assignments,
        )
        return PhaseExecution(operation_id=child_id, result={"cached_nodes": list(cached)})

    def get(self, operation_id: str) -> Any:
        with self._sessions() as session:
            child = session.get(Job, operation_id)
            if child is None or child.kind != "artifact-distribution":
                raise KeyError(operation_id)
            operations = list(session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == child.id)
                .order_by(AgentOperation.node_id)
            ))
            members = []
            evidence = []
            for operation in operations:
                attempt = session.scalar(select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                ))
                raw = (attempt.progress if attempt is not None else None) or {}
                result = (attempt.result if attempt is not None else None) or {}
                members.append({
                    "node_id": operation.node_id,
                    "phase": "transfer",
                    "state": self._member_state(operation.state),
                    "completed_bytes": self._int(raw.get("bytes")) or self._int(raw.get("completed_bytes")) or 0,
                    "total_bytes": self._int(raw.get("total_bytes")),
                    "error": result.get("reason") if isinstance(result, Mapping) else None,
                })
                if isinstance(result, Mapping) and result:
                    evidence.append(dict(result))
            state = child.state
            completed = sum(self._int(item.get("completed_bytes")) or 0 for item in members)
            totals = [self._int(item.get("total_bytes")) for item in members]
            total = sum(value for value in totals if value is not None) if all(value is not None for value in totals) else None
            payload = {
                "progress": {
                    "phase": "transfer",
                    "completed_bytes": completed,
                    "total_bytes": total,
                    "total_bytes_known": total is not None,
                    "members": members,
                },
                "members": members,
                "evidence": evidence,
            }
            return _ChildView(state=state, result=payload)

    def _ensure_child(self, plan: RunSwitchPlan, phase: RunSwitchPhase, *, actor: str, request_key: str, cached: tuple[str, ...], assignments: Mapping[str, DistributionAssignment]) -> str:
        child_request = str(uuid.uuid5(uuid.UUID(request_key), f"artifact-distribution:{phase.kind}:{phase.index}"))
        now = self._clock()
        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == child_request))
            if existing is not None:
                if existing.kind != "artifact-distribution" or existing.payload.get("plan_digest") != plan.plan_digest:
                    raise RuntimeError("distribution child request key was reused")
                return existing.id
        # Register immutable assignments before opening the child transaction;
        # this avoids nested session transactions while retaining replay safety.
        for assignment in assignments.values():
            try:
                self._distribution.register(assignment)
            except DistributionError as error:
                if error.code != "distribution.assignment_conflict":
                    raise
                existing = self._distribution.authorize(
                    node_id=assignment.node_id, plan_digest=assignment.plan_digest
                )
                if existing.to_mapping() | {"assignment_id": assignment.assignment_id} != assignment.to_mapping():
                    raise
        with self._sessions.begin() as session:
            progress = {
                "phase": phase.kind,
                "completed_bytes": 0,
                "total_bytes": sum(item.bytes for assignment in assignments.values() for item in assignment.objects),
                "total_bytes_known": True,
                "members": [{"node_id": node_id, "state": "pending", "completed_bytes": 0, "total_bytes": None, "error": None} for node_id in (*cached, *assignments)],
            }
            child = Job(
                id=str(uuid.uuid5(uuid.UUID(request_key), f"artifact-child:{phase.index}")),
                request_id=child_request,
                kind="artifact-distribution",
                state="queued",
                actor=actor,
                authority_revision=plan.plan_digest,
                targets=list(assignments),
                payload_digest=self._digest({"plan_digest": plan.plan_digest, "phase": phase.kind}),
                payload={"plan_digest": plan.plan_digest, "phase": phase.kind, "progress": progress},
                result={"progress": progress, "members": progress["members"]},
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
                    {"schema_version": 1, "authority_revision": plan.plan_digest, "plan_digest": plan.plan_digest},
                    operation_id=str(uuid.uuid5(uuid.UUID(child.id), node_id)),
                )
            return child.id

    def _model_objects(self, plan: RunSwitchPlan) -> tuple[DistributionObject, ...]:
        preparation = plan.preparation
        if preparation is None:
            raise RuntimeError("exact model preparation is unavailable")
        source = getattr(self._distribution.source, "model_source", self._distribution.source)
        getter = getattr(source, "objects_for_set", None)
        if not callable(getter):
            raise RuntimeError("verified model cache manifest provider is unavailable")
        objects = tuple(getter(preparation.model.artifact_set_sha256))
        if not objects or any(item.kind != "model" for item in objects):
            raise RuntimeError("verified model cache manifest is incomplete")
        return objects

    def _archive(self, plan: RunSwitchPlan) -> DistributionObject:
        if plan.recipe_build_id is None or plan.image_digest is None or plan.build.oci_layout_sha256 is None or plan.build.image_bytes is None:
            raise RuntimeError("succeeded OCI build identity is unavailable")
        with self._sessions() as session:
            build = session.get(RecipeBuild, str(plan.recipe_build_id))
            if build is None or build.state != "succeeded" or build.image_digest != plan.image_digest or build.oci_layout_sha256 != plan.build.oci_layout_sha256 or build.image_bytes != plan.build.image_bytes:
                raise RuntimeError("OCI build authority changed")
        return DistributionObject("image.oci.tar", plan.build.oci_layout_sha256, plan.build.image_bytes, "oci-archive")

    def _assignment(self, plan: RunSwitchPlan, node_id: str, model_objects: tuple[DistributionObject, ...], archive: DistributionObject) -> DistributionAssignment:
        preparation = plan.preparation
        assert preparation is not None
        return DistributionAssignment.parse({
            "schema_version": 2,
            "assignment_id": str(uuid.uuid4()),
            "plan_digest": plan.plan_digest,
            "generation": 1,
            "node_id": node_id,
            "expires_at": (plan.generated_at.astimezone(UTC) + timedelta(hours=1)).isoformat(),
            "model_artifact_set_sha256": preparation.model.artifact_set_sha256,
            "objects": [item.to_mapping() for item in (*model_objects, archive)],
            "oci_image_digest": plan.image_digest,
            "oci_archive_sha256": archive.sha256,
        })

    @staticmethod
    def _cached_targets(plan: RunSwitchPlan, targets: tuple[str, ...]) -> tuple[str, ...]:
        preparation = plan.preparation
        if preparation is None:
            return ()
        model = {item.node_id: item for item in preparation.model.targets}
        image = {item.node_id: item for item in preparation.runtime_image.targets}
        return tuple(node_id for node_id in targets if (
            model.get(node_id) is not None
            and model[node_id].state == "ready"
            and model[node_id].verified_sha256 == preparation.model.artifact_set_sha256
            and image.get(node_id) is not None
            and image[node_id].state == "ready"
            and image[node_id].verified_sha256 == preparation.runtime_image.oci_layout_sha256
            and image[node_id].imported_image_digest == preparation.runtime_image.image_digest
        ))

    @staticmethod
    def _member_state(value: str) -> str:
        return {"queued": "pending", "running": "running", "succeeded": "succeeded", "failed": "failed"}.get(value, "unknown")

    @staticmethod
    def _int(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    @staticmethod
    def _digest(value: Mapping[str, object]) -> str:
        import hashlib
        return hashlib.sha256(canonical_message(value)).hexdigest()


__all__ = ["DurableDistributionPhaseExecutor"]
