"""A wait on a live owner never expires; only a stall of the work's own progress ends it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from vonk_control.models import AgentNode, Job
from vonk_control.reservation_owners import OwnerKind, OwnerRef, owner
from vonk_control.stall_window import Verdict, Wait, progress_deadline, progress_verdict

from .test_recipe_operations import setup_services

START = datetime(2026, 10, 13, 12, tzinfo=UTC)
WINDOW = timedelta(minutes=30)


def _verdict(now, *, progressed=None, waiting_on=None):
    return progress_verdict(
        started_at=START,
        progressed_at=progressed,
        window=WINDOW,
        now=now,
        waiting_on=waiting_on,
    )


def test_work_waiting_on_a_live_owner_never_stalls():
    """Catches a fixed cap: days past any window, a live owner keeps it waiting."""
    assert _verdict(START + timedelta(days=3), waiting_on=Wait(True)) is Verdict.WAITING
    assert (
        progress_deadline(
            started_at=START,
            progressed_at=None,
            window=WINDOW,
            now=START + timedelta(days=3),
            waiting_on=Wait(True),
        )
        == START + timedelta(days=3) + WINDOW
    )


def test_own_progress_restarts_the_window_and_silence_stalls_it():
    progressed = START + timedelta(hours=5)
    assert _verdict(progressed + WINDOW / 2, progressed=progressed) is (
        Verdict.PROGRESSING
    )
    assert _verdict(progressed + WINDOW, progressed=progressed) is Verdict.STALLED


def test_time_waiting_is_not_charged_once_the_owner_ended():
    """A load that waited hours on another does not stall when that one ends."""
    ended = START + timedelta(hours=2)
    owner_ended = Wait(False, ended_at=ended)
    assert _verdict(ended + timedelta(minutes=1), waiting_on=owner_ended) is (
        Verdict.PROGRESSING
    )
    assert _verdict(ended + WINDOW, waiting_on=owner_ended) is Verdict.STALLED


def test_owner_arms_name_live_and_ended_owners(tmp_path: Path):
    sessions, _service, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    now = START
    with sessions.begin() as session:
        job = Job(
            kind="recipe.start",
            state="running",
            actor="admin",
            authority_revision="a" * 64,
            payload_digest="b" * 64,
            request_id="00000000-0000-4000-8000-0000000000aa",
            payload={},
            targets=list(nodes),
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        job_id = job.id
    with sessions.begin() as session:
        assert owner(session, OwnerRef(OwnerKind.JOB, job_id)).live
        assert not owner(session, OwnerRef(OwnerKind.JOB, "missing")).live
        spark = owner(session, OwnerRef(OwnerKind.NODE, nodes[0]))
        assert spark.live
        session.get(AgentNode, nodes[0]).last_seen_at = now
        assert not owner(session, OwnerRef(OwnerKind.SPARK_RETURN, nodes[0]), now).live
        assert owner(
            session,
            OwnerRef(OwnerKind.SPARK_RETURN, nodes[0]),
            now + timedelta(hours=1),
        ).live
        stored = session.get(Job, job_id)
        stored.state = "failed"
        stored.updated_at = now + timedelta(minutes=5)
        ended = owner(session, OwnerRef(OwnerKind.JOB, job_id))
        assert not ended.live and ended.ended_at == now + timedelta(minutes=5)
        session.get(AgentNode, nodes[0]).revoked_at = now
        revoked = owner(session, OwnerRef(OwnerKind.NODE, nodes[0]))
        assert not revoked.live and revoked.ended_at == now
