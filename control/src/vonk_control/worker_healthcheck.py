"""Container-local readiness for the production scheduler worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .models import ControlProcessHeartbeat


def verify_worker_ready(
    sessions,
    *,
    process_instance_id: str,
    now: datetime,
    maximum_age_seconds: int = 30,
) -> None:
    """Require a recent completed scheduler loop from this exact process identity."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("worker readiness clock must be timezone-aware")
    timestamp = now.astimezone(UTC)
    with sessions() as session:
        heartbeat = session.scalar(
            select(ControlProcessHeartbeat).where(
                ControlProcessHeartbeat.process_kind == "worker",
            )
        )
    if heartbeat is None:
        raise RuntimeError("worker readiness heartbeat is unavailable")
    completed_at = heartbeat.completed_at
    if completed_at is None:
        raise RuntimeError("worker readiness evidence is invalid or stale")
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    age = timestamp - completed_at.astimezone(UTC)
    if (
        heartbeat.process_instance_id != process_instance_id
        or heartbeat.loop_sequence < 1
        or age < timedelta(0)
        or age > timedelta(seconds=maximum_age_seconds)
    ):
        raise RuntimeError("worker readiness evidence is invalid or stale")


def main() -> None:
    from .db import build_engine, session_factory
    from .settings import WorkerSettings
    from .worker import current_worker_instance_id

    worker = WorkerSettings.from_env_and_secrets()
    verify_worker_ready(
        session_factory(build_engine(worker.database_url)),
        process_instance_id=current_worker_instance_id(),
        now=datetime.now(UTC),
    )


if __name__ == "__main__":
    main()
