from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Base,
    Job,
    PackageRollout,
    PackageRolloutNode,
)
from vonk_control.package_rollout_worker import PackageRolloutWorker

NODE = "spk_" + "1" * 32
NOW = datetime(2026, 8, 6, tzinfo=UTC)


def _sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'package-worker.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _rollout(
    sessions,
    *,
    state: str = "planned",
    node_state: str = "pending",
    operation_state: str | None = None,
    created_at: datetime = NOW,
) -> str:
    rollout_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    operation_id = str(uuid.uuid4()) if operation_state is not None else None
    with sessions.begin() as session:
        if session.get(AgentNode, NODE) is None:
            session.add(AgentNode(node_id=NODE, state="active", capabilities=[]))
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid.uuid4()),
                kind="package.rollout",
                state="running",
                actor="test",
                base_commit="a" * 40,
                targets=[NODE],
                payload_digest="1" * 64,
                payload={"rollout_id": rollout_id},
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.add(
            PackageRollout(
                id=rollout_id,
                job_id=job_id,
                deployment_id="generic-workload",
                deployment_digest="2" * 64,
                release_digest="3" * 64,
                base_commit="a" * 40,
                policy_digest="4" * 64,
                tuf_target_digest="5" * 64,
                fleet_digest="6" * 64,
                topology_digest="7" * 64,
                plan_digest=uuid.uuid4().hex * 2,
                state=state,
                actor="test",
                plan={},
                progress={"accepted": 0, "total": 1},
                current_batch=0,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.add(
            PackageRolloutNode(
                rollout_id=rollout_id,
                node_id=NODE,
                batch_index=0,
                node_order=0,
                is_canary=True,
                state=node_state,
                operation_kind="package.prepare",
                graph_operation_id="node-prepare",
                operation_key="node-prepare",
                operation_id=operation_id,
                operation_history=[],
                expected_payload_digest="9" * 64,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        if operation_id is not None:
            session.add(
                AgentOperation(
                    id=operation_id,
                    parent_job_id=job_id,
                    node_id=NODE,
                    kind="package.prepare",
                    payload_digest="a" * 64,
                    payload={},
                    base_commit="a" * 40,
                    state=operation_state,
                    current_attempt=1,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    return rollout_id


class Orchestrator:
    def __init__(self, entered: threading.Event | None = None) -> None:
        self.calls: list[str] = []
        self.entered = entered
        self.release = threading.Event()

    def advance(self, rollout_id: str) -> str:
        self.calls.append(rollout_id)
        if self.entered is not None:
            self.entered.set()
            self.release.wait(timeout=2)
        return "running"


def test_worker_advances_oldest_planned_package_rollout(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    oldest = _rollout(sessions)
    _rollout(sessions, created_at=NOW + timedelta(seconds=1))
    orchestrator = Orchestrator()

    assert PackageRolloutWorker(sessions, orchestrator).tick() is True
    assert orchestrator.calls == [oldest]


def test_worker_waits_for_inflight_agent_operation(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    _rollout(
        sessions,
        state="running",
        node_state="preparing",
        operation_state="running",
    )
    orchestrator = Orchestrator()

    assert PackageRolloutWorker(sessions, orchestrator).tick() is False
    assert orchestrator.calls == []


def test_worker_advances_after_agent_operation_is_terminal(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    rollout_id = _rollout(
        sessions,
        state="running",
        node_state="prepared",
        operation_state="succeeded",
    )
    orchestrator = Orchestrator()

    assert PackageRolloutWorker(sessions, orchestrator).tick() is True
    assert orchestrator.calls == [rollout_id]


def test_workers_cannot_advance_same_package_rollout_concurrently(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    rollout_id = _rollout(sessions)
    entered = threading.Event()
    orchestrator = Orchestrator(entered)
    first = PackageRolloutWorker(sessions, orchestrator)
    second = PackageRolloutWorker(sessions, orchestrator)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first.tick)
        assert entered.wait(timeout=2)
        two = pool.submit(second.tick)
        assert two.result(timeout=2) is False
        orchestrator.release.set()
        assert one.result(timeout=2) is True

    assert orchestrator.calls == [rollout_id]
