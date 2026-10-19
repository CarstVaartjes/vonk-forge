"""A parked profile load names the lock that refuses it, and in time it wins.

Production 2026-10-13: a load stayed in ``prepare`` for 21 minutes after its
preparations succeeded. Every retry of its admission fence was refused on a
lock, and the load only said "waiting for the active workload owner": not
which lock, nor who held it, and its retries never queued for the lock.
"""

from __future__ import annotations

import threading
from uuid import uuid4

from sqlalchemy import select
from vonk_agent_protocol import ProfileReasonCode
from vonk_control.admission_locking import PATIENT_AFTER_RETRIES, label_transaction
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationView,
)
from vonk_control.fleet_profiles.persistence import _persisted_profile_progress
from vonk_control.models import AgentNode, FleetProfileApplication

from .test_profile_capacity_admission import _capacity_profile


def _hold_node_row(sessions, node_id: str, held: threading.Event, release) -> None:
    """A node writer (here: a heartbeat) holding the Spark's row."""

    with sessions.begin() as holder:
        label_transaction(holder, "agent-heartbeat")
        assert holder.get(AgentNode, node_id, with_for_update=True) is not None
        held.set()
        assert release.wait(timeout=30), "test never released the node row"


def _pending(sessions, request_key: str) -> FleetProfileApplicationProgress:
    with sessions() as session:
        row = session.scalar(
            select(FleetProfileApplication).where(
                FleetProfileApplication.request_key == request_key
            )
        )
        assert row is not None
        return _persisted_profile_progress(row)


def _pending_due(sessions, request_key: str):
    from vonk_control.persisted_waits import wait_due

    with sessions() as session:
        row = session.scalar(
            select(FleetProfileApplication).where(
                FleetProfileApplication.request_key == request_key
            )
        )
        assert row is not None
        due = wait_due(row)
        assert due is not None
        return due


def test_parked_load_names_its_lock_holder_then_queues_for_the_lock(
    tmp_path, postgres_engine
) -> None:
    sessions, profiles, _, profile, api, headers, _, nodes = _capacity_profile(
        tmp_path, postgres_engine
    )
    request_key = str(uuid4())
    held = threading.Event()
    release = threading.Event()
    holder = threading.Thread(
        target=_hold_node_row, args=(sessions, nodes[0], held, release)
    )
    holder.start()
    try:
        assert held.wait(timeout=10)
        response = api.post(
            f"/api/profile/{profile.number}/load",
            headers=headers,
            json={"request_key": request_key},
        )
        assert response.status_code == 202, response.text
        waiting = FleetProfileApplicationView.model_validate_json(response.content)
        assert any(
            blocker.code == ProfileReasonCode.ADMISSION_BUSY
            and "retry automatically" in blocker.detail
            and "review again" not in blocker.detail.lower()
            for blocker in waiting.blockers
        ), waiting.blockers
        retry_at = waiting.next_attempt_at
        assert retry_at is not None
        assert retry_at.isoformat() in (waiting.status_reason or "")

        # Each retry while the row stays held is refused, and the parked load
        # says which lock refused it and who holds it.
        while (parked := _pending(sessions, request_key)).admission_attempt < (
            PATIENT_AFTER_RETRIES
        ):
            assert _pending_due(sessions, request_key) is not None
            next_check = _pending_due(sessions, request_key)
            profiles._clock = lambda due=next_check: due
            assert profiles.tick()
            refused = _pending(sessions, request_key)
            assert refused.admission_pending is True
            assert refused.admission_attempt == parked.admission_attempt + 1
            detail = " ".join(blocker.detail for blocker in refused.blockers)
            assert "agent_nodes" in detail, detail
            assert "agent-heartbeat" in detail, detail
            assert "review again" not in detail.lower(), detail
            assert any(
                blocker.code == ProfileReasonCode.ADMISSION_BUSY
                and "retry automatically" in blocker.detail
                for blocker in refused.blockers
            ), refused.blockers
            retry_at = _pending_due(sessions, request_key)
            assert retry_at is not None
            with sessions() as session:
                row = session.scalar(
                    select(FleetProfileApplication).where(
                        FleetProfileApplication.request_key == request_key
                    )
                )
                assert row is not None
                assert retry_at.isoformat() in (row.status_reason or "")

        # After repeated refusals the retry queues for the lock: the writer
        # lets go while it waits, and the load is admitted on that same retry.
        due = _pending_due(sessions, request_key)
        assert due is not None
        profiles._clock = lambda: due
        letting_go = threading.Timer(0.5, release.set)
        letting_go.start()
        assert profiles.tick()
        letting_go.join(timeout=30)
    finally:
        release.set()
        holder.join(timeout=10)
        assert not holder.is_alive()

    admitted = _pending(sessions, request_key)
    assert admitted.admission_pending is False, admitted
    assert admitted.switch_adapter is not None
