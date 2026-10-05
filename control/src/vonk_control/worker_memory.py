"""Always-on worker memory self-report with on-demand allocation tracing.

The worker publishes no port, so a daemon sampler writes one small typed
document to the shared state volume and the API exports it through
``/metrics``. Sampling reads ``/proc`` and the cgroup files only. Python
allocation tracing is expensive, so it is off until resident memory crosses a
threshold (or an operator drops a request file), runs for a short window,
publishes the code locations that gained the most heap, and stops.

Reading the report: ``rss`` far above the cgroup ``anon`` bytes is not heap;
``file`` or ``shmem`` (a tmpfs ``/tmp``) points at page cache or temporary
files; ``rss`` far above the traced Python bytes points at allocator
fragmentation or native memory rather than a retained Python object.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import tracemalloc
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .logging import log_event
from .worker_memory_contract import (
    MAX_LOCATION_LENGTH,
    MAX_TOP_ALLOCATIONS,
    WorkerMemoryCgroupKind,
    WorkerMemoryCgroupSize,
    WorkerMemoryComponent,
    WorkerMemoryComponentSize,
    WorkerMemoryReport,
    WorkerTopAllocation,
    WorkerTraceReport,
    WorkerTraceState,
)

_LOGGER = logging.getLogger("vonk-control-worker")
_REPORT_NAME = "worker-memory.json"
_TRACE_REQUEST_NAME = "worker-memory.trace-request"
_MAX_REPORT_BYTES = 64 * 1024
_CGROUP_KEYS = {
    "anon": WorkerMemoryCgroupKind.ANON,
    "file": WorkerMemoryCgroupKind.FILE,
    "shmem": WorkerMemoryCgroupKind.SHMEM,
    "kernel": WorkerMemoryCgroupKind.KERNEL,
}


def worker_memory_report_path(state_path: Path) -> Path:
    return state_path / "diagnostics" / _REPORT_NAME


def worker_memory_trace_request_path(state_path: Path) -> Path:
    return state_path / "diagnostics" / _TRACE_REQUEST_NAME


@dataclass(frozen=True, slots=True)
class ProcessReading:
    rss_bytes: int
    peak_rss_bytes: int | None
    children_rss_bytes: int | None
    threads: int
    cgroup: tuple[WorkerMemoryCgroupSize, ...]


class ProcessProbe(Protocol):
    def read(self) -> ProcessReading: ...


class LinuxProcessProbe:
    """Read this process, its children and its cgroup from procfs."""

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        pid: int | None = None,
        page_size: int | None = None,
    ) -> None:
        self._proc = proc_root
        self._cgroup = cgroup_root
        self._pid = os.getpid() if pid is None else pid
        self._page_size = page_size or os.sysconf("SC_PAGE_SIZE")

    def read(self) -> ProcessReading:
        own = self._proc / str(self._pid)
        status = self._status(own / "status")
        return ProcessReading(
            rss_bytes=self._rss(own / "statm"),
            peak_rss_bytes=status.get("VmHWM"),
            children_rss_bytes=self._children_rss(),
            threads=status.get("Threads", threading.active_count()),
            cgroup=self._cgroup_sizes(),
        )

    def _rss(self, statm: Path) -> int:
        return int(statm.read_text().split()[1]) * self._page_size

    @staticmethod
    def _status(path: Path) -> dict[str, int]:
        found: dict[str, int] = {}
        try:
            for line in path.read_text().splitlines():
                name, _, value = line.partition(":")
                if name in ("VmHWM", "Threads"):
                    number = int(value.split()[0])
                    found[name] = number * 1024 if name == "VmHWM" else number
        except (OSError, ValueError, IndexError):
            pass
        return found

    def _children_rss(self) -> int | None:
        total = 0
        try:
            entries = list(self._proc.iterdir())
        except OSError:
            return None
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == self._pid:
                continue
            try:
                stat = (entry / "stat").read_text()
                parent = int(stat[stat.rindex(")") + 2 :].split()[1])
                if parent != self._pid:
                    continue
                total += int((entry / "statm").read_text().split()[1]) * self._page_size
            except (OSError, ValueError, IndexError):
                continue  # the child exited between listing and reading
        return total

    def _cgroup_sizes(self) -> tuple[WorkerMemoryCgroupSize, ...]:
        sizes: list[WorkerMemoryCgroupSize] = []
        try:
            sizes.append(
                WorkerMemoryCgroupSize(
                    kind=WorkerMemoryCgroupKind.CURRENT,
                    bytes=int((self._cgroup / "memory.current").read_text()),
                )
            )
            for line in (self._cgroup / "memory.stat").read_text().splitlines():
                key, _, value = line.partition(" ")
                if key in _CGROUP_KEYS:
                    sizes.append(
                        WorkerMemoryCgroupSize(kind=_CGROUP_KEYS[key], bytes=int(value))
                    )
        except (OSError, ValueError):
            pass  # no cgroup v2 here; the process figures still stand
        return tuple(sizes)


def _location(frame: tracemalloc.Frame) -> str:
    """A code location without the install prefix; paths hold no user content."""

    name = frame.filename.replace("\\", "/")
    packages = name.rfind("/site-packages/")
    if packages >= 0:
        name = name[packages + len("/site-packages/") :]
    else:
        own = name.rfind("/vonk_control/")
        if own >= 0:
            name = name[own + 1 :]
    return f"{name}:{frame.lineno}"[-MAX_LOCATION_LENGTH:]


class WorkerMemoryMonitor:
    """Sample, trace on demand and persist the worker's memory report."""

    def __init__(
        self,
        *,
        report_path: Path,
        trace_request_path: Path | None,
        probe: ProcessProbe,
        components: Callable[[], Mapping[WorkerMemoryComponent, int]],
        clock: Callable[[], datetime],
        trace_rss_bytes: int,
        trace_rearm_bytes: int,
        trace_window_seconds: float,
        trace_frames: int,
    ) -> None:
        self._report_path = report_path
        self._trace_request_path = trace_request_path
        self._probe = probe
        self._components = components
        self._clock = clock
        self._trace_rss_bytes = trace_rss_bytes
        self._trace_rearm_bytes = trace_rearm_bytes
        self._trace_window_seconds = trace_window_seconds
        self._trace_frames = trace_frames
        self._state = WorkerTraceState.OFF
        self._started_at: datetime | None = None
        self._started_rss: int | None = None
        self._baseline: tracemalloc.Snapshot | None = None
        self._captured = WorkerTraceReport(state=WorkerTraceState.OFF)
        self._rearm_floor = trace_rss_bytes

    def sample(self) -> WorkerMemoryReport:
        """Take one reading, advance tracing, publish; never raises for tracing."""

        reading = self._probe.read()
        now = self._clock()
        self._advance_trace(reading.rss_bytes, now)
        report = WorkerMemoryReport(
            schema_version=1,
            recorded_at=now.astimezone(UTC),
            rss_bytes=reading.rss_bytes,
            peak_rss_bytes=reading.peak_rss_bytes,
            children_rss_bytes=reading.children_rss_bytes,
            threads=reading.threads,
            python_allocated_blocks=sys.getallocatedblocks(),
            cgroup=reading.cgroup,
            components=tuple(
                WorkerMemoryComponentSize(component=name, entries=max(0, int(size)))
                for name, size in sorted(self._components().items())
            ),
            trace=self._trace_report(),
        )
        self._publish(report)
        return report

    def close(self) -> None:
        """Stop tracing so shutdown never leaves the allocator instrumented."""

        self._stop_trace()

    # Tracing -------------------------------------------------------------

    def _trace_report(self) -> WorkerTraceReport:
        if self._state is WorkerTraceState.SAMPLING:
            traced, peak = tracemalloc.get_traced_memory()
            return WorkerTraceReport(
                state=WorkerTraceState.SAMPLING,
                started_at_rss_bytes=self._started_rss,
                traced_bytes=traced,
                peak_traced_bytes=peak,
            )
        return self._captured

    def _requested(self) -> bool:
        path = self._trace_request_path
        if path is None:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True

    def _advance_trace(self, rss_bytes: int, now: datetime) -> None:
        try:
            if self._state is WorkerTraceState.SAMPLING:
                assert self._started_at is not None
                if (now - self._started_at).total_seconds() >= (
                    self._trace_window_seconds
                ):
                    self._capture(rss_bytes, now)
                return
            if rss_bytes < self._trace_rss_bytes:
                self._rearm_floor = self._trace_rss_bytes
            if tracemalloc.is_tracing():
                return  # tracing owned by someone else (PYTHONTRACEMALLOC)
            if self._requested() or rss_bytes >= self._rearm_floor:
                self._start_trace(rss_bytes, now)
        except Exception as error:  # noqa: BLE001 - diagnostics never break the worker
            self._stop_trace()
            log_event(
                _LOGGER,
                "worker.memory_trace_failed",
                service="control-worker",
                error=type(error).__name__,
            )

    def _start_trace(self, rss_bytes: int, now: datetime) -> None:
        tracemalloc.start(self._trace_frames)
        self._baseline = tracemalloc.take_snapshot()
        self._state = WorkerTraceState.SAMPLING
        self._started_at = now
        self._started_rss = rss_bytes
        log_event(
            _LOGGER,
            "worker.memory_trace_started",
            service="control-worker",
            rss_bytes=rss_bytes,
            window_seconds=self._trace_window_seconds,
        )

    def _capture(self, rss_bytes: int, now: datetime) -> None:
        baseline = self._baseline
        assert baseline is not None
        snapshot = tracemalloc.take_snapshot().filter_traces(
            (tracemalloc.Filter(False, tracemalloc.__file__),)
        )
        traced, peak = tracemalloc.get_traced_memory()
        growth = sorted(
            snapshot.compare_to(baseline, "lineno"),
            key=lambda item: item.size_diff,
            reverse=True,
        )[:MAX_TOP_ALLOCATIONS]
        top = tuple(
            WorkerTopAllocation(
                location=_location(item.traceback[0]),
                size_bytes=item.size,
                count=item.count,
                growth_bytes=item.size_diff,
            )
            for item in growth
        )
        self._captured = WorkerTraceReport(
            state=WorkerTraceState.CAPTURED,
            started_at_rss_bytes=self._started_rss,
            captured_at=now.astimezone(UTC),
            traced_bytes=traced,
            peak_traced_bytes=peak,
            top=top,
        )
        self._stop_trace()
        self._state = WorkerTraceState.OFF
        self._rearm_floor = rss_bytes + self._trace_rearm_bytes
        log_event(
            _LOGGER,
            "worker.memory_trace_captured",
            service="control-worker",
            rss_bytes=rss_bytes,
            traced_bytes=traced,
            peak_traced_bytes=peak,
            top=[
                f"{item.location} +{item.growth_bytes}B x{item.count}" for item in top
            ],
        )

    def _stop_trace(self) -> None:
        if self._state is WorkerTraceState.SAMPLING and tracemalloc.is_tracing():
            tracemalloc.stop()
        self._baseline = None
        self._started_at = None
        if self._state is WorkerTraceState.SAMPLING:
            self._state = WorkerTraceState.OFF

    # Persistence ---------------------------------------------------------

    def _publish(self, report: WorkerMemoryReport) -> None:
        directory = self._report_path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._report_path.with_name(f".{self._report_path.name}.part")
        temporary.write_text(report.model_dump_json(), encoding="utf-8")
        os.replace(temporary, self._report_path)


def read_worker_memory_report(
    path: Path, *, now: datetime, max_age_seconds: float
) -> WorkerMemoryReport | None:
    """The current report, or ``None`` when absent, damaged or stale."""

    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_REPORT_BYTES + 1)
        if len(raw) > _MAX_REPORT_BYTES:
            return None
        report = WorkerMemoryReport.model_validate_json(raw)
    except (OSError, ValueError, ValidationError):
        return None
    if (now - report.recorded_at).total_seconds() > max_age_seconds:
        return None
    return report


def start_worker_memory_sampler(
    monitor: WorkerMemoryMonitor,
    *,
    interval_seconds: float,
    stop: threading.Event,
) -> threading.Thread:
    """Sample on its own thread so a long tick cannot hide the growth."""

    def run() -> None:
        while True:
            try:
                monitor.sample()
            except Exception as error:  # noqa: BLE001 - reporting is best effort
                log_event(
                    _LOGGER,
                    "worker.memory_report_failed",
                    service="control-worker",
                    error=type(error).__name__,
                )
            if stop.wait(interval_seconds):
                monitor.close()
                return

    thread = threading.Thread(target=run, name="worker-memory", daemon=True)
    thread.start()
    return thread
