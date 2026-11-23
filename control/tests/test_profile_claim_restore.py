"""A restored profile claim is admitted by the rule every admission uses."""

from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from vonk_control.admission_locking import (
    PATIENT_AFTER_RETRIES,
    acquire_admission_keys,
    node_admission_key,
    patient_admission,
)
from vonk_control.models import (
    FleetProfileApplication,
    NodeInventorySnapshot,
    ResourceReservation,
)
from vonk_control.profile_claim_restore import (
    ProfileClaimCapacityShort,
    restore_released_profile_claims,
)

from .test_profile_port_claims import _ready_profile

GIB = 1024**3
POOL = 128 * GIB

FLOOR = 2 * GIB


def _world(tmp_path, engine):
    """A load whose memory claims were released; returns what a test needs."""
    sessions, profiles, _, profile, _, _, _, _ = _ready_profile(tmp_path, engine)
    loaded = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            # A whole, idle Spark with one shared pool: only commitments can
            # refuse a claim.
            snapshot.memory_pool = "shared"
            snapshot.host_memory_total_bytes = snapshot.gpu_memory_total_bytes = POOL
            snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = POOL
        claims = list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == loaded.id,
                    ResourceReservation.kind != "port",
                )
            )
        )
        assert claims
        for claim in claims:
            claim.state = "released"
            claim.released_at = datetime.now(UTC)
        demand = sum(claim.amount_bytes for claim in claims)
        node_id, kind = claims[0].node_id, claims[0].kind
        received = max(
            snapshot.received_at
            for snapshot in session.scalars(select(NodeInventorySnapshot))
        )
    return sessions, loaded.id, demand, node_id, kind, received.replace(tzinfo=UTC)


def _hold(node_id, kind, amount):
    return ResourceReservation(
        node_id=node_id,
        kind=kind,
        resource_key=str(uuid4()),
        amount_bytes=amount,
        owner_kind="fleet-profile",
        owner_id=str(uuid4()),
        state="promised",
        plan_digest="d" * 64,
        created_at=datetime.now(UTC),
    )


def _restore(sessions, application_id, now):
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None
        restore_released_profile_claims(
            session,
            application,
            now=now,
            memory_floor_bytes=FLOOR,
            inventory_max_age_seconds=300,
        )


def _waits_on_an_advisory_lock(engine) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND NOT granted"
                )
            )
        )


def _live_claims(sessions, application_id):
    with sessions() as session:
        return list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application_id,
                    ResourceReservation.kind != "port",
                    ResourceReservation.state.in_(("active", "promised")),
                )
            )
        )


def test_a_restore_inside_the_floor_band_is_refused(tmp_path, postgres_engine) -> None:
    sessions, application_id, demand, node_id, kind, received = _world(
        tmp_path, postgres_engine
    )
    now = received
    # Others commit all but half the floor: the claim fits the pool, but would
    # leave less than the platform floor.
    with sessions.begin() as session:
        session.add(_hold(node_id, kind, POOL - demand - FLOOR // 2))

    with pytest.raises(ProfileClaimCapacityShort):
        _restore(sessions, application_id, now)
    assert not _live_claims(sessions, application_id)


def test_a_restore_with_room_above_the_floor_takes_the_claims_back(
    tmp_path, postgres_engine
) -> None:
    sessions, application_id, demand, node_id, kind, received = _world(
        tmp_path, postgres_engine
    )
    with sessions.begin() as session:
        session.add(_hold(node_id, kind, POOL - demand - 2 * FLOOR))

    _restore(sessions, application_id, received)

    assert _live_claims(sessions, application_id)


def test_a_restore_without_a_current_sample_is_not_admitted(
    tmp_path, postgres_engine
) -> None:
    """Admission's own answer to stale inventory: not admitted. The retry then
    previews, waits, and resumes on the next fresh sample."""
    sessions, application_id, _, _, _, received = _world(tmp_path, postgres_engine)

    with pytest.raises(ProfileClaimCapacityShort):
        _restore(sessions, application_id, received + timedelta(days=1))
    assert not _live_claims(sessions, application_id)


def test_a_concurrent_admission_and_a_restore_do_not_overcommit(
    tmp_path, postgres_engine
) -> None:
    """Another admission holds the node's key while it inserts its claim; the
    restore queues behind it and then sees that claim."""
    sessions, application_id, demand, node_id, kind, received = _world(
        tmp_path, postgres_engine
    )
    inserted, may_commit = Event(), Event()

    def admission() -> None:
        other = sessionmaker(postgres_engine)
        with other.begin() as session:
            acquire_admission_keys(session, (node_admission_key(node_id),))
            session.add(_hold(node_id, kind, POOL - demand - FLOOR // 2))
            session.flush()
            inserted.set()
            may_commit.wait(timeout=30)

    thread = Thread(target=admission)
    thread.start()
    assert inserted.wait(timeout=30)
    outcome: list[BaseException | None] = []

    def restore() -> None:
        try:
            with patient_admission(PATIENT_AFTER_RETRIES):
                _restore(sessions, application_id, received)
            outcome.append(None)
        except BaseException as error:  # noqa: BLE001 -- the outcome is the evidence
            outcome.append(error)

    restoring = Thread(target=restore)
    restoring.start()
    # Proof of waiting is the lock queue itself: the restore is a waiter on
    # the node's advisory key while the other admission still holds it.
    idle = Event()
    waiting = False
    polls = 0
    while not idle.wait(1):
        if _waits_on_an_advisory_lock(postgres_engine):
            waiting = True
            break
        polls += 1
        if polls >= 30:
            break
    assert waiting, "the restore never queued behind the node's key"
    may_commit.set()
    thread.join(timeout=30)
    restoring.join(timeout=30)

    assert isinstance(outcome[0], ProfileClaimCapacityShort)
    assert not _live_claims(sessions, application_id)


# --- end to end: the retry of a backing-off load whose child is still live ---


def seed_idle_spark(sessions, node_id, now, *, pool="shared", size=10**12):
    """A current inventory sample of a whole, idle Spark."""
    with sessions.begin() as session:
        session.add(
            NodeInventorySnapshot(
                id=str(uuid4()),
                node_id=node_id,
                observed_at=now,
                received_at=now,
                disk_total_bytes=size,
                disk_free_bytes=size,
                host_memory_total_bytes=size,
                host_memory_free_bytes=size,
                gpu_memory_total_bytes=size,
                gpu_memory_free_bytes=size,
                gpu_count=1,
                memory_pool=pool,
                artifact_store_read_only=False,
                capabilities=[],
                evidence_digest=uuid4().hex + uuid4().hex,
            )
        )


def _backing_off_load(postgres_engine, pool, *, hold):
    """A load whose child is live but whose claims were released when it backed
    off; ``hold`` is memory another workload took meanwhile (None: none)."""
    from vonk_agent_protocol import LifecycleState
    from vonk_control.fleet_profile_contract import FleetProfileInput
    from vonk_control.fleet_profiles import FleetProfileService
    from vonk_control.models import Base

    from .test_fleet_profiles import NOW as START
    from .test_fleet_profiles import _node_id, _seed, _uuid
    from .test_profile_continuity_postgres import (
        _assessment_with_promises,
        _SQLCancellationAdapter,
    )

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    node_id = _node_id(1)
    profiles = FleetProfileService(
        sessions,
        clock=lambda: START,
        switch_adapter=_SQLCancellationAdapter(),
        assessment_provider=_assessment_with_promises,
        memory_floor_bytes=0,
    )
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Restore",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [node_id],
                        "desired_state": "running",
                        "assignment_name": "lane-1",
                    }
                ],
            }
        ),
        actor="admin",
    )
    original = profiles.apply(profile.id, request_key=_uuid(19300), actor="admin")
    assert profiles.tick()
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, original.id)
        assert row is not None
        profiles._lifecycle.fail(row, "Lost child observation", START, session=session)
        row.state = LifecycleState.BACKOFF.value
    seed_idle_spark(sessions, node_id, START, pool=pool, size=10_000)
    if hold is not None:
        with sessions.begin() as session:
            session.add(_hold(node_id, "unified-memory", hold))
    return sessions, profiles, original.id


@pytest.mark.parametrize("pool", ["shared", "separate"])
def test_a_retry_resumes_the_live_child_when_the_released_memory_is_free(
    postgres_engine, pool
) -> None:
    from .test_fleet_profiles import _uuid

    sessions, profiles, original_id = _backing_off_load(
        postgres_engine, pool, hold=None
    )

    view = profiles.retry(original_id, request_key=_uuid(19301), actor="admin")

    assert view.id == original_id
    assert _live_claims(sessions, original_id)


@pytest.mark.parametrize("pool", ["shared", "separate"])
def test_a_retry_plans_again_when_another_workload_took_the_released_memory(
    postgres_engine, pool
) -> None:
    from .test_fleet_profiles import _uuid

    # Almost the whole Spark is promised elsewhere: the load's own 100 bytes do
    # not fit any more, so resuming its plan would overcommit.
    sessions, profiles, original_id = _backing_off_load(
        postgres_engine, pool, hold=10_000 - 50
    )

    view = profiles.retry(original_id, request_key=_uuid(19301), actor="admin")

    # The shortfall (ConstraintReplan) was met inside the retry: nothing raised
    # and the live child was not resumed on the old claims; the load was planned
    # again (a new application) or waits for conditions that admit it.
    assert view is not None
    assert not [
        claim
        for claim in _live_claims(sessions, original_id)
        if claim.kind == "unified-memory"
    ]
