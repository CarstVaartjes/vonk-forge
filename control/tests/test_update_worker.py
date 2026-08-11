from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.models import AgentNode, Base, UpdateRollout, UpdateRolloutNode
from vonk_control.update_routes import RouteRenewalResult, UpdateRouteError
from vonk_control.update_worker import UpdateRolloutWorker

NODE = "spk_" + "1" * 32
NOW = datetime(2026, 8, 6, tzinfo=UTC)


def _sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'update-worker.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _rollout(
    sessions,
    *,
    state: str,
    created_at: datetime = NOW,
    node_state: str = "pending",
    rollback_admin_grant: dict[str, object] | None = None,
) -> str:
    rollout_id = str(uuid.uuid4())
    with sessions.begin() as session:
        if session.get(AgentNode, NODE) is None:
            session.add(AgentNode(node_id=NODE, state="active", capabilities=[]))
        session.add(
            UpdateRollout(
                id=rollout_id,
                state=state,
                plan_digest=uuid.uuid4().hex * 2,
                release_digest="1" * 64,
                base_commit="a" * 40,
                fleet_digest="2" * 64,
                topology_digest="3" * 64,
                agent_input_digest="4" * 64,
                target_platform_version="1.0.0",
                target_build_digest="sha256:" + "5" * 64,
                rollback_admin_grant=rollback_admin_grant,
                plan={},
                current_batch=0,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        mutating = node_state not in {"pending", "routes-withdrawn"}
        session.add(
            UpdateRolloutNode(
                rollout_id=rollout_id,
                node_id=NODE,
                batch_index=0,
                node_order=0,
                is_canary=True,
                state=node_state,
                operation_history=[],
                source_identity_digest="6" * 64,
                target_artifact_digest="7" * 64,
                route_withdrawal_evidence_digest=(
                    "8" * 64 if mutating or state == "paused" else None
                ),
                dispatch_at=created_at if mutating else None,
                activation_deadline=(
                    created_at + timedelta(seconds=600) if mutating else None
                ),
                created_at=created_at,
                updated_at=created_at,
            )
        )
    return rollout_id


class Orchestrator:
    def __init__(self, *, entered: threading.Event | None = None) -> None:
        self.calls: list[str] = []
        self.rollback_calls: list[tuple[str, str, str]] = []
        self.entered = entered
        self.release = threading.Event()

    def advance(self, rollout_id: str) -> str:
        self.calls.append(rollout_id)
        if self.entered is not None:
            self.entered.set()
            self.release.wait(timeout=2)
        return "advanced"

    def begin_rollback(self, rollout_id: str, actor: str, request_id: str) -> str:
        self.rollback_calls.append((rollout_id, actor, request_id))
        return "rolling-back"


class Routes:
    def __init__(self, result: RouteRenewalResult | None = None) -> None:
        self.result = result or RouteRenewalResult("not-active", None)
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []
        self.error: Exception | None = None

    def renew_if_active(self, rollout_id, batch_index, targets):
        self.calls.append((rollout_id, batch_index, targets))
        if self.error is not None:
            raise self.error
        return self.result


class GrantRefresher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []
        self.error: Exception | None = None

    def refresh_update_grant(self, rollout_id, batch_index, node_ids):
        self.calls.append((rollout_id, batch_index, node_ids))
        if self.error is not None:
            raise self.error
        return {"claims": {}, "signature": "test"}


def _worker(sessions, orchestrator, routes, grants=None):
    return UpdateRolloutWorker(
        sessions,
        orchestrator,
        routes,
        grants or GrantRefresher(),
    )


def test_restart_selects_oldest_persisted_actionable_rollout(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    oldest = _rollout(sessions, state="planned", created_at=NOW)
    _rollout(sessions, state="planned", created_at=NOW + timedelta(seconds=1))
    orchestrator = Orchestrator()

    restarted = _worker(sessions, orchestrator, Routes())

    assert restarted.tick() is True
    assert orchestrator.calls == [oldest]


def test_concurrent_workers_cannot_advance_the_same_rollout(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    rollout_id = _rollout(sessions, state="planned")
    entered = threading.Event()
    orchestrator = Orchestrator(entered=entered)
    first = _worker(sessions, orchestrator, Routes())
    second = _worker(sessions, orchestrator, Routes())

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first.tick)
        assert entered.wait(timeout=2)
        two = pool.submit(second.tick)
        assert two.result(timeout=2) is False
        orchestrator.release.set()
        assert one.result(timeout=2) is True

    assert orchestrator.calls == [rollout_id]


def test_active_route_is_renewed_before_long_running_update_advances(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    rollout_id = _rollout(sessions, state="updating", node_state="updating")
    routes = Routes(RouteRenewalResult("renewed", "9" * 64))
    orchestrator = Orchestrator()

    worker = _worker(sessions, orchestrator, routes)

    assert worker.tick() is True
    assert routes.calls == [(rollout_id, 0, (NODE,))]
    assert orchestrator.calls == [rollout_id]


def test_route_renewal_failure_never_advances_update(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    rollout_id = _rollout(sessions, state="soaking", node_state="soaking")
    routes = Routes()
    routes.error = UpdateRouteError("fresh endpoint evidence unavailable")
    orchestrator = Orchestrator()

    worker = _worker(sessions, orchestrator, routes)

    assert worker.tick() is True
    assert routes.calls == [(rollout_id, 0, (NODE,))]
    assert orchestrator.calls == []
    assert worker.last_error == "fresh endpoint evidence unavailable"


def test_paused_compensation_renews_but_inactive_pause_is_not_actionable(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path)
    paused = _rollout(sessions, state="paused", node_state="failed")
    active_routes = Routes(RouteRenewalResult("renewed", "9" * 64))
    orchestrator = Orchestrator()

    assert _worker(sessions, orchestrator, active_routes).tick() is True
    assert orchestrator.calls == [paused]

    inactive_routes = Routes(RouteRenewalResult("not-active", None))
    assert _worker(sessions, Orchestrator(), inactive_routes).tick() is False


def test_worker_consumes_api_authorized_rollback_without_private_grant_key(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path)
    nonce = "30000000-0000-4000-8000-000000000003"
    rollout_id = _rollout(
        sessions,
        state="paused",
        node_state="failed",
        rollback_admin_grant={"claims": {"nonce": nonce}},
    )
    routes = Routes(RouteRenewalResult("renewed", "9" * 64))
    orchestrator = Orchestrator()

    assert _worker(sessions, orchestrator, routes).tick() is True
    assert orchestrator.rollback_calls == [
        (rollout_id, "control-worker", nonce)
    ]
    assert orchestrator.calls == []


def test_withdrawal_pending_is_advanced_instead_of_treated_as_renewal_failure(
    tmp_path,
) -> None:
    sessions = _sessions(tmp_path)
    rollout_id = _rollout(sessions, state="withdrawing")
    routes = Routes(RouteRenewalResult("withdrawal-pending", None))
    orchestrator = Orchestrator()

    assert _worker(sessions, orchestrator, routes).tick() is True
    assert routes.calls == [(rollout_id, 0, (NODE,))]
    assert orchestrator.calls == [rollout_id]


def test_planned_batch_refreshes_exact_grant_before_any_advance(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    rollout_id = _rollout(sessions, state="planned")
    grants = GrantRefresher()
    orchestrator = Orchestrator()

    assert _worker(sessions, orchestrator, Routes(), grants).tick() is True

    assert grants.calls == [(rollout_id, 0, (NODE,))]
    assert orchestrator.calls == [rollout_id]


def test_grant_refresh_failure_never_withdraws_or_advances(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    rollout_id = _rollout(sessions, state="planned")
    grants = GrantRefresher()
    grants.error = RuntimeError("grant authority unavailable")
    orchestrator = Orchestrator()
    routes = Routes()

    worker = _worker(sessions, orchestrator, routes, grants)

    assert worker.tick() is True
    assert grants.calls == [(rollout_id, 0, (NODE,))]
    assert routes.calls == []
    assert orchestrator.calls == []
    assert worker.last_error == "grant authority unavailable"
