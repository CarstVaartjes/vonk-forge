"""A conflict re-plans, never loops: a constraint violation is decided by name.

A plan assumption another writer took (REPLAN) is planned again with it taken;
a request's own key (REPLAY) answers with the record that holds it; any other
constraint (DEFECT) fails the request at once, naming the constraint, and
leaves nothing behind that blocks the next request.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Index, PrimaryKeyConstraint, UniqueConstraint, event, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LifecycleState, ProfileReasonCode
from vonk_control import profile_capacity
from vonk_control.admission_locking import (
    CONSTRAINTS,
    Conflict,
    ConstraintDefect,
    ConstraintReplan,
    admitting,
)
from vonk_control.jobs import JobService
from vonk_control.model_primitives import Base
from vonk_control.models import FleetProfileApplication, Job, ResourceReservation
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_profile_colocated_ports import _admitted, _promises, _two_on_one_spark
from .test_profile_port_claims import _ready_profile
from .test_recipe_operations import NOW


def _unique_keys() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Every unique key by the name PostgreSQL gives it: (table, columns)."""

    keys: dict[str, tuple[str, tuple[str, ...]]] = {}
    for table in Base.metadata.tables.values():
        for key in (*table.constraints, *table.indexes):
            if not (
                isinstance(key, PrimaryKeyConstraint | UniqueConstraint)
                or (isinstance(key, Index) and key.unique)
            ):
                continue
            columns = tuple(column.name for column in key.columns)
            name = (
                key.name
                if isinstance(key.name, str)
                else f"{table.name}_pkey"
                if isinstance(key, PrimaryKeyConstraint)
                else "_".join((table.name, *columns, "key"))
            )
            keys[name] = (table.name, columns)
    return keys


def test_every_registered_constraint_is_a_unique_key_and_request_keys_replay():
    keys = _unique_keys()
    assert sorted(set(CONSTRAINTS) - set(keys)) == []
    request_keys = [
        name
        for name, (_, columns) in keys.items()
        if columns in {("request_id",), ("request_key",)}
    ]
    assert request_keys
    assert {name: CONSTRAINTS.get(name) for name in request_keys} == dict.fromkeys(
        request_keys, Conflict.REPLAY
    )


def test_a_port_taken_at_the_index_replans_and_is_admitted(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, profiles, profile = _two_on_one_spark(tmp_path, postgres_engine)
    inspect = RunSwitchOperationService.inspect_request
    preview = profiles.preview
    reviews = 0

    def first_review_gives_one_port_twice(*args, **kwargs):
        # Fault: the first review (an older Controller's) promises both
        # assignments one port; every later review is today's.
        nonlocal reviews
        reviews += 1
        if reviews > 1:
            return preview(*args, **kwargs)
        with monkeypatch.context() as patched:
            patched.setattr(
                RunSwitchOperationService,
                "inspect_request",
                lambda self, request, **kw: inspect(
                    self, request, **{**kw, "planned_ports": frozenset()}
                ),
            )
            return preview(*args, **kwargs)

    attempts: list[tuple[tuple[str, int], ...]] = []
    clear = profile_capacity.clear_port_promises

    def unchecked(session, application_id, wanted, *, now):
        # The plan's own duplicate check is skipped: the database index meets
        # the duplicate promise instead (the REPLAN registry branch).
        pairs = tuple(wanted)
        attempts.append(pairs)
        clear(session, application_id, list(dict.fromkeys(pairs)), now=now)

    monkeypatch.setattr(profiles, "preview", first_review_gives_one_port_twice)
    monkeypatch.setattr(profile_capacity, "clear_port_promises", unchecked)
    load = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")

    for step in range(1, 7):
        if _admitted(sessions, load.id):
            break
        profiles._clock = lambda step=step: NOW + timedelta(minutes=step)
        profiles.tick()

    assert _admitted(sessions, load.id)
    (first, *_), last = attempts, attempts[-1]
    assert len(set(first)) < len(first)
    assert len(set(last)) == len(last)
    # Never the same plan twice: each admission attempt had a new input.
    assert len(attempts) == len(set(attempts))
    assert len(_promises(sessions, load.id)) == 2


def test_a_released_port_promised_elsewhere_replans_instead_of_resuming(
    tmp_path, postgres_engine
):
    sessions, profiles, _, profile, _, _, _, _ = _ready_profile(
        tmp_path, postgres_engine
    )
    loaded = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    ((node_id, port),) = _promises(sessions, loaded.id)
    with sessions.begin() as session:
        (claim,) = session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == loaded.id,
                ResourceReservation.kind == "port",
            )
        )
        claim.state = "released"
        claim.released_at = datetime.now(UTC)
        session.flush()
        # Meanwhile another load promised that port.
        session.add(
            ResourceReservation(
                node_id=node_id,
                kind="port",
                resource_key=port,
                amount_bytes=0,
                owner_kind="fleet-profile",
                owner_id=str(uuid4()),
                state="promised",
                plan_digest="d" * 64,
                created_at=datetime.now(UTC),
            )
        )

    with pytest.raises(ConstraintReplan) as taken, sessions.begin() as session:
        application = session.get(FleetProfileApplication, loaded.id)
        assert application is not None
        profile_capacity.restore_released_profile_claims(session, application)

    assert taken.value.constraint == "uq_promised_node_port"
    assert not _promises(sessions, loaded.id)


def test_a_request_key_raced_by_the_same_request_returns_the_original(
    postgres_engine,
):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    jobs = JobService(sessions, clock=lambda: NOW)
    request_id = str(uuid4())

    def enqueue() -> Job:
        return jobs.enqueue(
            "diagnostic.conflict",
            "admin",
            "r" * 64,
            ["spark-a"],
            {"step": 1},
            request_id=request_id,
        )

    winner: list[Job] = []
    raced = False

    def other_process_commits_first(session: Session, *_: object) -> None:
        # The same request, from another Controller process, commits between
        # this request's lookup and its insert.
        nonlocal raced
        if not raced and any(isinstance(row, Job) for row in session.new):
            raced = True
            winner.append(enqueue())

    event.listen(Session, "before_flush", other_process_commits_first)
    try:
        replayed = enqueue()
    finally:
        event.remove(Session, "before_flush", other_process_commits_first)

    assert winner and replayed.id == winner[0].id
    with sessions() as session:
        assert session.scalars(
            select(Job.id).where(Job.request_id == request_id)
        ).all() == [winner[0].id]


def test_an_unregistered_constraint_fails_at_once_and_a_fresh_load_is_admitted(
    tmp_path, postgres_engine
):
    sessions, profiles, profile = _two_on_one_spark(tmp_path, postgres_engine)
    with postgres_engine.begin() as connection:
        # A constraint no admission path registered: one port per load.
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX test_one_port_per_load ON resource_reservations "
            "(owner_id) WHERE kind = 'port' AND state = 'promised'"
        )

    refused = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")

    assert refused.state == LifecycleState.FAILED
    reason = next(
        item
        for item in refused.progress.blockers
        if item.code == ProfileReasonCode.ADMISSION_DEFECT
    )
    assert "test_one_port_per_load" in reason.detail
    assert "will retry" not in (refused.status_reason or "")
    assert refused.next_attempt_at is None
    assert not refused.progress.admission_pending
    with sessions() as session:
        # Retain failure evidence, without a retry or a held resource claim.
        assert session.scalars(select(FleetProfileApplication.id)).all() == [refused.id]
        assert not session.scalars(
            select(ResourceReservation.id).where(
                ResourceReservation.owner_kind == "fleet-profile"
            )
        ).all()

    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX test_one_port_per_load")
    fresh = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    assert _admitted(sessions, fresh.id)


def test_admitting_names_the_constraint_of_a_non_unique_violation(postgres_engine):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    with (
        pytest.raises(ConstraintDefect) as defect,
        admitting(),
        sessions.begin() as session,
    ):
        session.add(
            ResourceReservation(
                node_id="spark-that-does-not-exist",
                kind="port",
                resource_key="8000",
                amount_bytes=0,
                owner_kind="run",
                owner_id=str(uuid4()),
                state="promised",
                plan_digest="d" * 64,
                created_at=datetime.now(UTC),
            )
        )

    # Not a unique key: a DEFECT whatever its name, and the name is evidence.
    assert defect.value.sqlstate is not None
    assert defect.value.sqlstate.startswith("23")
    assert defect.value.sqlstate != "23505"
    assert defect.value.constraint != "unknown-constraint"
    assert defect.value.constraint in str(defect.value)
