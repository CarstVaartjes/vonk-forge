"""A Stop of a record's own container is granted by identity, whatever drifted.

The drift matrix sends the agent's own Stop requests (``agent_grant_requests``)
for every kind that may Stop, after each kind of bookkeeping drift, and expects
the grant; it expects a refusal only at a real edge: another node, certificate
or fence, an ended lease, or a container that is not the order's own.

On the queue, a cleanup order (``CLEANUP_KINDS``) survives a newer workload
intent on its Sparks and any time since: it is enqueued, claimed, renewed and
completed under the intent it was admitted under, while a non-cleanup order of
the same intent is still superseded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, inspect, select, update
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    LifecycleState,
    ReservationState,
    SignedHostHelperGrant,
    canonical_message,
)
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import RecipeStartPayload, RecipeStopPayload
from vonk_control.agent_api import HostRuntimeGrantRequest
from vonk_control.agent_jobs import AgentJobService
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostRuntimeAuthorityService,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.own_cleanup import CLEANUP_ACTIONS, CLEANUP_KINDS, EFFECT_ACTIONS
from vonk_control.recipe_lifecycle_contract import RecipeOperationCancellationResult
from vonk_control.recipe_operations import RecipeOperationService

from .agent_fences import fenced_operation
from .agent_grant_requests import own_stop_grant_request, stop_grant_request
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import claim_agent
from .runtime_image_fixtures import refresh_inventory
from .test_agent_jobs import (
    COMMIT,
    NODE_A,
    NODE_B,
    STOP_PAYLOAD,
    STOP_RESULT,
    canonical_start_payload,
    parent,
    service,  # noqa: F401 -- pytest fixture
)
from .test_host_helper_authority import (
    NOW,
    RUNTIME_INSTALLATION_ID,
    RUNTIME_RUN_ID,
    runtime_service,
    service_stop_runtime_service,
)
from .test_recipe_operations import (
    ConcurrentPublisher,
    bind_route_publications,
    installed_recipe,
    setup_services,
    started_recipe,
)

NODE = "spk_" + "1" * 32
OTHER = "c0000000-0000-4000-8000-00000000000c"
OWN_ORDER_KINDS = ("recipe.start", "recipe.job.run.v1")


@dataclass(frozen=True)
class _Order:
    kind: str
    service: HostRuntimeAuthorityService
    job_id: str
    operation_id: str
    fence: str


def _order(kind: str, *, collective: bool = False) -> _Order:
    if kind == "recipe.stop":
        return _Order(kind, *service_stop_runtime_service())
    authority = runtime_service(
        operation_kind=kind,
        operation_payload={"phase": "collective-readiness"} if collective else None,
    )
    return _Order(
        kind,
        authority,
        "20000000-0000-4000-8000-000000000002",
        "30000000-0000-4000-8000-000000000003",
        "40000000-0000-4000-8000-000000000004",
    )


def _requests(order: _Order) -> list[HostRuntimeGrantRequest]:
    """The agent's Stop requests for this order: a Stop order's claim as sent,
    or the Stop it derives for its own order with either ``cancel_pending_start``
    (a Start order sends ``False`` to clean up, ``True`` to cancel)."""
    with order.service._sessions() as session:
        operation = session.get(AgentOperation, order.operation_id)
        assert operation is not None
        payload = canonical_message(operation.payload)
    if order.kind == "recipe.stop":
        stop = RecipeStopPayload.model_validate_json(payload)
        return [stop_grant_request(stop, fence=order.fence)]
    own = (
        RecipeStartPayload.model_validate_json(payload)
        if order.kind == "recipe.start"
        else RecipeJobRunRequest.model_validate_json(payload)
    )
    return [
        own_stop_grant_request(own, fence=order.fence, cancel_pending_start=flag)
        for flag in (False, True)
    ]


def _grant(
    order: _Order,
    body: HostRuntimeGrantRequest,
    *,
    node_id: str = NODE,
    certificate_serial: str = "certificate-1",
) -> SignedHostHelperGrant:
    return order.service.issue_grant(
        node_id=node_id,
        certificate_serial=certificate_serial,
        fence=body.fence,
        action=ContainerRuntimeAction(body.action),
        request_sha256=body.request_sha256,
        start_plan_sha256=body.start_plan_sha256,
        stop_plan_sha256=body.stop_plan_sha256,
        run_generation=body.run_generation,
        runtime_run_id=body.runtime_run_id,
        runtime_target_id=body.runtime_target_id,
        runtime_installation_id=body.runtime_installation_id,
        expires_in_seconds=body.expires_in_seconds,
    )


def _stopped(grant: SignedHostHelperGrant) -> bool:
    operation = grant.claims.operation
    return (
        isinstance(operation, ExecuteContainerRuntimeRequestOperation)
        and operation.action == ContainerRuntimeAction.STOP.value
    )


def _no_drift(session: Session, order: _Order) -> None:
    pass


def _drift(order: _Order, change: Callable[[Session, _Order], None]) -> None:
    with order.service._sessions.begin() as session:
        change(session, order)


def _move_workload_intent(session: Session, order: _Order) -> None:
    for node in session.scalars(select(AgentNode)):
        node.workload_intent_ordinal = 2


def _cancel(session: Session, order: _Order, *, seconds_ago: int = 0) -> None:
    job = session.get(Job, order.job_id)
    assert job is not None
    job.result = RecipeOperationCancellationResult(
        cancel_requested=True,
        cancel_requested_at=NOW - timedelta(seconds=seconds_ago),
        cancel_request_id="50000000-0000-4000-8000-000000000005",
        cancel_actor="admin",
        reason="cancellation requested",
    ).model_dump(mode="json", exclude_none=True)


def _cancel_long_ago(session: Session, order: _Order) -> None:
    # Past the 660 s window that once ended a cancelled order's Stop.
    _cancel(session, order, seconds_ago=3600)


def _supersede_and_cancel_long_ago(session: Session, order: _Order) -> None:
    _move_workload_intent(session, order)
    _cancel_long_ago(session, order)


def _recover_run(session: Session, order: _Order) -> None:
    run = session.get(RecipeRun, RUNTIME_RUN_ID)
    assert run is not None
    run.run_generation = 2


def _refresh_run_plan(session: Session, order: _Order) -> None:
    run = session.get(RecipeRun, RUNTIME_RUN_ID)
    assert run is not None
    run.plan_digest = "f" * 64


def _drift_record_digests(session: Session, order: _Order) -> None:
    job = session.get(Job, order.job_id)
    operation = session.get(AgentOperation, order.operation_id)
    assert job is not None and operation is not None
    job.payload_digest = operation.payload_digest = "0" * 64


def _drop_manifest(session: Session, order: _Order) -> None:
    # An order queued before its producer recorded the phase manifest (#1569).
    job = session.get(Job, order.job_id)
    assert job is not None
    job.payload = {key: value for key, value in job.payload.items() if key != "phases"}


def _revision_id(session: Session) -> str:
    installation = session.get(RecipeInstallation, RUNTIME_INSTALLATION_ID)
    assert installation is not None
    return installation.recipe_revision_id


def _drift_revision_digest(session: Session, order: _Order) -> None:
    # The revision's digest no longer matches what the Stop was derived from
    # (a derivation that changed, as across a Controller upgrade).
    session.execute(
        update(CatalogDocumentRevision)
        .where(CatalogDocumentRevision.id == _revision_id(session))
        .values(content_digest="e" * 64)
    )


def _remove_revision(session: Session, order: _Order) -> None:
    # The revision row is gone, removed as retention removes one.
    session.execute(
        delete(CatalogDocumentRevision).where(
            CatalogDocumentRevision.id == _revision_id(session)
        )
    )


#: Drift every kind that may Stop must survive: the Stop document is the
#: order's, so what the run, its revision, the node's intent or the records'
#: digests became since never decides it.
_DRIFT: dict[str, Callable[[Session, _Order], None]] = {
    "current": _no_drift,
    "workload intent moved": _move_workload_intent,
    "cancellation recorded": _cancel,
    "cancelled long ago": _cancel_long_ago,
    "superseded and cancelled long ago": _supersede_and_cancel_long_ago,
    "run recovered to a newer generation": _recover_run,
    "run plan refreshed": _refresh_run_plan,
    "revision digest drifted": _drift_revision_digest,
    "revision removed": _remove_revision,
    "record digests drifted": _drift_record_digests,
    "phase manifest missing": _drop_manifest,
}
STOP_KINDS = (*OWN_ORDER_KINDS, "recipe.stop")


@pytest.mark.parametrize("drift", _DRIFT)
@pytest.mark.parametrize("kind", STOP_KINDS)
@pytest.mark.usefixtures("damaged_json_rows")
def test_a_stop_of_the_orders_own_container_is_granted_whatever_drifted(
    kind: str, drift: str
) -> None:
    order = _order(kind)
    _drift(order, _DRIFT[drift])
    for body in _requests(order):
        operation = _grant(order, body).claims.operation
        assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
        # The agent's own document is what the helper executes; the grant
        # binds it to the order's identity, which the helper checks it against.
        assert operation.action == ContainerRuntimeAction.STOP.value
        assert operation.stop_plan_sha256 == body.stop_plan_sha256
        assert (
            operation.run_generation,
            operation.runtime_run_id,
            operation.runtime_target_id,
            operation.runtime_installation_id,
        ) == (
            body.run_generation,
            body.runtime_run_id,
            body.runtime_target_id,
            body.runtime_installation_id,
        )


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_collective_readiness_order_stops_its_own_rank_only() -> None:
    order = _order("recipe.start", collective=True)
    other_rank = "spk_" + "2" * 32
    with order.service._sessions.begin() as session:
        session.add(
            AgentCertificate(
                serial="certificate-2",
                node_id=other_rank,
                not_before=NOW,
                not_after=NOW + timedelta(minutes=5),
                fingerprint="fingerprint-2",
            )
        )
    for body in _requests(order):
        assert _stopped(_grant(order, body))
        # The other rank's Spark, with its own valid certificate, cannot use
        # this rank's order.
        with pytest.raises(HostHelperAuthorityError):
            _grant(order, body, node_id=other_rank, certificate_serial="certificate-2")


@pytest.mark.parametrize("kind", OWN_ORDER_KINDS)
def test_a_superseded_order_still_stops_but_never_starts(kind: str) -> None:
    order = _order(kind)
    _drift(order, _move_workload_intent)
    with pytest.raises(HostHelperAuthorityError):
        order.service.issue_grant(
            node_id=NODE,
            fence=order.fence,
            action=ContainerRuntimeAction.START,
            request_sha256="e" * 64,
            certificate_serial="certificate-1",
        )
    for body in _requests(order):
        assert _stopped(_grant(order, body))


def _expire_lease(session: Session, order: _Order) -> None:
    for attempt in session.scalars(select(AgentOperationAttempt)):
        attempt.lease_deadline = NOW


def _disown_order(session: Session, order: _Order) -> None:
    job = session.get(Job, order.job_id)
    assert job is not None
    job.payload = {**job.payload, "owner_id": OTHER}


#: What the caller changes at the edge: its node or certificate, the request
#: it sends, or the stored record it claims under.
_CALLER_EDGES: dict[str, dict[str, str]] = {
    "another node": {"node_id": "spk_" + "2" * 32},
    "another certificate": {"certificate_serial": "certificate-2"},
}
_REQUEST_EDGES: dict[str, dict[str, object]] = {
    "another fence": {"fence": OTHER},
    "another run": {"runtime_run_id": OTHER},
    "another container": {"runtime_target_id": OTHER},
    "another installation": {"runtime_installation_id": OTHER},
    "another generation": {"run_generation": 2},
    "no Stop document": {"stop_plan_sha256": None},
}
_RECORD_EDGES: dict[str, Callable[[Session, _Order], None]] = {
    "the lease ended": _expire_lease,
    "an order its parent does not own": _disown_order,
}


@pytest.mark.parametrize("kind", STOP_KINDS)
@pytest.mark.parametrize("edge", [*_CALLER_EDGES, *_REQUEST_EDGES, *_RECORD_EDGES])
@pytest.mark.usefixtures("damaged_json_rows")
def test_a_stop_is_refused_only_at_a_real_edge(kind: str, edge: str) -> None:
    order = _order(kind)
    if edge in _RECORD_EDGES:
        _drift(order, _RECORD_EDGES[edge])
    for body in _requests(order):
        sent = body.model_copy(update=_REQUEST_EDGES.get(edge, {}))
        with pytest.raises(HostHelperAuthorityError):
            _grant(order, sent, **_CALLER_EDGES.get(edge, {}))


def test_every_runtime_action_is_a_cleanup_or_an_effect() -> None:
    assert CLEANUP_ACTIONS.isdisjoint(EFFECT_ACTIONS)
    assert CLEANUP_ACTIONS | EFFECT_ACTIONS == set(ContainerRuntimeAction)


def test_the_drift_matrix_covers_every_kind_a_cleanup_serves() -> None:
    served = {
        kind
        for action in CLEANUP_ACTIONS
        for kind in HostRuntimeAuthorityService._ACTION_KINDS[action]
    }
    assert served == set(STOP_KINDS)


def test_every_cleanup_kind_is_an_order_kind_a_cleanup_action_serves() -> None:
    served = {
        kind
        for action in CLEANUP_ACTIONS
        for kind in HostRuntimeAuthorityService._ACTION_KINDS[action]
    }
    assert CLEANUP_KINDS <= served - set(OWN_ORDER_KINDS)


def _state(session: Session, model: type[Job | AgentOperation], key: str) -> str:
    row = session.get(model, key)
    assert row is not None
    return row.state


def _newer_intent(jobs, sessions, clock) -> None:
    with sessions.begin() as session:
        for node in session.scalars(select(AgentNode)):
            node.workload_intent_ordinal = 2
        jobs.request_superseded_workload_cancellation_in_session(
            session, (NODE_A, NODE_B), 2, clock.now
        )


def _admit_newer_work(jobs, sessions, clock) -> None:
    """The newer load's own order is admitted and claimed."""
    newer = parent(sessions, clock)
    with sessions.begin() as session:
        stored = session.get(Job, newer.id)
        assert stored is not None
        stored.payload = {"workload_intent_ordinal": 2}
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30))
    order = jobs.enqueue(newer.id, NODE_A, "recipe.start", COMMIT, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None and fenced_operation(sessions, claim).id == order.id


#: Seconds a cleanup order is held after a newer intent: none, and past the
#: 660 s window a superseded order's cancellation is given.
_HELD = {"newer intent": 0, "newer intent, past the cancellation window": 700}


@pytest.mark.parametrize("held", _HELD)
def test_an_issued_cleanup_order_outlives_a_newer_intent(service, held: str) -> None:  # noqa: F811 -- pytest fixture
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    order = jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    _newer_intent(jobs, sessions, clock)
    with sessions() as session:
        # The newer load never waits for it, and nothing asks it to cancel.
        assert not AgentJobService.assess_superseded_agent_effects_in_session(
            session, (NODE_A, NODE_B), 2, clock.now
        )
    for _ in range(_HELD[held] // 20 + 1):
        directive = jobs.heartbeat(claim, None, 30)
        assert directive.cancel_requested is False
        assert directive.deadline > clock.now
        clock.advance(seconds=20)
    jobs.succeed(claim, STOP_RESULT)
    with sessions() as session:
        assert _state(session, AgentOperation, order.id) == "succeeded"
        assert _state(session, Job, job.id) == "succeeded"
    _admit_newer_work(jobs, sessions, clock)


def test_an_unissued_cleanup_order_is_claimed_after_a_newer_intent(service) -> None:  # noqa: F811 -- pytest fixture
    """An offline Spark's Stop waits for it and runs when it returns."""
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    order = jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    _newer_intent(jobs, sessions, clock)
    # A later phase of the same Stop is still queued under its own intent.
    later = jobs.enqueue(
        job.id,
        NODE_B,
        "recipe.stop",
        COMMIT,
        recipe_stop_payload(NODE_B, plan_digest=COMMIT),
    )
    for node, serial, expected in (
        (NODE_A, "serial-a", order.id),
        (NODE_B, "serial-b", later.id),
    ):
        claim = claim_agent(jobs, node, serial)
        assert claim is not None
        assert fenced_operation(sessions, claim).id == expected
        jobs.succeed(claim, STOP_RESULT)
    with sessions() as session:
        assert _state(session, Job, job.id) == "succeeded"
    _admit_newer_work(jobs, sessions, clock)


def test_a_newer_intent_still_supersedes_an_order_that_is_not_cleanup(service) -> None:  # noqa: F811 -- pytest fixture
    """The newest request steers every order but cleanup."""
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    payload = canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30))
    jobs.enqueue(job.id, NODE_A, "recipe.start", COMMIT, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    _newer_intent(jobs, sessions, clock)
    assert jobs.heartbeat(claim, None, 30).cancel_requested is True


def test_a_silent_sparks_cleanup_order_waits_and_runs_when_it_returns(service) -> None:  # noqa: F811 -- pytest fixture
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    order = jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a")
    assert first is not None
    # The Spark goes silent and a newer load takes its intent.
    _newer_intent(jobs, sessions, clock)
    clock.advance(seconds=3600)
    jobs.reconcile_orders()
    with sessions() as session:
        assert _state(session, AgentOperation, order.id) != "cancelled"
    # On its return the same Stop is issued again and completes.
    returned = None
    for _ in range(10):
        returned = claim_agent(jobs, NODE_A, "serial-a")
        if returned is not None:
            break
        clock.advance(seconds=60)
    assert returned is not None and returned.fence != first.fence
    assert fenced_operation(sessions, returned).id == order.id
    jobs.succeed(returned, STOP_RESULT)
    with sessions() as session:
        assert _state(session, Job, job.id) == "succeeded"
    _admit_newer_work(jobs, sessions, clock)


def _accept_service_stop(tmp_path, monkeypatch):
    """A service Stop accepted and withdrawn whose process was lost before it
    dispatched: its continuation finishes it."""
    sessions, operations, jobs, mapping, build, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        operations, mapping, build, nodes, request_id=str(uuid4())
    )
    run_id = started_recipe(
        sessions, operations, installation.owner_id, nodes, request_id=str(uuid4())
    ).owner_id
    operations, routes = bind_route_publications(
        sessions, operations, ConcurrentPublisher()
    )
    routes.publish_run(run_id)
    preview = operations.preview_stop(run_id)
    request = str(uuid4())

    def process_lost(*args, **kwargs):
        raise RuntimeError("process lost after route withdrawal")

    monkeypatch.setattr(operations, "_dispatch_stop_after_withdrawal", process_lost)
    with pytest.raises(RuntimeError):
        operations.stop(
            run_id, plan_digest=preview.plan_digest, actor="admin", request_id=request
        )
    with sessions() as session:
        accepted = session.scalar(select(Job).where(Job.request_id == request))
        assert accepted is not None
    return (
        sessions,
        operations,
        jobs,
        routes,
        installation.owner_id,
        run_id,
        nodes,
        accepted.id,
    )


def _run_moved_to_a_newer_generation(session: Session, run_id: str) -> None:
    run = session.get(RecipeRun, run_id)
    assert run is not None
    run.run_generation += 1


def _reservation_released(session: Session, run_id: str) -> None:
    # A rank's claim released before the Stop reached it (a partly stopped run).
    reservation = session.scalar(
        select(ResourceReservation).where(ResourceReservation.owner_id == run_id)
    )
    assert reservation is not None
    reservation.state = ReservationState.RELEASED


def _run_plan_digest_drifted(session: Session, run_id: str) -> None:
    # The run's stored digest differs from the one the Stop was reviewed under.
    run = session.get(RecipeRun, run_id)
    assert run is not None
    run.plan_digest = "f" * 64


#: Drift between acceptance and dispatch an accepted Stop must survive: it
#: stops its own run as the run is now.
_ACCEPTED_DRIFT: dict[str, Callable[[Session, str], None]] = {
    "run moved to a newer generation": _run_moved_to_a_newer_generation,
    "reservation released": _reservation_released,
    "run plan digest drifted": _run_plan_digest_drifted,
}


@pytest.mark.parametrize("drift", _ACCEPTED_DRIFT)
@pytest.mark.usefixtures("damaged_json_rows")
def test_an_accepted_stop_stops_its_run_as_it_is_now(
    tmp_path, monkeypatch, drift: str
) -> None:
    accepted = _accept_service_stop(tmp_path, monkeypatch)
    sessions, operations, jobs, routes, installation_id, run_id, nodes, accepted_id = (
        accepted
    )
    with sessions.begin() as session:
        _ACCEPTED_DRIFT[drift](session, run_id)
    clock = NOW + timedelta(seconds=6)
    restarted = RecipeOperationService(
        sessions,
        install_admission=operations._install_admission,
        run_admission=operations._run_admission,
        agent_jobs=jobs,
        route_publications=routes,
        clock=lambda: clock,
    )
    restarted.reconcile_pending_service_stops()
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        orders = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == accepted_id
                )
            )
        )
        # The Stop is issued for the run's current container.
        assert orders
        assert {order.payload["run_generation"] for order in orders} == {
            run.run_generation
        }
    for node_id in nodes:
        restarted.record_node_result(accepted_id, node_id, succeeded=True, evidence={})
    assert restarted.get(accepted_id).state == LifecycleState.SUCCEEDED
    refresh_inventory(sessions, clock)
    plan = restarted.preview_run(installation_id, "after-the-stop")
    fresh = restarted.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
    )
    assert fresh.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}


def _lost_job_run(tmp_path):
    """A one-shot run whose JobRun lost its lease and was cancelled by its
    operator: its container may remain, so the run's Stop must remove it."""
    from vonk_control import artifact_job_states as ajs

    from .test_artifact_jobs import (
        MutableClock,
        running_artifact_service,
        submitted_artifact_job,
    )
    from .test_recipe_operations import NOW as RECIPE_NOW

    sessions, operations, _queue, artifacts, run_id, node_id = running_artifact_service(
        tmp_path
    )
    clock = MutableClock(RECIPE_NOW)
    jobs = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message) -> None:
        artifacts.consume_agent_result(session, operation, attempt, message)
        operations.consume_agent_result(session, operation, attempt, message)

    jobs.set_result_consumer(consume)
    operations._agent_jobs = jobs
    assert claim_agent(jobs, node_id, "serial-0") is None
    submitted = submitted_artifact_job(artifacts, run_id, request_suffix=300)
    assert claim_agent(jobs, node_id, "serial-0") is not None
    clock.advance(seconds=31)
    assert claim_agent(jobs, node_id, "serial-0") is None
    artifacts.cancel(
        submitted.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000302",
        reason="operator stopped the lost job",
    )
    for _ in range(40):
        if artifacts.get(submitted.id).state == ajs.CANCELLED:
            break
        clock.advance(seconds=120)
        jobs.reconcile_orders()
    assert artifacts.get(submitted.id).state == ajs.CANCELLED
    return sessions, operations, jobs, run_id, node_id


def _jobrun_plan_refreshed(session: Session, run_id: str) -> None:
    _run_plan_digest_drifted(session, run_id)


def _jobrun_recovered(session: Session, run_id: str) -> None:
    _run_moved_to_a_newer_generation(session, run_id)


def _jobrun_record_digests_drifted(session: Session, run_id: str) -> None:
    # Digests of the Controller's own rows no longer match their payloads (a
    # stored document read back with a field a newer Controller added).
    for job in session.scalars(select(Job)):
        job.payload_digest = "0" * 64
    for operation in session.scalars(select(AgentOperation)):
        operation.payload_digest = "0" * 64


#: Drift a JobRun Stop must survive, before it is accepted or while its order
#: runs: it removes the run's own job container, identified by the job, its
#: run and its Spark, whatever the run's plan or the rows' digests became.
_JOBRUN_DRIFT: dict[str, Callable[[Session, str], None]] = {
    "current": lambda session, run_id: None,
    "run plan refreshed": _jobrun_plan_refreshed,
    "run recovered to a newer generation": _jobrun_recovered,
    "record digests drifted": _jobrun_record_digests_drifted,
}


@pytest.mark.parametrize("moment", ["before the Stop", "while its order runs"])
@pytest.mark.parametrize("drift", _JOBRUN_DRIFT)
def test_a_jobrun_stop_removes_its_own_job_container_whatever_drifted(
    tmp_path, drift: str, moment: str
) -> None:
    sessions, operations, jobs, run_id, node_id = _lost_job_run(tmp_path)
    if moment == "before the Stop":
        with sessions.begin() as session:
            _JOBRUN_DRIFT[drift](session, run_id)
    plan = operations.preview_stop(run_id)
    assert plan.allowed
    stop = operations.stop(
        run_id,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000310",
    )
    claim = claim_agent(jobs, node_id, "serial-0")
    assert claim is not None
    assert fenced_operation(sessions, claim).parent_job_id == stop.id
    if moment == "while its order runs":
        with sessions.begin() as session:
            _JOBRUN_DRIFT[drift](session, run_id)
    jobs.succeed(claim, STOP_RESULT)
    assert operations.get(stop.id).state == LifecycleState.SUCCEEDED
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "stopped"
        installation_id = run.installation_id
    # Nothing is left holding the Spark: a fresh activation is admitted.
    fresh = operations.preview_run(installation_id, "image-job")
    activated = operations.activate_job_run(
        fresh,
        plan_digest=fresh.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000311",
    )
    assert activated.owner_id != run_id


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_stop_is_derived_after_retention_removed_its_revision(
    tmp_path, monkeypatch
) -> None:
    """The revision row is provenance: a Stop derived after retention removed
    it names the digest the installation's accepted plan recorded."""
    accepted = _accept_service_stop(tmp_path, monkeypatch)
    sessions, operations, jobs, routes, installation_id, _run, nodes, accepted_id = (
        accepted
    )
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        digest = revision.content_digest
        row = {
            attribute.key: getattr(revision, attribute.key)
            for attribute in inspect(revision).mapper.column_attrs
        }
        session.execute(
            delete(CatalogDocumentRevision).where(
                CatalogDocumentRevision.id == revision.id
            )
        )
    clock = NOW + timedelta(seconds=6)
    restarted = RecipeOperationService(
        sessions,
        install_admission=operations._install_admission,
        run_admission=operations._run_admission,
        agent_jobs=jobs,
        route_publications=routes,
        clock=lambda: clock,
    )
    restarted.reconcile_pending_service_stops()
    with sessions() as session:
        orders = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == accepted_id
                )
            )
        )
        assert orders
        assert {order.payload["recipe_content_sha256"] for order in orders} == {digest}
    for node_id in nodes:
        restarted.record_node_result(accepted_id, node_id, succeeded=True, evidence={})
    assert restarted.get(accepted_id).state == LifecycleState.SUCCEEDED
    # The catalog brings the revision back on its next sync; the freed Spark
    # admits a fresh Start.
    with sessions.begin() as session:
        session.execute(insert(CatalogDocumentRevision).values(**row))
    refresh_inventory(sessions, clock)
    plan = restarted.preview_run(installation_id, "after-the-stop")
    fresh = restarted.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
    )
    assert fresh.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}
