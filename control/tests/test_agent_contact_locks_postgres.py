"""Agent traffic never holds a node-row lock that admission or publication waits for.

Production 2026-10-25 (release 037bf432): profile loads waited on
``admission.capacity_busy`` and PostgreSQL reported ``DeadlockDetected`` on an
``agent_nodes`` row while no workload ran; every agent call held that row FOR
UPDATE. Contact facts now live on ``agent_node_contacts`` (one upsert per
transaction), and admission locks ``agent_nodes`` FOR NO KEY UPDATE, which the
KEY SHARE of a child row's foreign-key check never blocks.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    RecipeStartResult,
    canonical_message,
)
from vonk_agent_protocol.recipe_operations import RecipeStopResult
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    lock_admission_rows,
    set_admission_timeout,
)
from vonk_control.agent_contacts import record_observation_delivery
from vonk_control.agent_jobs import AgentJobService
from vonk_control.auth import AgentIdentity, AgentSource
from vonk_control.inventory_items import inventory_items
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentNodeContact,
    AgentOperation,
    Base,
    Job,
)

from .agent_fences import fenced_operation
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_recipe_operations import NOW as RECIPE_NOW
from .test_recipe_operations import (
    installed_recipe,
    setup_services,
    start_evidence,
    started_recipe,
)

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
# Sparks 0 and 1 keep one order running (heartbeat, re-claim, delivery);
# Sparks 2 and 3 take fresh orders and end them (succeed, fail).
NODES = tuple("spk_" + letter * 32 for letter in "abcd")
REVISION = "a" * 64
_NODE_LOCK = ("FOR UPDATE", "FOR NO KEY UPDATE", "FOR SHARE", "FOR KEY SHARE")
# The only mode agent traffic may hold on a node row: a foreign-key check.
_FK_CHECK = "For Key Share"
_QUEUED = 40


def _source(index: int) -> AgentSource:
    return AgentSource(
        identity=AgentIdentity(
            node_id=NODES[index],
            certificate_serial=f"serial-{index}",
            certificate_fingerprint=f"fingerprint-{index}",
            verified=True,
        ),
        management_address=f"10.0.0.{11 + index}",
    )


def _parent(session, targets: list[str]) -> Job:
    payload = {"workload_intent_ordinal": 1}
    parent = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=REVISION,
        targets=targets,
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        current_attempt=0,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(parent)
    session.flush()
    return parent


@pytest.fixture
def fleet(postgres_engine):
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgrowlocks")
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node in NODES:
            session.add(
                AgentNode(
                    node_id=node,
                    state=NodeIdentityState.ACTIVE,
                    workload_intent_ordinal=1,
                )
            )
        session.flush()
        for index, node in enumerate(NODES):
            session.add(AgentNodeContact(node_id=node))
            session.add(
                AgentCertificate(
                    serial=f"serial-{index}",
                    node_id=node,
                    not_before=NOW - timedelta(seconds=1),
                    not_after=NOW + timedelta(hours=1),
                    fingerprint=f"fingerprint-{index}",
                )
            )
        parent = _parent(session, list(NODES[:2]))
    service = AgentJobService(sessions, clock=lambda: NOW)
    for node in NODES[:2]:
        service.enqueue(
            parent.id,
            node,
            "recipe.stop",
            REVISION,
            recipe_stop_payload(node, plan_digest=REVISION),
        )
    claims = [
        claim_agent(service, node, f"serial-{index}", source=_source(index))
        for index, node in enumerate(NODES[:2])
    ]
    assert all(claim is not None for claim in claims)
    return sessions, service, parent, claims


def _enqueue(sessions, service, index: int) -> None:
    """Queue a new order for a worker Spark (an orchestrator, not the agent)."""
    with sessions.begin() as session:
        parent_id = _parent(session, [NODES[index]]).id
    service.enqueue(
        parent_id,
        NODES[index],
        "recipe.stop",
        REVISION,
        recipe_stop_payload(NODES[index], plan_digest=REVISION),
    )


def _claim(service, index: int):
    return claim_agent(service, NODES[index], f"serial-{index}", source=_source(index))


def _fresh_claim(sessions, service, index: int):
    _enqueue(sessions, service, index)
    claim = _claim(service, index)
    assert claim is not None
    return claim


def _finish(service, claim, index: int, *, failed: bool) -> None:
    if failed:
        service.fail(claim, "injected agent failure", source=_source(index))
    else:
        service.succeed(claim, RecipeStopResult())


def _deliver_observation(sessions, node_id: str) -> None:
    with sessions.begin() as session:
        record_observation_delivery(session, node_id, 204, NOW)


def _admit(sessions) -> None:
    """Capacity admission's and image publication's NOWAIT node lock."""
    with sessions.begin() as session:
        set_admission_timeout(session)
        lock_admission_rows(
            session,
            (
                AdmissionRowLock(
                    "target-agent-nodes",
                    AgentNode,
                    select(AgentNode).where(AgentNode.node_id.in_(NODES)),
                ),
            ),
        )


def _job_then_nodes(sessions, job_id: str) -> None:
    """The RunSwitch loop's order: its Job row first, then the target nodes."""
    with sessions.begin() as session:
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.get(Job, job_id, with_for_update=True)
        list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(NODES))
                .order_by(AgentNode.node_id)
                .with_for_update()
            )
        )


def _agent_write(sessions, service, claims, kind: str) -> Callable[[], object]:
    """One agent write a Spark sends, as one Controller transaction."""
    if kind == "heartbeat":
        return lambda: service.heartbeat(
            claims[0].fence, {"phase": "stopping"}, 30, source=_source(0)
        )
    if kind == "reclaim":
        return lambda: claim_agent(
            service,
            NODES[1],
            "serial-1",
            source=_source(1),
            claim_fence=claims[1].fence,
        )
    if kind == "fresh-claim":
        _enqueue(sessions, service, 2)
        return lambda: _claim(service, 2)
    if kind in {"succeed", "fail"}:
        claim = _fresh_claim(sessions, service, 3)
        return lambda: _finish(service, claim, 3, failed=kind == "fail")
    return lambda: _deliver_observation(sessions, NODES[0])


class AgentWriteRefused(RuntimeError):
    """The probed agent write was refused before it reached COMMIT."""


class NodeLockHeld(Exception):
    """An open agent transaction holds a node-row lock admission waits for."""


def _assert_admission_free(
    postgres_engine, action: Callable[[], object], admit: Callable[[], None]
) -> None:
    """Hold one agent transaction just before COMMIT; raise ``NodeLockHeld``
    when it holds more than a foreign-key check on the node rows or admission's
    NOWAIT node lock is refused, ``AgentWriteRefused`` when the write itself
    failed (so a refused input can never pass for a lock)."""
    paused, release = threading.Event(), threading.Event()
    agent_thread: list[int] = []

    def hold_before_commit(_connection) -> None:
        if threading.get_ident() in agent_thread:
            paused.set()
            release.wait(timeout=30)

    event.listen(postgres_engine, "commit", hold_before_commit)
    errors: list[Exception] = []

    def agent() -> None:
        agent_thread.append(threading.get_ident())
        try:
            action()
        except Exception as error:  # noqa: BLE001 - reported to the test thread
            errors.append(error)

    worker = threading.Thread(target=agent)
    worker.start()
    try:
        while not paused.wait(timeout=0.05):
            if not worker.is_alive():
                break
        if not paused.is_set():
            worker.join(timeout=30)
            raise AgentWriteRefused(f"agent write ended before commit: {errors!r}")
        with postgres_engine.connect() as probe:
            held = {
                mode
                for (modes,) in probe.exec_driver_sql(
                    "SELECT modes FROM pgrowlocks('agent_nodes')"
                )
                for mode in modes
            }
        refused: Exception | None = None
        try:
            admit()
        except AdmissionLockBusy as error:
            refused = error
    finally:
        release.set()
        worker.join(timeout=30)
        event.remove(postgres_engine, "commit", hold_before_commit)
    if errors:
        raise AgentWriteRefused(f"agent write failed: {errors!r}")
    if not held <= {_FK_CHECK} or refused is not None:
        raise NodeLockHeld(f"node-row locks {sorted(held)}; admission: {refused!r}")


@pytest.mark.parametrize(
    "kind", ("heartbeat", "reclaim", "fresh-claim", "succeed", "fail", "delivery")
)
def test_open_agent_transaction_leaves_admission_and_publication_free(
    fleet, postgres_engine, kind
):
    """Fails on main: every agent transaction held its nodes FOR UPDATE."""
    sessions, service, _, claims = fleet
    # The agent transaction holds everything it took. On the node rows that is
    # at most a foreign-key check, which NO KEY admission never waits on.
    _assert_admission_free(
        postgres_engine,
        _agent_write(sessions, service, claims, kind),
        lambda: _admit(sessions),
    )


# The production result consumer on real recipe.start / recipe.stop parents.
_ARM64 = {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}


@pytest.fixture
def recipe_world(tmp_path, postgres_engine):
    sessions, lifecycle, _recorded, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgrowlocks")
    jobs = AgentJobService(
        sessions,
        clock=lambda: RECIPE_NOW,
        result_consumer=lifecycle.consume_agent_result,
    )
    lifecycle._agent_jobs = jobs  # production: one queue serves both sides
    installation = installed_recipe(
        lifecycle, mapping, build, nodes, request_id=str(uuid.uuid4())
    )
    return sessions, lifecycle, jobs, installation, nodes


def _recipe_claim(sessions, jobs, node_id: str):
    with sessions() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        fingerprint = inventory_items(node).preflight_fingerprint
    claim = claim_agent(
        jobs,
        node_id,
        "serial-0",
        runtime_identity=_ARM64,
        preflight_fingerprint=fingerprint,
    )
    assert claim is not None
    return claim


def _start(lifecycle, installation) -> None:
    plan = lifecycle.preview_run(installation.owner_id, "contact")
    lifecycle.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )


def _admit_recipe_nodes(sessions, nodes) -> Callable[[], None]:
    def admit() -> None:
        with sessions.begin() as session:
            set_admission_timeout(session)
            lock_admission_rows(
                session,
                (
                    AdmissionRowLock(
                        "target-agent-nodes",
                        AgentNode,
                        select(AgentNode).where(AgentNode.node_id.in_(nodes)),
                    ),
                ),
            )

    return admit


def _recipe_write(world, kind: str) -> Callable[[], object]:
    sessions, lifecycle, jobs, installation, nodes = world
    if kind == "claim":
        _start(lifecycle, installation)
        return lambda: _recipe_claim(sessions, jobs, nodes[0])
    if kind in {"heartbeat", "succeed", "failed-start"}:
        _start(lifecycle, installation)
        claim = _recipe_claim(sessions, jobs, nodes[0])
        if kind == "heartbeat":
            return lambda: jobs.heartbeat(claim.fence, None, lease_seconds=60)
        if kind == "succeed":
            with sessions() as session:
                operation = session.get(
                    AgentOperation, fenced_operation(sessions, claim).id
                )
                assert operation is not None
                evidence = start_evidence(operation.payload)
            return lambda: jobs.record_result(
                AgentResult(
                    fence=claim.fence,
                    state=AgentResultState.SUCCEEDED,
                    result=RecipeStartResult.model_validate(evidence),
                )
            )
        return lambda: jobs.fail(claim, "injected start failure")
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    plan = lifecycle.preview_stop(run.owner_id)
    lifecycle.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    claim = _recipe_claim(sessions, jobs, nodes[0])
    cancelled = AgentResult.model_validate(
        {
            "fence": claim.fence,
            "state": AgentResultState.CANCELLED,
            "result": {
                "error_code": "operation_cancelled",
                "reason": "the agent cancelled the stop",
            },
        }
    )
    return lambda: jobs.record_result(cancelled)


@pytest.mark.parametrize("kind", ("claim", "heartbeat", "succeed"))
def test_recipe_agent_writes_through_the_production_consumer_leave_admission_free(
    recipe_world, postgres_engine, kind
):
    """Fails on main: the start claim, its heartbeat and result locked nodes."""
    sessions, *_, nodes = recipe_world
    _assert_admission_free(
        postgres_engine,
        _recipe_write(recipe_world, kind),
        _admit_recipe_nodes(sessions, nodes),
    )


_STEP_4 = {
    "failed-start": (
        "lock plan step 4: _intent_is_current (recipe_operations/intent.py:107) "
        "and the cleanup Stop's enqueue: terminal_projection.py:369 -> "
        "persistence.py:432/245 _admit_workload_intent -> lock_admission_rows, "
        "and persistence.py:536 -> orchestration.py:101 -> scope.py:90"
    ),
    "cancelled-stop": (
        "lock plan step 4: _intent_is_current (recipe_operations/intent.py:107)"
    ),
}


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(
            kind,
            marks=pytest.mark.xfail(strict=True, raises=NodeLockHeld, reason=reason),
        )
        for kind, reason in _STEP_4.items()
    ],
)
def test_recipe_intent_check_in_a_result_leaves_admission_free(
    recipe_world, postgres_engine, kind
):
    """Known: a failed or cancelled start/stop result still checks the workload
    intent under FOR UPDATE on its target nodes, and a failed start enqueues its
    cleanup Stop under admission's node lock (lock plan step 4 flips both)."""
    sessions, *_, nodes = recipe_world
    _assert_admission_free(
        postgres_engine,
        _recipe_write(recipe_world, kind),
        _admit_recipe_nodes(sessions, nodes),
    )


def test_hammered_agent_traffic_never_refuses_admission_or_deadlocks(
    fleet, postgres_engine
):
    """Agents hammer contact and fresh claim/succeed/fail while admission,
    publication and the RunSwitch Job-then-nodes order run: no busy refusal,
    no deadlock, and no agent statement locks or updates agent_nodes."""
    sessions, service, parent, claims = fleet
    agents: set[int] = set()
    node_statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if threading.get_ident() not in agents:
            return
        lowered = " ".join(statement.split())
        if (
            "UPDATE agent_nodes" in lowered
            or "FROM agent_nodes" in lowered
            and any(mode in lowered for mode in _NODE_LOCK)
        ):
            node_statements.append(lowered)

    event.listen(postgres_engine, "before_cursor_execute", record)
    # Orchestrators queue the orders before the agents start ending them.
    for index in (2, 3):
        for _ in range(_QUEUED):
            _enqueue(sessions, service, index)
    stop = threading.Event()
    failures: list[Exception] = []
    passes = [0]
    ended = {2: 0, 3: 0}

    def contact(index: int) -> None:
        agents.add(threading.get_ident())
        claim = claims[index]
        calls = (
            lambda: service.heartbeat(
                claim.fence, {"phase": "stopping"}, 30, source=_source(index)
            ),
            lambda: claim_agent(
                service,
                NODES[index],
                f"serial-{index}",
                source=_source(index),
                claim_fence=claim.fence,
            ),
            lambda: _deliver_observation(sessions, NODES[index]),
        )
        while not stop.is_set():
            for call in calls:
                try:
                    call()
                except Exception as error:  # noqa: BLE001 - reported to the test thread
                    failures.append(error)
                    return
                passes[0] += 1

    def worker(index: int, *, failed: bool) -> None:
        agents.add(threading.get_ident())
        while not stop.is_set():
            try:
                claim = _claim(service, index)
                if claim is None:
                    return  # every queued order was claimed and ended
                _finish(service, claim, index, failed=failed)
            except Exception as error:  # noqa: BLE001 - reported to the test thread
                failures.append(error)
                return
            ended[index] += 1

    threads = [
        *(threading.Thread(target=contact, args=(index,)) for index in (0, 0, 1, 1)),
        threading.Thread(target=worker, args=(2,), kwargs={"failed": False}),
        threading.Thread(target=worker, args=(3,), kwargs={"failed": True}),
    ]
    for thread in threads:
        thread.start()
    refusals: list[Exception] = []
    rounds = 0
    deadline = time.monotonic() + 3  # wall-clock: hammer duration, not a limit
    try:
        while time.monotonic() < deadline and not failures:
            for step in (
                lambda: _admit(sessions),
                lambda: _job_then_nodes(sessions, parent.id),
            ):
                try:
                    step()
                except (AdmissionLockBusy, DBAPIError) as error:
                    refusals.append(error)
            rounds += 1
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
        event.remove(postgres_engine, "before_cursor_execute", record)

    assert not failures, failures[:3]
    assert not refusals, refusals[:3]
    assert node_statements == []
    assert rounds > 0 and passes[0] > 0
    assert ended[2] > 0 and ended[3] > 0
    with sessions() as session:
        stored = {row.node_id: row for row in session.scalars(select(AgentNodeContact))}
        assert {
            node_id: row.recipe_observation_http_status
            for node_id, row in stored.items()
            if node_id in NODES[:2]
        } == {NODES[0]: 204, NODES[1]: 204}
        assert all(stored[node].last_seen_at is not None for node in NODES)
