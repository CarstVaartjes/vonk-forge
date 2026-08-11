from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control import updates
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AuditEvent,
    Job,
    NodeMutationLease,
    UpdateAuthorizationIntent,
    UpdateRollout,
    UpdateRolloutNode,
)
from vonk_control.node_leases import NodeLeaseService
from vonk_control.update_admin import DurableUpdateGrantRefresher, durable_update_status
from vonk_control.update_grants import AdminActionGrantIssuer
from vonk_control.update_routes import RouteDrainReceipt

NODE_A = "spk_00000000000000000000000000000001"
NODE_B = "spk_00000000000000000000000000000002"
NODE_C = "spk_00000000000000000000000000000003"
BASE_COMMIT = "f" * 40
OLD_BUILD = "sha256:" + "c" * 64
TARGET_BUILD = "sha256:" + "a" * 64


@dataclass
class Clock:
    now: datetime = datetime(2026, 8, 5, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass(frozen=True)
class Finalization:
    operation: AgentOperation | None
    stale: bool


class Routes:
    def __init__(
        self,
        events: list[tuple[str, tuple[str, ...]]],
        clock: Clock,
    ) -> None:
        self.events = events
        self.clock = clock
        self.withdraw_failures = 0
        self.restore_failures = 0
        self.after_withdraw = None
        self.after_restore = None
        self.expired_drain_receipt = False

    def withdraw(
        self, rollout_id: str, batch_index: int, node_ids: tuple[str, ...]
    ) -> RouteDrainReceipt:
        assert rollout_id
        assert batch_index >= 0
        if self.withdraw_failures:
            self.withdraw_failures -= 1
            raise RuntimeError("route withdrawal temporarily unavailable")
        self.events.append(("routes.withdraw", tuple(node_ids)))
        if self.after_withdraw is not None:
            self.after_withdraw()
        now = self.clock()
        drained_at = (
            now - timedelta(seconds=120)
            if self.expired_drain_receipt
            else now
        )
        expires_at = (
            now - timedelta(seconds=60)
            if self.expired_drain_receipt
            else now + timedelta(seconds=60)
        )
        return RouteDrainReceipt.issue(
            rollout_id=rollout_id,
            batch_index=batch_index,
            targets=node_ids,
            route_digest="d" * 64,
            drained_at=drained_at,
            expires_at=expires_at,
        )

    def restore(
        self, rollout_id: str, batch_index: int, node_ids: tuple[str, ...]
    ) -> str:
        assert rollout_id
        assert batch_index >= 0
        if self.restore_failures:
            self.restore_failures -= 1
            raise RuntimeError("route restoration temporarily unavailable")
        self.events.append(("routes.restore", tuple(node_ids)))
        if self.after_restore is not None:
            self.after_restore()
        return "e" * 64


class AgentJobs:
    """Fast queue boundary that persists the operation the orchestrator binds."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Clock,
        events: list[tuple[str, tuple[str, ...]]],
    ) -> None:
        self.sessions = sessions
        self.clock = clock
        self.events = events

    def prepare_update(self, payload: dict[str, object]) -> object:
        return dict(payload)

    def reserve_update_authorization_in_session(
        self,
        session: Session,
        *,
        rollout_id: str,
        rollout_node_id: str,
        operation: str,
        payload: dict[str, object],
        target_release_digest: str | None,
    ) -> object:
        node = session.get(UpdateRolloutNode, rollout_node_id)
        assert node is not None and node.rollout_id == rollout_id
        return {
            "node_id": node.node_id,
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "payload": dict(payload),
            "rollout_node_id": rollout_node_id,
            "target_release_digest": target_release_digest,
        }

    def sign_update_authorization(self, reserved: object) -> dict[str, object]:
        assert isinstance(reserved, dict)
        return {"signed_payload": dict(reserved["payload"])}

    def finalize_update_authorization_in_session(
        self,
        session: Session,
        reserved: object,
        response: dict[str, object],
    ) -> Finalization:
        assert isinstance(reserved, dict)
        operation = str(reserved["operation"])
        node_id = str(reserved["node_id"])
        rollout_node = session.get(
            UpdateRolloutNode, str(reserved["rollout_node_id"])
        )
        assert rollout_node is not None
        rollout = session.get(UpdateRollout, rollout_node.rollout_id)
        assert rollout is not None and rollout.job_id is not None
        payload = response["signed_payload"]
        assert isinstance(payload, dict)
        stored = self.enqueue_in_session(
            session,
            rollout.job_id,
            node_id,
            operation,
            rollout.base_commit,
            payload,
            operation_id=str(reserved["operation_id"]),
            prepared_update=payload if operation == "agent.update" else None,
        )
        return Finalization(stored, False)

    def mark_update_authorizations_stale(self, reserved: list[object]) -> None:
        assert reserved

    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        base_commit: str,
        payload: dict[str, object],
        *,
        operation_id: str,
        prepared_update: object | None = None,
    ) -> AgentOperation:
        if operation == "agent.update":
            assert prepared_update == payload
        self.events.append((f"agent.enqueue:{operation}", (node_id,)))
        stored = AgentOperation(
            id=operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=operation,
            payload_digest=hashlib.sha256(repr(payload).encode()).hexdigest(),
            payload=payload,
            base_commit=base_commit,
            state="queued",
            current_attempt=0,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        session.add(stored)
        session.flush()
        return stored

    def enqueue(
        self,
        parent_job_id: str,
        node_id: str,
        operation: str,
        base_commit: str,
        payload: dict[str, object],
    ) -> AgentOperation:
        with self.sessions.begin() as session:
            stored = self.enqueue_in_session(
                session,
                parent_job_id,
                node_id,
                operation,
                base_commit,
                payload,
                operation_id=str(uuid.uuid4()),
            )
            session.expunge(stored)
            return stored

    def notify_available(self) -> None:
        return None


class SignedAgentJobsAuthority:
    def refresh_and_validate(self, payload):
        return dict(payload)

    def authorize(self, payload, **bindings):
        return {
            **payload,
            "receipt": {
                "attempt": 1,
                "claim_deadline": bindings["claim_deadline"],
                "expires_at": bindings["expires_at"],
                "fence": bindings["fence"],
                "node_id": bindings["node_id"],
                "operation_id": bindings["operation_id"],
                "previous_generation": bindings["previous_generation"],
                "previous_sha256": bindings["previous_sha256"],
                "previous_slot": bindings["previous_slot"],
            },
            "signature": {
                "algorithm": "ed25519",
                "key_id": "d" * 64,
                "value": "e" * 128,
            },
        }


class SignedAgentJobsSigner:
    def __init__(self, sessions=None) -> None:
        self.sessions = sessions
        self.seen_states: list[tuple[str, str]] = []

    def authorize(self, request):
        if self.sessions is not None:
            with self.sessions() as session:
                intent = session.get(
                    UpdateAuthorizationIntent, request["intent_id"]
                )
                assert intent is not None and intent.state == "reserved"
                rollout_node = session.get(
                    UpdateRolloutNode, intent.rollout_node_id
                )
                assert rollout_node is not None
                self.seen_states.append(
                    (str(request["action"]), rollout_node.state)
                )
        authority = SignedAgentJobsAuthority()
        source = request["source"]
        common = {
            "operation_id": request["operation_id"],
            "fence": request["fence"],
            "expires_at": request["expires_at"],
            "node_id": request["node_id"],
            "attempt": request["attempt"],
            "claim_deadline": request["claim_deadline"],
        }
        if request["action"] == "agent.update":
            signed_payload = authority.authorize(
                request["payload"],
                previous_slot=source["slot"],
                previous_sha256=source["sha256"],
                previous_generation=source["generation"],
                **common,
            )
            signed_payload["receipt"]["platform_target_sha256"] = request[
                "expected_tuf_target_sha256"
            ]
            signed_payload["receipt"]["platform_target_name"] = request[
                "platform_target_name"
            ]
            signed_payload["receipt"]["tuf_targets_version"] = request[
                "expected_tuf_targets_version"
            ]
        else:
            signed_payload = self.authorize_rollback(
                current_slot=source["slot"],
                current_sha256=source["sha256"],
                current_generation=source["generation"],
                **common,
            )
        return {
            "intent_id": request["intent_id"],
            "request_digest": hashlib.sha256(
                canonical_message(request) + b"\n"
            ).hexdigest(),
            "schema_version": 1,
            "signed_payload": signed_payload,
        }

    def authorize_rollback(self, **bindings):
        return {
            "receipt": {
                "action": "operator-rollback",
                "attempt": bindings["attempt"],
                "claim_deadline": bindings["claim_deadline"],
                "current_generation": bindings["current_generation"],
                "current_sha256": bindings["current_sha256"],
                "current_slot": bindings["current_slot"],
                "expires_at": bindings["expires_at"],
                "fence": bindings["fence"],
                "node_id": bindings["node_id"],
                "operation_id": bindings["operation_id"],
            },
            "signature": {
                "algorithm": "ed25519",
                "key_id": "d" * 64,
                "value": "e" * 128,
            },
        }


class SourceDriftingSigner(SignedAgentJobsSigner):
    def authorize(self, request):
        response = super().authorize(request)
        assert self.sessions is not None
        with self.sessions.begin() as session:
            node = session.get(AgentNode, request["node_id"])
            assert node is not None and node.supervisor_generation is not None
            node.supervisor_generation += 1
        return response


class TamperedReceiptSigner(SignedAgentJobsSigner):
    def authorize(self, request):
        response = super().authorize(request)
        response["signed_payload"]["receipt"]["operation_id"] = str(uuid.uuid4())
        return response


def _target() -> updates.TargetPlatform:
    return updates.TargetPlatform(
        platform_version="2.0.0",
        build_digest=TARGET_BUILD,
        release_digest="sha256:" + "b" * 64,
        base_commit=BASE_COMMIT,
        protocol_minimum=1,
        protocol_maximum=2,
        tuf_targets_version=7,
        artifacts=(
            updates.PlatformAgentArtifact(
                architecture="linux-arm64",
                oci_manifest_digest="sha256:" + "1" * 64,
                payload_name="vonk-agent",
                payload_sha256="2" * 64,
                payload_size=4096,
            ),
        ),
    )


def _observation(node_id: str) -> updates.AgentObservation:
    return updates.AgentObservation(
        node_id=node_id,
        state="active",
        online=True,
        architecture="linux-arm64",
        platform_version="1.0.0",
        build_digest=OLD_BUILD,
        protocol_version=1,
        active_slot="A",
        agent_sha256="3" * 64,
        supervisor_generation=1,
        capabilities=("agent.rollback", "agent.update"),
        last_seen_at=datetime(2026, 8, 5, tzinfo=UTC),
    )


def _plan(
    workloads: tuple[updates.DistributedWorkload, ...] = (),
    *,
    node_ids: tuple[str, ...] = (NODE_A, NODE_B),
    batch_size: int = 1,
) -> updates.UpdatePlan:
    return updates.UpdatePlanner().plan(
        _target(),
        tuple(_observation(node_id) for node_id in node_ids),
        workloads,
        updates.RolloutPolicy(batch_size=batch_size, soak_seconds=30),
    )


@dataclass
class Harness:
    sessions: sessionmaker[Session]
    clock: Clock
    events: list[tuple[str, tuple[str, ...]]]
    jobs: AgentJobs
    routes: Routes

    def orchestrator(self):
        return updates.UpdateOrchestrator(
            self.sessions,
            self.jobs,
            self.routes,
            clock=self.clock,
        )


def _harness(
    tmp_path, node_ids: tuple[str, ...] = (NODE_A, NODE_B)
) -> Harness:
    engine = create_engine(f"sqlite:///{tmp_path / 'updates.sqlite'}")
    from vonk_control.models import Base

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    with sessions.begin() as session:
        for node_id in node_ids:
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    protocol_version=1,
                    platform_version="1.0.0",
                    build_digest=OLD_BUILD,
                    active_slot="A",
                    agent_sha256="3" * 64,
                    supervisor_generation=1,
                    capabilities=["agent.rollback", "agent.update"],
                    last_seen_at=clock(),
                )
            )
            session.add(
                AgentCertificate(
                    serial=f"candidate-{node_id}",
                    node_id=node_id,
                    not_before=clock() - timedelta(days=1),
                    not_after=clock() + timedelta(days=1),
                    fingerprint=f"fingerprint-{node_id}",
                    state="active",
                    generation=1,
                )
            )
    events: list[tuple[str, tuple[str, ...]]] = []
    return Harness(
        sessions,
        clock,
        events,
        AgentJobs(sessions, clock, events),
        Routes(events, clock),
    )


def _rollout(harness: Harness, rollout_id: str) -> UpdateRollout:
    with harness.sessions() as session:
        stored = session.get(UpdateRollout, rollout_id)
        assert stored is not None
        session.expunge(stored)
        return stored


def _rollout_node(
    harness: Harness,
    rollout_id: str,
    node_id: str,
) -> UpdateRolloutNode:
    with harness.sessions() as session:
        stored = session.scalar(
            select(UpdateRolloutNode).where(
                UpdateRolloutNode.rollout_id == rollout_id,
                UpdateRolloutNode.node_id == node_id,
            )
        )
        assert stored is not None
        session.expunge(stored)
        return stored


def _finish_update_operation(
    harness: Harness,
    rollout_id: str,
    node_id: str,
    *,
    reconnect_target: bool,
    readiness: bool = True,
) -> None:
    with harness.sessions.begin() as session:
        node_rollout = session.scalar(
            select(UpdateRolloutNode).where(
                UpdateRolloutNode.rollout_id == rollout_id,
                UpdateRolloutNode.node_id == node_id,
            )
        )
        assert node_rollout is not None and node_rollout.operation_id is not None
        operation = session.get(AgentOperation, node_rollout.operation_id)
        assert operation is not None
        operation.state = "succeeded"
        operation.updated_at = harness.clock()
        if reconnect_target:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.platform_version = "2.0.0"
            node.build_digest = TARGET_BUILD
            node.protocol_version = 1
            node.active_slot = "B"
            node.agent_sha256 = "2" * 64
            node.supervisor_generation = 2
            node.supervisor_ready_generation = 2 if readiness else None
            node.self_test_passed = readiness
            node.last_seen_at = harness.clock()
            if readiness:
                _record_test_contact(session, node)
            else:
                node.contact_certificate_serial = None
                node.contact_observation_digest = None


def _record_test_contact(session: Session, node: AgentNode) -> None:
    serial = f"candidate-{node.node_id}"
    certificate = session.get(AgentCertificate, serial)
    assert certificate is not None and node.last_seen_at is not None
    runtime_identity = {
        "active_slot": node.active_slot,
        "architecture": node.architecture or "linux-arm64",
        "agent_sha256": node.agent_sha256,
        "build_digest": node.build_digest,
        "platform_version": node.platform_version,
        "self_test_passed": node.self_test_passed,
        "supervisor_generation": node.supervisor_generation,
        "supervisor_ready_generation": node.supervisor_ready_generation,
    }
    node.architecture = str(runtime_identity["architecture"])
    node.contact_certificate_serial = serial
    node.contact_observation_digest = hashlib.sha256(
        canonical_message(
            {
                "certificate_fingerprint": certificate.fingerprint,
                "certificate_serial": serial,
                "node_id": node.node_id,
                "observed_at": node.last_seen_at.replace(tzinfo=UTC).isoformat(),
                "runtime_identity": runtime_identity,
            }
        )
    ).hexdigest()


def _fail_update_operation(
    harness: Harness,
    rollout_id: str,
    node_id: str,
) -> None:
    with harness.sessions.begin() as session:
        node_rollout = session.scalar(
            select(UpdateRolloutNode).where(
                UpdateRolloutNode.rollout_id == rollout_id,
                UpdateRolloutNode.node_id == node_id,
            )
        )
        assert node_rollout is not None and node_rollout.operation_id is not None
        operation = session.get(AgentOperation, node_rollout.operation_id)
        assert operation is not None
        operation.state = "failed"
        operation.updated_at = harness.clock()


def _reach_failed_canary(harness: Harness) -> tuple[object, str]:
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    with harness.sessions.begin() as session:
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None
        rollout.update_admin_grant = {"test": True}
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.active_slot = "B"
        node.supervisor_generation = 2
        node.last_seen_at = harness.clock()
    _fail_update_operation(harness, rollout_id, NODE_A)
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "paused"
    return orchestrator, rollout_id


def test_canary_route_withdrawal_reconnect_soak_and_next_batch_are_durable(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )

    orchestrator.advance(rollout_id)

    assert harness.events == []
    assert _rollout(harness, rollout_id).state == "withdrawing"
    orchestrator.advance(rollout_id)

    assert harness.events[:2] == [
        ("routes.withdraw", (NODE_A,)),
        ("agent.enqueue:agent.update", (NODE_A,)),
    ]
    canary = _rollout_node(harness, rollout_id, NODE_A)
    assert canary.state == "updating"
    assert canary.operation_id is not None

    _finish_update_operation(
        harness, rollout_id, NODE_A, reconnect_target=False
    )
    orchestrator.advance(rollout_id)
    assert _rollout_node(harness, rollout_id, NODE_A).state == "updating"
    assert "routes.restore" not in [event[0] for event in harness.events]

    _finish_update_operation(
        harness, rollout_id, NODE_A, reconnect_target=True
    )
    orchestrator.advance(rollout_id)
    soaking = _rollout_node(harness, rollout_id, NODE_A)
    assert soaking.state == "soaking"
    assert soaking.observed_platform_version == "2.0.0"
    assert soaking.observed_build_digest == TARGET_BUILD
    assert soaking.observed_active_slot == "B"
    assert soaking.soak_until is not None
    assert soaking.soak_until.replace(tzinfo=UTC) == harness.clock() + timedelta(
        seconds=30
    )

    restarted = harness.orchestrator()
    restarted.advance(rollout_id)
    assert _rollout_node(harness, rollout_id, NODE_A).state == "soaking"
    harness.clock.advance(31)
    restarted.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "publishing"
    restarted.advance(rollout_id)
    assert _rollout_node(harness, rollout_id, NODE_A).state == "accepted"
    with harness.sessions() as session:
        assert session.get(NodeMutationLease, NODE_A) is None

    restarted.advance(rollout_id)
    restarted.advance(rollout_id)
    assert harness.events[-2:] == [
        ("routes.withdraw", (NODE_B,)),
        ("agent.enqueue:agent.update", (NODE_B,)),
    ]
    assert _rollout(harness, rollout_id).current_batch == 1


def test_target_contact_without_supervisor_self_test_readiness_is_not_accepted(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(node_ids=(NODE_A,)), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(
        harness,
        rollout_id,
        NODE_A,
        reconnect_target=True,
        readiness=False,
    )

    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "updating"

    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.self_test_passed = True
        node.supervisor_ready_generation = node.supervisor_generation
        _record_test_contact(session, node)
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "soaking"


def test_target_contact_digest_must_match_authenticated_certificate_and_identity(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(node_ids=(NODE_A,)), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=True)
    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.contact_observation_digest = "4" * 64

    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "updating"

    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        _record_test_contact(session, node)
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "soaking"


def test_offline_targets_are_durable_and_online_completion_is_partial(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    plan = updates.UpdatePlanner().plan(
        _target(),
        (
            _observation(NODE_A),
            replace(_observation(NODE_B), online=False),
        ),
        (),
        updates.RolloutPolicy(batch_size=1, soak_seconds=30),
    )
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        plan, actor="admin", request_id=str(uuid.uuid4())
    )

    pending = _rollout_node(harness, rollout_id, NODE_B)
    assert pending.state == "offline-pending"
    assert pending.batch_index == -1

    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=True)
    orchestrator.advance(rollout_id)
    harness.clock.advance(31)
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)

    assert _rollout(harness, rollout_id).state == "partial"
    assert _rollout_node(harness, rollout_id, NODE_B).state == "offline-pending"


def test_first_agent_failure_pauses_before_any_later_batch(tmp_path) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _fail_update_operation(harness, rollout_id, NODE_A)

    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "failure-publishing"
    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason is not None
    assert rollout.failure_evidence_digest is not None
    assert _rollout_node(harness, rollout_id, NODE_A).state == "failed"
    assert harness.events[-1] == ("routes.restore", (NODE_A,))
    with pytest.raises(ValueError, match="no mutated node"):
        orchestrator.begin_rollback(
            rollout_id, actor="admin", request_id=str(uuid.uuid4())
        )
    assert not any(
        event == ("agent.enqueue:agent.update", (NODE_B,))
        for event in harness.events
    )


def test_create_persists_exact_api_grant_before_rollout_becomes_visible(tmp_path) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    bindings: list[dict[str, object]] = []

    def issue(**values: object) -> dict[str, object]:
        bindings.append(dict(values))
        return {"grant": "update"}

    rollout_id = orchestrator.create(
        _plan(),
        actor="admin",
        request_id=str(uuid.uuid4()),
        admin_grant_factory=issue,
    )

    with harness.sessions() as session:
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None and rollout.job_id is not None
        assert rollout.update_admin_grant == {"grant": "update"}
        assert bindings == [
            {
                "node_ids": (NODE_A,),
                "parent_job_id": rollout.job_id,
                "rollout_id": rollout_id,
                "target_release_digest": "sha256:" + "b" * 64,
            }
        ]


def test_durable_update_status_accepts_rollout_or_job_id_with_one_exact_projection(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    rollout_id = harness.orchestrator().create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    with harness.sessions() as session:
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None and rollout.job_id is not None
        job_id = rollout.job_id

    by_rollout = durable_update_status(harness.sessions, rollout_id)
    by_job = durable_update_status(harness.sessions, job_id)

    assert by_rollout == by_job
    assert by_rollout == {
        "batches": [[NODE_A], [NODE_B]],
        "can_approve_resume": False,
        "current_batch": 0,
        "failure_reason": None,
        "id": rollout_id,
        "job_id": job_id,
        "nodes": [
            {"node_id": NODE_A, "state": "pending"},
            {"node_id": NODE_B, "state": "pending"},
        ],
        "plan_digest": _plan().plan_digest,
        "required_action": None,
        "resume_required": False,
        "state": "planned",
    }


def test_batch_grant_refresh_is_exact_idempotent_and_durably_audited(tmp_path) -> None:
    harness = _harness(tmp_path)
    rollout_id = harness.orchestrator().create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    refresher = DurableUpdateGrantRefresher(
        harness.sessions,
        AdminActionGrantIssuer(
            ed25519.Ed25519PrivateKey.generate(),
            clock=harness.clock,
            nonce_factory=lambda: uuid.UUID(
                "30000000-0000-4000-8000-000000000003"
            ),
        ),
        clock=harness.clock,
    )
    request_id = "40000000-0000-4000-8000-000000000004"

    first = refresher.refresh_update_grant(
        rollout_id,
        0,
        (NODE_A,),
        actor="control-worker",
        request_id=request_id,
    )
    repeated = refresher.refresh_update_grant(
        rollout_id,
        0,
        (NODE_A,),
        actor="control-worker",
        request_id="50000000-0000-4000-8000-000000000005",
    )

    assert repeated == first
    assert first["claims"]["node_ids"] == [NODE_A]
    with harness.sessions() as session:
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None and rollout.update_admin_grant == first
        refreshes = tuple(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "platform.update.grant-refresh"
                )
            )
        )
        assert [(event.request_id, event.targets) for event in refreshes] == [
            (request_id, [NODE_A])
        ]

    with pytest.raises(ValueError, match="batch nodes"):
        refresher.refresh_update_grant(
            rollout_id,
            0,
            (NODE_B,),
            actor="control-worker",
            request_id=str(uuid.uuid4()),
        )


def test_begin_rollback_persists_exact_api_grant_before_reservation(tmp_path) -> None:
    harness = _harness(tmp_path)
    orchestrator, rollout_id = _reach_failed_canary(harness)
    bindings: list[dict[str, object]] = []
    paused_status = durable_update_status(harness.sessions, rollout_id)
    assert paused_status["required_action"] == "authorize-rollback"

    def issue(**values: object) -> dict[str, object]:
        bindings.append(dict(values))
        return {"grant": "rollback"}

    orchestrator.authorize_rollback(
        rollout_id,
        actor="admin",
        request_id=str(uuid.uuid4()),
        admin_grant_factory=issue,
    )

    with harness.sessions() as session:
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None and rollout.job_id is not None
        assert rollout.rollback_admin_grant == {"grant": "rollback"}
        assert bindings == [
            {
                "node_ids": (NODE_A,),
                "parent_job_id": rollout.job_id,
                "rollout_id": rollout_id,
                "target_release_digest": None,
            }
        ]
    authorized_status = durable_update_status(harness.sessions, rollout_id)
    assert authorized_status["can_approve_resume"] is False
    assert authorized_status["required_action"] is None
    assert authorized_status["resume_required"] is False
    orchestrator.begin_rollback(
        rollout_id,
        actor="control-worker",
        request_id=str(uuid.uuid4()),
    )
    assert _rollout_node(harness, rollout_id, NODE_A).rollback_operation_id is not None


def test_rollback_is_bound_to_prior_slot_and_waits_for_authenticated_identity(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator, rollout_id = _reach_failed_canary(harness)

    orchestrator.begin_rollback(
        rollout_id, actor="admin", request_id=str(uuid.uuid4())
    )

    node_rollout = _rollout_node(harness, rollout_id, NODE_A)
    assert node_rollout.state == "rolling-back"
    assert node_rollout.rollback_operation_id is not None
    with harness.sessions() as session:
        operation = session.get(AgentOperation, node_rollout.rollback_operation_id)
        assert operation is not None
        assert operation.kind == "agent.rollback"
        assert operation.payload == {}

    with harness.sessions.begin() as session:
        operation = session.get(AgentOperation, node_rollout.rollback_operation_id)
        assert operation is not None
        operation.state = "succeeded"
        operation.updated_at = harness.clock()
    orchestrator.advance(rollout_id)
    assert _rollout_node(harness, rollout_id, NODE_A).state == "rolling-back"

    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.platform_version = "1.0.0"
        node.build_digest = OLD_BUILD
        node.active_slot = "A"
        node.agent_sha256 = "3" * 64
        node.supervisor_generation = 3
        node.last_seen_at = harness.clock()
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "rolling-back"

    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.self_test_passed = True
        node.supervisor_ready_generation = node.supervisor_generation
        _record_test_contact(session, node)
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "rollback-publishing"
    orchestrator.advance(rollout_id)

    assert _rollout_node(harness, rollout_id, NODE_A).state == "rolled-back"
    assert _rollout(harness, rollout_id).state == "waiting-for-approval"
    assert (
        durable_update_status(harness.sessions, rollout_id)["required_action"]
        == "approve-resume"
    )
    assert harness.events[-1] == ("routes.restore", (NODE_A,))


def test_failed_rollback_pauses_with_routes_and_mutation_lease_still_fenced(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator, rollout_id = _reach_failed_canary(harness)
    orchestrator.begin_rollback(
        rollout_id, actor="admin", request_id=str(uuid.uuid4())
    )
    rollback = _rollout_node(harness, rollout_id, NODE_A)
    assert rollback.rollback_operation_id is not None
    with harness.sessions.begin() as session:
        operation = session.get(AgentOperation, rollback.rollback_operation_id)
        assert operation is not None
        operation.state = "failed"
        operation.updated_at = harness.clock()

    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "agent rollback failed"
    assert _rollout_node(harness, rollout_id, NODE_A).state == "failed"
    with harness.sessions() as session:
        lease = session.get(NodeMutationLease, NODE_A)
        assert lease is not None and lease.state == "held"
    assert harness.events[-1] != ("routes.restore", (NODE_A,))


def test_successful_rollback_without_source_reconnect_pauses_at_deadline(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator, rollout_id = _reach_failed_canary(harness)
    orchestrator.begin_rollback(
        rollout_id, actor="admin", request_id=str(uuid.uuid4())
    )
    rollback = _rollout_node(harness, rollout_id, NODE_A)
    assert rollback.rollback_operation_id is not None
    with harness.sessions.begin() as session:
        operation = session.get(AgentOperation, rollback.rollback_operation_id)
        assert operation is not None
        operation.state = "succeeded"
        operation.updated_at = harness.clock()

    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "rolling-back"
    harness.clock.advance(181)
    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "agent rollback reconnect timed out"
    with harness.sessions() as session:
        lease = session.get(NodeMutationLease, NODE_A)
        assert lease is not None and lease.state == "held"


def test_administrator_can_retry_failed_rollback_without_losing_operation_history(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator, rollout_id = _reach_failed_canary(harness)
    orchestrator.begin_rollback(
        rollout_id, actor="admin", request_id=str(uuid.uuid4())
    )
    first = _rollout_node(harness, rollout_id, NODE_A)
    assert first.rollback_operation_id is not None
    first_id = first.rollback_operation_id
    with harness.sessions.begin() as session:
        operation = session.get(AgentOperation, first_id)
        assert operation is not None
        operation.state = "failed"
        operation.updated_at = harness.clock()
    orchestrator.advance(rollout_id)

    orchestrator.begin_rollback(
        rollout_id, actor="admin", request_id=str(uuid.uuid4())
    )

    retried = _rollout_node(harness, rollout_id, NODE_A)
    assert retried.state == "rolling-back"
    assert retried.rollback_operation_id not in {None, first_id}
    assert {
        (item["id"], item["role"], item["state"])
        for item in retried.operation_history
    } >= {(first_id, "rollback", "failed")}


def test_begin_rollback_claims_only_exact_durable_rollout_binding(tmp_path) -> None:
    harness = _harness(tmp_path)
    signer = SignedAgentJobsSigner(harness.sessions)
    jobs = AgentJobService(
        harness.sessions,
        clock=harness.clock,
        update_signer=signer,
    )
    harness.jobs = jobs
    orchestrator, rollout_id = _reach_failed_canary(harness)
    certificate_serial = f"candidate-{NODE_A}"

    orchestrator.begin_rollback(
        rollout_id,
        actor="admin",
        request_id=str(uuid.uuid4()),
        admin_grant={"test": True},
    )
    claim = jobs.claim(NODE_A, certificate_serial, 30)

    assert claim is not None
    assert claim.operation.value == "agent.rollback"
    assert signer.seen_states == [
        ("agent.update", "routes-withdrawn"),
        ("agent.rollback", "failed"),
    ]
    jobs.succeed(
        claim,
        {
            "artifact_sha256": "3" * 64,
            "build_digest": OLD_BUILD,
            "platform_version": "1.0.0",
            "previous_slot": "B",
            "status": "pending-rollback",
            "target_slot": "A",
        },
    )
    with harness.sessions() as session:
        rollout_node = session.scalar(
            select(UpdateRolloutNode).where(
                UpdateRolloutNode.rollout_id == rollout_id,
                UpdateRolloutNode.node_id == NODE_A,
            )
        )
        assert rollout_node is not None
        assert rollout_node.rollback_operation_id == claim.operation_id
        operation = session.get(AgentOperation, claim.operation_id)
        assert operation is not None
        assert operation.state == "succeeded"


def test_failed_real_agent_rollback_retry_queues_a_fresh_operation(tmp_path) -> None:
    harness = _harness(tmp_path)
    signer = SignedAgentJobsSigner(harness.sessions)
    jobs = AgentJobService(
        harness.sessions,
        clock=harness.clock,
        update_signer=signer,
    )
    harness.jobs = jobs
    orchestrator, rollout_id = _reach_failed_canary(harness)
    certificate_serial = f"candidate-{NODE_A}"

    orchestrator.begin_rollback(
        rollout_id,
        actor="admin",
        request_id=str(uuid.uuid4()),
        admin_grant={"test": True},
    )
    first_claim = jobs.claim(NODE_A, certificate_serial, 30)
    assert first_claim is not None
    jobs.fail(first_claim, "rollback self-test failed")
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "paused"

    orchestrator.begin_rollback(
        rollout_id,
        actor="admin",
        request_id=str(uuid.uuid4()),
        admin_grant={"test": True},
    )

    retried = _rollout_node(harness, rollout_id, NODE_A)
    assert retried.rollback_operation_id not in {None, first_claim.operation_id}
    second_claim = jobs.claim(NODE_A, certificate_serial, 30)
    assert second_claim is not None
    assert second_claim.operation_id == retried.rollback_operation_id


def test_signer_response_is_discarded_when_source_drifts_before_queue_cas(
    tmp_path,
) -> None:
    harness = _harness(tmp_path, (NODE_A,))
    signer = SourceDriftingSigner(harness.sessions)
    harness.jobs = AgentJobService(
        harness.sessions,
        clock=harness.clock,
        update_signer=signer,
    )
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(node_ids=(NODE_A,)),
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    with harness.sessions.begin() as session:
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None
        rollout.update_admin_grant = {"test": True}
    assert orchestrator.advance(rollout_id) == "withdrawing"

    with pytest.raises(ValueError, match="became stale"):
        orchestrator.advance(rollout_id)

    with harness.sessions() as session:
        intent = session.scalar(select(UpdateAuthorizationIntent))
        assert intent is not None and intent.state == "stale"
        assert session.scalar(select(AgentOperation)) is None
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None and rollout.state == "withdrawing"


def test_source_drift_after_route_withdrawal_blocks_intent_reservation(
    tmp_path,
) -> None:
    harness = _harness(tmp_path, (NODE_A,))
    harness.jobs = AgentJobService(
        harness.sessions,
        clock=harness.clock,
        update_signer=SignedAgentJobsSigner(harness.sessions),
    )
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(node_ids=(NODE_A,)),
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    with harness.sessions.begin() as session:
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None
        rollout.update_admin_grant = {"test": True}
    assert orchestrator.advance(rollout_id) == "withdrawing"

    def drift() -> None:
        with harness.sessions.begin() as session:
            node = session.get(AgentNode, NODE_A)
            assert node is not None and node.supervisor_generation is not None
            node.supervisor_generation += 1

    harness.routes.after_withdraw = drift
    with pytest.raises(ValueError, match="planned update source identity changed"):
        orchestrator.advance(rollout_id)

    with harness.sessions() as session:
        assert session.scalar(select(UpdateAuthorizationIntent)) is None
        assert session.scalar(select(AgentOperation)) is None


def test_response_with_wrong_exact_receipt_binding_is_never_queued(tmp_path) -> None:
    harness = _harness(tmp_path, (NODE_A,))
    harness.jobs = AgentJobService(
        harness.sessions,
        clock=harness.clock,
        update_signer=TamperedReceiptSigner(harness.sessions),
    )
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(node_ids=(NODE_A,)),
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    with harness.sessions.begin() as session:
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None
        rollout.update_admin_grant = {"test": True}
    assert orchestrator.advance(rollout_id) == "withdrawing"

    with pytest.raises(ValueError, match="became stale"):
        orchestrator.advance(rollout_id)

    with harness.sessions() as session:
        intent = session.scalar(select(UpdateAuthorizationIntent))
        assert intent is not None and intent.state == "stale"
        assert session.scalar(select(AgentOperation)) is None


def test_resume_after_rollback_requires_durable_attributed_approval(tmp_path) -> None:
    harness = _harness(tmp_path)
    orchestrator, rollout_id = _reach_failed_canary(harness)
    orchestrator.begin_rollback(
        rollout_id, actor="admin", request_id=str(uuid.uuid4())
    )
    node_rollout = _rollout_node(harness, rollout_id, NODE_A)
    assert node_rollout.rollback_operation_id is not None
    with harness.sessions.begin() as session:
        operation = session.get(AgentOperation, node_rollout.rollback_operation_id)
        assert operation is not None
        operation.state = "succeeded"
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.platform_version = "1.0.0"
        node.build_digest = OLD_BUILD
        node.active_slot = "A"
        node.agent_sha256 = "3" * 64
        node.supervisor_generation = 3
        node.supervisor_ready_generation = 3
        node.self_test_passed = True
        node.last_seen_at = harness.clock()
        _record_test_contact(session, node)
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    event_count = len(harness.events)

    orchestrator.advance(rollout_id)
    assert len(harness.events) == event_count
    assert _rollout(harness, rollout_id).state == "waiting-for-approval"

    approval_request = str(uuid.uuid4())
    orchestrator.approve_resume(
        rollout_id,
        actor="second-admin",
        request_id=approval_request,
        reason="canary rollback reviewed",
    )
    approved = _rollout(harness, rollout_id)
    assert approved.approval_actor == "second-admin"
    assert approved.approval_request_id == approval_request
    assert approved.approval_reason == "canary rollback reviewed"
    assert approved.approval_at is not None
    assert approved.approval_at.replace(tzinfo=UTC) == harness.clock()
    assert approved.approval_evidence_digest is not None
    approved_node = _rollout_node(harness, rollout_id, NODE_A)
    assert {item["role"] for item in approved_node.operation_history} == {
        "rollback",
        "update",
    }

    harness.orchestrator().advance(rollout_id)
    harness.orchestrator().advance(rollout_id)
    assert harness.events[-1] == ("agent.enqueue:agent.update", (NODE_A,))


def test_update_does_not_overlap_a_running_reconciliation_mutation(tmp_path) -> None:
    harness = _harness(tmp_path)
    now = harness.clock()
    with harness.sessions.begin() as session:
        parent = Job(
            id="reconciliation-job",
            request_id=str(uuid.uuid4()),
            kind="reconcile",
            state="running",
            actor="admin",
            base_commit=BASE_COMMIT,
            targets=[NODE_A],
            payload_digest="9" * 64,
            payload={},
            current_attempt=1,
            created_at=now,
            updated_at=now,
        )
        session.add(parent)
        session.add(
            AgentOperation(
                id="running-reconciliation-operation",
                parent_job_id=parent.id,
                node_id=NODE_A,
                kind="workload.start",
                payload_digest="8" * 64,
                payload={},
                base_commit=BASE_COMMIT,
                state="running",
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
        )

    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert "overlap" in (rollout.failure_reason or "")
    assert not any(
        event == ("agent.enqueue:agent.update", (NODE_A,))
        for event in harness.events
    )

    with harness.sessions.begin() as session:
        operation = session.get(AgentOperation, "running-reconciliation-operation")
        assert operation is not None
        operation.state = "succeeded"
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "planned"
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    assert harness.events[-1] == ("agent.enqueue:agent.update", (NODE_A,))


def test_update_holds_a_durable_node_lease_until_routes_are_restored(tmp_path) -> None:
    harness = _harness(tmp_path)
    lease_service = NodeLeaseService(clock=harness.clock)
    reconciliation_id = str(uuid.uuid4())
    with harness.sessions.begin() as session:
        reconciliation_grant = lease_service.acquire_in_session(
            session,
            [NODE_A],
            owner_kind="reconciliation",
            owner_id=reconciliation_id,
        )
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )

    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "node mutation lease blocks platform update"
    assert harness.events == []


    with harness.sessions.begin() as session:
        lease_service.mark_releasing_in_session(session, reconciliation_grant)
        releasing = lease_service.owned_grant_in_session(
            session,
            [NODE_A],
            owner_kind="reconciliation",
            owner_id=reconciliation_id,
        )
        assert releasing is not None
        lease_service.release_in_session(session, releasing)

    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "planned"
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "withdrawing"
    with harness.sessions() as session:
        lease = session.get(NodeMutationLease, NODE_A)
        assert lease is not None
        assert lease.owner_kind == "update-rollout"
        assert lease.owner_id == rollout_id

    orchestrator.advance(rollout_id)
    _fail_update_operation(harness, rollout_id, NODE_A)
    orchestrator.advance(rollout_id)
    with harness.sessions() as session:
        assert session.get(NodeMutationLease, NODE_A) is not None
    orchestrator.advance(rollout_id)
    with harness.sessions() as session:
        assert session.get(NodeMutationLease, NODE_A) is None


def test_execution_rechecks_current_workload_health_and_serving_evidence(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    replicas = tuple(
        updates.WorkloadReplicaObservation(
            node_id=node_id,
            healthy=True,
            serving=True,
            observed_at=harness.clock(),
            evidence_digest=("7" if node_id == NODE_A else "8") * 64,
        )
        for node_id in (NODE_A, NODE_B)
    )
    workload = updates.DistributedWorkload(
        workload_id="distributed-model",
        members=(NODE_A, NODE_B),
        minimum_available=1,
        replicas=replicas,
    )
    rollout_id = harness.orchestrator().create(
        _plan(workloads=(workload,)),
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    state = harness.orchestrator().advance(rollout_id)

    assert state == "paused"
    assert _rollout(harness, rollout_id).failure_reason == (
        "distributed workload quorum changed"
    )
    assert harness.events == []


def test_persisted_plan_tampering_fails_before_route_or_agent_side_effects(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    with harness.sessions.begin() as session:
        rollout = session.get(UpdateRollout, rollout_id)
        assert rollout is not None
        document = dict(rollout.plan)
        document["soak_seconds"] = 0
        rollout.plan = document

    with pytest.raises(ValueError, match="disagrees"):
        orchestrator.advance(rollout_id)

    assert harness.events == []


def test_concurrent_create_is_idempotent_by_immutable_plan(tmp_path) -> None:
    harness = _harness(tmp_path)
    plan = _plan()

    def create() -> str:
        return harness.orchestrator().create(
            plan, actor="admin", request_id=str(uuid.uuid4())
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rollout_ids = tuple(pool.map(lambda _index: create(), range(2)))

    assert rollout_ids[0] == rollout_ids[1]
    with harness.sessions() as session:
        assert len(tuple(session.scalars(select(UpdateRollout)))) == 1
        jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "platform.update"))
        )
        assert len(jobs) == 1


def test_route_side_effect_intents_are_retryable_after_boundary_failure(tmp_path) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )

    orchestrator.advance(rollout_id)
    harness.routes.withdraw_failures = 1
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "withdrawing"
    assert harness.events == []

    orchestrator.advance(rollout_id)
    _fail_update_operation(harness, rollout_id, NODE_A)
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "failure-publishing"
    harness.routes.restore_failures = 1
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "failure-publishing"

    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "paused"


def test_expired_route_drain_receipt_blocks_agent_dispatch(tmp_path) -> None:
    harness = _harness(tmp_path)
    harness.routes.expired_drain_receipt = True
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(node_ids=(NODE_A,)), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)

    with pytest.raises(RuntimeError, match="drain receipt"):
        orchestrator.advance(rollout_id)

    assert _rollout(harness, rollout_id).state == "withdrawing"
    assert not any(
        event[0] == "agent.enqueue:agent.update" for event in harness.events
    )


def test_successful_operation_that_never_reconnects_times_out(tmp_path) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=False)

    harness.clock.advance(181)
    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "agent activation reconnect timed out"
    with harness.sessions() as session:
        job = session.get(Job, rollout.job_id)
        assert job is not None
        assert job.state == "waiting-for-operator"


def test_reconnect_deadline_starts_when_operation_succeeds_not_when_dispatched(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    harness.clock.advance(170)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=False)

    harness.clock.advance(20)
    orchestrator.advance(rollout_id)

    assert _rollout(harness, rollout_id).state == "updating"
    harness.clock.advance(161)
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).failure_reason == (
        "agent activation reconnect timed out"
    )


def test_running_operation_timeout_expires_its_fence_before_batch_pause(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    node_rollout = _rollout_node(harness, rollout_id, NODE_A)
    assert node_rollout.operation_id is not None
    with harness.sessions.begin() as session:
        operation = session.get(AgentOperation, node_rollout.operation_id)
        assert operation is not None
        operation.state = "running"
        operation.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=harness.clock() + timedelta(hours=1),
                agent_certificate_serial=f"candidate-{NODE_A}",
                state="running",
            )
        )
    harness.clock.advance(601)

    orchestrator.advance(rollout_id)

    with harness.sessions() as session:
        operation = session.get(AgentOperation, node_rollout.operation_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == node_rollout.operation_id
            )
        )
        assert operation is not None and operation.state == "waiting-for-operator"
        assert attempt is not None and attempt.state == "expired"
        assert attempt.lease_deadline.replace(tzinfo=UTC) == harness.clock()


def test_unsafe_expired_update_attempt_requires_rollback_even_with_stale_source(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    with harness.sessions.begin() as session:
        node_rollout = session.scalar(
            select(UpdateRolloutNode).where(
                UpdateRolloutNode.rollout_id == rollout_id,
                UpdateRolloutNode.node_id == NODE_A,
            )
        )
        assert node_rollout is not None and node_rollout.operation_id is not None
        operation = session.get(AgentOperation, node_rollout.operation_id)
        assert operation is not None
        operation.state = "waiting-for-operator"

    orchestrator.advance(rollout_id)

    assert _rollout(harness, rollout_id).state == "paused"
    assert (
        _rollout_node(harness, rollout_id, NODE_A).failure_reason
        == "batch peer update state uncertain"
    )
    orchestrator.begin_rollback(
        rollout_id, actor="admin", request_id=str(uuid.uuid4())
    )
    assert _rollout_node(harness, rollout_id, NODE_A).rollback_operation_id is not None


def test_target_identity_is_revalidated_before_route_publication(tmp_path) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=True)
    orchestrator.advance(rollout_id)
    harness.clock.advance(31)
    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.build_digest = OLD_BUILD
        node.last_seen_at = harness.clock()

    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "target identity lost during soak"
    assert ("routes.restore", (NODE_A,)) not in harness.events


def test_identity_drift_during_route_restore_is_compensated_and_paused(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=True)
    orchestrator.advance(rollout_id)
    harness.clock.advance(31)
    orchestrator.advance(rollout_id)

    def drift_after_restore() -> None:
        with harness.sessions.begin() as session:
            node = session.get(AgentNode, NODE_A)
            assert node is not None
            node.state = "pending"
            node.last_seen_at = harness.clock()

    harness.routes.after_restore = drift_after_restore
    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "compensating-withdrawal"
    assert harness.events[-1] == ("routes.restore", (NODE_A,))
    with harness.sessions() as session:
        lease = session.get(NodeMutationLease, NODE_A)
        assert lease is not None and lease.state == "releasing"

    harness.routes.withdraw_failures = 1
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "compensating-withdrawal"
    with harness.sessions() as session:
        lease = session.get(NodeMutationLease, NODE_A)
        assert lease is not None and lease.state == "releasing"

    orchestrator.advance(rollout_id)

    assert harness.events[-2:] == [
        ("routes.restore", (NODE_A,)),
        ("routes.withdraw", (NODE_A,)),
    ]
    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "target identity lost during route publication"
    with harness.sessions() as session:
        lease = session.get(NodeMutationLease, NODE_A)
        assert lease is not None and lease.state == "held"


def test_stale_compensation_never_reholds_or_releases_the_rollout_lease(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=True)
    orchestrator.advance(rollout_id)
    harness.clock.advance(31)
    orchestrator.advance(rollout_id)

    def drift_after_restore() -> None:
        with harness.sessions.begin() as session:
            node = session.get(AgentNode, NODE_A)
            assert node is not None
            node.state = "pending"
            node.last_seen_at = harness.clock()

    harness.routes.after_restore = drift_after_restore
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "compensating-withdrawal"

    def supersede_compensation() -> None:
        with harness.sessions.begin() as session:
            rollout = session.get(UpdateRollout, rollout_id)
            assert rollout is not None
            rollout.state = "failed"
            rollout.updated_at = harness.clock()

    harness.routes.after_withdraw = supersede_compensation

    assert orchestrator.advance(rollout_id) == "failed"
    with harness.sessions() as session:
        lease = session.get(NodeMutationLease, NODE_A)
        assert lease is not None
        assert lease.state == "releasing"
        assert lease.owner_id == rollout_id


def test_stale_target_contact_blocks_route_restore_even_after_soak(tmp_path) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=True)
    orchestrator.advance(rollout_id)
    harness.clock.advance(301)

    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "target identity lost during soak"
    assert ("routes.restore", (NODE_A,)) not in harness.events


def test_later_batch_source_drift_blocks_withdrawal(tmp_path) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=True)
    orchestrator.advance(rollout_id)
    harness.clock.advance(31)
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "planned"
    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_B)
        assert node is not None
        node.build_digest = "sha256:" + "9" * 64
        node.last_seen_at = harness.clock()

    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "planned source identity changed"
    assert ("routes.withdraw", (NODE_B,)) not in harness.events


def test_execution_time_quorum_drift_blocks_withdrawal(tmp_path) -> None:
    harness = _harness(tmp_path)
    workload = updates.DistributedWorkload(
        workload_id="distributed-model",
        members=(NODE_A, NODE_B),
        minimum_available=1,
    )
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan((workload,)), actor="admin", request_id=str(uuid.uuid4())
    )
    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_B)
        assert node is not None
        node.state = "pending"

    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "distributed workload quorum changed"
    assert harness.events == []

    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_B)
        assert node is not None
        node.state = "active"
        node.last_seen_at = harness.clock()
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "planned"
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "withdrawing"


def test_other_owners_mutation_lease_counts_as_quorum_unavailable(tmp_path) -> None:
    harness = _harness(tmp_path)
    workload = updates.DistributedWorkload(
        workload_id="distributed-model",
        members=(NODE_A, NODE_B),
        minimum_available=1,
    )
    lease_service = NodeLeaseService(clock=harness.clock)
    with harness.sessions.begin() as session:
        lease_service.acquire_in_session(
            session,
            [NODE_B],
            owner_kind="reconciliation",
            owner_id=str(uuid.uuid4()),
        )
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan((workload,)), actor="admin", request_id=str(uuid.uuid4())
    )

    orchestrator.advance(rollout_id)

    rollout = _rollout(harness, rollout_id)
    assert rollout.state == "paused"
    assert rollout.failure_reason == "distributed workload quorum changed"
    assert harness.events == []


def test_successful_activation_that_reports_source_is_treated_as_auto_rollback(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(), actor="admin", request_id=str(uuid.uuid4())
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=False)
    with harness.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.supervisor_generation = 2
        node.supervisor_ready_generation = 2
        node.self_test_passed = True
        node.last_seen_at = harness.clock()
        _record_test_contact(session, node)

    orchestrator.advance(rollout_id)

    assert _rollout(harness, rollout_id).state == "rollback-publishing"
    assert _rollout_node(harness, rollout_id, NODE_A).state == "rolling-back"
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "waiting-for-approval"
    assert harness.events[-1] == ("routes.restore", (NODE_A,))


def test_multi_node_failure_quiesces_peers_before_targeted_rollback(tmp_path) -> None:
    harness = _harness(tmp_path, (NODE_A, NODE_B, NODE_C))
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(node_ids=(NODE_A, NODE_B, NODE_C), batch_size=2),
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=True)
    orchestrator.advance(rollout_id)
    harness.clock.advance(31)
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "updating"
    _fail_update_operation(harness, rollout_id, NODE_B)
    with harness.sessions.begin() as session:
        node_rollout = session.scalar(
            select(UpdateRolloutNode).where(
                UpdateRolloutNode.rollout_id == rollout_id,
                UpdateRolloutNode.node_id == NODE_C,
            )
        )
        assert node_rollout is not None and node_rollout.operation_id is not None
        operation = session.get(AgentOperation, node_rollout.operation_id)
        assert operation is not None
        operation.state = "running"
        operation.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=harness.clock() + timedelta(hours=1),
                agent_certificate_serial=f"candidate-{NODE_C}",
                state="running",
            )
        )

    orchestrator.advance(rollout_id)

    assert _rollout(harness, rollout_id).state == "paused"
    node_b = _rollout_node(harness, rollout_id, NODE_B)
    node_c = _rollout_node(harness, rollout_id, NODE_C)
    assert node_b.failure_reason == "agent update failed"
    assert node_c.failure_reason == "batch peer update state uncertain"
    with harness.sessions() as session:
        peer_operation = session.get(AgentOperation, node_c.operation_id)
        assert peer_operation is not None
        assert peer_operation.state == "waiting-for-operator"
        peer_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == peer_operation.id
            )
        )
        assert peer_attempt is not None and peer_attempt.state == "expired"

    orchestrator.begin_rollback(
        rollout_id, actor="admin", request_id=str(uuid.uuid4())
    )
    assert _rollout_node(harness, rollout_id, NODE_B).rollback_operation_id is None
    assert _rollout_node(harness, rollout_id, NODE_C).rollback_operation_id is not None


def test_mixed_target_and_auto_source_rolls_back_only_the_target(tmp_path) -> None:
    harness = _harness(tmp_path, (NODE_A, NODE_B, NODE_C))
    orchestrator = harness.orchestrator()
    rollout_id = orchestrator.create(
        _plan(node_ids=(NODE_A, NODE_B, NODE_C), batch_size=2),
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    _finish_update_operation(harness, rollout_id, NODE_A, reconnect_target=True)
    orchestrator.advance(rollout_id)
    harness.clock.advance(31)
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    orchestrator.advance(rollout_id)
    assert _rollout(harness, rollout_id).state == "updating"

    _finish_update_operation(harness, rollout_id, NODE_B, reconnect_target=True)
    _finish_update_operation(harness, rollout_id, NODE_C, reconnect_target=False)
    with harness.sessions.begin() as session:
        source = session.get(AgentNode, NODE_C)
        assert source is not None
        source.supervisor_generation = 2
        source.supervisor_ready_generation = 2
        source.self_test_passed = True
        source.last_seen_at = harness.clock()
        _record_test_contact(session, source)

    orchestrator.advance(rollout_id)

    assert _rollout(harness, rollout_id).state == "paused"
    assert _rollout_node(harness, rollout_id, NODE_C).state == "rolled-back"
    orchestrator.begin_rollback(
        rollout_id, actor="admin", request_id=str(uuid.uuid4())
    )
    node_b = _rollout_node(harness, rollout_id, NODE_B)
    node_c = _rollout_node(harness, rollout_id, NODE_C)
    assert node_b.rollback_operation_id is not None
    assert node_c.rollback_operation_id is None
