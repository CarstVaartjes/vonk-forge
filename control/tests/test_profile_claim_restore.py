"""A restored profile claim never promises capacity the Spark no longer has."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from vonk_control.admission_locking import ConstraintReplan
from vonk_control.models import (
    FleetProfileApplication,
    NodeInventorySnapshot,
    ResourceReservation,
)
from vonk_control.profile_claim_restore import restore_released_profile_claims

from .test_profile_port_claims import _ready_profile


def _release(sessions, application_id):
    with sessions.begin() as session:
        claims = list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application_id,
                    ResourceReservation.kind != "port",
                )
            )
        )
        assert {claim.kind == "disk" for claim in claims} == {True, False}
        for claim in claims:
            claim.state = "released"
            claim.released_at = datetime.now(UTC)


def _states(sessions, application_id):
    with sessions() as session:
        return {
            claim.kind: claim.state
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application_id,
                    ResourceReservation.kind != "port",
                )
            )
        }


def _restore(sessions, application_id):
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None
        received = session.scalar(select(func.max(NodeInventorySnapshot.received_at)))
        assert received is not None
        # The moment the Spark's newest inventory arrived: current evidence.
        restore_released_profile_claims(
            session, application, now=received.replace(tzinfo=UTC)
        )


@pytest.mark.parametrize("scarce", ["disk", "memory"])
def test_a_restore_without_capacity_replans_instead_of_overcommitting(
    tmp_path, postgres_engine, scarce
) -> None:
    sessions, profiles, _, profile, _, _, _, _ = _ready_profile(
        tmp_path, postgres_engine
    )
    loaded = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    _release(sessions, loaded.id)
    # Other work took what the released claims held.
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            if scarce == "disk":
                snapshot.disk_free_bytes = 0
                snapshot.unused_model_bytes = 0
            else:
                snapshot.host_memory_free_bytes = 0
                snapshot.gpu_memory_free_bytes = 0

    with pytest.raises(ConstraintReplan):
        _restore(sessions, loaded.id)

    # The restore rolled back whole: nothing was promised again.
    assert set(_states(sessions, loaded.id).values()) == {"released"}
    with sessions() as session:
        assert not session.scalar(
            select(func.count()).where(
                ResourceReservation.owner_id == loaded.id,
                ResourceReservation.kind != "port",
                ResourceReservation.state.in_(("active", "promised")),
            )
        )


def test_a_restore_with_capacity_takes_the_claims_back(
    tmp_path, postgres_engine
) -> None:
    sessions, profiles, _, profile, _, _, _, _ = _ready_profile(
        tmp_path, postgres_engine
    )
    loaded = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    _release(sessions, loaded.id)

    _restore(sessions, loaded.id)

    assert set(_states(sessions, loaded.id).values()) <= {"active", "promised"}
