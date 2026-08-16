from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.models import (
    Base,
    Reconciliation,
    RoutePublication,
    RoutePublicationOwner,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.route_runtime import (
    AcceptedEndpointEvidence,
    ActivationMarker,
    AtomicRouteBundlePublisher,
    RouteBundleRequest,
    RouteRuntimeError,
    endpoint_evidence_digest,
)
from vonk_control.update_routes import (
    ProductionUpdateRouteBoundary,
    RouteDrainReceipt,
    RouteRenewalResult,
    UpdateRouteError,
)

NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32
NODE_C = "spk_" + "3" * 32
RECONCILIATION_ID = "db229ad6-560e-4654-94ab-0bf40a0e544f"
PLAN_DIGEST = "a" * 64
EVIDENCE_DIGEST = "b" * 64


class Clock:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class AdvancingClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.now
        self.now += timedelta(milliseconds=1)
        return current


def _quota() -> dict[str, int]:
    return {"requests_per_minute": 30, "tokens_per_minute": 10_000}


def _route(workload: str, nodes: list[str], entrypoint: str, port: int) -> dict[str, object]:
    quota = _quota()
    return {
        "workload_id": workload,
        "nodes": nodes,
        "entrypoint_node_id": entrypoint,
        "scheme": "http",
        "port": port,
        "path": "/v1",
        "quota": quota,
        "quota_digest": hashlib.sha256(canonical_message(quota)).hexdigest(),
    }


ROUTES: dict[str, object] = {
    "distributed": _route("distributed", [NODE_A, NODE_B], NODE_A, 8000),
    "solo": _route("solo", [NODE_C], NODE_C, 8001),
}


def _request(clock: Clock, addresses: dict[str, str]) -> RouteBundleRequest:
    now = clock()
    endpoints: dict[str, AcceptedEndpointEvidence] = {}
    for workload, node_id in (("distributed", NODE_A), ("solo", NODE_C)):
        verify_digest = ("c" if node_id == NODE_A else "d") * 64
        operation_id = f"{workload}:{node_id}:workload.verify"
        endpoints[node_id] = AcceptedEndpointEvidence(
            node_id=node_id,
            address=addresses[node_id],
            observed_at=now,
            operation_id=operation_id,
            verify_evidence_digest=verify_digest,
            evidence_digest=endpoint_evidence_digest(
                node_id=node_id,
                address=addresses[node_id],
                observed_at=now,
                operation_id=operation_id,
                verify_evidence_digest=verify_digest,
            ),
        )
    return RouteBundleRequest(
        reconciliation_id=RECONCILIATION_ID,
        plan_digest=PLAN_DIGEST,
        evidence_set_digest=EVIDENCE_DIGEST,
        routes=ROUTES,
        endpoints=endpoints,
        expires_at=now + timedelta(seconds=180),
        base_commit="e" * 40,
    )


def _harness(tmp_path: Path, *, clock=None):
    clock = clock or Clock()
    addresses = {NODE_A: "10.0.0.11", NODE_C: "10.0.0.13"}
    route_root = tmp_path / "routes"
    publisher = AtomicRouteBundlePublisher(
        route_root,
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
    )
    initial = publisher.publish(_request(clock, addresses))
    engine = create_engine(f"sqlite:///{tmp_path / 'control.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id=RECONCILIATION_ID,
                base_commit="e" * 40,
                status="succeeded",
                summary={},
                plan_digest=PLAN_DIGEST,
                current_phase="completed",
                created_at=clock(),
            )
        )
        session.add(
            RoutePublication(
                reconciliation_id=RECONCILIATION_ID,
                state="completed",
                generation=initial.generation,
                plan_digest=PLAN_DIGEST,
                evidence_digest=EVIDENCE_DIGEST,
                route_digest=initial.routes_sha256,
                litellm_digest=initial.litellm_sha256,
                bundle_digest=initial.manifest_sha256,
                activation_marker=json.loads(initial.canonical_bytes()),
                activation_marker_digest=initial.digest,
                lease_issued_at=datetime.fromisoformat(initial.issued_at),
                lease_expires_at=datetime.fromisoformat(initial.expires_at),
            )
        )
        session.add(
            RoutePublicationOwner(
                singleton_id=1,
                reconciliation_id=RECONCILIATION_ID,
                owner_generation=7,
                updated_at=clock(),
            )
        )

    def load(_session: Session, reconciliation_id: str) -> RouteBundleRequest:
        assert reconciliation_id == RECONCILIATION_ID
        return _request(clock, addresses)

    boundary = ProductionUpdateRouteBoundary(
        sessions,
        publisher,
        route_root=route_root,
        request_loader=load,
        clock=clock,
    )
    return boundary, publisher, sessions, clock, addresses, route_root


def _active_documents(route_root: Path, marker: ActivationMarker):
    generation = route_root / "generations" / marker.directory
    return (
        json.loads((generation / "routes.json").read_bytes()),
        json.loads((generation / "litellm.json").read_bytes()),
    )


def test_withdraw_removes_distributed_route_but_preserves_unaffected_route(
    tmp_path: Path,
) -> None:
    boundary, publisher, _sessions, _clock, _addresses, route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())

    receipt = boundary.withdraw(rollout_id, 0, (NODE_B,))
    retried = boundary.withdraw(rollout_id, 0, (NODE_B,))

    marker = publisher.inspect()
    routes, litellm = _active_documents(route_root, marker)
    assert isinstance(receipt, RouteDrainReceipt)
    assert isinstance(retried, RouteDrainReceipt)
    assert receipt.route_digest == retried.route_digest == marker.digest
    assert receipt.targets == (NODE_B,)
    assert set(routes["routes"]) == {"solo"}
    assert [item["model_name"] for item in litellm["model_list"]] == ["solo"]


def test_restore_rebuilds_all_routes_from_fresh_authoritative_endpoint_evidence(
    tmp_path: Path,
) -> None:
    boundary, publisher, sessions, clock, addresses, route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    boundary.withdraw(rollout_id, 0, (NODE_A, NODE_B, NODE_C))
    withdrawn = publisher.inspect()
    withdrawn_routes, _litellm = _active_documents(route_root, withdrawn)
    assert withdrawn.state == "maintenance"
    assert withdrawn_routes["routes"] == {}
    clock.now += timedelta(seconds=10)
    addresses[NODE_A] = "10.0.0.21"

    receipt = boundary.restore(rollout_id, 0, (NODE_A, NODE_B, NODE_C))
    retried = boundary.restore(rollout_id, 0, (NODE_A, NODE_B, NODE_C))

    restored = publisher.inspect()
    routes, _litellm = _active_documents(route_root, restored)
    assert receipt == retried == restored.digest
    assert routes["routes"]["distributed"]["address"] == "10.0.0.21"
    assert set(routes["routes"]) == {"distributed", "solo"}
    with sessions() as session:
        publication = session.get(RoutePublication, RECONCILIATION_ID)
        assert publication is not None
        assert publication.activation_marker_digest == restored.digest
        assert publication.generation == restored.generation


def test_restore_refuses_superseded_publication_and_retains_fence(
    tmp_path: Path,
) -> None:
    boundary, publisher, sessions, _clock, _addresses, _route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    boundary.withdraw(rollout_id, 0, (NODE_B,))
    exact = publisher.inspect()
    successor_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id=successor_id,
                base_commit="f" * 40,
                status="running",
                summary={},
                current_phase="routes-withdrawn",
                created_at=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
            )
        )
        owner = session.get(RoutePublicationOwner, 1)
        assert owner is not None
        owner.reconciliation_id = successor_id
        owner.owner_generation += 1

    with pytest.raises(UpdateRouteError, match="superseded"):
        boundary.restore(rollout_id, 0, (NODE_B,))

    assert publisher.inspect() == exact
    with pytest.raises(RouteRuntimeError, match="different update boundary"):
        publisher.claim_update_boundary("f" * 64)


def test_withdraw_retry_recovers_after_publication_before_receipt_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, publisher, _sessions, _clock, _addresses, _route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    publish = publisher.publish

    def crash_after_publish(*args, **kwargs):
        publish(*args, **kwargs)
        raise RouteRuntimeError("simulated process crash")

    monkeypatch.setattr(publisher, "publish", crash_after_publish)
    with pytest.raises(UpdateRouteError, match="failed closed"):
        boundary.withdraw(rollout_id, 0, (NODE_B,))
    exact = publisher.inspect()
    monkeypatch.setattr(publisher, "publish", publish)

    receipt = boundary.withdraw(rollout_id, 0, (NODE_B,))

    assert receipt.route_digest == exact.digest
    assert publisher.inspect() == exact


def test_disjoint_reconciliation_cannot_supersede_active_update_restore(
    tmp_path: Path,
) -> None:
    boundary, publisher, _sessions, _clock, _addresses, _route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    boundary.withdraw(rollout_id, 0, (NODE_B,))
    exact = publisher.inspect()

    with pytest.raises(RouteRuntimeError, match="update boundary"):
        publisher.withdraw(
            reconciliation_id=str(uuid.uuid4()),
            plan_digest="f" * 64,
            targets=(NODE_C,),
            reason="disjoint reconciliation",
        )

    assert publisher.inspect() == exact
    boundary.restore(rollout_id, 0, (NODE_B,))


def test_renew_active_extends_partial_bundle_from_fresh_evidence_without_alias_restore(
    tmp_path: Path,
) -> None:
    boundary, publisher, _sessions, clock, addresses, route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    boundary.withdraw(rollout_id, 0, (NODE_B,))
    before = publisher.inspect()
    clock.now += timedelta(seconds=90)
    addresses[NODE_C] = "10.0.0.23"

    receipt = boundary.renew_active(rollout_id, 0, (NODE_B,))
    retried = boundary.renew_active(rollout_id, 0, (NODE_B,))

    renewed = publisher.inspect()
    routes, _litellm = _active_documents(route_root, renewed)
    assert receipt == retried == renewed.digest
    assert renewed.generation == before.generation + 1
    assert set(routes["routes"]) == {"solo"}
    assert routes["routes"]["solo"]["address"] == "10.0.0.23"


def test_renew_active_rejects_stale_endpoint_evidence_without_changing_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, publisher, _sessions, clock, addresses, _route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    boundary.withdraw(rollout_id, 0, (NODE_B,))
    exact = publisher.inspect()
    stale = _request(clock, addresses)
    clock.now += timedelta(seconds=301)
    stale = replace(stale, expires_at=clock() + timedelta(seconds=150))
    monkeypatch.setattr(boundary, "_request_loader", lambda _session, _owner: stale)

    with pytest.raises(UpdateRouteError, match="renewal failed"):
        boundary.renew_active(rollout_id, 0, (NODE_B,))

    assert publisher.inspect(verify_lease=False) == exact


def test_renew_active_retry_recovers_after_publication_before_receipt_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, publisher, _sessions, clock, _addresses, _route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    boundary.withdraw(rollout_id, 0, (NODE_B,))
    clock.now += timedelta(seconds=90)
    publish = publisher.publish

    def crash_after_publish(*args, **kwargs):
        publish(*args, **kwargs)
        raise RouteRuntimeError("simulated process crash")

    monkeypatch.setattr(publisher, "publish", crash_after_publish)
    with pytest.raises(UpdateRouteError, match="renewal failed"):
        boundary.renew_active(rollout_id, 0, (NODE_B,))
    exact = publisher.inspect()
    monkeypatch.setattr(publisher, "publish", publish)

    receipt = boundary.renew_active(rollout_id, 0, (NODE_B,))

    assert receipt == exact.digest
    assert publisher.inspect() == exact


def test_restore_replays_full_routes_after_renewal_publication_receipt_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, publisher, _sessions, clock, _addresses, route_root = _harness(
        tmp_path
    )
    rollout_id = str(uuid.uuid4())
    boundary.withdraw(rollout_id, 0, (NODE_B,))
    clock.now += timedelta(seconds=90)
    write = boundary._write_record

    def crash_before_renewal_receipt(_record):
        raise UpdateRouteError("simulated renewal receipt crash")

    monkeypatch.setattr(boundary, "_write_record", crash_before_renewal_receipt)
    with pytest.raises(UpdateRouteError, match="simulated renewal receipt crash"):
        boundary.renew_active(rollout_id, 0, (NODE_B,))
    partial = publisher.inspect()
    partial_routes, _partial_litellm = _active_documents(route_root, partial)
    assert set(partial_routes["routes"]) == {"solo"}
    monkeypatch.setattr(boundary, "_write_record", write)

    boundary.restore(rollout_id, 0, (NODE_B,))

    restored = publisher.inspect()
    restored_routes, _restored_litellm = _active_documents(route_root, restored)
    assert set(restored_routes["routes"]) == {"distributed", "solo"}
    assert publisher.inspect_update_boundary() is None


def test_renew_active_refuses_owner_generation_drift_and_retains_fence(
    tmp_path: Path,
) -> None:
    boundary, publisher, sessions, _clock, _addresses, _route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    boundary.withdraw(rollout_id, 0, (NODE_B,))
    with sessions.begin() as session:
        owner = session.get(RoutePublicationOwner, 1)
        assert owner is not None
        owner.owner_generation += 1

    with pytest.raises(UpdateRouteError, match="superseded"):
        boundary.renew_active(rollout_id, 0, (NODE_B,))

    with pytest.raises(RouteRuntimeError, match="different update boundary"):
        publisher.claim_update_boundary("f" * 64)


def test_same_key_can_compensate_a_restoration_by_withdrawing_again(
    tmp_path: Path,
) -> None:
    boundary, publisher, _sessions, _clock, _addresses, route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    boundary.withdraw(rollout_id, 0, (NODE_B,))
    boundary.restore(rollout_id, 0, (NODE_B,))

    receipt = boundary.withdraw(rollout_id, 0, (NODE_B,))

    marker = publisher.inspect()
    routes, _litellm = _active_documents(route_root, marker)
    assert receipt.route_digest == marker.digest
    assert set(routes["routes"]) == {"solo"}


def test_typed_renewal_projection_distinguishes_active_and_restored_fences(
    tmp_path: Path,
) -> None:
    boundary, _publisher, _sessions, clock, _addresses, _route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())

    assert boundary.renew_if_active(rollout_id, 0, (NODE_B,)) == RouteRenewalResult(
        "not-active", None
    )
    boundary.withdraw(rollout_id, 0, (NODE_B,))
    clock.now += timedelta(seconds=90)
    renewed = boundary.renew_if_active(rollout_id, 0, (NODE_B,))
    assert renewed.status == "renewed"
    assert renewed.receipt is not None
    boundary.restore(rollout_id, 0, (NODE_B,))
    assert boundary.renew_if_active(rollout_id, 0, (NODE_B,)) == RouteRenewalResult(
        "not-active", None
    )


def test_typed_renewal_projection_reports_crash_pending_exact_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, publisher, _sessions, _clock, _addresses, _route_root = _harness(tmp_path)
    rollout_id = str(uuid.uuid4())
    write = boundary._write_record

    def crash_before_record(_record):
        raise UpdateRouteError("simulated crash")

    monkeypatch.setattr(boundary, "_write_record", crash_before_record)
    with pytest.raises(UpdateRouteError, match="simulated crash"):
        boundary.withdraw(rollout_id, 0, (NODE_B,))
    monkeypatch.setattr(boundary, "_write_record", write)

    result = boundary.renew_if_active(rollout_id, 0, (NODE_B,))

    assert result == RouteRenewalResult("withdrawal-pending", None)
    publisher.release_update_boundary(boundary._identity(rollout_id, 0, (NODE_B,))[0])


def test_typed_renewal_projection_never_hides_conflicting_fence(
    tmp_path: Path,
) -> None:
    boundary, publisher, _sessions, _clock, _addresses, _route_root = _harness(tmp_path)
    publisher.claim_update_boundary("f" * 64)

    with pytest.raises(UpdateRouteError, match="different rollout"):
        boundary.renew_if_active(str(uuid.uuid4()), 0, (NODE_B,))


def test_route_activation_and_supervisor_ack_run_outside_database_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, publisher, _sessions, clock, _addresses, _route_root = _harness(tmp_path)
    captured: dict[str, Session] = {}
    load = boundary._request_loader
    publish = publisher.publish

    def capture(session, reconciliation_id):
        captured["session"] = session
        return load(session, reconciliation_id)

    def assert_outside_transaction(*args, **kwargs):
        assert captured["session"].in_transaction() is False
        return publish(*args, **kwargs)

    monkeypatch.setattr(boundary, "_request_loader", capture)
    monkeypatch.setattr(publisher, "publish", assert_outside_transaction)
    rollout_id = str(uuid.uuid4())

    boundary.withdraw(rollout_id, 0, (NODE_B,))
    clock.now += timedelta(seconds=90)
    boundary.renew_active(rollout_id, 0, (NODE_B,))
    boundary.restore(rollout_id, 0, (NODE_B,))


def test_all_route_withdrawal_and_supervisor_ack_run_outside_database_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, publisher, _sessions, _clock, _addresses, _route_root = _harness(
        tmp_path
    )
    captured: dict[str, Session] = {}
    load = boundary._request_loader
    withdraw = publisher.withdraw

    def capture(session, reconciliation_id):
        captured["session"] = session
        return load(session, reconciliation_id)

    def assert_outside_transaction(*args, **kwargs):
        assert captured["session"].in_transaction() is False
        return withdraw(*args, **kwargs)

    monkeypatch.setattr(boundary, "_request_loader", capture)
    monkeypatch.setattr(publisher, "withdraw", assert_outside_transaction)

    boundary.withdraw(
        str(uuid.uuid4()),
        0,
        (NODE_A, NODE_B, NODE_C),
    )


def test_owner_drift_during_publication_aborts_and_retains_exact_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, publisher, sessions, _clock, _addresses, _route_root = _harness(
        tmp_path
    )
    publish = publisher.publish

    def publish_then_change_owner(*args, **kwargs):
        marker = publish(*args, **kwargs)
        with sessions.begin() as session:
            owner = session.get(RoutePublicationOwner, 1)
            assert owner is not None
            owner.owner_generation += 1
        return marker

    monkeypatch.setattr(publisher, "publish", publish_then_change_owner)
    rollout_id = str(uuid.uuid4())

    with pytest.raises(UpdateRouteError, match="superseded"):
        boundary.withdraw(rollout_id, 0, (NODE_B,))

    assert publisher.inspect().state == "maintenance"
    with pytest.raises(RouteRuntimeError, match="different update boundary"):
        publisher.claim_update_boundary("f" * 64)


def test_request_lease_allows_real_clock_progress_without_losing_safety_horizon(
    tmp_path: Path,
) -> None:
    boundary, _publisher, _sessions, _clock, _addresses, _route_root = _harness(
        tmp_path,
        clock=AdvancingClock(),
    )

    receipt = boundary.withdraw(str(uuid.uuid4()), 0, (NODE_B,))

    assert len(receipt.evidence_digest) == 64
