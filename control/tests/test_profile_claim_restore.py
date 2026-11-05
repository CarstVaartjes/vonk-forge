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
            # A whole, idle Spark: only commitments can refuse a claim.
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


@pytest.mark.parametrize("stale", [False, True], ids=["current", "stale-inventory"])
def test_a_restore_inside_the_floor_band_is_refused(
    tmp_path, postgres_engine, stale
) -> None:
    sessions, application_id, demand, node_id, kind, received = _world(
        tmp_path, postgres_engine
    )
    now = received + (timedelta(days=1) if stale else timedelta())
    # Others commit all but half the floor: the claim fits the pool, but would
    # leave less than the platform floor. Commitments alone refuse it.
    with sessions.begin() as session:
        session.add(_hold(node_id, kind, POOL - demand - FLOOR // 2))

    with pytest.raises(ProfileClaimCapacityShort):
        _restore(sessions, application_id, now)
    assert not _live_claims(sessions, application_id)


@pytest.mark.parametrize("stale", [False, True], ids=["current", "stale-inventory"])
def test_a_restore_with_room_above_the_floor_takes_the_claims_back(
    tmp_path, postgres_engine, stale
) -> None:
    sessions, application_id, demand, node_id, kind, received = _world(
        tmp_path, postgres_engine
    )
    now = received + (timedelta(days=1) if stale else timedelta())
    with sessions.begin() as session:
        session.add(_hold(node_id, kind, POOL - demand - 2 * FLOOR))

    _restore(sessions, application_id, now)

    assert _live_claims(sessions, application_id)


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
    while not idle.wait(1):
        if _waits_on_an_advisory_lock(postgres_engine):
            break
    may_commit.set()
    thread.join(timeout=30)
    restoring.join(timeout=30)

    assert isinstance(outcome[0], ProfileClaimCapacityShort)
    assert not _live_claims(sessions, application_id)
