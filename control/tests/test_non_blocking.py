"""Prove ending alone cannot pass if a hold or refusal blocks the next request."""

from dataclasses import dataclass, field

import pytest

from .non_blocking import Hold, assert_ended_without_blocking, assert_no_orphaned_holds


@dataclass
class Operation:
    state: str = "running"
    request_key: str = "old"
    reason_code: str | None = None
    refusal: object | None = None


@dataclass
class World:
    holds: list[Hold] = field(default_factory=list)


def test_ending_proves_new_key_admission_and_returns_receipts():
    original = Operation()
    ended, admitted = assert_ended_without_blocking(
        World(),
        original,
        end=lambda op: Operation("cancelled", op.request_key),
        fresh=lambda world: Operation("queued", "new"),
    )
    assert ended.state == "cancelled"
    assert admitted.request_key == "new"


@pytest.mark.parametrize(
    "kind", ["removal gate", "reservation", "claim", "lease", "busy flag"]
)
@pytest.mark.parametrize(
    "state", [None, "failed", "cancelled", "superseded", "needs-operator"]
)
def test_orphaned_or_ended_owner_fails_for_every_hold_class(kind, state):
    with pytest.raises(AssertionError, match="orphaned"):
        assert_no_orphaned_holds(World([Hold(kind, "owner", state)]))
    assert_no_orphaned_holds(World([Hold(kind, "owner", "running")]))


@pytest.mark.parametrize(
    "ended,fresh",
    [
        (Operation("needs-operator"), Operation("queued", "new")),
        (Operation("running"), Operation("queued", "new")),
        (Operation("failed"), Operation("queued", "new")),
        (Operation("cancelled"), Operation("queued", "old")),
        (Operation("cancelled"), Operation("failed", "new")),
        (Operation("cancelled"), Operation("queued", "new", refusal="busy")),
    ],
)
def test_bad_end_or_reused_or_refused_fresh_request_fails(ended, fresh):
    with pytest.raises(AssertionError):
        assert_ended_without_blocking(
            World(), Operation(), end=lambda op: ended, fresh=lambda world: fresh
        )


def test_hold_is_checked_before_fresh_request_and_after_it():
    world = World([Hold("gate", None, None)])
    called: list[bool] = []
    with pytest.raises(AssertionError):
        assert_ended_without_blocking(
            world,
            Operation(),
            end=lambda op: Operation("cancelled"),
            fresh=lambda w: (called.append(True), Operation("queued", "new"))[1],
        )
    assert not called
    world.holds.clear()

    def fresh(w):
        w.holds.append(Hold("lease", "old", "cancelled"))
        return Operation("queued", "new")

    with pytest.raises(AssertionError):
        assert_ended_without_blocking(
            world, Operation(), end=lambda op: Operation("cancelled"), fresh=fresh
        )


def test_sql_reservations_and_session_factory_are_inspected(tmp_path):
    from .test_attempt_residues import _load
    from .test_reservation_owners import _end_application

    load = _load(tmp_path)
    assert_no_orphaned_holds(load)
    _end_application(load, "cancelled")
    with pytest.raises(AssertionError, match="reservation"):
        assert_no_orphaned_holds(load)
    with load.sessions() as session, pytest.raises(AssertionError, match="reservation"):
        assert_no_orphaned_holds(session)


@pytest.mark.parametrize("owner_kind", ["model-cache-operation", "recipe-image-job"])
def test_sql_removal_gate_with_missing_owner_is_detected(owner_kind):
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from vonk_control.models import ArtifactLifecycleGate, Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        gate = ArtifactLifecycleGate(
            artifact_kind="model-set",
            artifact_sha256="a" * 64,
            removal_owner_kind=owner_kind,
            removal_owner_id=str(uuid.uuid4()),
            removal_fence=str(uuid.uuid4()),
            updated_at=datetime.now(UTC),
        )
        session.add(gate)
        session.flush()
        with pytest.raises(AssertionError, match="orphaned removal gate"):
            assert_no_orphaned_holds(session)
        session.delete(gate)
        session.flush()
        assert_no_orphaned_holds(session)
    engine.dispose()


def test_real_view_blocker_codes_and_service_release_callback_are_supported():
    @dataclass
    class Blocker:
        code: str

    @dataclass
    class View:
        state: str
        request_key: str
        blockers: list[Blocker] = field(default_factory=list)

    old = View("running", "old")
    ended = View("failed", "old", [Blocker("runtime.exited")])
    admitted = View("observing", "new")
    checked = []
    result = assert_ended_without_blocking(
        None,
        old,
        end=lambda op: ended,
        fresh=lambda world: admitted,
        assert_released=lambda: checked.append(True),
    )
    assert result == (ended, admitted)
    assert checked == [True, True]


def test_failed_end_checks_typed_child_evidence_through_service_callback():
    @dataclass
    class JobView:
        state: str
        request_id: str

    old = JobView("running", "old")
    ended = JobView("failed", "old")
    admitted = JobView("queued", "new")
    checked = []
    assert_ended_without_blocking(
        None,
        old,
        end=lambda op: ended,
        fresh=lambda world: admitted,
        assert_released=lambda: None,
        assert_reason=lambda op: checked.append(op),
    )
    assert checked == [ended]

    def no_typed_child(op):
        raise AssertionError("child failure has no typed reason")

    with pytest.raises(AssertionError, match="child failure"):
        assert_ended_without_blocking(
            None,
            old,
            end=lambda op: ended,
            fresh=lambda world: admitted,
            assert_released=lambda: None,
            assert_reason=no_typed_child,
        )
