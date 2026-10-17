"""A Spark whose copies from the NAS are far slower than another's is shown."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)
from vonk_control.nas_transfer_rate import slow_nas_transfers
from vonk_control.stored_json import write_guard_mode

NOW = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
FAST = "spk_" + "2818" * 8
SLOW = "spk_" + "9a86" * 8
NAMES = {FAST: "spark-3542", SLOW: "spark-2297"}
GB = 10**9


def _sessions() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id in (FAST, SLOW):
            session.add(
                AgentNode(node_id=node_id, state="active", architecture="linux-arm64")
            )
        session.add(
            Job(
                id="job",
                request_id="request",
                kind="recipe.install",
                state="running",
                actor="test",
                authority_revision="a" * 64,
                targets=[FAST, SLOW],
                payload_digest="b" * 64,
                payload={},
                created_at=NOW - timedelta(hours=3),
                updated_at=NOW,
            )
        )
    return sessions


def _transfer(
    sessions: sessionmaker[Session],
    node_id: str,
    size: int,
    seconds: float | None,
    *,
    ago: timedelta = timedelta(hours=1),
    state: str = "succeeded",
    kind: str = "artifact.distribution.v1",
    progress: object = None,
) -> None:
    operation_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id="job",
                node_id=node_id,
                kind=kind,
                payload_digest="c" * 64,
                payload={},
                authority_revision="a" * 64,
                state=state,
                current_attempt=1,
                created_at=NOW - ago,
                updated_at=NOW - ago,
            )
        )
        session.add(
            AgentOperationAttempt(
                operation_id=operation_id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=NOW,
                agent_certificate_serial="serial",
                state=state,
                progress=progress
                if progress is not None
                else {
                    "phase": "completed",
                    "completed_bytes": size,
                    "total_bytes_known": False,
                    "elapsed_seconds": seconds,
                },
            )
        )


def _notices(
    sessions: sessionmaker[Session], names: dict[str, str] = NAMES
) -> dict[str, object]:
    with sessions() as session:
        return dict(slow_nas_transfers(session, names, NOW))


def test_production_shape_names_the_slow_spark_and_both_rates() -> None:
    sessions = _sessions()
    _transfer(sessions, FAST, int(48.8 * GB), 553)
    _transfer(sessions, FAST, int(30.0 * GB), 500, ago=timedelta(hours=2))
    _transfer(sessions, SLOW, int(25.8 * GB), 7089)

    with sessions() as session:
        notices = slow_nas_transfers(session, NAMES, NOW)

    assert set(notices) == {SLOW}
    notice = notices[SLOW]
    assert notice.detail.startswith("Copies from the NAS at 3.6 MB/s vs 74.1 MB/s")
    assert "on spark-3542" in notice.detail
    assert "25.8 GB in 7089 s" in notice.detail
    assert "spark-3542" in notice.recommendation


def test_comparable_sparks_raise_nothing() -> None:
    sessions = _sessions()
    _transfer(sessions, FAST, 48 * GB, 550)
    _transfer(sessions, SLOW, 25 * GB, 600)

    assert _notices(sessions) == {}


def test_one_spark_has_nothing_to_be_compared_with() -> None:
    sessions = _sessions()
    _transfer(sessions, SLOW, 25 * GB, 7000)

    assert _notices(sessions) == {}


def test_small_failed_old_and_other_orders_are_not_evidence() -> None:
    sessions = _sessions()
    _transfer(sessions, FAST, 48 * GB, 550)
    _transfer(sessions, SLOW, GB // 2, 600)
    _transfer(sessions, SLOW, 25 * GB, 7000, state="failed")
    _transfer(sessions, SLOW, 25 * GB, 7000, ago=timedelta(days=8))
    _transfer(sessions, SLOW, 25 * GB, 7000, kind="recipe.build")

    assert _notices(sessions) == {}


def test_an_instant_already_present_transfer_does_not_make_a_healthy_spark_slow() -> (
    None
):
    sessions = _sessions()
    # Present objects report every byte at once: one sample at a "GB/s" rate.
    _transfer(sessions, FAST, 48 * GB, 2, ago=timedelta(minutes=5))
    _transfer(sessions, FAST, 30 * GB, 500)
    _transfer(sessions, FAST, 30 * GB, 520, ago=timedelta(hours=2))
    _transfer(sessions, SLOW, 25 * GB, 600)

    assert _notices(sessions) == {}


def test_the_median_ignores_one_inflated_sample_of_the_slow_spark() -> None:
    sessions = _sessions()
    _transfer(sessions, FAST, 48 * GB, 550)
    _transfer(sessions, SLOW, 25 * GB, 3, ago=timedelta(minutes=5))
    _transfer(sessions, SLOW, 25 * GB, 7000)
    _transfer(sessions, SLOW, 20 * GB, 6000, ago=timedelta(hours=2))

    assert set(_notices(sessions)) == {SLOW}


def test_unreadable_or_unmeasured_progress_is_skipped_not_fatal() -> None:
    sessions = _sessions()
    _transfer(sessions, FAST, 48 * GB, 550)
    with write_guard_mode(strict=False):
        _transfer(sessions, SLOW, 0, None, progress={"completed_bytes": "lots"})
    _transfer(sessions, SLOW, 25 * GB, None)
    _transfer(sessions, SLOW, 25 * GB, 0)

    assert _notices(sessions) == {}

    _transfer(sessions, SLOW, 25 * GB, 7000)
    assert set(_notices(sessions)) == {SLOW}
