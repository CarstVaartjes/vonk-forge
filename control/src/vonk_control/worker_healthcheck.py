"""Container-local readiness for the production scheduler worker."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_control.db.connections import build_engine, session_factory
from vonk_control.models.jobs import ControlProcessHeartbeat
from vonk_control.settings import Settings

#: How recent the worker's readiness heartbeat must be to count as live.
WORKER_READINESS_WINDOW_SECONDS = 30

#: One step may run this long before the watchdog restarts the worker; a step
#: that started within it is still the worker's live work, not a stale record.
WORKER_STEP_WINDOW_SECONDS = 180


def verify_worker_ready(
    sessions,
    *,
    process_instance_id: str,
    now: datetime,
    maximum_age_seconds: int = WORKER_READINESS_WINDOW_SECONDS,
) -> None:
    """Require a recent completed scheduler loop from this exact process identity."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("worker readiness clock must be timezone-aware")
    timestamp = now.astimezone(UTC)
    with sessions() as session:
        heartbeat = session.scalar(
            select(ControlProcessHeartbeat).where(
                ControlProcessHeartbeat.process_kind == "worker",
                ControlProcessHeartbeat.process_instance_id == process_instance_id,
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
    if heartbeat.loop_sequence < 1 or age > timedelta(seconds=maximum_age_seconds):
        raise RuntimeError("worker readiness evidence is invalid or stale")


def main() -> None:
    settings = Settings.from_env_and_secrets()
    verify_worker_ready(
        session_factory(build_engine(settings.database_url, component="healthcheck")),
        process_instance_id=current_worker_instance_id(),
        now=datetime.now(UTC),
    )


if __name__ == "__main__":
    main()


def current_worker_instance_id(proc_root: Path = Path("/proc"), pid: int = 1) -> str:
    """Identify one Linux process lifetime without persistent mutable state."""
    boot_id = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    process_stat = (proc_root / str(pid) / "stat").read_text().strip()
    closing_parenthesis = process_stat.rfind(")")
    fields_after_name = process_stat[closing_parenthesis + 2 :].split()
    if closing_parenthesis < 1 or len(fields_after_name) < 20:
        raise UnknownOutcomeError(
            "worker process identity is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    start_ticks = fields_after_name[19]
    pid_namespace = (proc_root / str(pid) / "ns/pid").stat().st_ino
    material = f"{boot_id}\n{pid}\n{pid_namespace}\n{start_ticks}\n".encode()
    return hashlib.sha256(material).hexdigest()
