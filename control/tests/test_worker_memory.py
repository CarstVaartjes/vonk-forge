from __future__ import annotations

import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from vonk_control.metrics import MetricsRegistry
from vonk_control.storage_demands import StorageDemands
from vonk_control.worker import Worker
from vonk_control.worker_memory import (
    LinuxProcessProbe,
    ProcessReading,
    WorkerMemoryMonitor,
    read_worker_memory_report,
    worker_memory_report_path,
    worker_memory_trace_request_path,
)
from vonk_control.worker_memory_contract import (
    WorkerMemoryCgroupKind,
    WorkerMemoryCgroupSize,
    WorkerMemoryComponent,
    WorkerTraceState,
)

START = datetime(2026, 10, 5, 12, tzinfo=UTC)
GIB = 1024**3


class FakeProbe:
    def __init__(self, rss: int) -> None:
        self.rss = rss

    def read(self) -> ProcessReading:
        return ProcessReading(
            rss_bytes=self.rss,
            peak_rss_bytes=self.rss,
            children_rss_bytes=0,
            threads=3,
            cgroup=(WorkerMemoryCgroupSize(kind=WorkerMemoryCgroupKind.ANON, bytes=5),),
        )


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def _no_tracing_left_behind():
    yield
    if tracemalloc.is_tracing():
        tracemalloc.stop()


def _monitor(tmp_path: Path, probe: FakeProbe, clock: Clock) -> WorkerMemoryMonitor:
    return WorkerMemoryMonitor(
        report_path=worker_memory_report_path(tmp_path),
        trace_request_path=worker_memory_trace_request_path(tmp_path),
        probe=probe,
        components=lambda: {WorkerMemoryComponent.STORAGE_DEMANDS: 2},
        clock=clock,
        trace_rss_bytes=2 * GIB,
        trace_rearm_bytes=GIB,
        trace_window_seconds=60,
        trace_frames=5,
    )


def test_sample_is_always_on_and_never_traces_below_the_threshold(tmp_path) -> None:
    clock = Clock()
    monitor = _monitor(tmp_path, FakeProbe(300 * 1024**2), clock)

    report = monitor.sample()

    assert not tracemalloc.is_tracing()
    assert report.trace.state is WorkerTraceState.OFF
    stored = read_worker_memory_report(
        worker_memory_report_path(tmp_path), now=clock.now, max_age_seconds=30
    )
    assert stored == report
    assert stored is not None
    assert [(c.component, c.entries) for c in stored.components] == [
        (WorkerMemoryComponent.STORAGE_DEMANDS, 2)
    ]


def test_tracing_starts_on_growth_names_the_retaining_line_and_stops(tmp_path) -> None:
    clock = Clock()
    probe = FakeProbe(3 * GIB)
    monitor = _monitor(tmp_path, probe, clock)
    retained: list[bytes] = []

    started = monitor.sample()
    assert started.trace.state is WorkerTraceState.SAMPLING
    assert tracemalloc.is_tracing()

    retained.extend(bytes(10_000) for _ in range(50))  # the leak under test
    clock.now += timedelta(seconds=61)
    captured = monitor.sample()

    assert not tracemalloc.is_tracing()
    assert captured.trace.state is WorkerTraceState.CAPTURED
    top = captured.trace.top[0]
    assert "test_worker_memory.py:" in top.location
    assert top.growth_bytes >= 400_000
    # Captured evidence stays published after tracing stopped.
    clock.now += timedelta(seconds=15)
    assert monitor.sample().trace.top == captured.trace.top


def test_tracing_does_not_retrigger_until_rss_grows_by_another_step(tmp_path) -> None:
    clock = Clock()
    probe = FakeProbe(3 * GIB)
    monitor = _monitor(tmp_path, probe, clock)
    monitor.sample()
    clock.now += timedelta(seconds=61)
    monitor.sample()

    clock.now += timedelta(seconds=15)
    monitor.sample()
    assert not tracemalloc.is_tracing()

    probe.rss = 4 * GIB + 1
    clock.now += timedelta(seconds=15)
    assert monitor.sample().trace.state is WorkerTraceState.SAMPLING


def test_operator_request_file_starts_one_trace_and_is_consumed(tmp_path) -> None:
    clock = Clock()
    monitor = _monitor(tmp_path, FakeProbe(200 * 1024**2), clock)
    request = worker_memory_trace_request_path(tmp_path)
    request.parent.mkdir(parents=True)
    request.touch()

    assert monitor.sample().trace.state is WorkerTraceState.SAMPLING
    assert not request.exists()
    monitor.close()
    assert not tracemalloc.is_tracing()


def test_stale_or_damaged_report_is_not_current(tmp_path) -> None:
    clock = Clock()
    path = worker_memory_report_path(tmp_path)
    _monitor(tmp_path, FakeProbe(1), clock).sample()

    assert (
        read_worker_memory_report(
            path, now=clock.now + timedelta(seconds=300), max_age_seconds=120
        )
        is None
    )
    path.write_text("{not json")
    assert read_worker_memory_report(path, now=clock.now, max_age_seconds=120) is None
    assert (
        read_worker_memory_report(
            tmp_path / "missing", now=clock.now, max_age_seconds=120
        )
        is None
    )


def test_linux_probe_separates_heap_cache_and_children(tmp_path) -> None:
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42/statm").write_text("100 25 0 0 0 0 0\n")
    (proc / "42/status").write_text("Name:\tx\nVmHWM:\t200 kB\nThreads:\t7\n")
    (proc / "43").mkdir()
    (proc / "43/stat").write_text("43 (sko (peo)) S 42 1 1 0\n")
    (proc / "43/statm").write_text("10 4 0 0 0 0 0\n")
    (proc / "44").mkdir()
    (proc / "44/stat").write_text("44 (other) S 1 1 1 0\n")
    (proc / "44/statm").write_text("10 9 0 0 0 0 0\n")
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.current").write_text("9000\n")
    (cgroup / "memory.stat").write_text("anon 4000\nfile 3000\nshmem 500\nslab 7\n")

    reading = LinuxProcessProbe(
        proc_root=proc, cgroup_root=cgroup, pid=42, page_size=4096
    ).read()

    assert reading.rss_bytes == 25 * 4096
    assert reading.peak_rss_bytes == 200 * 1024
    assert reading.threads == 7
    assert reading.children_rss_bytes == 4 * 4096
    assert {item.kind: item.bytes for item in reading.cgroup} == {
        WorkerMemoryCgroupKind.CURRENT: 9000,
        WorkerMemoryCgroupKind.ANON: 4000,
        WorkerMemoryCgroupKind.FILE: 3000,
        WorkerMemoryCgroupKind.SHMEM: 500,
    }


def test_worker_collects_footprints_and_skips_a_failing_source() -> None:
    class Broken:
        def memory_footprint(self):
            raise RuntimeError("boom")

    demands = StorageDemands(lambda: START)
    demands.request("nas:model", 5, source="x", reason="r")
    worker = Worker(
        jobs=None,  # type: ignore[arg-type]
        worker_id="w",
        handlers={},
        memory_sources=[Broken(), demands, object()],
    )

    assert worker.memory_footprint() == {WorkerMemoryComponent.STORAGE_DEMANDS: 1}


def test_metrics_export_bounded_worker_gauges_and_trace_growth(tmp_path) -> None:
    clock = Clock()
    probe = FakeProbe(3 * GIB)
    monitor = _monitor(tmp_path, probe, clock)
    monitor.sample()
    keep = [bytes(5000) for _ in range(30)]
    clock.now += timedelta(seconds=61)
    report = monitor.sample()
    registry = MetricsRegistry()

    registry.set_worker_memory(report, clock.now + timedelta(seconds=5))
    text = registry.render()

    assert f"vonk_worker_rss_bytes {3 * GIB}" in text
    assert 'vonk_worker_cgroup_bytes{kind="anon"} 5' in text
    assert 'vonk_worker_component_entries{component="storage_demands"} 2' in text
    assert 'vonk_worker_trace_state{state="captured"} 1' in text
    assert "vonk_worker_memory_report_age_seconds 5" in text
    assert text.count("vonk_worker_trace_growth_bytes{") <= 25
    assert text.endswith("# EOF\n")
    del keep

    registry.set_worker_memory(None)
    assert "vonk_worker_rss_bytes" not in registry.render()


def test_production_worker_reports_every_declared_component(tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_control.jobs import JobService
    from vonk_control.model_cache import ModelCacheService
    from vonk_control.models import Base
    from vonk_control.presence import ManagementAddressPolicy
    from vonk_control.route_runtime import AtomicRouteBundlePublisher
    from vonk_control.worker import assemble_production_worker

    engine = create_engine(f"sqlite:///{tmp_path / 'w.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    jobs = JobService(sessions, clock=Clock())
    model_cache = ModelCacheService(sessions, tmp_path / "models", clock=Clock())

    class AgentJobs:
        def enqueue_in_session(self, *_args, **_kwargs):
            raise AssertionError("not exercised")

        def notify_available(self):
            return None

        def reconcile_orders(self):
            return False

    worker = assemble_production_worker(
        jobs=jobs,
        sessions=sessions,
        agent_jobs=AgentJobs(),
        publisher=AtomicRouteBundlePublisher(tmp_path / "routes"),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=Clock(),
        worker_id="w",
        artifact_job_root=tmp_path / "aj" / "blobs",
        artifact_job_storage_max_bytes=GIB,
        artifact_job_retention_seconds=86400,
        artifact_job_reconcile_interval_seconds=3600,
        artifact_job_reconcile_batch_limit=10,
        model_cache=model_cache,
        model_cache_root=tmp_path / "models",
        agent_artifact_root=tmp_path / "agent-artifacts",
        recipe_image_artifact_root=tmp_path / "agent-artifacts",
    )
    try:
        # A new component without a producer, or a producer outside the enum,
        # fails here instead of silently reporting nothing.
        assert set(worker.memory_footprint()) == set(WorkerMemoryComponent)
    finally:
        worker.close()
        model_cache.close()
