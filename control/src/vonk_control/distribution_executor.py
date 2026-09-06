"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    DistributionAssignment,
    DistributionObject,
    canonical_message,
)

from .agent_jobs import AgentJobService
from .distribution import DistributionError, DistributionService
from .model_cache import ModelCacheNotFound
from .models import (
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    RuntimeImageAuthorization,
    RuntimeImageReceipt,
)
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
            raise RuntimeError(f"unexpected {phase.kind} item index {item_index}")
        if phase.kind == "verify":
            targets = tuple(phase.node_ids)
            cached = self._cached_targets(plan, targets)
            if len(cached) == len(targets):
                return PhaseExecution(result={
                    "skipped": True,
                    "verified": True,
                    "verified_digests": list(plan.storage.artifact_digests),
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
                    "cached_nodes": list(cached),
                    "cached_target_totals": {node_id: self._target_bytes(plan, node_id) for node_id in cached},
                })
            return PhaseExecution(result=self._verify_evidence(plan, progress, targets, cached))
        targets = tuple(phase.node_ids)
        cached = self._cached_targets(plan, targets)
        missing = tuple(node_id for node_id in targets if node_id not in cached)
        if not missing:
            return PhaseExecution(result={
                "skipped": True,
                "verified": phase.kind == "verify",
                "verified_digests": list(plan.storage.artifact_digests),
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
            })
        model_objects, model_set_digest, model_set_bytes = self._model_objects(
            plan, progress
        )
        image_digest, layout_digest, image_bytes, build_id = self._runtime_identity(plan, progress)
        effective_execution_key = self._runtime_execution_key(progress)
        archive = self._archive(
            plan,
            build_id=build_id,
            image_digest=image_digest,
            layout_digest=layout_digest,
            image_bytes=image_bytes,
            effective_execution_key=effective_execution_key,
        )
        assignments = {
            node_id: self._assignment(
                plan,
                node_id,
                model_objects,
                archive,
                image_digest=image_digest,
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
            target_bytes=model_set_bytes + image_bytes,
        )
        return PhaseExecution(
            operation_id=child_id,
            result={
                "cached_nodes": list(cached),
                # Persist the exact assignment already verified against the
                # succeeded build and cache manifest for the verify phase.
                "assignments": {
                    node_id: assignment.to_mapping()
                    for node_id, assignment in assignments.items()
                },
            },
        )

    def get(self, operation_id: str) -> Any:
        with self._sessions() as session:
            child = session.get(Job, operation_id)
            if child is None or child.kind != "artifact-distribution":
                raise KeyError(operation_id)
            # AgentJobService owns the parent state transition. Reconcile the
            # durable child before projecting it so a restart cannot leave a
            # completed set of node operations looking queued.
            self._operations._aggregate_parent(session, child.id)
            operations = list(session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == child.id)
                .order_by(AgentOperation.node_id)
            ))
            members = []
            evidence = []
            cached_nodes = tuple(
                value for value in child.payload.get("cached_nodes", [])
                if isinstance(value, str)
            )
            cached_totals = child.payload.get("target_totals", {})
            if not isinstance(cached_totals, Mapping):
                cached_totals = {}
            for node_id in cached_nodes:
                total = self._int(cached_totals.get(node_id))
                members.append({
                    "node_id": node_id,
                    "phase": "transfer",
                    "state": "succeeded",
                    "completed_bytes": total or 0,
                    "total_bytes": total,
                    "error": None,
                    "cached": True,
                })
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
                    "total_bytes": self._int(raw.get("total_bytes"))
                    or self._int(cached_totals.get(operation.node_id)),
                    "error": result.get("reason") if isinstance(result, Mapping) else None,
                })
                if isinstance(result, Mapping) and result:
                    evidence.append({"node_id": operation.node_id, **dict(result)})
            by_node = {str(item["node_id"]): item for item in members}
            target_order = child.payload.get("target_order", list(by_node))
            if isinstance(target_order, list):
                members = [by_node[node_id] for node_id in target_order if isinstance(node_id, str) and node_id in by_node]
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

    def _ensure_child(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        actor: str,
        request_key: str,
        cached: tuple[str, ...],
        assignments: Mapping[str, DistributionAssignment],
        target_order: tuple[str, ...],
        target_bytes: int | None = None,
    ) -> str:
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
                existing_mapping = existing.to_mapping()
                requested_mapping = assignment.to_mapping()
                existing_mapping.pop("assignment_id", None)
                requested_mapping.pop("assignment_id", None)
                if existing_mapping != requested_mapping:
                    raise
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
                        "completed_bytes": target_totals[node_id] if node_id in cached else 0,
                        "total_bytes": target_totals[node_id],
                        "error": None,
                        "cached": node_id in cached,
                    }
                    for node_id in (*cached, *assignments)
                ],
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
                payload={
                    "plan_digest": plan.plan_digest,
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
                    {
                        "schema_version": 1,
                        "authority_revision": plan.plan_digest,
                        "plan_digest": plan.plan_digest,
                        "distribution_assignment": assignment.to_mapping(),
                    },
                    operation_id=str(uuid.uuid5(uuid.UUID(child.id), node_id)),
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
        source = getattr(self._distribution.source, "model_source", self._distribution.source)
        getter = getattr(source, "objects_for_set", None)
        if not callable(getter):
            raise TypeError("verified model cache manifest provider is unavailable")
        objects = tuple(getter(model_set_digest))
        if not objects or any(item.kind != "model" for item in objects):
            raise RuntimeError("verified model cache manifest is incomplete")
        expected_digests = set(plan.storage.artifact_digests)
        if expected_digests and {item.sha256 for item in objects} != expected_digests:
            raise RuntimeError("verified model cache manifest does not match the plan")
        actual_bytes = sum(item.bytes for item in objects)
        if model_set_bytes is not None and actual_bytes != model_set_bytes:
            raise RuntimeError("verified model cache byte total does not match the plan")
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
                candidate = raw.get("result") if isinstance(raw.get("result"), Mapping) else raw
                if not isinstance(candidate, Mapping):
                    continue
                runtime_receipt = candidate.get("runtime_image")
                if isinstance(runtime_receipt, Mapping):
                    candidate = {**candidate, **runtime_receipt}
                if candidate.get("source") == "published":
                    image_digest = candidate.get("image_digest") or image_digest
                    layout_digest = candidate.get(
                        "oci_layout_sha256", candidate.get("oci_archive_sha256")
                    ) or layout_digest
                    image_bytes = candidate.get("image_bytes") or image_bytes
                else:
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
    def _runtime_execution_key(progress: Mapping[str, object]) -> str | None:
        phase_results = progress.get("phase_results")
        if not isinstance(phase_results, list):
            return None
        for raw in reversed(phase_results):
            if not isinstance(raw, Mapping):
                continue
            runtime_receipt = raw.get("runtime_image")
            if isinstance(runtime_receipt, Mapping):
                value = runtime_receipt.get("effective_execution_key")
                if isinstance(value, str):
                    return value
            value = raw.get("effective_execution_key")
            if isinstance(value, str):
                return value
        return None

    def _archive(
        self,
        plan: RunSwitchPlan,
        *,
        build_id: str | None,
        image_digest: str,
        layout_digest: str,
        image_bytes: int,
        effective_execution_key: str | None = None,
    ) -> DistributionObject:
        if not image_digest or not layout_digest or image_bytes < 1:
            raise RuntimeError("verified OCI runtime image identity is unavailable")
        with self._sessions() as session:
            if build_id is not None:
                build = session.get(RecipeBuild, build_id)
                if (
                    build is None
                    or build.state != "succeeded"
                    or build.image_digest != image_digest
                    or build.oci_layout_sha256 != layout_digest
                    or build.image_bytes != image_bytes
                ):
                    raise RuntimeError("OCI build authority changed")
                if plan.recipe_revision_id is not None:
                    authorization = session.scalar(
                        select(RuntimeImageAuthorization).where(
                            RuntimeImageAuthorization.recipe_revision_id == plan.recipe_revision_id,
                            RuntimeImageAuthorization.source == "controller-build",
                            RuntimeImageAuthorization.build_id == build.id,
                            RuntimeImageAuthorization.effective_execution_key
                            == effective_execution_key,
                            RuntimeImageAuthorization.platform_manifest_digest == image_digest,
                            RuntimeImageAuthorization.oci_archive_sha256 == layout_digest,
                            RuntimeImageAuthorization.image_bytes == image_bytes,
                            RuntimeImageAuthorization.state == "authorized",
                        )
                    )
                    if authorization is None:
                        raise RuntimeError("current recipe is not authorized for OCI build receipt")
                    receipt = session.scalar(
                        select(RuntimeImageReceipt).where(
                            RuntimeImageReceipt.id == authorization.receipt_id,
                            RuntimeImageReceipt.state == "verified",
                            RuntimeImageReceipt.source == authorization.source,
                            RuntimeImageReceipt.build_id == authorization.build_id,
                            RuntimeImageReceipt.original_content_digest
                            == authorization.original_content_digest,
                            RuntimeImageReceipt.effective_execution_key
                            == authorization.effective_execution_key,
                            RuntimeImageReceipt.platform_manifest_digest
                            == authorization.platform_manifest_digest,
                            RuntimeImageReceipt.local_image_config_id
                            == authorization.local_image_config_id,
                            RuntimeImageReceipt.oci_archive_sha256
                            == authorization.oci_archive_sha256,
                            RuntimeImageReceipt.image_bytes == authorization.image_bytes,
                        )
                    )
                    if receipt is None:
                        raise RuntimeError("OCI build receipt authority changed")
            else:
                receipt = None
                if plan.recipe_revision_id is not None:
                    authorization = session.scalar(
                        select(RuntimeImageAuthorization).where(
                            RuntimeImageAuthorization.recipe_revision_id == plan.recipe_revision_id,
                            RuntimeImageAuthorization.source == "published",
                            RuntimeImageAuthorization.effective_execution_key
                            == effective_execution_key,
                            RuntimeImageAuthorization.platform_manifest_digest == image_digest,
                            RuntimeImageAuthorization.oci_archive_sha256 == layout_digest,
                            RuntimeImageAuthorization.image_bytes == image_bytes,
                            RuntimeImageAuthorization.state == "authorized",
                        )
                    )
                    if authorization is not None:
                        receipt = session.scalar(
                            select(RuntimeImageReceipt).where(
                                RuntimeImageReceipt.id == authorization.receipt_id,
                                RuntimeImageReceipt.state == "verified",
                                RuntimeImageReceipt.source == authorization.source,
                                RuntimeImageReceipt.build_id.is_(None),
                                RuntimeImageReceipt.original_content_digest
                                == authorization.original_content_digest,
                                RuntimeImageReceipt.effective_execution_key
                                == authorization.effective_execution_key,
                                RuntimeImageReceipt.registry_manifest_digest
                                == authorization.registry_manifest_digest,
                                RuntimeImageReceipt.platform_manifest_digest
                                == authorization.platform_manifest_digest,
                                RuntimeImageReceipt.local_image_config_id
                                == authorization.local_image_config_id,
                                RuntimeImageReceipt.oci_archive_sha256
                                == authorization.oci_archive_sha256,
                                RuntimeImageReceipt.image_bytes == authorization.image_bytes,
                            )
                        )
                else:
                    receipt = session.scalar(
                        select(RuntimeImageReceipt).where(
                            RuntimeImageReceipt.source == "published",
                            RuntimeImageReceipt.original_content_digest == plan.recipe_content_sha256,
                            RuntimeImageReceipt.effective_execution_key == effective_execution_key,
                            RuntimeImageReceipt.state == "verified",
                            RuntimeImageReceipt.platform_manifest_digest == image_digest,
                            RuntimeImageReceipt.oci_archive_sha256 == layout_digest,
                            RuntimeImageReceipt.image_bytes == image_bytes,
                        )
                    )
                if receipt is None:
                    raise RuntimeError("published runtime image receipt authority changed")
        return DistributionObject("image.oci.tar", layout_digest, image_bytes, "oci-archive")

    def _assignment(
        self,
        plan: RunSwitchPlan,
        node_id: str,
        model_objects: tuple[DistributionObject, ...],
        archive: DistributionObject,
        *,
        image_digest: str,
        model_set_digest: str,
    ) -> DistributionAssignment:
        generation = getattr(getattr(plan, "mapping", None), "mapping_generation", None)
        if type(generation) is not int or generation < 1:
            generation = 1
        # UUID v4 is part of the wire contract, while the digest-derived bytes
        # make replay after a Controller restart yield the same assignment.
        seed = f"{plan.plan_digest}:{generation}:{node_id}:{model_set_digest}:{archive.sha256}"
        assignment_bytes = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
        assignment_bytes[6] = (assignment_bytes[6] & 0x0F) | 0x40
        assignment_bytes[8] = (assignment_bytes[8] & 0x3F) | 0x80
        return DistributionAssignment.parse({
            "schema_version": 2,
            "assignment_id": str(uuid.UUID(bytes=bytes(assignment_bytes))),
            "plan_digest": plan.plan_digest,
            "generation": generation,
            "node_id": node_id,
            "expires_at": (plan.generated_at.astimezone(UTC) + timedelta(hours=1)).isoformat(),
            "model_artifact_set_sha256": model_set_digest,
            "objects": [item.to_mapping() for item in (*model_objects, archive)],
            "oci_image_digest": image_digest,
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
            and getattr(model[node_id], "verified_at", None) is not None
            and model[node_id].verified_sha256 == preparation.model.artifact_set_sha256
            and image.get(node_id) is not None
            and image[node_id].state == "ready"
            and getattr(image[node_id], "verified_at", None) is not None
            and image[node_id].verified_sha256 == preparation.runtime_image.oci_layout_sha256
            and image[node_id].imported_image_digest == preparation.runtime_image.image_digest
        ))

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
        """Validate terminal agent receipts for every non-cached target."""
        expected_digests = set(plan.storage.artifact_digests)
        preparation = plan.preparation
        expected_image = plan.image_digest or (
            preparation.runtime_image.image_digest if preparation is not None else None
        )
        expected_registry = plan.image_digest
        expected_layout = plan.build.oci_layout_sha256 or (
            preparation.runtime_image.oci_layout_sha256 if preparation is not None else None
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
                candidate_registry = runtime_receipt.get("registry_manifest_digest")
                if isinstance(candidate_registry, str):
                    expected_registry = candidate_registry
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
                if not isinstance(node_id, str) or not isinstance(assignment_raw, Mapping):
                    continue
                try:
                    assignment = DistributionAssignment.parse(assignment_raw)
                except (TypeError, ValueError):
                    continue
                if (
                    assignment.plan_digest == plan.plan_digest
                    and assignment.node_id in targets
                    and assignment.model_artifact_set_sha256 == (
                        preparation.model.artifact_set_sha256 if preparation is not None else None
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
            if not isinstance(node_id, str) or node_id not in targets:
                continue
            evidence = candidate.get("evidence", candidate)
            if isinstance(evidence, Mapping):
                receipts[node_id] = evidence
        cached_nodes = set(cached)
        cached_nodes.update({
            value for value in progress.get("cached_nodes", [])
            if isinstance(value, str)
        })
        for candidate in phase_results:
            if isinstance(candidate, Mapping):
                cached = candidate.get("cached_nodes")
                if isinstance(cached, list):
                    cached_nodes.update(value for value in cached if isinstance(value, str))
        missing = set(targets) - cached_nodes
        if missing - receipts.keys():
            raise RuntimeError("verification requires terminal evidence from every target")
        for node_id in missing:
            receipt = receipts[node_id]
            if receipt.get("verified") is not True:
                raise RuntimeError(f"target {node_id} did not verify its distribution")
            digests = receipt.get("verified_digests")
            if not isinstance(digests, list) or set(digests) != expected_digests:
                raise RuntimeError(f"target {node_id} model evidence is incomplete")
            if receipt.get("verified_image_digest") != expected_image:
                raise RuntimeError(f"target {node_id} image evidence is not exact")
            if receipt.get("imported_image_digest") != expected_image:
                raise RuntimeError(f"target {node_id} image import evidence is missing")
            if receipt.get("verified_oci_layout_sha256") != expected_layout:
                raise RuntimeError(f"target {node_id} OCI archive evidence is not exact")
        return {
            "verified": True,
            "verified_digests": sorted(expected_digests),
            "verified_image_digest": expected_image,
            "verified_registry_manifest_digest": expected_registry,
            "verified_oci_layout_sha256": expected_layout,
            "cached_nodes": sorted(cached_nodes),
            "evidence": [dict(receipts[node_id]) for node_id in sorted(receipts)],
        }

    @staticmethod
    def _member_state(value: str) -> str:
        return {"queued": "pending", "running": "running", "succeeded": "succeeded", "failed": "failed"}.get(value, "unknown")

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
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._model_cache = model_cache
        self._runtime_image_preparer = runtime_image_preparer

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
            runtime_result = self._prepare_runtime_image(plan)
            if runtime_result is None:
                raise RuntimeError("runtime image preparation returned no evidence")
            return PhaseExecution(result=runtime_result)
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
            model.artifact_set_sha256 if model is not None else plan.storage.artifact_set_sha256
        )
        model_version_sha256 = (
            model.model_version_sha256 if model is not None else plan.model_version_sha256
        )
        recipe_revision_sha256 = (
            model.recipe_revision_sha256 if model is not None else plan.recipe_content_sha256
        )
        artifact_count = (
            model.artifact_count if model is not None else len(plan.storage.artifact_digests)
        )
        artifact_set_bytes = (
            model.artifact_set_bytes if model is not None else plan.storage.artifact_set_bytes
        )
        if not artifact_set_sha256 or not artifact_count or not artifact_set_bytes:
            raise RuntimeError("exact model preparation is unavailable")
        preview_method = getattr(self._model_cache, "download_preview", None)
        start_method = getattr(self._model_cache, "start_download", None)
        if not callable(preview_method) or not callable(start_method):
            raise TypeError("model-cache download provider is unavailable")
        # An exact persisted set is sufficient to resolve the opaque manifest.
        # When a recipe revision ID is available, include the model pin as an
        # additional cross-check; never send both recipe ID and recipe digest.
        pins: dict[str, object] = {
            "artifact_set_sha256": artifact_set_sha256,
        }
        if plan.recipe_revision_id is not None:
            pins.update(
                model_version_sha256=model_version_sha256,
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
        manifest_getter = getattr(self._model_cache, "manifest_for_artifact_set", None)
        manifest = (
            manifest_getter(artifact_set_sha256)
            if callable(manifest_getter)
            else preview.get("_manifest")
        )
        if (
            getattr(manifest, "digest", None) != artifact_set_sha256
            or getattr(manifest, "recipe_revision_sha256", None)
            != recipe_revision_sha256
        ):
            raise RuntimeError("model-cache manifest recipe identity is not exact")
        blockers = preview.get("blockers", [])
        if isinstance(blockers, Sequence) and not isinstance(blockers, (str, bytes, bytearray)) and blockers:
            raise RuntimeError("model-cache download is blocked: " + "; ".join(map(str, blockers)))
        expected_bytes = preview.get("expected_bytes")
        if type(expected_bytes) is not int or expected_bytes < 1:
            raise RuntimeError("model-cache download total is unavailable")
        if preview.get("new_bytes") == 0:
            return PhaseExecution(result={
                "skipped": True,
                "coverage": "complete",
                "artifact_set_sha256": artifact_set_sha256,
                # The set is already covered, so this phase transferred no
                # bytes.  ``expected_bytes`` is the immutable set size while
                # ``new_bytes`` is the operation's transfer envelope.
                "downloaded_bytes": 0,
                "total_bytes": 0,
            })
        cache_request_key = str(uuid.uuid5(uuid.UUID(request_key), f"model-download:{phase.index}:{artifact_set_sha256}"))
        view = start_method(
            actor=actor,
            request_key=cache_request_key,
            plan_digest=preview["plan_digest"],
            **pins,
        )
        return PhaseExecution(operation_id=view.id, result=self._cache_result(view))

    def _prepare_runtime_image(
        self, plan: RunSwitchPlan
    ) -> Mapping[str, object] | None:
        """Prepare one Controller image before target distribution.

        This callback is deliberately supplied only to the durable worker
        executor.  API preview, admission, and agent spec reads use the
        read-only receipt resolver in ``ControllerExecutionPlanService``.
        """

        if self._runtime_image_preparer is None:
            return None
        if plan.recipe_revision_id is None or not plan.spark_group.nodes:
            raise RuntimeError("runtime image preparation identity is unavailable")
        from .execution_plan_service import _bind_runtime_artifacts
        from .recipe_runtime_specs import compile_runtime_spec, resolve_recipe_entities

        node = min(plan.spark_group.nodes, key=lambda item: (item.rank, item.node_id))
        with self._sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == plan.recipe_revision_id,
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
            if revision is None:
                raise RuntimeError("runtime image preparation recipe revision is unavailable")
            build = (
                session.get(RecipeBuild, plan.recipe_build_id)
                if plan.recipe_build_id is not None
                else None
            )
            package_handle = None
            execution = revision.document.get("execution")
            if isinstance(execution, Mapping) and execution.get("mode") == "build":
                if build is None or build.state != "succeeded":
                    raise RuntimeError("runtime image preparation build receipt is unavailable")
                package_handle = {
                    "image_digest": build.image_digest,
                    "image_reference": f"localhost/vonk/recipe-build@{build.image_digest}",
                    "build_input_sha256": build.build_input_sha256,
                    "platform": "linux/arm64",
                }
            entities = resolve_recipe_entities(session, revision.document)
            parameters = (
                dict(plan.mapping.parameters)
                if plan.mapping is not None
                else {}
            )
            runtime_spec = compile_runtime_spec(
                revision.document,
                resolved_entities=entities,
                parameters=parameters,
                role=node.role,
                rank=node.rank,
                package_handle=package_handle,
            )
            runtime_spec = _bind_runtime_artifacts(
                runtime_spec,
                entities["models"],
            )
        receipt = self._runtime_image_preparer(
            revision.document,
            runtime_spec,
            build,
        )
        to_mapping = getattr(receipt, "to_mapping", None)
        raw = to_mapping() if callable(to_mapping) else receipt
        if not isinstance(raw, Mapping):
            raise TypeError("runtime image preparation returned invalid evidence")
        identity = runtime_spec.get("identity")
        effective_execution_key = (
            identity.get("execution_sha256")
            if isinstance(identity, Mapping)
            else None
        )
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
        if callable(getter):
            try:
                view = getter(operation_id)
                return _ChildView(
                    state=self._cache_state(view.state),
                    result=self._cache_result(view),
                )
            except ModelCacheNotFound:
                pass
        return super().get(operation_id)

    @staticmethod
    def _cache_state(state: object) -> str:
        if state in {"downloading", "verifying", "partial"}:
            return "running"
        if state in {"queued", "succeeded", "failed"}:
            return str(state)
        return "unknown"

    @staticmethod
    def _cache_result(view: Any) -> Mapping[str, object]:
        progress = dict(view.progress) if isinstance(view.progress, Mapping) else {}
        downloaded = progress.get("downloaded_bytes")
        expected = progress.get("expected_bytes")
        if type(downloaded) is not int or downloaded < 0:
            downloaded = 0
        if type(expected) is not int or expected < 0:
            expected = None
        result: dict[str, object] = {
            "progress": {
                "phase": "model-download",
                "completed_bytes": downloaded,
                "total_bytes": expected,
                "total_bytes_known": expected is not None,
            },
            "artifact_set_sha256": view.artifact_set_sha256,
            "downloaded_bytes": downloaded,
            "total_bytes": expected,
        }
        if view.last_error:
            result["reason"] = view.last_error
        if view.result is not None:
            evidence = dict(view.result) if isinstance(view.result, Mapping) else {}
            if evidence.get("artifact_set_sha256") != view.artifact_set_sha256:
                raise RuntimeError("model-cache completion identity is not exact")
            if evidence.get("coverage") == "complete":
                result["coverage"] = "complete"
            result["evidence"] = evidence
        return result


__all__ = ["CompositeDistributionPhaseExecutor", "DurableDistributionPhaseExecutor"]
