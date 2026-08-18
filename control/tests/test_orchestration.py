from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.models import Base, Reconciliation
from vonk_control.orchestration import ReconciliationOrchestrator

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
BASE_COMMIT = "a" * 40


def _digest(document: object) -> str:
    return hashlib.sha256(canonical_message(document)).hexdigest()


def _persisted_gate_plan() -> tuple[dict[str, object], dict[str, object], str, str]:
    stop_payload: dict[str, object] = {}
    gate_payload = {"require_active_nvidia_compute_processes": 0}
    install_payload: dict[str, object] = {}
    nodes: list[dict[str, object]] = []
    for suffix, node_id in (("a", NODE_A), ("b", NODE_B)):
        nodes.append(
            {
                "operation_id": f"old:{suffix}:stop",
                "node_id": node_id,
                "workload_id": "old",
                "kind": "workload.stop",
                "dependencies": [],
                "compensation_kind": None,
                "payload_digest": _digest(stop_payload),
            }
        )
    stop_ids = ["old:a:stop", "old:b:stop"]
    for suffix, node_id in (("a", NODE_A), ("b", NODE_B)):
        nodes.append(
            {
                "operation_id": f"gate:{suffix}",
                "node_id": node_id,
                "workload_id": "node-gate",
                "kind": "node.probe",
                "dependencies": stop_ids,
                "compensation_kind": None,
                "payload_digest": _digest(gate_payload),
            }
        )
    gate_ids = ["gate:a", "gate:b"]
    for suffix, node_id in (("a", NODE_A), ("b", NODE_B)):
        nodes.append(
            {
                "operation_id": f"new:{suffix}:install",
                "node_id": node_id,
                "workload_id": "new",
                "kind": "release.install",
                "dependencies": gate_ids,
                "compensation_kind": None,
                "payload_digest": _digest(install_payload),
            }
        )
    graph: dict[str, object] = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "targets": [NODE_A, NODE_B],
        "nodes": nodes,
    }
    payloads = {
        "old:a:stop": stop_payload,
        "old:b:stop": stop_payload,
        "gate:a": gate_payload,
        "gate:b": gate_payload,
        "new:a:install": install_payload,
        "new:b:install": install_payload,
    }
    resolved: dict[str, object] = {
        "commit": BASE_COMMIT,
        "targets": [NODE_A, NODE_B],
        "placements": {},
        "routes": {},
        "releases": {},
        "workload_groups": {},
        "input_digests": {"fleet": "f" * 64},
        "fleet_evidence_digest": "e" * 64,
        "operation_graph": graph,
        "operation_payloads": payloads,
        "agent_protocol_range": [1, 1],
    }
    return graph, resolved, _digest(graph), _digest(resolved)


def _corrupt_persisted_plan(
    corruption: str,
) -> tuple[dict[str, object], dict[str, object], str, str, int | None]:
    graph, resolved, _, _ = _persisted_gate_plan()
    graph = deepcopy(graph)
    resolved = deepcopy(resolved)
    nodes = graph["nodes"]
    payloads = resolved["operation_payloads"]
    assert isinstance(nodes, list) and isinstance(payloads, dict)
    by_id = {node["operation_id"]: node for node in nodes}
    completion_generation: int | None = 1
    if corruption == "empty-gate-payload":
        payloads["gate:a"] = {}
        by_id["gate:a"]["payload_digest"] = _digest({})
    elif corruption == "missing-gate":
        nodes.remove(by_id["gate:b"])
        payloads.pop("gate:b")
        for install_id in ("new:a:install", "new:b:install"):
            by_id[install_id]["dependencies"] = ["gate:a"]
    elif corruption == "partial-gate-stops":
        by_id["gate:a"]["dependencies"] = ["old:a:stop"]
    elif corruption == "partial-install-gates":
        by_id["new:a:install"]["dependencies"] = ["gate:a"]
    elif corruption == "duplicate-gate-target":
        duplicate = deepcopy(by_id["gate:a"])
        duplicate["operation_id"] = "gate:a:duplicate"
        nodes.insert(nodes.index(by_id["new:a:install"]), duplicate)
        payloads["gate:a:duplicate"] = deepcopy(payloads["gate:a"])
        for install_id in ("new:a:install", "new:b:install"):
            by_id[install_id]["dependencies"].append("gate:a:duplicate")
    elif corruption == "extra-gate-target":
        extra = deepcopy(by_id["gate:a"])
        extra["operation_id"] = "gate:extra"
        extra["node_id"] = "spk_" + "c" * 32
        graph["targets"].append(extra["node_id"])
        resolved["targets"].append(extra["node_id"])
        nodes.insert(nodes.index(by_id["new:a:install"]), extra)
        payloads["gate:extra"] = deepcopy(payloads["gate:a"])
        for install_id in ("new:a:install", "new:b:install"):
            by_id[install_id]["dependencies"].append("gate:extra")
    elif corruption == "ordinary-probe":
        ordinary = deepcopy(by_id["gate:a"])
        ordinary["operation_id"] = "model:probe"
        ordinary["workload_id"] = "model"
        ordinary["dependencies"] = []
        nodes.insert(nodes.index(by_id["new:a:install"]), ordinary)
        payloads["model:probe"] = deepcopy(payloads["gate:a"])
    elif corruption == "reserved-gate-nonprobe":
        reserved = deepcopy(by_id["gate:a"])
        reserved["operation_id"] = "gate:health"
        reserved["kind"] = "workload.health"
        reserved["dependencies"] = []
        nodes.insert(nodes.index(by_id["new:a:install"]), reserved)
        payloads["gate:health"] = deepcopy(payloads["gate:a"])
    else:
        health_payload: dict[str, object] = {}
        health = {
            "operation_id": "model:health",
            "node_id": NODE_A,
            "workload_id": "model",
            "kind": "workload.health",
            "dependencies": [],
            "compensation_kind": None,
            "payload_digest": _digest(health_payload),
        }
        graph["targets"] = [NODE_A]
        graph["nodes"] = [health]
        resolved["targets"] = [NODE_A]
        resolved["operation_payloads"] = {"model:health": health_payload}
        resolved.pop("workload_groups")
        completion_generation = None
    resolved["operation_graph"] = graph
    return graph, resolved, _digest(graph), _digest(resolved), completion_generation


def distributed_plan() -> dict[str, object]:
    return {
        "base_commit": BASE_COMMIT,
        "targets": [NODE_B, NODE_A],
        "route_withdrawal_generation": 3,
        "operations": [
            {
                "operation_id": "worker:stop",
                "node_id": NODE_B,
                "workload_id": "model-a",
                "kind": "workload.stop",
                "dependencies": ["head:stop"],
                "compensation_kind": None,
                "payload_digest": "4" * 64,
            },
            {
                "operation_id": "head:start",
                "node_id": NODE_A,
                "workload_id": "model-a",
                "kind": "workload.start",
                "dependencies": ["worker:start"],
                "compensation_kind": "workload.stop",
                "payload_digest": "2" * 64,
            },
            {
                "operation_id": "worker:start",
                "node_id": NODE_B,
                "workload_id": "model-a",
                "kind": "workload.start",
                "dependencies": [],
                "compensation_kind": "workload.stop",
                "payload_digest": "1" * 64,
            },
            {
                "operation_id": "head:stop",
                "node_id": NODE_A,
                "workload_id": "model-a",
                "kind": "workload.stop",
                "dependencies": ["head:start"],
                "compensation_kind": None,
                "payload_digest": "3" * 64,
            },
        ],
    }


@pytest.fixture
def planner(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orchestration.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    orchestrator = ReconciliationOrchestrator(
        sessions,
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )
    return orchestrator, sessions


def test_graph_is_dependency_ordered_and_digest_stable(planner) -> None:
    orchestrator, _ = planner

    graph = orchestrator.plan(distributed_plan())
    repeated = orchestrator.plan(distributed_plan())

    assert graph.dependencies("head:start") == ("worker:start",)
    assert graph.dependencies("worker:stop") == ("head:stop",)
    assert tuple(node.operation_id for node in graph.nodes) == (
        "worker:start",
        "head:start",
        "head:stop",
        "worker:stop",
    )
    assert graph.targets == (NODE_A, NODE_B)
    assert graph.digest == repeated.digest == (
        "def0e95a03404d2efb9ad6ab53ce2dbc8040d6cc0d40c337a533783b0cad3317"
    )
    assert graph.document == repeated.document


def test_independent_nodes_are_sorted_by_canonical_operation_id(planner) -> None:
    orchestrator, _ = planner
    document = distributed_plan()
    document["operations"] = [
        {
            "operation_id": "z:probe",
            "node_id": NODE_B,
            "workload_id": "model-a",
            "kind": "node.probe",
            "dependencies": [],
            "compensation_kind": None,
            "payload_digest": "6" * 64,
        },
        {
            "operation_id": "a:probe",
            "node_id": NODE_A,
            "workload_id": "model-a",
            "kind": "node.probe",
            "dependencies": [],
            "compensation_kind": None,
            "payload_digest": "5" * 64,
        },
    ]

    graph = orchestrator.plan(document)

    assert tuple(node.operation_id for node in graph.nodes) == (
        "a:probe",
        "z:probe",
    )


def test_zero_compute_gate_is_the_only_cross_workload_barrier(planner) -> None:
    orchestrator, _ = planner
    document = distributed_plan()
    document["operations"] = [
        {
            "operation_id": "old:stop",
            "node_id": NODE_A,
            "workload_id": "model-a",
            "kind": "workload.stop",
            "dependencies": [],
            "compensation_kind": None,
            "payload_digest": "1" * 64,
        },
        {
            "operation_id": "node:gate",
            "node_id": NODE_A,
            "workload_id": "node-gate",
            "kind": "node.probe",
            "dependencies": ["old:stop"],
            "compensation_kind": None,
            "payload_digest": "2" * 64,
        },
        {
            "operation_id": "new:install",
            "node_id": NODE_B,
            "workload_id": "model-b",
            "kind": "release.install",
            "dependencies": ["node:gate"],
            "compensation_kind": None,
            "payload_digest": "3" * 64,
        },
    ]

    graph = orchestrator.plan(document)

    assert graph.dependencies("node:gate") == ("old:stop",)
    assert graph.dependencies("new:install") == ("node:gate",)


def test_package_operations_are_not_control_plane_graph_operations(planner) -> None:
    orchestrator, sessions = planner
    document = distributed_plan()
    document["targets"] = [NODE_A]
    document["operations"] = [
        {
            "operation_id": "removed-package:prepare",
            "node_id": NODE_A,
            "workload_id": "removed-package",
            "kind": "package.prepare",
            "dependencies": [],
            "compensation_kind": None,
            "payload_digest": "1" * 64,
        }
    ]

    with pytest.raises(ValueError, match="agent registry"):
        orchestrator.plan(document)

    with sessions() as session:
        assert session.scalars(select(Reconciliation)).all() == []


@pytest.mark.parametrize(
    "corruption",
    (
        "empty-gate-payload",
        "missing-gate",
        "partial-gate-stops",
        "partial-install-gates",
        "duplicate-gate-target",
        "extra-gate-target",
        "ordinary-probe",
        "reserved-gate-nonprobe",
        "nonmutating-missing-workload-groups",
    ),
)
def test_persisted_plan_consumers_reject_semantic_gate_and_nonmutating_corruption(
    planner, corruption: str
) -> None:
    orchestrator, sessions = planner
    graph, resolved, graph_digest, plan_digest, completion_generation = (
        _corrupt_persisted_plan(corruption)
    )
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id=f"restart-{corruption}",
                base_commit=BASE_COMMIT,
                status="succeeded",
                summary={},
                graph=graph,
                graph_digest=graph_digest,
                plan_digest=plan_digest,
                resolved_plan=resolved,
                current_phase="completed",
                route_withdrawal_generation=0,
                completion_generation=completion_generation,
                created_at=datetime(2026, 8, 3, tzinfo=UTC),
            )
        )

    with pytest.raises((TypeError, ValueError), match="persisted resolved plan"):
        orchestrator.resolved_plan(plan_digest)


def test_persisted_plan_consumers_reject_deleted_package_operations(planner) -> None:
    orchestrator, sessions = planner
    package_payload = {
        "schema_version": 1,
        "deployment_id": "removed-package",
        "release_digest": "a" * 64,
        "deployment_digest": "b" * 64,
    }
    package_node = {
        "operation_id": "removed-package:prepare",
        "node_id": NODE_A,
        "workload_id": "removed-package",
        "kind": "package.prepare",
        "dependencies": [],
        "compensation_kind": None,
        "payload_digest": _digest(package_payload),
    }
    graph: dict[str, object] = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "targets": [NODE_A],
        "nodes": [package_node],
    }
    resolved: dict[str, object] = {
        "commit": BASE_COMMIT,
        "targets": [NODE_A],
        "placements": {},
        "routes": {},
        "releases": {},
        "workload_groups": {},
        "input_digests": {"fleet": "f" * 64},
        "fleet_evidence_digest": "e" * 64,
        "operation_graph": graph,
        "operation_payloads": {"removed-package:prepare": package_payload},
        "agent_protocol_range": [1, 1],
    }
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id="persisted-package-operation",
                base_commit=BASE_COMMIT,
                status="succeeded",
                summary={},
                graph=graph,
                graph_digest=_digest(graph),
                plan_digest=_digest(resolved),
                resolved_plan=resolved,
                current_phase="completed",
                route_withdrawal_generation=0,
                completion_generation=1,
                created_at=datetime(2026, 8, 3, tzinfo=UTC),
            )
        )

    with pytest.raises(ValueError, match="persisted resolved plan graph"):
        orchestrator.resolved_plan(_digest(resolved))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("cycle", "cycle"),
        ("unknown-target", "target"),
        ("duplicate-operation", "duplicate"),
        ("unknown-dependency", "dependency"),
        ("cross-workload", "workload"),
        ("unsupported-kind", "operation kind"),
        ("unsupported-compensation", "compensation"),
    ],
)
def test_invalid_graphs_are_rejected_without_persistence(
    planner, mutation: str, message: str
) -> None:
    orchestrator, sessions = planner
    document = distributed_plan()
    operations = document["operations"]
    assert isinstance(operations, list)
    by_id = {item["operation_id"]: item for item in operations}
    if mutation == "cycle":
        by_id["worker:start"]["dependencies"] = ["worker:stop"]
    elif mutation == "unknown-target":
        by_id["worker:start"]["node_id"] = "spk_" + "c" * 32
    elif mutation == "duplicate-operation":
        operations.append(deepcopy(by_id["worker:start"]))
    elif mutation == "unknown-dependency":
        by_id["head:start"]["dependencies"] = ["missing:start"]
    elif mutation == "cross-workload":
        by_id["head:start"]["workload_id"] = "model-b"
    elif mutation == "unsupported-kind":
        by_id["worker:start"]["kind"] = "system.exec"
    else:
        by_id["worker:start"]["compensation_kind"] = "system.exec"

    with pytest.raises(ValueError, match=message):
        orchestrator.plan(document)

    with sessions() as session:
        assert session.scalars(select(Reconciliation)).all() == []


def test_plan_persists_immutable_canonical_graph_and_progress_fields(planner) -> None:
    orchestrator, sessions = planner

    graph = orchestrator.plan(distributed_plan())

    with sessions() as session:
        stored = session.get(Reconciliation, graph.reconciliation_id)
        assert stored is not None
        assert stored.base_commit == BASE_COMMIT
        assert stored.graph == graph.document
        assert stored.graph_digest == graph.digest
        assert stored.status == "planned"
        assert stored.current_phase == "planned"
        assert stored.route_withdrawal_generation == 3
        assert stored.terminal_reason is None
        assert stored.summary == {
            "operation_count": 4,
            "target_count": 2,
        }


def test_advance_changes_mutable_state_but_cancel_is_disabled(planner) -> None:
    orchestrator, sessions = planner
    graph = orchestrator.plan(distributed_plan())
    original_document = deepcopy(graph.document)

    orchestrator.advance(
        graph.reconciliation_id,
        "routes-withdrawn",
        route_withdrawal_generation=4,
    )
    orchestrator.advance(graph.reconciliation_id, "dispatching")

    with sessions() as session:
        stored = session.get(Reconciliation, graph.reconciliation_id)
        assert stored is not None
        assert stored.status == "running"
        assert stored.current_phase == "dispatching"
        assert stored.route_withdrawal_generation == 4
        assert stored.graph == original_document
        assert stored.graph_digest == graph.digest

    with pytest.raises(ValueError, match="transition"):
        orchestrator.advance(graph.reconciliation_id, "completed")

    with pytest.raises(RuntimeError, match="durable cancellation"):
        orchestrator.cancel(graph.reconciliation_id, "operator cancelled rollout")

    with sessions() as session:
        stored = session.get(Reconciliation, graph.reconciliation_id)
        assert stored is not None
        assert stored.status == "running"
        assert stored.current_phase == "dispatching"
        assert stored.terminal_reason is None
        assert stored.graph == original_document
        assert stored.graph_digest == graph.digest

    orchestrator.advance(graph.reconciliation_id, "accepting")


def test_successful_completion_assigns_unique_monotonic_causal_generations(
    planner,
) -> None:
    orchestrator, sessions = planner
    first = orchestrator.plan(distributed_plan())
    second = orchestrator.plan(distributed_plan())

    for graph in (first, second):
        for phase in ("routes-withdrawn", "dispatching", "accepting", "completed"):
            orchestrator.advance(graph.reconciliation_id, phase)

    pending = orchestrator.plan(distributed_plan())
    with sessions() as session:
        stored = {
            item.id: item
            for item in session.scalars(select(Reconciliation)).all()
        }
        assert stored[first.reconciliation_id].completion_generation == 1
        assert stored[second.reconciliation_id].completion_generation == 2
        assert stored[pending.reconciliation_id].completion_generation is None
