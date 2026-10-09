"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import select
from vonk_agent_protocol import AgentOperation as AgentOperationKind
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.agent_words import ProfileChildPhase, ProfileEffectState

from ..distribution_assignment import NodeDistributionAssignment
from ..job_documents import (
    DistributionJobIdentity,
    DistributionJobPayload,
    DistributionTransferProgress,
)
from ..lifecycle.job import JobAdapter
from ..models import (
    AgentOperation,
    Job,
)
from ..run_switch_contract import (
    RunSwitchChildProgress,
    RunSwitchDistributionChildResult,
    RunSwitchMemberReceipt,
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchTargetTransferResult,
)
from ..run_switch_operations import PhaseExecution
from ..strict_json import read_stored_model
from .receipts import _child_receipt, _phase_receipt
from .verification import DistributionVerification


class DistributionChildren(DistributionVerification):
    """Children boundary for durable artifact distribution."""

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
            payload = read_stored_model(
                DistributionJobPayload, child.payload, from_json=True
            )
            receipt = _phase_receipt(
                RunSwitchTargetTransferResult(
                    phase=ProfileChildPhase.TRANSFER.value,
                    subphase=ProfileChildPhase.TARGET_COPY.value,
                    cached_nodes=payload.cached_nodes,
                    assignments=payload.assignments,
                ),
                phase=phase,
            )
            assignments, cached = payload.assignments, payload.cached_nodes
            if (
                child.targets != list(assignments)
                or set(assignments).intersection(cached)
                or set(assignments).union(cached) != set(phase.node_ids)
                or any(raw.node_id != node_id for node_id, raw in assignments.items())
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
        recovered_child_id: str | None = None,
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
            members = [
                RunSwitchMemberReceipt(
                    node_id=node_id,
                    state=LifecycleState.SUCCEEDED.value
                    if node_id in cached
                    else ProfileEffectState.PENDING.value,
                    completed_bytes=target_totals[node_id] if node_id in cached else 0,
                    total_bytes=target_totals[node_id],
                    cached=node_id in cached,
                )
                for node_id in (*cached, *assignments)
            ]
            progress = DistributionTransferProgress(
                phase=phase.kind,
                completed_bytes=cached_bytes,
                total_bytes=total_bytes,
                total_bytes_known=True,
                members=members,
            )
            identity = DistributionJobIdentity(
                plan_digest=plan.plan_digest,
                phase=phase.kind,
                workload_intent_ordinal=workload_intent_ordinal,
            )
            payload = DistributionJobPayload(
                **identity.model_dump(mode="python"),
                progress=progress,
                cached_nodes=list(cached),
                target_order=list(target_order),
                target_totals=target_totals,
                assignments=dict(assignments),
            )
            child = JobAdapter.new_job(
                # The request key provides replay identity. Job/operation IDs
                # are persisted once and follow the shared helper UUIDv4
                # contract when this transfer requests a runtime image pull.
                id=recovered_child_id
                or str(uuid.UUID(bytes=uuid.UUID(child_request).bytes, version=4)),
                request_id=child_request,
                kind="artifact-distribution",
                actor=actor,
                authority_revision=plan.plan_digest,
                targets=list(assignments),
                payload_digest=self._digest(identity),
                payload=payload.model_dump(mode="json"),
                result=_child_receipt(
                    RunSwitchDistributionChildResult(
                        phase=ProfileChildPhase.TRANSFER.value,
                        subphase=ProfileChildPhase.TARGET_COPY.value,
                        progress=RunSwitchChildProgress(
                            phase=ProfileChildPhase.TRANSFER.value,
                            completed_bytes=cached_bytes,
                            total_bytes=total_bytes,
                            total_bytes_known=True,
                            members=members,
                        ),
                        members=members,
                        evidence=[],
                    )
                ).model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            session.add(child)
            session.flush()
            for node_id, assignment in assignments.items():
                # A missing Job row does not erase an already-issued node order.
                # Reattach the same exact operation before considering dispatch.
                existing_order = session.scalar(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == child.id,
                        AgentOperation.node_id == node_id,
                        AgentOperation.kind == AgentOperationKind.ARTIFACT_DISTRIBUTION,
                    )
                )
                if existing_order is not None:
                    continue
                self._operations.enqueue_in_session(
                    session,
                    child.id,
                    node_id,
                    "artifact.distribution.v1",
                    plan.plan_digest,
                    {"plan_digest": plan.plan_digest},
                    operation_id=str(
                        uuid.UUID(
                            bytes=uuid.uuid5(uuid.UUID(child.id), node_id).bytes,
                            version=4,
                        )
                    ),
                )
            return child.id
