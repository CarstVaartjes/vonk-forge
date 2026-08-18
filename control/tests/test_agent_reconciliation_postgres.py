from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentResult, canonical_message
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.agent_reconciliation import AgentReconciliationService
from vonk_control.auth import AgentIdentity, AgentSource
from vonk_control.enrollment import EnrollmentService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AgentPresence,
    Base,
    Job,
    NodeMutationLease,
    Reconciliation,
    ReconciliationCancellation,
    ReconciliationOperation,
    RoutePublication,
    RoutePublicationOwner,
)
from vonk_control.node_leases import NodeLeaseConflict, NodeLeaseService
from vonk_control.pki import CertificateAuthority, IssuedCertificate
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.route_runtime import AtomicRouteBundlePublisher

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
NODE_C = "spk_" + "c" * 32
BASE_COMMIT = "a" * 40
NOW = datetime(2026, 8, 5, tzinfo=UTC)
AGENT_CAPABILITIES = (
    "agent.runtime.rust.v1",
    "node.probe",
    "release.install",
    "workload.health",
    "workload.prepare",
    "workload.start",
    "workload.stop",
    "workload.verify",
)


class RevokingAuthority(CertificateAuthority):
    def issue_node(
        self, node_id: str, csr_pem: bytes, now: datetime
    ) -> IssuedCertificate:
        raise NotImplementedError

    def renew_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        raise NotImplementedError

    def revocation_bundle(self, now: datetime) -> bytes:
        return b""

    def revoke_node(self, serial: str, now: datetime) -> None:
        return None


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


def _json_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _verify_result() -> dict[str, object]:
    return {
        "status": "ok",
        "evidence": {
            "status": "healthy",
            "action": "verify",
            "workload_id": "model",
            "release_digest": "a" * 64,
            "evidence_digest": "e" * 64,
        },
    }


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        pytest.fail("Docker is required for mandatory PostgreSQL races")
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-p",
            "127.0.0.1::5432",
            "postgres:16",
        ],
        text=True,
    ).strip()
    try:
        port = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                container,
            ],
            text=True,
        ).strip()
        engine = create_engine(
            f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres"
        )
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except (OSError, SQLAlchemyError):
                time.sleep(0.1)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(
            ["docker", "stop", container], check=False, capture_output=True
        )


def test_postgres_node_lease_race_has_one_database_owner(
    postgres_engine: Engine,
) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active", capabilities=[]))
    barrier = threading.Barrier(2)

    def acquire(owner_id: str) -> str | None:
        barrier.wait(timeout=5)
        try:
            with sessions.begin() as session:
                NodeLeaseService(clock=lambda: NOW).acquire_in_session(
                    session,
                    (NODE_A,),
                    owner_kind="update-rollout",
                    owner_id=owner_id,
                )
            return owner_id
        except NodeLeaseConflict:
            return None

    owners = (str(uuid.uuid4()), str(uuid.uuid4()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(acquire, owners))

    winner = next(result for result in results if result is not None)
    assert sum(result is not None for result in results) == 1
    with sessions() as session:
        row = session.get(NodeMutationLease, NODE_A)
        assert row is not None
        assert row.owner_id == winner


def _source(address: str) -> AgentSource:
    return AgentSource(
        AgentIdentity(NODE_A, "serial-a", "fingerprint-a", True),
        address,
    )


def _system(
    postgres_engine: Engine,
    route_root: Path,
    *,
    clock=lambda: NOW,
    await_supervisor_ack=None,
):
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    operation_id = f"model:{NODE_A}:workload.verify"
    payload = {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "a" * 64,
        "adapter_id": "node-runtime-v1",
        "expected_digest": "e" * 64,
    }
    operation = {
        "operation_id": operation_id,
        "node_id": NODE_A,
        "workload_id": "model",
        "kind": "workload.verify",
        "dependencies": [],
        "compensation_kind": None,
        "payload_digest": _digest(payload),
    }
    graph = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "targets": [NODE_A],
        "nodes": [operation],
    }
    quota = {"requests_per_minute": 20, "tokens_per_minute": 1000}
    routes = {
        "model": {
            "workload_id": "model",
            "nodes": [NODE_A],
            "entrypoint_node_id": NODE_A,
            "scheme": "http",
            "port": 8000,
            "path": "/v1",
            "quota": quota,
            "quota_digest": hashlib.sha256(
                canonical_message(quota)
            ).hexdigest(),
        }
    }
    resolved = {
        "commit": BASE_COMMIT,
        "targets": [NODE_A],
        "placements": {},
        "routes": routes,
        "releases": {},
        "workload_groups": {},
        "input_digests": {"fleet": "f" * 64},
        "fleet_evidence_digest": "e" * 64,
        "operation_graph": graph,
        "operation_payloads": {operation_id: payload},
        "agent_protocol_range": [3, 3],
    }
    reconciliation_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active", capabilities=[]))
        session.flush()
        session.add(
            AgentCertificate(
                serial="serial-a",
                node_id=NODE_A,
                not_before=NOW - timedelta(minutes=1),
                not_after=NOW + timedelta(hours=1),
                fingerprint="fingerprint-a",
            )
        )
        session.add(
            Reconciliation(
                id=reconciliation_id,
                base_commit=BASE_COMMIT,
                status="planned",
                summary={},
                graph=graph,
                graph_digest=_json_digest(graph),
                plan_digest=_json_digest(resolved),
                resolved_plan=resolved,
                current_phase="planned",
                route_withdrawal_generation=0,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid.uuid4()),
                kind="reconcile",
                state="queued",
                actor="operator",
                base_commit=BASE_COMMIT,
                targets=[NODE_A],
                payload_digest=_digest({}),
                payload={},
                current_attempt=0,
                created_at=NOW,
                updated_at=NOW,
                reconciliation_id=reconciliation_id,
            )
        )
    policy = ManagementAddressPolicy.parse("10.0.0.0/24")
    presence = AgentPresenceService(sessions, policy, clock=clock)
    presence.observe(_source("10.0.0.42"))
    operations = AgentJobService(sessions, clock=clock)
    publisher = AtomicRouteBundlePublisher(
        route_root,
        management_policy=policy,
        clock=clock,
        maximum_lease_seconds=300,
        await_supervisor_ack=await_supervisor_ack,
    )
    reconciliations = AgentReconciliationService(
        sessions,
        agent_jobs=operations,
        publisher=publisher,
        endpoint_resolver=lambda session, node_id: (
            presence.latest_in_session(
                session, node_id, maximum_age_seconds=300
            ).address,
            presence.latest_in_session(
                session, node_id, maximum_age_seconds=300
            ).observed_at,
        ),
        clock=clock,
    )
    operations.set_result_consumer(reconciliations.consume_result)
    return sessions, presence, operations, reconciliations, reconciliation_id, job_id


def _claimed(system):
    sessions, _presence, operations, reconciliations, reconciliation_id, job_id = system
    for _ in range(4):
        assert reconciliations.tick(reconciliation_id) is True
    claim = operations.claim(NODE_A, "serial-a", 30)
    assert claim is not None
    return sessions, operations, reconciliations, reconciliation_id, job_id, claim


def _clone_service(system) -> AgentReconciliationService:
    sessions, presence, operations, reconciliations, _reconciliation_id, _job_id = system

    def endpoint(session, node_id):
        observation = presence.latest_in_session(
            session, node_id, maximum_age_seconds=300
        )
        return observation.address, observation.observed_at

    return AgentReconciliationService(
        sessions,
        agent_jobs=operations,
        publisher=reconciliations._publisher,
        endpoint_resolver=endpoint,
        clock=reconciliations._clock,
    )


def test_postgres_authority_prefetch_runs_after_snapshot_locks_are_released(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    system = _system(postgres_engine, tmp_path / "authority-prefetch-locks")
    sessions, _presence, operations, original, reconciliation_id, job_id = system
    observed = {"unlocked": False}

    def prefetch(*_args) -> None:
        with sessions.begin() as competing:
            assert competing.scalar(
                select(Reconciliation)
                .where(Reconciliation.id == reconciliation_id)
                .with_for_update(nowait=True)
            ) is not None
            assert competing.scalar(
                select(Job).where(Job.id == job_id).with_for_update(nowait=True)
            ) is not None
            assert competing.scalar(
                select(AgentNode)
                .where(AgentNode.node_id == NODE_A)
                .with_for_update(nowait=True)
            ) is not None
            assert competing.scalar(
                select(AgentCertificate)
                .where(AgentCertificate.serial == "serial-a")
                .with_for_update(nowait=True)
            ) is not None
            assert competing.scalar(
                select(AgentPresence)
                .where(AgentPresence.node_id == NODE_A)
                .with_for_update(nowait=True)
            ) is not None
        observed["unlocked"] = True

    reconciliations = AgentReconciliationService(
        sessions,
        agent_jobs=operations,
        publisher=original._publisher,
        endpoint_resolver=original._endpoint_resolver,
        clock=lambda: NOW,
        authority_prefetch=prefetch,
        authority_check=lambda *_args: True,
        authority_clear=lambda: None,
    )

    assert reconciliations.tick(reconciliation_id) is True
    assert observed["unlocked"] is True


def _insert_successor(
    sessions,
    predecessor_id: str,
    *,
    created_at: datetime = NOW + timedelta(seconds=1),
    fleet_digest: str = "9" * 64,
) -> tuple[str, str]:
    reconciliation_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    with sessions.begin() as session:
        predecessor = session.get(Reconciliation, predecessor_id)
        assert predecessor is not None and predecessor.resolved_plan is not None
        resolved = deepcopy(predecessor.resolved_plan)
        resolved["input_digests"]["fleet"] = fleet_digest
        plan_digest = _json_digest(resolved)
        session.add(
            Reconciliation(
                id=reconciliation_id,
                base_commit=predecessor.base_commit,
                status="planned",
                summary=deepcopy(predecessor.summary),
                graph=deepcopy(predecessor.graph),
                graph_digest=predecessor.graph_digest,
                plan_digest=plan_digest,
                resolved_plan=resolved,
                current_phase="planned",
                route_withdrawal_generation=0,
                created_at=created_at,
            )
        )
        session.flush()
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid.uuid4()),
                kind="reconcile",
                state="queued",
                actor="operator",
                base_commit=predecessor.base_commit,
                targets=list(predecessor.graph["targets"]),
                payload_digest=_digest({}),
                payload={"reconciliation_id": reconciliation_id},
                current_attempt=0,
                created_at=created_at,
                updated_at=created_at,
                reconciliation_id=reconciliation_id,
            )
        )
    return reconciliation_id, job_id


def _mark_completed(
    sessions,
    reconciliation_id: str,
    *,
    owner: bool,
    now: datetime = NOW,
) -> None:
    with sessions.begin() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        job = session.scalar(
            select(Job).where(Job.reconciliation_id == reconciliation_id)
        )
        assert reconciliation is not None and job is not None
        generation = (
            session.scalar(select(func.max(Reconciliation.completion_generation)))
            or 0
        ) + 1
        reconciliation.current_phase = "completed"
        reconciliation.status = "succeeded"
        reconciliation.completion_generation = generation
        job.state = "succeeded"
        job.status_reason = None
        publication = session.get(RoutePublication, reconciliation_id)
        if publication is None:
            publication = RoutePublication(
                reconciliation_id=reconciliation_id,
                state="completed",
                generation=None,
                plan_digest=reconciliation.plan_digest,
            )
            session.add(publication)
        else:
            publication.state = "completed"
            publication.generation = None
        publication.lease_issued_at = now
        publication.lease_expires_at = now + timedelta(minutes=5)
        if owner:
            current_owner = session.get(RoutePublicationOwner, 1)
            if current_owner is None:
                session.add(
                    RoutePublicationOwner(
                        singleton_id=1,
                        reconciliation_id=reconciliation_id,
                        owner_generation=1,
                        updated_at=now,
                    )
                )
            else:
                current_owner.reconciliation_id = reconciliation_id
                current_owner.owner_generation += 1
                current_owner.updated_at = now


def _set_publication_owner(sessions, reconciliation_id: str) -> None:
    with sessions.begin() as session:
        owner = session.get(RoutePublicationOwner, 1)
        if owner is None:
            session.add(
                RoutePublicationOwner(
                    singleton_id=1,
                    reconciliation_id=reconciliation_id,
                    owner_generation=1,
                    updated_at=NOW,
                )
            )
        else:
            owner.reconciliation_id = reconciliation_id
            owner.owner_generation += 1
            owner.updated_at = NOW


def _complete(system) -> str:
    _sessions, operations, reconciliations, reconciliation_id, _job_id, claim = (
        _claimed(system)
    )
    operations.succeed(claim, _verify_result())
    for _ in range(3):
        assert reconciliations.tick(reconciliation_id) is True
    return reconciliation_id


def _race(*calls):
    start = threading.Barrier(len(calls))

    def invoke(call):
        start.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(invoke, call) for call in calls]
        return [future.result(timeout=10) for future in futures]


def test_postgres_contact_failure_rolls_back_claim_lease_and_presence(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "claim-contact")
    sessions, presence, operations, reconciliations, reconciliation_id, _job_id = system

    def reject_contact(session, source) -> None:
        presence.observe_in_session(session, source)
        raise ValueError("contact write rejected")

    operations.set_contact_consumer(reject_contact)
    for _ in range(4):
        reconciliations.tick(reconciliation_id)
    with pytest.raises(ValueError, match="contact write rejected"):
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            source=_source("10.0.0.43"),
        )

    with sessions() as session:
        stored = session.scalar(select(AgentOperation))
        contact = session.get(AgentPresence, NODE_A)
        assert stored is not None and stored.state == "queued"
        assert stored.current_attempt == 0
        assert session.scalar(select(func.count()).select_from(AgentOperationAttempt)) == 0
        assert contact is not None and contact.management_address == "10.0.0.42"


def test_postgres_phase_rejection_rolls_back_result_contact_and_projection(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "result-contact")
    sessions, presence, operations, _reconciliations, reconciliation_id, job_id = system
    operations.set_contact_consumer(presence.observe_in_session)
    sessions, operations, _reconciliations, reconciliation_id, job_id, claim = _claimed(
        system
    )
    with sessions.begin() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert stored is not None and job is not None
        stored.current_phase = "failed"
        stored.status = "failed"
        job.state = "failed"
    message = AgentResult(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state="succeeded",
        result=_verify_result(),
    )

    with pytest.raises(StaleAgentAttempt):
        operations.record_result(message, source=_source("10.0.0.44"))

    with sessions() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        attempt = session.scalar(select(AgentOperationAttempt))
        projection = session.scalar(select(ReconciliationOperation))
        contact = session.get(AgentPresence, NODE_A)
        assert operation is not None and operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.result is None
        assert projection is not None and projection.state == "queued"
        assert contact is not None and contact.management_address == "10.0.0.42"


def test_postgres_stale_result_does_not_write_contact(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "stale-contact")
    sessions, presence, operations, _reconciliations, _reconciliation_id, _job_id = system
    operations.set_contact_consumer(presence.observe_in_session)
    sessions, operations, _reconciliations, _reconciliation_id, _job_id, claim = _claimed(
        system
    )
    stale = AgentResult(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=str(uuid.uuid4()),
        node_id=claim.node_id,
        deadline=claim.deadline,
        state="succeeded",
        result=_verify_result(),
    )

    with pytest.raises(StaleAgentAttempt):
        operations.record_result(stale, source=_source("10.0.0.45"))

    with sessions() as session:
        contact = session.get(AgentPresence, NODE_A)
        assert contact is not None and contact.management_address == "10.0.0.42"


def test_postgres_publication_reuses_tick_session_for_presence(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "publication-session")
    _sessions, _presence, operations, reconciliations, reconciliation_id, _job_id = system
    _sessions, operations, reconciliations, reconciliation_id, _job_id, claim = _claimed(
        system
    )
    operations.succeed(claim, _verify_result())
    reconciliations.tick(reconciliation_id)
    reconciliations.tick(reconciliation_id)

    def bounded_lock_wait(_dbapi_connection, connection_record, connection_proxy) -> None:
        del connection_record, connection_proxy
        with _dbapi_connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '500ms'")

    event.listen(postgres_engine, "checkout", bounded_lock_wait)
    try:
        assert reconciliations.tick(reconciliation_id) is True
    finally:
        event.remove(postgres_engine, "checkout", bounded_lock_wait)

    with _sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        assert stored is not None and stored.current_phase == "completed"


def test_postgres_tick_tick_race_enqueues_one_operation(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "tick-tick")
    sessions, _presence, _operations, reconciliations, reconciliation_id, _job_id = system
    for _ in range(3):
        reconciliations.tick(reconciliation_id)
    other = _clone_service(system)

    assert sorted(_race(reconciliations.tick, other.tick)) == [False, True]

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentOperation)) == 1
        assert (
            session.scalar(select(func.count()).select_from(ReconciliationOperation))
            == 1
        )


def test_postgres_result_tick_race_preserves_exact_acceptance(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "result-tick")
    sessions, operations, _reconciliations, _reconciliation_id, _job_id, claim = _claimed(
        system
    )
    other = _clone_service(system)

    outcomes = _race(
        lambda: operations.succeed(claim, _verify_result()),
        other.tick,
    )

    assert outcomes[0] is None
    if outcomes[1] is False:
        assert other.tick() is True
    else:
        assert outcomes[1] is True
    with sessions() as session:
        projection = session.scalar(select(ReconciliationOperation))
        assert projection is not None and projection.state == "accepted"
        assert projection.result_digest == _digest(_verify_result())
        assert session.scalar(select(func.count()).select_from(AgentOperation)) == 1


def test_postgres_result_revocation_race_never_publishes_revoked_target(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    system = _system(postgres_engine, tmp_path / "result-revocation")
    sessions, operations, reconciliations, reconciliation_id, job_id, claim = _claimed(
        system
    )
    enrollment = EnrollmentService(sessions, RevokingAuthority(), clock=lambda: NOW)

    try:
        _race(
            lambda: operations.succeed(claim, _verify_result()),
            lambda: enrollment.revoke_node(NODE_A, "administrator"),
        )
    except StaleAgentAttempt:
        pass
    assert reconciliations.tick(reconciliation_id) is True

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        stored = session.get(Reconciliation, reconciliation_id)
        job = session.get(Job, job_id)
        assert node is not None and node.state == "retired"
        assert stored is not None
        assert stored.current_phase == "waiting-for-operator"
        assert stored.status == "failed"
        assert job is not None and job.state == "waiting-for-operator"
        marker = json.loads(
            (tmp_path / "result-revocation" / "activation.json").read_bytes()
        )
        assert marker["state"] == "maintenance"


def test_postgres_publication_publication_race_activates_one_bundle(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    route_root = tmp_path / "publication-publication"
    system = _system(postgres_engine, route_root)
    sessions, operations, reconciliations, reconciliation_id, _job_id, claim = _claimed(
        system
    )
    operations.succeed(claim, _verify_result())
    reconciliations.tick(reconciliation_id)
    reconciliations.tick(reconciliation_id)
    other = _clone_service(system)

    outcomes = _race(reconciliations.tick, other.tick)

    assert sorted(outcomes) == [False, True]
    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "published"
    assert marker["reconciliation_id"] == reconciliation_id
    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        publication = session.get(RoutePublication, reconciliation_id)
        assert stored is not None and stored.current_phase == "completed"
        assert publication is not None and publication.state == "completed"


def test_postgres_newer_noncompleted_owner_survives_old_completed_restart(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """Restarting an old completed row must not replace newer maintenance."""
    route_root = tmp_path / "owner-restart"
    system = _system(postgres_engine, route_root)
    sessions, _presence, _operations, reconciliations, old_id, _job_id = system
    assert _complete(system) == old_id
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    assert reconciliations.tick(new_id) is True
    assert reconciliations.tick(new_id) is True
    before = json.loads((route_root / "activation.json").read_bytes())
    assert before["state"] == "maintenance"
    assert before["reconciliation_id"] == new_id

    restarted = _clone_service(system)
    assert restarted.tick(old_id) is False

    after = json.loads((route_root / "activation.json").read_bytes())
    assert after == before


def test_postgres_successor_owner_transfers_only_after_exact_withdrawal_ack(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "owner-after-exact-ack"
    acknowledged = []
    blocked_reconciliation = [None]
    ack_entered = threading.Event()
    release_ack = threading.Event()

    def acknowledge(marker) -> None:
        acknowledged.append(marker)
        if (
            marker.reconciliation_id == blocked_reconciliation[0]
            and marker.state == "maintenance"
        ):
            ack_entered.set()
            assert release_ack.wait(timeout=5)

    system = _system(
        postgres_engine,
        route_root,
        await_supervisor_ack=acknowledge,
    )
    sessions, _presence, _operations, service, old_id, _job_id = system
    assert _complete(system) == old_id
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        assert owner is not None and owner.reconciliation_id == old_id
        old_generation = owner.owner_generation
    published = (route_root / "activation.json").read_bytes()

    assert service.tick(new_id) is True

    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        successor = session.get(Reconciliation, new_id)
        publication = session.get(RoutePublication, new_id)
        assert owner is not None and owner.reconciliation_id == old_id
        assert owner.owner_generation == old_generation
        assert successor is not None
    assert successor.current_phase == "withdrawal-pending"
    assert publication is not None and publication.state == "withdrawal-pending"
    assert (route_root / "activation.json").read_bytes() == published
    assert service.tick(old_id) is False
    assert (route_root / "activation.json").read_bytes() == published

    blocked_reconciliation[0] = new_id
    outcomes: list[object] = []

    def withdraw_successor() -> None:
        try:
            outcomes.append(service.tick(new_id))
        except RuntimeError as error:  # pragma: no cover - asserted below
            outcomes.append(error)

    worker = threading.Thread(target=withdraw_successor)
    worker.start()
    assert ack_entered.wait(timeout=5)
    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        assert owner is not None and owner.reconciliation_id == old_id
        assert owner.owner_generation == old_generation
    release_ack.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcomes == [True]

    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        successor = session.get(Reconciliation, new_id)
        publication = session.get(RoutePublication, new_id)
        assert owner is not None and owner.reconciliation_id == new_id
        assert owner.owner_generation == old_generation + 1
        assert successor is not None and successor.current_phase == "routes-withdrawn"
        assert publication is not None and publication.state == "routes-withdrawn"
    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "maintenance"
    assert marker["reconciliation_id"] == new_id
    assert acknowledged[-1].reconciliation_id == new_id


def test_postgres_only_one_unacknowledged_publication_handoff_is_registered(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    system = _system(postgres_engine, tmp_path / "one-pending-publication-handoff")
    sessions, _presence, _operations, service, old_id, _job_id = system
    assert _complete(system) == old_id
    first_id, _first_job_id = _insert_successor(sessions, old_id)
    second_id, _second_job_id = _insert_successor(
        sessions,
        old_id,
        created_at=NOW + timedelta(seconds=2),
        fleet_digest="8" * 64,
    )

    assert service.tick(first_id) is True
    assert service.tick(second_id) is False

    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        first = session.get(Reconciliation, first_id)
        second = session.get(Reconciliation, second_id)
        pending = list(
            session.scalars(
                select(RoutePublication).where(
                    RoutePublication.state == "withdrawal-pending"
                )
            )
        )
        assert owner is not None and owner.reconciliation_id == old_id
        assert first is not None and first.current_phase == "withdrawal-pending"
        assert second is not None and second.current_phase == "planned"
        assert [publication.reconciliation_id for publication in pending] == [first_id]


def test_postgres_successor_authority_loss_withdraws_predecessor_before_wait(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "authority-loss-during-handoff"
    acknowledged = []
    authority = {"eligible": True, "commit": BASE_COMMIT}
    system = _system(
        postgres_engine,
        route_root,
        await_supervisor_ack=acknowledged.append,
    )
    sessions, _presence, _operations, service, old_id, _job_id = system
    service._commit_eligible = lambda commit: authority["eligible"] and commit == BASE_COMMIT
    service._current_commit = lambda: authority["commit"]
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.protocol_version = 3
        node.capabilities = list(AGENT_CAPABILITIES)
    assert _complete(system) == old_id
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    assert service.tick(new_id) is True
    authority["eligible"] = False

    assert service.tick(new_id) is True

    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "maintenance"
    assert marker["reconciliation_id"] == new_id
    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        successor = session.get(Reconciliation, new_id)
        publication = session.get(RoutePublication, new_id)
        assert owner is not None and owner.reconciliation_id == new_id
        assert successor is not None and successor.current_phase == "routes-withdrawn"
        assert publication is not None and publication.state == "routes-withdrawn"
    assert acknowledged[-1].state == "maintenance"
    assert acknowledged[-1].reconciliation_id == new_id

    assert service.tick(new_id) is True
    with sessions() as session:
        successor = session.get(Reconciliation, new_id)
        job = session.scalar(select(Job).where(Job.reconciliation_id == new_id))
        assert successor is not None
        assert successor.current_phase == "waiting-for-operator"
        assert job is not None and job.state == "waiting-for-operator"
    assert json.loads((route_root / "activation.json").read_bytes()) == marker


def test_postgres_pending_successor_does_not_block_fail_closed_owner_withdrawal(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "pending-successor-owner-authority-loss"
    acknowledged = []
    authority = {"eligible": True}
    system = _system(
        postgres_engine,
        route_root,
        await_supervisor_ack=acknowledged.append,
    )
    sessions, _presence, _operations, service, old_id, _job_id = system
    service._commit_eligible = lambda commit: authority["eligible"] and commit == BASE_COMMIT
    service._current_commit = lambda: BASE_COMMIT
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.protocol_version = 3
        node.capabilities = list(AGENT_CAPABILITIES)
    assert _complete(system) == old_id
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    assert service.tick(new_id) is True
    authority["eligible"] = False

    assert service.tick() is True

    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "maintenance"
    with sessions() as session:
        old = session.get(Reconciliation, old_id)
        successor = session.get(Reconciliation, new_id)
        owner = session.get(RoutePublicationOwner, 1)
        assert old is not None and old.current_phase == "waiting-for-operator"
        assert successor is not None
        assert successor.current_phase == "withdrawal-pending"
        assert owner is not None and owner.reconciliation_id == old_id
    assert acknowledged[-1].state == "maintenance"

    assert service.tick() is True
    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "maintenance"
    assert marker["reconciliation_id"] == new_id
    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        assert owner is not None and owner.reconciliation_id == new_id


def test_postgres_publication_ack_crash_then_authority_loss_withdraws_routes(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "publication-ack-crash-authority-loss"
    acknowledged = []
    authority = {"eligible": True}
    system = _system(
        postgres_engine,
        route_root,
        await_supervisor_ack=acknowledged.append,
    )
    sessions, _presence, operations, service, reconciliation_id, _job_id = system
    service._commit_eligible = lambda commit: authority["eligible"] and commit == BASE_COMMIT
    service._current_commit = lambda: BASE_COMMIT
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.protocol_version = 3
        node.capabilities = list(AGENT_CAPABILITIES)

    _sessions, operations, service, _reconciliation_id, _job_id, claim = _claimed(
        system
    )
    operations.succeed(claim, _verify_result())
    assert service.tick(reconciliation_id) is True
    assert service.tick(reconciliation_id) is True
    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        owner = session.get(RoutePublicationOwner, 1)
        assert reconciliation is not None
        assert reconciliation.current_phase == "publication-pending"
        assert owner is not None and owner.reconciliation_id == reconciliation_id

    inner = service._publisher

    class CrashAfterPublishAcknowledgement:
        def withdraw(self, **kwargs):
            return inner.withdraw(**kwargs)

        def publish(self, request):
            inner.publish(request)
            raise RuntimeError("crash after exact publication acknowledgement")

    service._publisher = CrashAfterPublishAcknowledgement()
    with pytest.raises(
        RuntimeError, match="crash after exact publication acknowledgement"
    ):
        service.tick(reconciliation_id)
    published = json.loads((route_root / "activation.json").read_bytes())
    assert published["state"] == "published"
    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        publication = session.get(RoutePublication, reconciliation_id)
        assert reconciliation is not None
        assert reconciliation.current_phase == "publication-pending"
        assert publication is not None and publication.state == "publication-pending"

    service._publisher = inner
    authority["eligible"] = False
    assert service.tick(reconciliation_id) is True

    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "maintenance"
    assert marker["reconciliation_id"] == reconciliation_id
    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        publication = session.get(RoutePublication, reconciliation_id)
        job = session.scalar(
            select(Job).where(Job.reconciliation_id == reconciliation_id)
        )
        assert reconciliation is not None
        assert reconciliation.current_phase == "waiting-for-operator"
        assert publication is not None and publication.state == "routes-withdrawn"
        assert job is not None and job.state == "waiting-for-operator"
    assert acknowledged[-1].state == "maintenance"


def test_postgres_publication_ack_crash_then_cancellation_withdraws_first(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "publication-ack-crash-cancellation"
    acknowledged = []
    system = _system(
        postgres_engine,
        route_root,
        await_supervisor_ack=acknowledged.append,
    )
    sessions, _presence, operations, service, reconciliation_id, _job_id = system
    _sessions, operations, service, _reconciliation_id, _job_id, claim = _claimed(
        system
    )
    operations.succeed(claim, _verify_result())
    assert service.tick(reconciliation_id) is True
    assert service.tick(reconciliation_id) is True

    inner = service._publisher

    class CrashAfterPublishAcknowledgement:
        def withdraw(self, **kwargs):
            return inner.withdraw(**kwargs)

        def publish(self, request):
            inner.publish(request)
            raise RuntimeError("crash after exact publication acknowledgement")

    service._publisher = CrashAfterPublishAcknowledgement()
    with pytest.raises(
        RuntimeError, match="crash after exact publication acknowledgement"
    ):
        service.tick(reconciliation_id)
    assert json.loads((route_root / "activation.json").read_bytes())["state"] == (
        "published"
    )

    service._publisher = inner
    service.enqueue_cancel(
        reconciliation_id,
        "cancel acknowledged pending publication",
        actor="administrator",
        request_id=str(uuid.uuid4()),
    )
    assert service.tick(reconciliation_id) is True
    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        cancellation = session.get(ReconciliationCancellation, reconciliation_id)
        assert reconciliation is not None
        assert reconciliation.current_phase == "publication-pending"
        assert cancellation is not None and cancellation.state == "withdrawal-pending"
    assert json.loads((route_root / "activation.json").read_bytes())["state"] == (
        "published"
    )

    assert service.tick(reconciliation_id) is True
    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "maintenance"
    assert marker["reconciliation_id"] == reconciliation_id
    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        cancellation = session.get(ReconciliationCancellation, reconciliation_id)
        publication = session.get(RoutePublication, reconciliation_id)
        assert reconciliation is not None
        assert reconciliation.current_phase == "publication-pending"
        assert cancellation is not None and cancellation.state == "withdrawn"
        assert publication is not None and publication.state == "routes-withdrawn"
    assert acknowledged[-1].state == "maintenance"

    assert service.tick(reconciliation_id) is True
    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        cancellation = session.get(ReconciliationCancellation, reconciliation_id)
        assert reconciliation is not None
        assert reconciliation.current_phase == "cancelled"
        assert cancellation is not None and cancellation.state == "completed"
    assert json.loads((route_root / "activation.json").read_bytes()) == marker


def test_postgres_successor_withdrawal_crash_restarts_before_owner_transfer(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "handoff-crash-before-owner-commit"
    acknowledged = []
    system = _system(
        postgres_engine,
        route_root,
        await_supervisor_ack=acknowledged.append,
    )
    sessions, _presence, _operations, service, old_id, _job_id = system
    assert _complete(system) == old_id
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        assert owner is not None
        owner_generation = owner.owner_generation
    assert service.tick(new_id) is True
    inner = service._publisher

    class CrashAfterAcknowledgement:
        def withdraw(self, **kwargs):
            marker = inner.withdraw(**kwargs)
            if kwargs["reconciliation_id"] == new_id:
                raise RuntimeError("crash after exact maintenance acknowledgement")
            return marker

        def publish(self, request):
            return inner.publish(request)

    service._publisher = CrashAfterAcknowledgement()
    with pytest.raises(RuntimeError, match="after exact maintenance acknowledgement"):
        service.tick(new_id)

    maintenance = (route_root / "activation.json").read_bytes()
    maintenance_marker = json.loads(maintenance)
    assert maintenance_marker["state"] == "maintenance"
    assert maintenance_marker["reconciliation_id"] == new_id
    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        successor = session.get(Reconciliation, new_id)
        publication = session.get(RoutePublication, new_id)
        assert owner is not None and owner.reconciliation_id == old_id
        assert owner.owner_generation == owner_generation
        assert successor is not None
        assert successor.current_phase == "withdrawal-pending"
        assert publication is not None and publication.state == "withdrawal-pending"

    assert service.tick(old_id) is False
    assert (route_root / "activation.json").read_bytes() == maintenance
    service._publisher = inner
    restarted = _clone_service(system)

    assert restarted.tick(new_id) is True

    assert (route_root / "activation.json").read_bytes() == maintenance
    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        successor = session.get(Reconciliation, new_id)
        publication = session.get(RoutePublication, new_id)
        assert owner is not None and owner.reconciliation_id == new_id
        assert owner.owner_generation == owner_generation + 1
        assert successor is not None and successor.current_phase == "routes-withdrawn"
        assert publication is not None and publication.state == "routes-withdrawn"
        assert publication.activation_marker == maintenance_marker
    successor_acks = [
        marker
        for marker in acknowledged
        if marker.reconciliation_id == new_id and marker.state == "maintenance"
    ]
    assert len(successor_acks) == 2
    assert successor_acks[0] == successor_acks[1]


def test_postgres_newer_maintenance_wins_completed_owner_critical_section(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """The target lock must serialize R2 ownership before rejecting R1 renewal."""
    route_root = tmp_path / "owner-race"
    system = _system(postgres_engine, route_root)
    sessions, _presence, _operations, reconciliations, old_id, _job_id = system
    assert _complete(system) == old_id
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    assert reconciliations.tick(new_id) is True

    entered_new_withdrawal = threading.Event()
    release_new_withdrawal = threading.Event()
    old_lock_query = threading.Event()
    inner = reconciliations._publisher

    class BlockingPublisher:
        def withdraw(self, **kwargs):
            if kwargs["reconciliation_id"] == new_id:
                entered_new_withdrawal.set()
                assert release_new_withdrawal.wait(timeout=5)
            return inner.withdraw(**kwargs)

        def publish(self, request):
            return inner.publish(request)

    blocking = BlockingPublisher()
    reconciliations._publisher = blocking
    old_service = _clone_service(system)
    old_service._publisher = blocking

    def observe_old_lock(
        _conn, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            threading.current_thread().name == "old-publication-owner"
            and "agent_nodes" in statement
            and "FOR UPDATE" in statement
        ):
            old_lock_query.set()

    outcomes: dict[str, object] = {}
    event.listen(postgres_engine, "before_cursor_execute", observe_old_lock)
    try:
        newer = threading.Thread(
            target=lambda: outcomes.setdefault("new", reconciliations.tick(new_id)),
            name="new-publication-owner",
        )
        older = threading.Thread(
            target=lambda: outcomes.setdefault("old", old_service.tick(old_id)),
            name="old-publication-owner",
        )
        newer.start()
        assert entered_new_withdrawal.wait(timeout=5)
        older.start()
        assert old_lock_query.wait(timeout=5)
        release_new_withdrawal.set()
        newer.join(timeout=5)
        older.join(timeout=5)
    finally:
        release_new_withdrawal.set()
        event.remove(postgres_engine, "before_cursor_execute", observe_old_lock)

    assert not newer.is_alive() and not older.is_alive()
    assert outcomes == {"new": True, "old": False}
    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "maintenance"
    assert marker["reconciliation_id"] == new_id


def test_postgres_old_completed_cancellation_cannot_clobber_newer_owner(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    route_root = tmp_path / "owner-cancellation"
    system = _system(postgres_engine, route_root)
    sessions, _presence, _operations, reconciliations, old_id, _job_id = system
    assert _complete(system) == old_id
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    assert reconciliations.tick(new_id) is True
    assert reconciliations.tick(new_id) is True
    before = (route_root / "activation.json").read_bytes()

    reconciliations.request_cancel(old_id, "operator cancelled historical plan")

    assert (route_root / "activation.json").read_bytes() == before
    with sessions() as session:
        old = session.get(Reconciliation, old_id)
        assert old is not None and old.current_phase == "cancelled"


def test_postgres_predecessor_cancellation_rechecks_pending_successor_before_effect(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "pending-owner-cancellation"
    system = _system(postgres_engine, route_root)
    sessions, _presence, _operations, service, old_id, _job_id = system
    assert _complete(system) == old_id
    service.enqueue_cancel(
        old_id,
        "operator cancelled predecessor",
        actor="administrator",
        request_id=str(uuid.uuid4()),
    )
    assert service.tick(old_id) is True
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    assert service.tick(new_id) is True
    published = (route_root / "activation.json").read_bytes()

    assert service.tick(old_id) is False

    assert (route_root / "activation.json").read_bytes() == published
    with sessions() as session:
        cancellation = session.get(ReconciliationCancellation, old_id)
        owner = session.get(RoutePublicationOwner, 1)
        assert cancellation is not None
        assert cancellation.state == "withdrawal-pending"
        assert owner is not None and owner.reconciliation_id == old_id

    assert service.tick(new_id) is True
    maintenance = (route_root / "activation.json").read_bytes()
    for _ in range(3):
        service.tick(old_id)
    assert (route_root / "activation.json").read_bytes() == maintenance
    with sessions() as session:
        old = session.get(Reconciliation, old_id)
        owner = session.get(RoutePublicationOwner, 1)
        assert old is not None and old.current_phase == "cancelled"
        assert owner is not None and owner.reconciliation_id == new_id


def test_postgres_automatic_cancellation_waits_for_pending_successor_withdrawal(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "automatic-pending-owner-cancellation"
    acknowledged = []
    system = _system(
        postgres_engine,
        route_root,
        await_supervisor_ack=acknowledged.append,
    )
    sessions, _presence, _operations, service, old_id, _job_id = system
    assert _complete(system) == old_id
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    assert service.tick(new_id) is True
    service.enqueue_cancel(
        old_id,
        "operator cancelled predecessor during successor handoff",
        actor="administrator",
        request_id=str(uuid.uuid4()),
    )

    assert service.tick() is True

    marker = json.loads((route_root / "activation.json").read_bytes())
    assert marker["state"] == "maintenance"
    assert marker["reconciliation_id"] == new_id
    with sessions() as session:
        old = session.get(Reconciliation, old_id)
        cancellation = session.get(ReconciliationCancellation, old_id)
        owner = session.get(RoutePublicationOwner, 1)
        successor = session.get(Reconciliation, new_id)
        assert old is not None and old.current_phase == "completed"
        assert cancellation is not None and cancellation.state == "requested"
        assert owner is not None and owner.reconciliation_id == new_id
        assert successor is not None and successor.current_phase == "routes-withdrawn"
    assert acknowledged[-1].state == "maintenance"

    outcomes = [service.tick() for _ in range(4)]
    assert any(outcomes)
    with sessions() as session:
        old = session.get(Reconciliation, old_id)
        cancellation = session.get(ReconciliationCancellation, old_id)
        assert old is not None and old.current_phase == "cancelled"
        assert cancellation is not None and cancellation.state == "completed"
    assert json.loads((route_root / "activation.json").read_bytes()) == marker


def _compensation_system(
    postgres_engine: Engine,
    route_root: Path,
    *,
    parallel: bool = False,
    operation_nodes: tuple[str, ...] | None = None,
    clock=lambda: NOW,
):
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    selected_nodes = operation_nodes or ((NODE_A, NODE_B) if parallel else (NODE_A,))
    target_nodes = tuple(dict.fromkeys((NODE_A, NODE_B, *selected_nodes)))
    operation_ids = [f"model:{node_id}:workload.start" for node_id in selected_nodes]
    payload = {
        "schema_version": 1,
        "workload_id": "model",
        "release_digest": "a" * 64,
        "adapter_id": "node-runtime-v1",
        "preparation_digest": "d" * 64,
    }
    graph = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "targets": list(target_nodes),
        "nodes": [
            {
                "operation_id": operation_id,
                "node_id": node_id,
                "workload_id": "model",
                "kind": "workload.start",
                "dependencies": [],
                "compensation_kind": "workload.stop",
                "payload_digest": _digest(payload),
            }
            for operation_id, node_id in zip(operation_ids, selected_nodes, strict=True)
        ],
    }
    resolved = {
        "commit": BASE_COMMIT,
        "targets": list(target_nodes),
        "placements": {},
        "routes": {},
        "releases": {},
        "workload_groups": {},
        "input_digests": {"fleet": "f" * 64},
        "fleet_evidence_digest": "e" * 64,
        "operation_graph": graph,
        "operation_payloads": {
            operation_id: payload for operation_id in operation_ids
        },
        "agent_protocol_range": [3, 3],
    }
    reconciliation_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    primary_ids = [str(uuid.uuid4()) for _ in operation_ids]
    with sessions.begin() as session:
        session.add_all(
            AgentNode(node_id=node_id, state="active", capabilities=[])
            for node_id in target_nodes
        )
        session.flush()
        session.add_all(
            AgentCertificate(
                serial=f"serial-{node_id[-1]}",
                node_id=node_id,
                not_before=NOW - timedelta(minutes=1),
                not_after=NOW + timedelta(hours=1),
                fingerprint=f"fingerprint-{node_id[-1]}",
            )
            for node_id in target_nodes
        )
        session.add(
            Reconciliation(
                id=reconciliation_id,
                base_commit=BASE_COMMIT,
                status="running",
                summary={},
                graph=graph,
                graph_digest=_json_digest(graph),
                plan_digest=_json_digest(resolved),
                resolved_plan=resolved,
                current_phase="compensating",
                route_withdrawal_generation=0,
                terminal_reason="start failed",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid.uuid4()),
                kind="reconcile",
                state="running",
                actor="operator",
                base_commit=BASE_COMMIT,
                targets=list(target_nodes),
                payload_digest=_digest({}),
                payload={},
                current_attempt=0,
                created_at=NOW,
                updated_at=NOW,
                reconciliation_id=reconciliation_id,
            )
        )
        session.flush()
        session.add_all(
            [
                AgentOperation(
                    id=primary_id,
                    parent_job_id=job_id,
                    node_id=node_id,
                    kind="workload.start",
                    payload_digest=_digest(payload),
                    payload=payload,
                    base_commit=BASE_COMMIT,
                    state="succeeded",
                    current_attempt=1,
                    created_at=NOW,
                    updated_at=NOW,
                )
                for primary_id, node_id in zip(
                    primary_ids, selected_nodes, strict=True
                )
            ]
        )
        session.add(
            RoutePublication(
                reconciliation_id=reconciliation_id,
                state="routes-withdrawn",
                generation=1,
                plan_digest=_json_digest(resolved),
            )
        )
        session.add(
            RoutePublicationOwner(
                singleton_id=1,
                reconciliation_id=reconciliation_id,
                owner_generation=1,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            [
                ReconciliationOperation(
                    reconciliation_id=reconciliation_id,
                    graph_operation_id=operation_id,
                    role="primary",
                    agent_operation_id=primary_id,
                    expected_payload_digest=_digest(payload),
                    state="accepted",
                    result_digest="1" * 64,
                    evidence_digest="2" * 64,
                    accepted_at=NOW,
                )
                for operation_id, primary_id in zip(
                    operation_ids,
                    primary_ids,
                    strict=True,
                )
            ]
        )
    policy = ManagementAddressPolicy.parse("10.0.0.0/24")
    queue = AgentJobService(sessions, clock=clock)
    publisher = AtomicRouteBundlePublisher(
        route_root,
        management_policy=policy,
        clock=clock,
    )
    service = AgentReconciliationService(
        sessions,
        agent_jobs=queue,
        publisher=publisher,
        endpoint_resolver=lambda _session, _node: ("10.0.0.42", NOW),
        clock=clock,
    )
    return sessions, queue, service, reconciliation_id


def test_postgres_historical_mutation_cancellation_does_not_compensate_new_owner(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "historical-mutation-cancellation"
    sessions, _queue, service, old_id = _compensation_system(
        postgres_engine,
        route_root,
    )
    with sessions.begin() as session:
        old = session.get(Reconciliation, old_id)
        job = session.scalar(select(Job).where(Job.reconciliation_id == old_id))
        publication = session.get(RoutePublication, old_id)
        assert old is not None and job is not None and publication is not None
        old.current_phase = "completed"
        old.status = "succeeded"
        old.completion_generation = 1
        old.terminal_reason = None
        job.state = "succeeded"
        job.status_reason = None
        publication.state = "completed"
        publication.generation = None
        publication.lease_issued_at = NOW
        publication.lease_expires_at = NOW + timedelta(minutes=5)
    new_id, _new_job_id = _insert_successor(sessions, old_id)
    assert service.tick(new_id) is True
    assert service.tick(new_id) is True
    maintenance = (route_root / "activation.json").read_bytes()
    service.enqueue_cancel(
        old_id,
        "cancel historical mutation after successor owns maintenance",
        actor="administrator",
        request_id=str(uuid.uuid4()),
    )

    outcomes = [service.tick() for _ in range(5)]

    assert any(outcomes)
    with sessions() as session:
        old = session.get(Reconciliation, old_id)
        cancellation = session.get(ReconciliationCancellation, old_id)
        owner = session.get(RoutePublicationOwner, 1)
        stops = list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "workload.stop")
            )
        )
        assert old is not None and old.current_phase == "cancelled"
        assert cancellation is not None and cancellation.state == "completed"
        assert owner is not None and owner.reconciliation_id == new_id
        assert stops == []
    assert (route_root / "activation.json").read_bytes() == maintenance


def test_postgres_compensation_tick_race_enqueues_one_stop(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    sessions, queue, service, _reconciliation_id = _compensation_system(
        postgres_engine, tmp_path / "compensation-tick"
    )
    other = AgentReconciliationService(
        sessions,
        agent_jobs=queue,
        publisher=service._publisher,
        endpoint_resolver=lambda _session, _node: ("10.0.0.42", NOW),
        clock=lambda: NOW,
    )

    assert sorted(_race(service.tick, other.tick)) == [False, True]

    with sessions() as session:
        compensations = list(
            session.scalars(
                select(ReconciliationOperation).where(
                    ReconciliationOperation.role == "compensation"
                )
            )
        )
        stops = list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "workload.stop")
            )
        )
        assert len(compensations) == len(stops) == 1
        assert compensations[0].agent_operation_id == stops[0].id


@pytest.mark.parametrize("sibling_state", ["queued", "running"])
def test_postgres_agent_declared_uncertainty_quiesces_all_primary_siblings(
    postgres_engine: Engine,
    tmp_path: Path,
    sibling_state: str,
) -> None:
    sessions, queue, service, reconciliation_id = _compensation_system(
        postgres_engine,
        tmp_path / f"declared-uncertainty-{sibling_state}",
        parallel=True,
    )
    queue.set_result_consumer(service.consume_result)
    with sessions.begin() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        assert reconciliation is not None
        reconciliation.current_phase = "dispatching"
        reconciliation.status = "running"
        reconciliation.terminal_reason = None
        for projection in session.scalars(
            select(ReconciliationOperation).where(
                ReconciliationOperation.role == "primary"
            )
        ):
            operation = session.get(AgentOperation, projection.agent_operation_id)
            assert operation is not None
            projection.state = "queued"
            projection.result_digest = None
            projection.evidence_digest = None
            projection.accepted_at = None
            operation.state = "queued"
            operation.current_attempt = 0

    declared = queue.claim(NODE_A, "serial-a", 30)
    assert declared is not None
    sibling = None
    if sibling_state == "running":
        sibling = queue.claim(NODE_B, "serial-b", 30)
        assert sibling is not None

    queue.wait_for_operator(declared, "mutation outcome requires operator")

    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        sibling_operation = session.scalar(
            select(AgentOperation).where(AgentOperation.node_id == NODE_B)
        )
        sibling_projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.graph_operation_id
                == f"model:{NODE_B}:workload.start",
                ReconciliationOperation.role == "primary",
            )
        )
        assert reconciliation is not None
        assert reconciliation.current_phase == "waiting-for-operator"
        expected = "failed" if sibling_state == "queued" else "waiting-for-operator"
        assert sibling_operation is not None and sibling_operation.state == expected
        assert sibling_projection is not None and sibling_projection.state == expected
        if sibling is not None:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == sibling.operation_id
                )
            )
            assert attempt is not None and attempt.state == "waiting-for-operator"

    assert queue.claim(NODE_B, "serial-b", 30) is None
    if sibling is not None:
        with pytest.raises(StaleAgentAttempt):
            queue.succeed(sibling, _verify_result())


def test_postgres_claim_fails_closed_on_authoritative_operator_wait(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    sessions, queue, _service, reconciliation_id = _compensation_system(
        postgres_engine,
        tmp_path / "authoritative-claim-phase",
        parallel=True,
    )
    with sessions.begin() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        job = session.scalar(
            select(Job).where(Job.reconciliation_id == reconciliation_id)
        )
        assert reconciliation is not None and job is not None
        reconciliation.current_phase = "waiting-for-operator"
        reconciliation.status = "failed"
        job.state = "waiting-for-operator"
        for projection in session.scalars(
            select(ReconciliationOperation).where(
                ReconciliationOperation.role == "primary"
            )
        ):
            operation = session.get(AgentOperation, projection.agent_operation_id)
            assert operation is not None
            projection.state = "queued"
            operation.state = "queued"
            operation.current_attempt = 0

    assert queue.claim(NODE_B, "serial-b", 30) is None
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.node_id == NODE_B)
        )
        assert operation is not None and operation.state == "failed"


@pytest.mark.parametrize("sibling_state", ["queued", "running"])
def test_postgres_maintenance_sweeps_unsafe_expiry_without_follow_up_claim(
    postgres_engine: Engine,
    tmp_path: Path,
    sibling_state: str,
) -> None:
    current = [NOW]
    sessions, queue, service, reconciliation_id = _compensation_system(
        postgres_engine,
        tmp_path / f"autonomous-expiry-{sibling_state}",
        parallel=True,
        clock=lambda: current[0],
    )
    queue.set_result_consumer(service.consume_result)
    with sessions.begin() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        assert reconciliation is not None
        reconciliation.current_phase = "dispatching"
        reconciliation.status = "running"
        reconciliation.terminal_reason = None
        for projection in session.scalars(
            select(ReconciliationOperation).where(
                ReconciliationOperation.role == "primary"
            )
        ):
            operation = session.get(AgentOperation, projection.agent_operation_id)
            assert operation is not None
            projection.state = "queued"
            projection.result_digest = None
            projection.evidence_digest = None
            projection.accepted_at = None
            operation.state = "queued"
            operation.current_attempt = 0

    expired = queue.claim(NODE_A, "serial-a", 30)
    assert expired is not None
    sibling = None
    if sibling_state == "running":
        sibling = queue.claim(NODE_B, "serial-b", 30)
        assert sibling is not None
    current[0] += timedelta(seconds=31)

    assert service.tick(reconciliation_id) is True

    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        job = session.scalar(
            select(Job).where(Job.reconciliation_id == reconciliation_id)
        )
        expired_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == expired.operation_id
            )
        )
        sibling_operation = session.scalar(
            select(AgentOperation).where(AgentOperation.node_id == NODE_B)
        )
        assert reconciliation is not None
        assert reconciliation.current_phase == "waiting-for-operator"
        assert job is not None and job.state == "waiting-for-operator"
        assert expired_attempt is not None and expired_attempt.state == "expired"
        expected = "failed" if sibling_state == "queued" else "waiting-for-operator"
        assert sibling_operation is not None and sibling_operation.state == expected

    assert queue.claim(NODE_B, "serial-b", 30) is None


def test_postgres_claim_expiry_quiesces_queued_and_running_siblings_and_rejects_callbacks(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    current = [NOW]
    sessions, queue, _service, reconciliation_id = _compensation_system(
        postgres_engine,
        tmp_path / "claim-expiry-whole-reconciliation",
        operation_nodes=(NODE_A, NODE_B, NODE_C),
        clock=lambda: current[0],
    )
    with sessions.begin() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        job = session.scalar(
            select(Job).where(Job.reconciliation_id == reconciliation_id)
        )
        assert reconciliation is not None and job is not None
        reconciliation.current_phase = "dispatching"
        reconciliation.status = "running"
        reconciliation.terminal_reason = None
        job.state = "running"
        for projection in session.scalars(
            select(ReconciliationOperation).where(
                ReconciliationOperation.role == "primary"
            )
        ):
            operation = session.get(AgentOperation, projection.agent_operation_id)
            assert operation is not None
            projection.state = "queued"
            projection.result_digest = None
            projection.evidence_digest = None
            projection.accepted_at = None
            operation.state = "queued"
            operation.current_attempt = 0

    expired = queue.claim(NODE_A, "serial-a", 10)
    running = queue.claim(NODE_B, "serial-b", 60)
    assert expired is not None and running is not None
    current[0] += timedelta(seconds=11)

    assert queue.claim(NODE_A, "serial-a", 30) is None

    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        job = session.scalar(
            select(Job).where(Job.reconciliation_id == reconciliation_id)
        )
        operations = {
            operation.node_id: operation
            for operation in session.scalars(
                select(AgentOperation).order_by(AgentOperation.node_id)
            )
        }
        projections = {
            projection.graph_operation_id: projection
            for projection in session.scalars(
                select(ReconciliationOperation).where(
                    ReconciliationOperation.role == "primary"
                )
            )
        }
        attempts = {
            attempt.operation_id: attempt
            for attempt in session.scalars(select(AgentOperationAttempt))
        }
        assert reconciliation is not None
        assert reconciliation.current_phase == "waiting-for-operator"
        assert job is not None and job.state == "waiting-for-operator"
        assert operations[NODE_A].state == "waiting-for-operator"
        assert attempts[expired.operation_id].state == "expired"
        assert projections[f"model:{NODE_A}:workload.start"].state == (
            "waiting-for-operator"
        )
        assert operations[NODE_B].state == "waiting-for-operator"
        assert attempts[running.operation_id].state == "waiting-for-operator"
        assert projections[f"model:{NODE_B}:workload.start"].state == (
            "waiting-for-operator"
        )
        assert operations[NODE_C].state == "failed"
        assert projections[f"model:{NODE_C}:workload.start"].state == "failed"

    with pytest.raises(StaleAgentAttempt):
        queue.heartbeat(running, {"phase": "must-not-persist"}, 60)
    with pytest.raises(StaleAgentAttempt):
        queue.succeed(running, {"status": "must-not-persist"})
    assert queue.claim(NODE_C, "serial-c", 30) is None

    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == running.operation_id
            )
        )
        assert attempt is not None
        assert attempt.progress is None
        assert attempt.result is None


def test_postgres_active_callbacks_reject_authoritative_operator_wait_without_mutation(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    sessions, queue, _service, reconciliation_id = _compensation_system(
        postgres_engine,
        tmp_path / "active-callback-authority",
    )
    with sessions.begin() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        job = session.scalar(
            select(Job).where(Job.reconciliation_id == reconciliation_id)
        )
        projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.role == "primary"
            )
        )
        assert reconciliation is not None and job is not None
        assert projection is not None
        operation = session.get(AgentOperation, projection.agent_operation_id)
        assert operation is not None
        reconciliation.current_phase = "dispatching"
        reconciliation.status = "running"
        reconciliation.terminal_reason = None
        job.state = "running"
        projection.state = "queued"
        projection.result_digest = None
        projection.evidence_digest = None
        projection.accepted_at = None
        operation.state = "queued"
        operation.current_attempt = 0

    claim = queue.claim(NODE_A, "serial-a", 60)
    assert claim is not None
    with sessions.begin() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        job = session.scalar(
            select(Job).where(Job.reconciliation_id == reconciliation_id)
        )
        assert reconciliation is not None and job is not None
        reconciliation.current_phase = "waiting-for-operator"
        reconciliation.status = "failed"
        reconciliation.terminal_reason = "authoritative operator wait"
        job.state = "waiting-for-operator"
        job.status_reason = "authoritative operator wait"

    with pytest.raises(StaleAgentAttempt):
        queue.heartbeat(claim, {"phase": "must-not-persist"}, 60)
    with pytest.raises(StaleAgentAttempt):
        queue.succeed(claim, {"status": "must-not-persist"})

    with sessions() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.agent_operation_id == claim.operation_id
            )
        )
        assert operation is not None and operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.progress is None
        assert attempt.result is None
        assert projection is not None and projection.state == "queued"


def test_postgres_automatic_ticks_do_not_starve_older_requested_cancellation(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    system = _system(postgres_engine, tmp_path / "automatic-cancellation-fairness")
    sessions, _presence, _queue, service, old_id, _old_job_id = system
    assert _complete(system) == old_id
    completed_id, _completed_job_id = _insert_successor(sessions, old_id)
    _mark_completed(sessions, completed_id, owner=True)
    cancellation = service.enqueue_cancel(
        old_id,
        "operator cancelled older reconciliation",
        actor="administrator",
        request_id=str(uuid.uuid4()),
    )
    assert cancellation.state == "requested"

    outcomes = [service.tick() for _ in range(6)]

    assert any(outcomes)
    with sessions() as session:
        old = session.get(Reconciliation, old_id)
        completed = session.get(Reconciliation, completed_id)
        stored_cancellation = session.get(ReconciliationCancellation, old_id)
        assert old is not None and old.current_phase == "cancelled"
        assert completed is not None and completed.current_phase == "completed"
        assert stored_cancellation is not None
        assert stored_cancellation.state == "completed"


def test_postgres_automatic_ticks_do_not_starve_older_planned_execution(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    system = _system(postgres_engine, tmp_path / "automatic-execution-fairness")
    sessions, _presence, _queue, service, execution_id, _job_id = system
    completed_id, _completed_job_id = _insert_successor(sessions, execution_id)
    _mark_completed(sessions, completed_id, owner=False)
    _set_publication_owner(sessions, execution_id)

    outcomes = [service.tick() for _ in range(6)]

    assert any(outcomes)
    with sessions() as session:
        execution = session.get(Reconciliation, execution_id)
        completed = session.get(Reconciliation, completed_id)
        assert execution is not None
        assert execution.current_phase == "dispatching"
        assert completed is not None and completed.current_phase == "completed"


def test_postgres_automatic_ticks_skip_stale_planned_before_later_execution(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    system = _system(postgres_engine, tmp_path / "automatic-stale-planned-scan")
    sessions, _presence, _queue, service, stale_id, _job_id = system
    completed_id, _completed_job_id = _insert_successor(sessions, stale_id)
    _mark_completed(sessions, completed_id, owner=True)
    later_id, _later_job_id = _insert_successor(
        sessions,
        stale_id,
        created_at=NOW + timedelta(seconds=2),
        fleet_digest="8" * 64,
    )

    outcomes = [service.tick() for _ in range(4)]

    assert any(outcomes)
    with sessions() as session:
        stale = session.get(Reconciliation, stale_id)
        completed = session.get(Reconciliation, completed_id)
        later = session.get(Reconciliation, later_id)
        assert stale is not None and stale.current_phase == "planned"
        assert completed is not None and completed.current_phase == "completed"
        assert later is not None and later.current_phase != "planned"


def test_postgres_automatic_ticks_scan_past_running_dispatch_for_cancellation(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    system = _system(postgres_engine, tmp_path / "automatic-dispatch-cancel-scan")
    sessions, _operations, service, old_id, _job_id, claim = _claimed(system)
    cancellation_id, _cancellation_job_id = _insert_successor(sessions, old_id)
    service.enqueue_cancel(
        cancellation_id,
        "cancel later reconciliation while predecessor waits for an agent",
        actor="administrator",
        request_id=str(uuid.uuid4()),
    )

    assert service.tick() is True

    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        cancellation = session.get(ReconciliationCancellation, cancellation_id)
        assert attempt is not None and attempt.state == "running"
        assert cancellation is not None and cancellation.state == "processing"


def test_postgres_automatic_tick_does_not_preempt_running_owner_with_later_plan(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    system = _system(postgres_engine, tmp_path / "automatic-running-owner-serialization")
    sessions, operations, service, old_id, _job_id, claim = _claimed(system)
    later_id, _later_job_id = _insert_successor(sessions, old_id)

    assert service.tick() is False
    assert service.tick(later_id) is False
    progress = operations.heartbeat(claim, {"phase": "still-authoritative"}, 60)
    assert progress.progress == {"phase": "still-authoritative"}

    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        old = session.get(Reconciliation, old_id)
        later = session.get(Reconciliation, later_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        assert owner is not None and owner.reconciliation_id == old_id
        assert old is not None and old.current_phase == "dispatching"
        assert later is not None and later.current_phase == "planned"
        assert attempt is not None and attempt.state == "running"


def test_postgres_automatic_ticks_do_not_starve_older_unsafe_expiry(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    current = [NOW]
    sessions, queue, service, expiry_id = _compensation_system(
        postgres_engine,
        tmp_path / "automatic-expiry-fairness",
        clock=lambda: current[0],
    )
    with sessions.begin() as session:
        reconciliation = session.get(Reconciliation, expiry_id)
        job = session.scalar(select(Job).where(Job.reconciliation_id == expiry_id))
        projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.role == "primary"
            )
        )
        assert reconciliation is not None and job is not None
        assert projection is not None
        operation = session.get(AgentOperation, projection.agent_operation_id)
        assert operation is not None
        reconciliation.current_phase = "dispatching"
        reconciliation.status = "running"
        reconciliation.terminal_reason = None
        job.state = "running"
        projection.state = "queued"
        projection.result_digest = None
        projection.evidence_digest = None
        projection.accepted_at = None
        operation.state = "queued"
        operation.current_attempt = 0
    claim = queue.claim(NODE_A, "serial-a", 10)
    assert claim is not None
    stale_id, _stale_job_id = _insert_successor(
        sessions,
        expiry_id,
        created_at=NOW - timedelta(seconds=1),
        fleet_digest="8" * 64,
    )
    completed_id, _completed_job_id = _insert_successor(sessions, expiry_id)
    _mark_completed(sessions, completed_id, owner=False)
    _set_publication_owner(sessions, expiry_id)
    current[0] += timedelta(seconds=11)

    outcomes = [service.tick() for _ in range(6)]

    assert any(outcomes)
    with sessions() as session:
        expiry = session.get(Reconciliation, expiry_id)
        stale = session.get(Reconciliation, stale_id)
        completed = session.get(Reconciliation, completed_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        assert expiry is not None
        assert expiry.current_phase == "waiting-for-operator"
        assert stale is not None and stale.current_phase == "planned"
        assert completed is not None and completed.current_phase == "completed"
        assert attempt is not None and attempt.state == "expired"


@pytest.mark.parametrize(
    ("role", "operation_state"),
    [
        ("primary", "queued"),
        ("primary", "running"),
        ("compensation", "queued"),
        ("compensation", "running"),
    ],
)
def test_postgres_revocation_quiesces_sibling_mutation_and_compensation(
    postgres_engine: Engine,
    tmp_path: Path,
    role: str,
    operation_state: str,
) -> None:
    sessions, queue, service, reconciliation_id = _compensation_system(
        postgres_engine, tmp_path / f"revocation-{role}-{operation_state}"
    )
    if role == "primary":
        with sessions.begin() as session:
            reconciliation = session.get(Reconciliation, reconciliation_id)
            projection = session.scalar(
                select(ReconciliationOperation).where(
                    ReconciliationOperation.role == "primary"
                )
            )
            operation = session.get(AgentOperation, projection.agent_operation_id)
            assert reconciliation is not None and operation is not None
            reconciliation.current_phase = "dispatching"
            projection.state = "queued"
            projection.result_digest = None
            projection.evidence_digest = None
            projection.accepted_at = None
            operation.state = "queued"
            operation.current_attempt = 0
    else:
        assert service.tick(reconciliation_id) is True

    with sessions() as session:
        projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.role == role,
                ReconciliationOperation.state == "queued",
            )
        )
        assert projection is not None and projection.agent_operation_id is not None
        operation_id = projection.agent_operation_id

    claim = None
    if operation_state == "running":
        claim = queue.claim(NODE_A, "serial-a", 30)
        assert claim is not None and claim.operation_id == operation_id

    enrollment = EnrollmentService(sessions, RevokingAuthority(), clock=lambda: NOW)
    enrollment.revoke_node(NODE_B, "administrator")
    assert service.tick(reconciliation_id) is True

    with sessions() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        operation = session.get(AgentOperation, operation_id)
        projection = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.agent_operation_id == operation_id
            )
        )
        assert reconciliation is not None
        assert reconciliation.current_phase == "waiting-for-operator"
        assert operation is not None
        expected = "failed" if operation_state == "queued" else "waiting-for-operator"
        assert operation.state == expected
        assert projection is not None and projection.state == expected
        if claim is not None:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation_id
                )
            )
            assert attempt is not None and attempt.state == "waiting-for-operator"


def test_postgres_stale_completed_candidate_cannot_withdraw_refreshed_lease(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    current = [NOW]
    system = _system(
        postgres_engine,
        tmp_path / "stale-completed-candidate",
        clock=lambda: current[0],
    )
    sessions, presence, operations, reconciliations, reconciliation_id, _job_id = system
    sessions, operations, reconciliations, reconciliation_id, _job_id, claim = _claimed(
        system
    )
    operations.succeed(claim, _verify_result())
    for _ in range(3):
        reconciliations.tick(reconciliation_id)
    current[0] += timedelta(seconds=31)

    stale_service = AgentReconciliationService(
        sessions,
        agent_jobs=operations,
        publisher=reconciliations._publisher,
        endpoint_resolver=lambda session, node_id: (
            presence.latest_in_session(
                session, node_id, maximum_age_seconds=300
            ).address,
            presence.latest_in_session(
                session, node_id, maximum_age_seconds=300
            ).observed_at,
        ),
        clock=lambda: current[0],
    )
    candidate_selected = threading.Event()
    release_candidate = threading.Event()
    stale_results: list[object] = []

    def pause_after_candidate(
        _conn, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            threading.current_thread().name == "stale-candidate"
            and "FROM reconciliations" in statement
            and "reconciliations.current_phase" in statement
            and "FOR UPDATE" not in statement
        ):
            candidate_selected.set()
            assert release_candidate.wait(timeout=5)

    def run_stale_candidate() -> None:
        try:
            stale_results.append(stale_service.tick())
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            SQLAlchemyError,
        ) as error:  # pragma: no cover - asserted below
            stale_results.append(error)

    event.listen(postgres_engine, "after_cursor_execute", pause_after_candidate)
    try:
        stale = threading.Thread(target=run_stale_candidate, name="stale-candidate")
        stale.start()
        assert candidate_selected.wait(timeout=5)
        for _ in range(3):
            assert reconciliations.tick(reconciliation_id) is True
        with sessions() as session:
            fresh = session.get(Reconciliation, reconciliation_id)
            fresh_publication = session.execute(
                select(
                    Reconciliation.current_phase,
                ).where(Reconciliation.id == reconciliation_id)
            ).scalar_one()
            assert fresh is not None and fresh.current_phase == "completed"
            assert fresh_publication == "completed"
        release_candidate.set()
        stale.join(timeout=5)
    finally:
        release_candidate.set()
        event.remove(postgres_engine, "after_cursor_execute", pause_after_candidate)

    assert not stale.is_alive()
    assert stale_results == [False]
    with sessions() as session:
        stored = session.get(Reconciliation, reconciliation_id)
        assert stored is not None and stored.current_phase == "completed"
