"""A run-switch is due the moment a child it waits on ends."""

from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import Future
from datetime import datetime
from threading import RLock
from typing import get_args

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from vonk_agent_protocol import canonical_message
from vonk_control.distribution_executor.background import BackgroundPreparations
from vonk_control.models import Job, ModelCacheOperation
from vonk_control.run_switch_contract import RunSwitchApplyRequest
from vonk_control.run_switch_contract.vocabulary import RunSwitchPhaseKind
from vonk_control.run_switch_operations.child_wake import CHILD_ROWS, ChildEndWake
from vonk_control.run_switch_operations.planning_helpers import _aware

from .test_run_switch_operations import (
    NOW,
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _child_operation_id,
    _replace_child,
    _request,
    _result,
    _service,
    setup_services,
)

#: Where each run-switch phase keeps the child it waits on (``None``: the phase
#: completes inline and leaves no child). The runtime preflight gate's probes
#: are ``Job`` rows too. A new phase kind fails here until its child storage is
#: named, and storage the wake does not watch fails the subset check.
PHASE_CHILD_ROWS: dict[str, frozenset[type[object]]] = {
    "transfer": frozenset({Job, ModelCacheOperation}),  # target-copy, model-download
    "prepare": frozenset({Job}),  # container-build, runtime-install
    "start": frozenset({Job}),
    "stop": frozenset({Job}),
    "uninstall": frozenset({Job}),
    "verify": frozenset(),
    "cleanup": frozenset(),
    "final_verify": frozenset(),
}


def test_every_child_a_run_switch_can_wait_on_is_watched() -> None:
    assert set(PHASE_CHILD_ROWS) == set(get_args(RunSwitchPhaseKind))
    assert frozenset().union(*PHASE_CHILD_ROWS.values()) <= set(CHILD_ROWS)


def _job_row(row_id: str, state: str) -> Job:
    payload = {"workload_intent_ordinal": 1}
    return Job(
        id=row_id,
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state=state,
        actor="operator",
        authority_revision="a" * 40,
        targets=[],
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        current_attempt=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _cache_row(row_id: str, state: str) -> ModelCacheOperation:
    return ModelCacheOperation(
        id=row_id,
        request_key=str(uuid.uuid4()),
        kind="download",
        state=state,
        payload={},
        progress={},
        actor="test",
        created_at=NOW,
        updated_at=NOW,
    )


_ROW_FACTORIES = {Job: _job_row, ModelCacheOperation: _cache_row}


def test_every_watched_row_has_a_fixture() -> None:
    assert set(_ROW_FACTORIES) == set(CHILD_ROWS)


def _due(sessions, job_id: str) -> datetime | None:
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return None if job.next_action_at is None else _aware(job.next_action_at)


def _waiting_on_transfer(tmp_path):
    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    operation = service.apply(
        RunSwitchApplyRequest(
            **_request(sessions, nodes[0]).model_dump(), request_key=str(uuid.uuid4())
        ),
        actor="admin",
    )
    for _ in range(8):
        service.redrive(operation.operation_id)
        if _result(service.get(operation.operation_id)).child_operation_id:
            break
    child_id = _child_operation_id(service.get(operation.operation_id))
    return service, executor, operation.operation_id, child_id


@pytest.mark.parametrize("ended", ["succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("model", list(CHILD_ROWS), ids=lambda model: model.__name__)
def test_ending_child_makes_its_run_switch_due_and_one_redrive_consumes_it(
    tmp_path, model, ended
) -> None:
    """Catches a parent left for the ``recipes`` rotation after its child ended."""
    service, executor, operation_id, child_id = _waiting_on_transfer(tmp_path)
    service.wake_on_child_end()
    sessions = service._sessions
    with sessions.begin() as session:
        session.add(_ROW_FACTORIES[model](child_id, "running"))
    with sessions() as session:
        job = session.get(Job, operation_id)
        assert job is not None and job.next_action_at is None  # nothing ended yet

    with sessions.begin() as session:
        session.get(model, child_id).state = ended

    assert _due(sessions, operation_id) == NOW
    # The worker's next look consumes the wake: no due clock is left behind
    # for the due-waits backstop to spin on.
    _replace_child(executor, child_id, state=ended)
    assert service.redrive(operation_id) is True
    due = _due(sessions, operation_id)
    assert due is None or due > NOW
    if ended == "succeeded":
        assert "transfer" in _result(service.get(operation_id)).completed_phases


def test_wake_runs_after_the_ending_connection_is_returned(tmp_path) -> None:
    """A wake inside the commit would hold a second pooled connection."""
    engine = create_engine(f"sqlite:///{tmp_path / 'wake.sqlite'}", poolclass=QueuePool)
    Job.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    woken: list[tuple[frozenset[str], int]] = []

    class Probe(ChildEndWake):
        def wake(self, child_ids: frozenset[str]) -> None:
            woken.append((child_ids, engine.pool.checkedout()))

    Probe.install(sessions, lambda: NOW)
    child_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(_job_row(child_id, "running"))
    assert woken == []

    with pytest.raises(RuntimeError), sessions.begin() as session:
        session.get(Job, child_id).state = "failed"
        session.flush()
        raise RuntimeError("rolled back")
    assert woken == []  # a rolled-back end wakes nobody

    with sessions.begin() as session:
        session.get(Job, child_id).state = "cancelled"
    assert woken == [(frozenset({child_id}), 0)]
    engine.dispose()


def test_a_row_inserted_already_ended_is_staged_by_its_own_id(tmp_path) -> None:
    """Catches staging before the insert assigns the id: ``None`` matched every
    run-switch with no child and cut its hold short."""
    engine = create_engine(f"sqlite:///{tmp_path / 'born.sqlite'}")
    Job.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    woken: list[frozenset[str]] = []

    class Probe(ChildEndWake):
        def wake(self, child_ids: frozenset[str]) -> None:
            woken.append(child_ids)

    Probe.install(sessions, lambda: NOW)
    born = _job_row(str(uuid.uuid4()), "succeeded")
    born.id = None  # type: ignore[assignment]
    with sessions.begin() as session:
        session.add(born)
    assert born.id is not None
    assert woken == [frozenset({born.id})]
    engine.dispose()


def test_failed_wake_never_reaches_the_committed_end(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'lost.sqlite'}")
    Job.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    def lost() -> object:
        raise OSError("injected wake loss")

    ChildEndWake.install(sessions, lost)  # type: ignore[arg-type]
    child_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(_job_row(child_id, "running"))
    with sessions.begin() as session:
        session.get(Job, child_id).state = "succeeded"
    with sessions() as session:
        assert session.get(Job, child_id).state == "succeeded"
    engine.dispose()


def test_finished_image_preparation_wakes_its_owner(tmp_path) -> None:
    """Catches a done preparation waiting out its five-second next check."""
    service, _executor, operation_id, _child_id = _waiting_on_transfer(tmp_path)
    with service._sessions() as session:
        job = session.get(Job, operation_id)
        assert job is not None and job.next_action_at is None
        request_id = job.request_id
    preparation: Future[None] = Future()

    class Preparations(BackgroundPreparations):
        def __init__(self) -> None:
            self._runtime_image_lock = RLock()
            self._runtime_image_futures = {(request_id, 0, 0): (preparation, "t")}
            self._runtime_image_inflight = {preparation}
            self._sessions = service._sessions
            self._clock = lambda: NOW

    background = Preparations()
    background.reconcile_background()  # still running: nothing to wake
    with service._sessions() as session:
        assert session.get(Job, operation_id).next_action_at is None

    preparation.set_result(None)
    background.reconcile_background()
    assert _due(service._sessions, operation_id) == NOW
    # The visit that collects the result still owns the future.
    assert (request_id, 0, 0) in background._runtime_image_futures
