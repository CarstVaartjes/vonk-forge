"""Persisted, deterministic operation graphs for agent reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentOperation

from .models import Reconciliation, ReconciliationCompletionGeneration

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_OPERATION_ID = re.compile(r"[a-z0-9](?:[a-z0-9._:-]{0,126}[a-z0-9])?\Z")
_WORKLOAD_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_IMPLEMENTED_OPERATIONS = frozenset(
    {
        AgentOperation.NODE_PROBE.value,
        AgentOperation.RELEASE_INSTALL.value,
        AgentOperation.WORKLOAD_PREPARE.value,
        AgentOperation.WORKLOAD_START.value,
        AgentOperation.WORKLOAD_STOP.value,
        AgentOperation.WORKLOAD_HEALTH.value,
        AgentOperation.WORKLOAD_VERIFY.value,
        # Workload-package operations use the same persisted graph and agent
        # queue as the legacy workload actions.  Keeping them in this registry
        # makes the graph validator family-agnostic without adding a second
        # transport.
        AgentOperation.PACKAGE_PREPARE.value,
        AgentOperation.PACKAGE_ACTIVATE.value,
        AgentOperation.PACKAGE_HEALTH.value,
        AgentOperation.PACKAGE_STOP.value,
        AgentOperation.PACKAGE_ROLLBACK.value,
        AgentOperation.PACKAGE_REMOVE.value,
        AgentOperation.PACKAGE_REPAIR.value,
        AgentOperation.PACKAGE_GC.value,
    }
)
_PHASE_TRANSITIONS = {
    "planned": "routes-withdrawn",
    "routes-withdrawn": "dispatching",
    "dispatching": "accepting",
    "accepting": "completed",
}
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class OperationNode:
    """One immutable, dependency-fenced operation in a reconciliation graph."""

    operation_id: str
    node_id: str
    workload_id: str
    kind: str
    dependencies: tuple[str, ...]
    compensation_kind: str | None
    payload_digest: str

    def to_document(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "node_id": self.node_id,
            "workload_id": self.workload_id,
            "kind": self.kind,
            "dependencies": list(self.dependencies),
            "compensation_kind": self.compensation_kind,
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True)
class OperationGraph:
    """Canonical topological graph persisted independently of input ordering."""

    reconciliation_id: str
    base_commit: str
    targets: tuple[str, ...]
    nodes: tuple[OperationNode, ...]
    digest: str

    @property
    def document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "base_commit": self.base_commit,
            "targets": list(self.targets),
            "nodes": [node.to_document() for node in self.nodes],
        }

    def dependencies(self, operation_id: str) -> tuple[str, ...]:
        for node in self.nodes:
            if node.operation_id == operation_id:
                return node.dependencies
        raise KeyError(operation_id)


class ReconciliationOrchestrator:
    """Validate and persist operation graphs without executing node work."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    def plan(self, document: Mapping[str, Any]) -> OperationGraph:
        base_commit, targets, generation, nodes = _parse_plan(document)
        graph_document = _graph_document(base_commit, targets, nodes)
        digest = _digest(graph_document)
        graph = OperationGraph(
            reconciliation_id=str(uuid.uuid4()),
            base_commit=base_commit,
            targets=targets,
            nodes=nodes,
            digest=digest,
        )
        stored = Reconciliation(
            id=graph.reconciliation_id,
            base_commit=base_commit,
            status="planned",
            summary={
                "operation_count": len(nodes),
                "target_count": len(targets),
            },
            graph=graph_document,
            graph_digest=digest,
            current_phase="planned",
            route_withdrawal_generation=generation,
            terminal_reason=None,
            created_at=self._clock(),
        )
        with self._sessions.begin() as session:
            session.add(stored)
        return graph

    def advance(
        self,
        reconciliation_id: str,
        phase: str,
        *,
        route_withdrawal_generation: int | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            stored = self._stored(session, reconciliation_id)
            if stored.status in _TERMINAL_STATUSES:
                raise ValueError("reconciliation is terminal")
            if _PHASE_TRANSITIONS.get(stored.current_phase) != phase:
                raise ValueError("reconciliation phase transition is invalid")
            if route_withdrawal_generation is not None:
                if phase != "routes-withdrawn":
                    raise ValueError("route generation is invalid for this transition")
                _generation(route_withdrawal_generation)
                if route_withdrawal_generation < stored.route_withdrawal_generation:
                    raise ValueError("route generation must not decrease")
                stored.route_withdrawal_generation = route_withdrawal_generation
            stored.current_phase = phase
            if phase == "completed":
                stored.completion_generation = self._next_completion_generation(session)
                stored.status = "succeeded"
            else:
                stored.status = "running"

    def store_resolved_plan(
        self,
        graph: OperationGraph,
        plan_digest: str,
        document: Mapping[str, object],
    ) -> None:
        """Attach the complete immutable plan to its accepted graph row."""

        stored_document = _resolved_document(plan_digest, document)
        with self._sessions.begin() as session:
            stored = self._stored(session, graph.reconciliation_id)
            if stored.graph_digest != graph.digest or stored.graph != graph.document:
                raise ValueError("resolved plan graph does not match persisted graph")
            if stored.plan_digest is not None:
                if (
                    stored.plan_digest != plan_digest
                    or stored.resolved_plan != stored_document
                ):
                    raise ValueError("reconciliation already has a different plan")
                return
            stored.plan_digest = plan_digest
            stored.resolved_plan = stored_document

    def get_or_create_resolved_plan(
        self,
        graph_plan: Mapping[str, Any],
        plan_digest: str,
        document: Mapping[str, object],
    ) -> OperationGraph:
        """Atomically persist or return the exact plan identified by its digest."""

        base_commit, targets, generation, nodes = _parse_plan(graph_plan)
        graph_document = _graph_document(base_commit, targets, nodes)
        graph_digest = _digest(graph_document)
        stored_document = _resolved_document(plan_digest, document)
        if stored_document.get("operation_graph") != graph_document:
            raise ValueError("resolved plan graph does not match operation graph")

        candidate = Reconciliation(
            id=str(uuid.uuid4()),
            base_commit=base_commit,
            status="planned",
            summary={
                "operation_count": len(nodes),
                "target_count": len(targets),
            },
            graph=graph_document,
            graph_digest=graph_digest,
            plan_digest=plan_digest,
            resolved_plan=stored_document,
            current_phase="planned",
            route_withdrawal_generation=generation,
            terminal_reason=None,
            created_at=self._clock(),
        )
        with self._sessions.begin() as session:
            try:
                with session.begin_nested():
                    session.add(candidate)
                    session.flush()
                accepted = candidate
            except IntegrityError:
                accepted = session.scalar(
                    select(Reconciliation).where(
                        Reconciliation.plan_digest == plan_digest
                    )
                )
                if accepted is None:
                    raise
            if (
                accepted.base_commit != base_commit
                or accepted.graph != graph_document
                or accepted.graph_digest != graph_digest
                or accepted.route_withdrawal_generation != generation
                or accepted.resolved_plan != stored_document
            ):
                raise ValueError("plan digest identifies different persisted content")
            return OperationGraph(
                accepted.id,
                accepted.base_commit,
                targets,
                nodes,
                accepted.graph_digest,
            )

    def resolved_plan(
        self, plan_digest: str
    ) -> tuple[OperationGraph, Mapping[str, object]] | None:
        """Load and revalidate a complete plan after a process restart."""

        if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
            return None
        with self._sessions() as session:
            stored = session.scalar(
                select(Reconciliation).where(Reconciliation.plan_digest == plan_digest)
            )
            if stored is None or stored.resolved_plan is None:
                return None
            return validate_persisted_resolved_plan(
                reconciliation_id=stored.id,
                base_commit=stored.base_commit,
                graph_document=stored.graph,
                graph_digest=stored.graph_digest,
                plan_digest=stored.plan_digest,
                resolved_document=stored.resolved_plan,
                route_withdrawal_generation=stored.route_withdrawal_generation,
            )

    def cancel(self, reconciliation_id: str, reason: str) -> None:
        raise RuntimeError(
            "direct cancellation is disabled; use durable cancellation intent"
        )

    @staticmethod
    def _stored(session: Session, reconciliation_id: str) -> Reconciliation:
        if not isinstance(reconciliation_id, str):
            raise KeyError("unknown reconciliation")
        stored = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == reconciliation_id)
            .with_for_update(of=Reconciliation)
        )
        if stored is None:
            raise KeyError("unknown reconciliation")
        return stored

    @staticmethod
    def _next_completion_generation(session: Session) -> int:
        statement = (
            select(ReconciliationCompletionGeneration)
            .where(ReconciliationCompletionGeneration.singleton_id == 1)
            .with_for_update()
        )
        counter = session.scalar(statement)
        if counter is None:
            try:
                with session.begin_nested():
                    session.add(
                        ReconciliationCompletionGeneration(
                            singleton_id=1,
                            last_generation=0,
                        )
                    )
                    session.flush()
            except IntegrityError:
                pass
            counter = session.scalar(statement)
        if counter is None or not 0 <= counter.last_generation < 2**63 - 1:
            raise RuntimeError("reconciliation completion generation is unavailable")
        counter.last_generation += 1
        return counter.last_generation


def _resolved_document(
    plan_digest: str, document: Mapping[str, object]
) -> dict[str, object]:
    if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
        raise ValueError("resolved plan digest is invalid")
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 1_048_576:
        raise ValueError("resolved reconciliation plan is too large")
    if hashlib.sha256(encoded).hexdigest() != plan_digest:
        raise ValueError("resolved plan content does not match its digest")
    return json.loads(encoded)


def validate_persisted_resolved_plan(
    *,
    reconciliation_id: object,
    base_commit: object,
    graph_document: object,
    graph_digest: object,
    plan_digest: object,
    resolved_document: object,
    route_withdrawal_generation: object,
) -> tuple[OperationGraph, Mapping[str, object]]:
    """Authenticate one complete persisted plan without database access."""

    if not isinstance(reconciliation_id, str) or not reconciliation_id:
        raise ValueError("persisted resolved plan reconciliation identity is invalid")
    if not isinstance(resolved_document, Mapping):
        raise TypeError("persisted resolved plan document is invalid")
    try:
        document = _resolved_document(plan_digest, resolved_document)
    except (TypeError, ValueError) as error:
        raise ValueError("persisted resolved plan digest is invalid") from error
    fields = {
        "commit",
        "targets",
        "placements",
        "routes",
        "releases",
        "workload_groups",
        "input_digests",
        "fleet_evidence_digest",
        "operation_graph",
        "operation_payloads",
        "agent_protocol_range",
    }
    if set(document) != fields:
        raise ValueError("persisted resolved plan fields are invalid")
    if (
        not isinstance(base_commit, str)
        or _COMMIT.fullmatch(base_commit) is None
        or document["commit"] != base_commit
    ):
        raise ValueError("persisted resolved plan base commit is invalid")
    for field in (
        "placements",
        "routes",
        "releases",
        "workload_groups",
        "input_digests",
        "operation_payloads",
    ):
        value = document[field]
        if not isinstance(value, Mapping) or not all(
            isinstance(key, str) for key in value
        ):
            raise TypeError(f"persisted resolved plan {field} is invalid")
    input_digests = document["input_digests"]
    if not all(
        isinstance(value, str) and _DIGEST.fullmatch(value)
        for value in input_digests.values()
    ):
        raise ValueError("persisted resolved plan input digest is invalid")
    fleet_evidence_digest = document["fleet_evidence_digest"]
    if (
        not isinstance(fleet_evidence_digest, str)
        or _DIGEST.fullmatch(fleet_evidence_digest) is None
    ):
        raise ValueError("persisted resolved plan fleet evidence digest is invalid")
    protocol = document["agent_protocol_range"]
    if (
        not isinstance(protocol, list)
        or len(protocol) != 2
        or not all(
            isinstance(value, int) and not isinstance(value, bool) for value in protocol
        )
        or protocol[0] < 1
        or protocol[0] > protocol[1]
    ):
        raise ValueError("persisted resolved plan protocol range is invalid")
    if not isinstance(graph_document, Mapping):
        raise TypeError("persisted resolved plan graph is invalid")
    try:
        parsed_commit, targets, _, nodes = _parse_plan(
            {
                "base_commit": graph_document.get("base_commit"),
                "targets": graph_document.get("targets"),
                "route_withdrawal_generation": route_withdrawal_generation,
                "operations": graph_document.get("nodes"),
            }
        )
    except (TypeError, ValueError) as error:
        raise ValueError("persisted resolved plan graph is invalid") from error
    expected_graph = _graph_document(parsed_commit, targets, nodes)
    if (
        expected_graph != graph_document
        or parsed_commit != base_commit
        or document["operation_graph"] != expected_graph
        or document["targets"] != list(targets)
        or not isinstance(graph_digest, str)
        or _DIGEST.fullmatch(graph_digest) is None
        or _digest(expected_graph) != graph_digest
    ):
        raise ValueError("persisted resolved plan graph consistency is invalid")
    payloads = document["operation_payloads"]
    if set(payloads) != {node.operation_id for node in nodes}:
        raise ValueError("persisted resolved plan operation payload set is invalid")
    for node in nodes:
        payload = payloads[node.operation_id]
        if not isinstance(payload, Mapping) or _digest(payload) != node.payload_digest:
            raise ValueError(
                "persisted resolved plan operation payload digest is invalid"
            )
    package_nodes = tuple(node for node in nodes if node.kind.startswith("package."))
    if package_nodes:
        _validate_package_plan(nodes, payloads)
        return (
            OperationGraph(
                reconciliation_id,
                base_commit,
                targets,
                nodes,
                graph_digest,
            ),
            document,
        )

    stop_nodes = tuple(
        node for node in nodes if node.kind == AgentOperation.WORKLOAD_STOP.value
    )
    install_nodes = tuple(
        node for node in nodes if node.kind == AgentOperation.RELEASE_INSTALL.value
    )
    gate_nodes = tuple(
        node
        for node in nodes
        if node.kind == AgentOperation.NODE_PROBE.value
        or node.workload_id == "node-gate"
    )
    if any(
        node.kind != AgentOperation.NODE_PROBE.value or node.workload_id != "node-gate"
        for node in gate_nodes
    ):
        raise ValueError("persisted resolved plan node gate identity is invalid")
    required_gate_targets = {node.node_id for node in (*stop_nodes, *install_nodes)}
    actual_gate_targets = {node.node_id for node in gate_nodes}
    if (
        len(gate_nodes) != len(actual_gate_targets)
        or actual_gate_targets != required_gate_targets
    ):
        raise ValueError("persisted resolved plan node gate targets are invalid")
    stop_ids = tuple(sorted(node.operation_id for node in stop_nodes))
    gate_ids = tuple(sorted(node.operation_id for node in gate_nodes))
    for gate in gate_nodes:
        if (
            payloads[gate.operation_id]
            != {"require_active_nvidia_compute_processes": 0}
            or gate.dependencies != stop_ids
        ):
            raise ValueError("persisted resolved plan node gate barrier is invalid")
    if any(node.dependencies != gate_ids for node in install_nodes):
        raise ValueError("persisted resolved plan install gate barrier is invalid")
    return (
        OperationGraph(
            reconciliation_id,
            base_commit,
            targets,
            nodes,
            graph_digest,
        ),
        document,
    )


def _validate_package_plan(
    nodes: tuple[OperationNode, ...],
    payloads: Mapping[str, object],
) -> None:
    """Validate the generic package graph without a model catalog.

    Package identities are carried only as exact digests in the request.  The
    graph validator deliberately does not enumerate adapters, models, or
    releases; those are authorized by the workload trust plane and checked by
    the GPU node package engine.
    """

    from vonk_agent_protocol import PackageOperationRequest

    allowed = {
        AgentOperation.PACKAGE_PREPARE.value,
        AgentOperation.PACKAGE_ACTIVATE.value,
        AgentOperation.PACKAGE_HEALTH.value,
        AgentOperation.PACKAGE_STOP.value,
        AgentOperation.PACKAGE_ROLLBACK.value,
        AgentOperation.PACKAGE_REMOVE.value,
        AgentOperation.PACKAGE_REPAIR.value,
    }
    by_target: dict[tuple[str, str], dict[str, OperationNode]] = {}
    for node in nodes:
        if node.kind not in allowed:
            continue
        payload = payloads.get(node.operation_id)
        if not isinstance(payload, Mapping):
            raise TypeError("persisted package operation payload is invalid")
        try:
            request = PackageOperationRequest.parse(AgentOperation(node.kind), payload)
        except Exception as error:
            raise ValueError(
                "persisted package operation payload is invalid"
            ) from error
        if request.deployment_id != node.workload_id:
            raise ValueError("package operation deployment identity is inconsistent")
        deployment = request.deployment_id
        assert deployment is not None
        family = by_target.setdefault((deployment, node.node_id), {})
        if node.kind in family:
            raise ValueError("duplicate package operation for deployment and node")
        family[node.kind] = node

    if not by_target:
        raise ValueError("package graph has no package operations")
    for (deployment, _node_id), operations in by_target.items():
        prepares = operations.get(AgentOperation.PACKAGE_PREPARE.value)
        activates = operations.get(AgentOperation.PACKAGE_ACTIVATE.value)
        health = operations.get(AgentOperation.PACKAGE_HEALTH.value)
        if prepares is None and activates is None and health is not None:
            if health.dependencies:
                raise ValueError("retained package health must not have dependencies")
            continue
        if prepares is None or activates is None or health is None:
            raise ValueError("package graph lifecycle is incomplete")
        if prepares.node_id != activates.node_id or activates.node_id != health.node_id:
            raise ValueError("package graph lifecycle target is inconsistent")
        expected_activation_dependencies = [prepares.operation_id]
        stop = operations.get(AgentOperation.PACKAGE_STOP.value)
        if stop is not None:
            expected_activation_dependencies.append(stop.operation_id)
        if activates.dependencies != tuple(sorted(expected_activation_dependencies)):
            raise ValueError("package activation must depend on preparation and stop")
        if health.dependencies != (activates.operation_id,):
            raise ValueError("package health must depend on activation")
        if activates.compensation_kind not in {
            AgentOperation.PACKAGE_ROLLBACK.value,
            None,
        }:
            raise ValueError("package activation compensation is invalid")
        if activates.compensation_kind == AgentOperation.PACKAGE_ROLLBACK.value and any(
            node.kind == AgentOperation.PACKAGE_ROLLBACK.value
            and node.workload_id == deployment
            for node in nodes
        ):
            # The rollback node is represented by the compensation kind on
            # activation; it must never be dispatched as a primary operation.
            raise ValueError("package rollback must be compensation-only")


def _parse_plan(
    document: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], int, tuple[OperationNode, ...]]:
    if not isinstance(document, Mapping) or set(document) != {
        "base_commit",
        "targets",
        "route_withdrawal_generation",
        "operations",
    }:
        raise ValueError("reconciliation plan fields are invalid")
    base_commit = document["base_commit"]
    if not isinstance(base_commit, str) or _COMMIT.fullmatch(base_commit) is None:
        raise ValueError("base commit is invalid")
    targets = _targets(document["targets"])
    generation = _generation(document["route_withdrawal_generation"])
    raw_operations = document["operations"]
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ValueError("reconciliation operations are required")
    parsed = [_operation(raw) for raw in raw_operations]
    by_id: dict[str, OperationNode] = {}
    for node in parsed:
        if node.operation_id in by_id:
            raise ValueError("reconciliation operation ID is duplicate")
        by_id[node.operation_id] = node
        if node.node_id not in targets:
            raise ValueError("reconciliation operation target is unknown")
    for node in parsed:
        for dependency in node.dependencies:
            required = by_id.get(dependency)
            if required is None:
                raise ValueError("reconciliation operation dependency is unknown")
            cross_workload_gate = (
                node.kind == AgentOperation.NODE_PROBE.value
                and node.workload_id == "node-gate"
                and required.kind == AgentOperation.WORKLOAD_STOP.value
            ) or (
                node.kind == AgentOperation.RELEASE_INSTALL.value
                and required.kind == AgentOperation.NODE_PROBE.value
                and required.workload_id == "node-gate"
            )
            if required.workload_id != node.workload_id and not cross_workload_gate:
                raise ValueError("cross-workload dependency is invalid")
    return base_commit, targets, generation, _topological(by_id)


def _targets(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("reconciliation targets are required")
    if not all(isinstance(item, str) and _NODE_ID.fullmatch(item) for item in value):
        raise ValueError("reconciliation target is invalid")
    targets = tuple(sorted(value))
    if len(targets) != len(set(targets)):
        raise ValueError("reconciliation target is duplicate")
    return targets


def _generation(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("route withdrawal generation is invalid")
    return value


def _operation(raw: Any) -> OperationNode:
    fields = {
        "operation_id",
        "node_id",
        "workload_id",
        "kind",
        "dependencies",
        "compensation_kind",
        "payload_digest",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ValueError("reconciliation operation fields are invalid")
    operation_id = raw["operation_id"]
    node_id = raw["node_id"]
    workload_id = raw["workload_id"]
    kind = raw["kind"]
    dependencies = raw["dependencies"]
    compensation = raw["compensation_kind"]
    payload_digest = raw["payload_digest"]
    if (
        not isinstance(operation_id, str)
        or _OPERATION_ID.fullmatch(operation_id) is None
    ):
        raise ValueError("reconciliation operation ID is invalid")
    if not isinstance(node_id, str) or _NODE_ID.fullmatch(node_id) is None:
        raise ValueError("reconciliation operation target is invalid")
    if not isinstance(workload_id, str) or _WORKLOAD_ID.fullmatch(workload_id) is None:
        raise ValueError("reconciliation workload ID is invalid")
    if not isinstance(kind, str) or kind not in _IMPLEMENTED_OPERATIONS:
        raise ValueError(
            "reconciliation operation kind is absent from the agent registry"
        )
    if compensation is not None and (
        not isinstance(compensation, str) or compensation not in _IMPLEMENTED_OPERATIONS
    ):
        raise ValueError(
            "reconciliation compensation kind is absent from the agent registry"
        )
    if not isinstance(payload_digest, str) or _DIGEST.fullmatch(payload_digest) is None:
        raise ValueError("reconciliation payload digest is invalid")
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and _OPERATION_ID.fullmatch(item) for item in dependencies
    ):
        raise ValueError("reconciliation operation dependencies are invalid")
    ordered_dependencies = tuple(sorted(dependencies))
    if len(ordered_dependencies) != len(set(ordered_dependencies)):
        raise ValueError("reconciliation operation dependency is duplicate")
    return OperationNode(
        operation_id=operation_id,
        node_id=node_id,
        workload_id=workload_id,
        kind=kind,
        dependencies=ordered_dependencies,
        compensation_kind=compensation,
        payload_digest=payload_digest,
    )


def _topological(by_id: Mapping[str, OperationNode]) -> tuple[OperationNode, ...]:
    unresolved = {
        operation_id: set(node.dependencies) for operation_id, node in by_id.items()
    }
    ordered: list[OperationNode] = []
    while unresolved:
        ready = sorted(
            operation_id
            for operation_id, dependencies in unresolved.items()
            if not dependencies
        )
        if not ready:
            raise ValueError("reconciliation operation graph contains a cycle")
        for operation_id in ready:
            ordered.append(by_id[operation_id])
            del unresolved[operation_id]
        for dependencies in unresolved.values():
            dependencies.difference_update(ready)
    return tuple(ordered)


def _graph_document(
    base_commit: str,
    targets: tuple[str, ...],
    nodes: tuple[OperationNode, ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "base_commit": base_commit,
        "targets": list(targets),
        "nodes": [node.to_document() for node in nodes],
    }


def _digest(document: Mapping[str, object]) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
