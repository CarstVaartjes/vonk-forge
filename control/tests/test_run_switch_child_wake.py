"""A run-switch is due the moment a child it waits on ends."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import cast, get_args

import pytest
from sqlalchemy import Engine, Table, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import WaitReason, canonical_message
from vonk_control.job_documents import RecipeStartParent, controller_recipe_document
from vonk_control.models import Job, ModelCacheOperation
from vonk_control.persisted_waits import read_wait
from vonk_control.run_switch_contract import RunSwitchApplyRequest
from vonk_control.run_switch_contract.vocabulary import RunSwitchPhaseKind
from vonk_control.run_switch_operations.awaited_children import start_request_key
from vonk_control.run_switch_operations.child_wake import CHILD_ROWS, ChildEndWake
from vonk_control.run_switch_operations.planning_helpers import _aware
from vonk_control.run_switch_operations.result_helpers import _read_progress
from vonk_control.run_switch_operations.results import _persisted_result
from vonk_control.stored_json import read_row_column

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


def _child_table(engine: Engine) -> None:
    cast(Table, Job.__table__).create(engine)


def _get[Row](session: Session, model: type[Row], row_id: str) -> Row:
    row = session.get(model, row_id)
    assert row is not None
    return row


def _due(sessions, job_id: str) -> datetime | None:
    with sessions() as session:
        due = _get(session, Job, job_id).next_action_at
        return None if due is None else _aware(due)


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


@pytest.mark.parametrize(
    ("model", "ended"),
    [
        (Job, "succeeded"),
        (Job, "failed"),
        (ModelCacheOperation, "succeeded"),
        (ModelCacheOperation, "cancelled"),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_ending_child_makes_its_run_switch_due_and_one_redrive_consumes_it(
    tmp_path, model, ended
) -> None:
    """Catches a parent left for the ``recipes`` rotation after its child ended."""
    service, executor, operation_id, child_id = _waiting_on_transfer(tmp_path)
    service.wake_on_child_end()
    sessions = service._sessions
    with sessions.begin() as session:
        session.add(_ROW_FACTORIES[model](child_id, "running"))
    assert _due(sessions, operation_id) is None  # nothing ended yet

    with sessions.begin() as session:
        _get(session, model, child_id).state = ended

    assert _due(sessions, operation_id) == NOW
    with sessions() as session:
        wait = read_wait(_get(session, Job, operation_id))
        # The wake names what the parent will read, not a generic fallback.
        assert wait is not None and wait.reason is WaitReason.RECEIPT_MISSING
    # The worker's next look consumes the wake: no due clock is left behind
    # for the due-waits backstop to spin on.
    _replace_child(executor, child_id, state=ended)
    assert service.redrive(operation_id) is True
    due = _due(sessions, operation_id)
    assert due is None or due > NOW
    if ended == "succeeded":
        assert "transfer" in _result(service.get(operation_id)).completed_phases


def test_a_recovery_child_ending_wakes_its_parent(tmp_path) -> None:
    """Catches the wake forgetting a job the observation window waits on."""
    service, _executor, operation_id, _child_id = _waiting_on_transfer(tmp_path)
    service.wake_on_child_end()
    sessions = service._sessions
    recovery_id = str(uuid.uuid4())
    with sessions.begin() as session:
        parent = _get(session, Job, operation_id)
        progress = _read_progress(read_row_column(parent, "result"))
        progress.child_operation_id = None
        progress.recovery_child_operation_id = recovery_id
        parent.result = _persisted_result(progress)
        session.add(_job_row(recovery_id, "running"))
    assert _due(sessions, operation_id) is None

    with sessions.begin() as session:
        _get(session, Job, recovery_id).state = "succeeded"

    assert _due(sessions, operation_id) == NOW


def test_the_start_a_run_switch_issued_ending_wakes_its_parent(tmp_path) -> None:
    """Catches a parent left for the rotation when its own start ended."""
    service, _executor, operation_id, _child_id = _waiting_on_transfer(tmp_path)
    service.wake_on_child_end()
    sessions = service._sessions
    start_id = str(uuid.uuid4())
    with sessions.begin() as session:
        start = _job_row(start_id, "running")
        start.kind = WireAgentOperation.RECIPE_START.value
        start.request_id = start_request_key(_get(session, Job, operation_id))
        start.payload = cast(
            dict[str, object],
            controller_recipe_document(
                RecipeStartParent(
                    schema_version=1,
                    owner_kind="run",
                    owner_id=str(uuid.uuid4()),
                    plan_digest="a" * 64,
                )
            ),
        )
        start.payload_digest = hashlib.sha256(
            canonical_message(start.payload)
        ).hexdigest()
        session.add(start)
    assert _due(sessions, operation_id) is None

    with sessions.begin() as session:
        _get(session, Job, start_id).state = "succeeded"

    assert _due(sessions, operation_id) == NOW


def test_wake_queries_do_not_grow_with_the_live_run_switches(tmp_path) -> None:
    """Catches a per-row lookup in the wake that fires after every ended child."""
    service, _executor, operation_id, _child_id = _waiting_on_transfer(tmp_path)
    sessions = service._sessions
    ended = frozenset({str(uuid.uuid4())})  # not a start, not any parent's child

    def queries_with(clones: int) -> int:
        with sessions.begin() as session:
            parent = _get(session, Job, operation_id)
            for _ in range(clones):
                session.add(
                    Job(
                        id=str(uuid.uuid4()),
                        request_id=str(uuid.uuid4()),
                        kind=parent.kind,
                        state=parent.state,
                        actor=parent.actor,
                        authority_revision=parent.authority_revision,
                        targets=parent.targets,
                        payload_digest=parent.payload_digest,
                        payload=parent.payload,
                        result=parent.result,
                        current_attempt=0,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
        count: list[str] = []

        def record(_conn, _cursor, statement, *_rest) -> None:
            count.append(statement)

        engine = sessions.kw["bind"]
        event.listen(engine, "before_cursor_execute", record)
        try:
            ChildEndWake(sessions, lambda: NOW).wake(ended)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(count)

    assert queries_with(1) == queries_with(8)


def test_wake_runs_after_the_ending_connection_is_returned(tmp_path) -> None:
    """A wake inside the commit would hold a second pooled connection."""
    engine = create_engine(f"sqlite:///{tmp_path / 'wake.sqlite'}", poolclass=QueuePool)
    _child_table(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    pool = cast(QueuePool, engine.pool)
    woken: list[tuple[frozenset[str], int]] = []

    class Probe(ChildEndWake):
        def wake(self, child_ids: frozenset[str]) -> None:
            woken.append((child_ids, pool.checkedout()))

    Probe.install(sessions, lambda: NOW)
    child_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(_job_row(child_id, "running"))
    assert woken == []

    with pytest.raises(RuntimeError), sessions.begin() as session:
        _get(session, Job, child_id).state = "failed"
        session.flush()
        raise RuntimeError("rolled back")
    assert woken == []  # a rolled-back end wakes nobody

    with sessions.begin() as session:
        _get(session, Job, child_id).state = "cancelled"
    assert woken == [(frozenset({child_id}), 0)]
    engine.dispose()


def test_a_row_inserted_already_ended_is_staged_by_its_own_id(tmp_path) -> None:
    """Catches staging before the insert assigns the id: ``None`` matched every
    run-switch with no child and cut its hold short."""
    engine = create_engine(f"sqlite:///{tmp_path / 'born.sqlite'}")
    _child_table(engine)
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
