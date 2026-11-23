from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update

from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_control import build_identity

from ..identity_insert import insert_identity
from ..logging import log_event
from ..models.jobs import ControlProcessHeartbeat
from ..stall_window import Verdict
from ..worker_healthcheck import (
    WORKER_READINESS_WINDOW_SECONDS,
    WORKER_STEP_WINDOW_SECONDS,
)

_PROCESS_INSTANCE = re.compile(r"[0-9a-f]{64}\Z")


_LOGGER = logging.getLogger("vonk-control-worker")


#: The longest one step may run (the pass beats before every step) before the
#: worker restarts: a hang, not a slow pass.
_WORKER_WATCHDOG_TIMEOUT_SECONDS = WORKER_STEP_WINDOW_SECONDS


#: How often the readiness heartbeat is written between the steps of a long
#: pass, so the pass stays inside the readiness window.
_READINESS_BEAT_SECONDS = WORKER_READINESS_WINDOW_SECONDS / 3


class WorkerWatchdog:
    """Track the pass thread's steps and detect one that hangs."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("worker watchdog timeout must be positive")
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._last_completed = clock()
        self._wall_clock = wall_clock
        self._step: str | None = None
        self._step_serial = 0
        #: (serial, step, monotonic start, wall start): one tuple, so the
        #: watcher thread never sees a step paired with another's start.
        self._running: tuple[int, str, float, datetime] | None = None

    def beat(self, step: str | None = None) -> None:
        """The pass is turning; ``step`` is the step it starts now, if any."""
        # One writer publishes one timestamp; readers need no coordination lock.
        # Sampling the clock cannot hold the watchdog's observation hostage.
        self._last_completed = self._clock()
        self._step = step
        self._step_serial += 1
        self._running = (
            (self._step_serial, step, self._last_completed, self._wall_clock())
            if step is not None
            else None
        )

    def running_longer_than(self, seconds: float) -> tuple[int, str, datetime] | None:
        """The step instance running for more than ``seconds``: (id, name, start)."""
        running = self._running
        if running is None or self._clock() - running[2] <= seconds:
            return None
        return running[0], running[1], running[3]

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def step(self) -> str | None:
        """The step that started at the last beat, if one did."""
        return self._step

    def stalled(self) -> bool:
        return self._clock() - self._last_completed > self._timeout_seconds

    def verdict(self) -> Verdict:
        """``STALLED`` once one step (or the gap between beats) outlives the timeout."""
        return Verdict.STALLED if self.stalled() else Verdict.PROGRESSING


def watch_worker(
    watchdog: WorkerWatchdog,
    stop: threading.Event,
    *,
    exit_process: Callable[[int], object] = os._exit,
    interval_seconds: float = 5.0,
    record_step: Callable[[str, datetime], None] | None = None,
    record_after_seconds: float = _READINESS_BEAT_SECONDS,
    spawn: Callable[[Callable[[], None]], object] | None = None,
) -> None:
    """Restart the worker (exit 70) when one step hangs past the timeout.

    A step that outlives ``record_after_seconds`` is also published, so the
    platform view names the slow step while the readiness heartbeat waits. The
    verdict is decided first and the write runs on its own daemon thread, so a
    blocked database never delays the restart. A failed write is retried on
    the next poll; one write is in flight at a time.
    """
    recorded = [0]
    inflight: list[threading.Thread] = []

    def start(work: Callable[[], None]) -> object:
        thread = threading.Thread(target=work, name="worker-step-record", daemon=True)
        inflight.append(thread)
        thread.start()
        return thread

    launch = spawn or start
    while not stop.wait(interval_seconds):
        stalled = watchdog.verdict() is Verdict.STALLED
        slow = watchdog.running_longer_than(record_after_seconds)
        inflight[:] = [thread for thread in inflight if thread.is_alive()]
        if (
            record_step is not None
            and not stalled
            and slow is not None
            and slow[0] != recorded[0]
            and not inflight
        ):
            launch(_publisher(record_step, recorded, *slow))
        if not stalled:
            continue
        log_event(
            _LOGGER,
            "worker.step_hung"
            if watchdog.step is not None
            else "worker.watchdog_stalled",
            service="control-worker",
            step=watchdog.step,
            timeout_seconds=watchdog.timeout_seconds,
            action="exiting non-zero for container restart",
        )
        exit_process(70)
        return


def _publisher(
    record_step: Callable[[str, datetime], None],
    recorded: list[int],
    serial: int,
    step: str,
    started_at: datetime,
) -> Callable[[], None]:
    def publish() -> None:
        try:
            record_step(step, started_at)
        except Exception as error:  # noqa: BLE001 - reporting must not stop the watchdog
            log_event(
                _LOGGER,
                "worker.step_record_failed",
                service="control-worker",
                step=step,
                error=type(error).__name__,
            )
            return
        recorded[0] = serial

    return publish


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


class WorkerHeartbeatRecorder:
    """Persist readiness for the currently running scheduler process."""

    def __init__(
        self,
        sessions: Any,
        *,
        process_instance_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        if _PROCESS_INSTANCE.fullmatch(process_instance_id) is None:
            raise ValueError("worker process instance ID is invalid")
        self._sessions = sessions
        self._process_instance_id = process_instance_id
        self._clock = clock
        self._register_process_start()

    def _register_process_start(self) -> None:

        identity = build_identity.packaged_runtime_identity()

        with self._sessions.begin() as session:
            heartbeat, created = insert_identity(
                session,
                ControlProcessHeartbeat(
                    process_kind="worker",
                    process_instance_id=self._process_instance_id,
                    source_sha=identity.source_sha,
                    worker_contract_sha256=identity.worker_contract_sha256,
                    loop_sequence=0,
                    completed_at=None,
                ),
                key=("process_kind", "process_instance_id"),
            )
            if created:
                return
            heartbeat.process_instance_id = self._process_instance_id
            heartbeat.source_sha = identity.source_sha
            heartbeat.worker_contract_sha256 = identity.worker_contract_sha256
            heartbeat.loop_sequence = 0
            heartbeat.completed_at = None
            heartbeat.running_step = None
            heartbeat.step_started_at = None

    def record_running_step(self, step: str, started_at: datetime) -> None:
        """Publish the step that is still running, so a slow one is named."""

        with self._sessions.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                # Reporting never waits long on a lock or a silent peer.
                for setting in ("statement_timeout", "lock_timeout"):
                    session.execute(
                        text("SELECT set_config(:name, '2000ms', true)"),
                        {"name": setting},
                    )
            session.execute(
                update(ControlProcessHeartbeat)
                .where(ControlProcessHeartbeat.process_kind == "worker")
                .where(
                    ControlProcessHeartbeat.process_instance_id
                    == self._process_instance_id
                )
                .values(running_step=step[:256], step_started_at=started_at)
            )

    def completed_loop(self) -> None:

        completed_at = self._clock()
        if (
            not isinstance(completed_at, datetime)
            or completed_at.tzinfo is None
            or completed_at.utcoffset() is None
        ):
            raise ValueError("worker heartbeat clock must be timezone-aware")
        with self._sessions.begin() as session:
            heartbeat = session.scalar(
                select(ControlProcessHeartbeat)
                .where(ControlProcessHeartbeat.process_kind == "worker")
                .where(
                    ControlProcessHeartbeat.process_instance_id
                    == self._process_instance_id
                )
                .with_for_update()
            )
            missing = heartbeat is None
            if heartbeat is not None:
                heartbeat.loop_sequence += 1
                heartbeat.completed_at = completed_at.astimezone(UTC)
                heartbeat.running_step = None
                heartbeat.step_started_at = None
        if missing:
            # A lost readiness projection is repaired after releasing SQL.
            # The next completed loop supplies fresh observation evidence.
            self._register_process_start()
