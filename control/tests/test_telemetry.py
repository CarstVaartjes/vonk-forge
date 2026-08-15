from __future__ import annotations

import math
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_control.models import (
    AgentNode,
    Base,
    NodeTelemetryLatest,
    NodeTelemetrySample,
)
from vonk_control.telemetry import (
    TelemetryDetailsInput,
    TelemetryRepository,
    TelemetrySampleInput,
)

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
BOOT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
BOOT_B = uuid.UUID("00000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
START = NOW - timedelta(minutes=5)
MAX_CAPACITY_BYTES = 16 * 1024**4
MAX_NETWORK_BYTES_PER_SECOND = 1_000_000_000_000_000


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def telemetry(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'telemetry.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            (
                AgentNode(node_id=NODE_A, state="active", capabilities=[]),
                AgentNode(node_id=NODE_B, state="active", capabilities=[]),
            )
        )
    clock = Clock()
    return TelemetryRepository(sessions, clock=clock), sessions, engine, clock


def sample(
    *,
    sequence: int,
    observed_at: datetime | None = None,
    boot_id: uuid.UUID = BOOT_A,
) -> TelemetrySampleInput:
    return TelemetrySampleInput(
        boot_id=boot_id,
        sequence=sequence,
        observed_at=observed_at or START + timedelta(seconds=sequence),
        cpu_utilization_percent=12.5,
        load_average_1m=1.25,
        memory_total_bytes=128_000_000_000,
        memory_available_bytes=64_000_000_000,
        disk_total_bytes=1_000_000_000_000,
        disk_free_bytes=750_000_000_000,
        gpu_utilization_percent=25.0,
        gpu_memory_total_bytes=128_000_000_000,
        gpu_memory_free_bytes=63_000_000_000,
        temperature_c=41.5,
        power_watts=17.25,
        network_receive_bytes_per_second=1_024.5,
        network_transmit_bytes_per_second=512.25,
        gap_samples=0,
        details=TelemetryDetailsInput(
            accelerator_name="NVIDIA GB10",
            accelerator_performance_state="P0",
        ),
    )


def test_newer_telemetry_replaces_latest_and_replay_does_not(telemetry) -> None:
    repository, _, _, _ = telemetry
    repository.record_batch(NODE_A, (sample(sequence=4), sample(sequence=5)))

    repository.record_batch(NODE_A, (sample(sequence=4),))

    assert repository.latest((NODE_A,))[NODE_A].sequence == 5
    assert [
        item.sequence for item in repository.history(NODE_A, START, NOW, 1_500)
    ] == [4, 5]


def test_new_boot_resets_sequence_but_only_newer_observation_advances_latest(
    telemetry,
) -> None:
    repository, _, _, _ = telemetry
    repository.record_batch(NODE_A, (sample(sequence=9),))
    repository.record_batch(
        NODE_A,
        (
            sample(
                boot_id=BOOT_B,
                sequence=0,
                observed_at=START + timedelta(seconds=10),
            ),
        ),
    )
    repository.record_batch(
        NODE_A,
        (
            sample(
                boot_id=uuid.UUID("00000000-0000-4000-8000-000000000003"),
                sequence=0,
                observed_at=START + timedelta(seconds=8),
            ),
        ),
    )

    latest = repository.latest((NODE_A, NODE_B))
    assert latest[NODE_A].boot_id == BOOT_B
    assert latest[NODE_A].sequence == 0
    assert NODE_B not in latest
    assert [
        item.observed_at for item in repository.history(NODE_A, START, NOW, 1_500)
    ] == [
        START + timedelta(seconds=8),
        START + timedelta(seconds=9),
        START + timedelta(seconds=10),
    ]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sequence": -1}, "sequence"),
        ({"sequence": 2**63}, "sequence"),
        ({"cpu_utilization_percent": math.nan}, "CPU utilization"),
        ({"cpu_utilization_percent": 100.01}, "CPU utilization"),
        ({"load_average_1m": -0.01}, "load average"),
        ({"memory_available_bytes": -1}, "memory available"),
        (
            {"memory_available_bytes": 128_000_000_001},
            "memory available cannot exceed total",
        ),
        ({"disk_free_bytes": 1_000_000_000_001}, "disk free cannot exceed total"),
        (
            {"gpu_memory_free_bytes": 128_000_000_001},
            "GPU memory free cannot exceed total",
        ),
        ({"gpu_utilization_percent": math.inf}, "GPU utilization"),
        ({"temperature_c": 300.01}, "temperature"),
        ({"power_watts": -0.01}, "power"),
        ({"network_receive_bytes_per_second": -0.01}, "network receive rate"),
        (
            {"network_receive_bytes_per_second": MAX_NETWORK_BYTES_PER_SECOND + 1},
            "network receive rate",
        ),
        ({"memory_total_bytes": MAX_CAPACITY_BYTES + 1}, "memory total"),
        ({"disk_total_bytes": MAX_CAPACITY_BYTES + 1}, "disk total"),
        ({"gpu_memory_total_bytes": MAX_CAPACITY_BYTES + 1}, "GPU memory total"),
        ({"gap_samples": 2**63}, "gap samples"),
    ],
)
def test_sample_rejects_non_finite_negative_and_out_of_range_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(sample(sequence=1), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"memory_total_bytes": None},
        {"memory_available_bytes": None},
        {"disk_total_bytes": None},
        {"disk_free_bytes": None},
        {"gpu_memory_total_bytes": None},
        {"gpu_memory_free_bytes": None},
    ],
)
def test_capacity_pairs_are_both_known_or_both_unknown(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="must both be present or both be absent"):
        replace(sample(sequence=1), **changes)


def test_details_are_exact_and_bounded() -> None:
    with pytest.raises(ValueError, match="accelerator name"):
        TelemetryDetailsInput(accelerator_name="")
    with pytest.raises(ValueError, match="accelerator name"):
        TelemetryDetailsInput(accelerator_name="x" * 257)
    with pytest.raises(ValueError, match="performance state"):
        TelemetryDetailsInput(accelerator_performance_state="x" * 33)


def test_sample_rejects_nil_boot_id() -> None:
    with pytest.raises(ValueError, match="boot ID"):
        replace(sample(sequence=1), boot_id=uuid.UUID(int=0))


def test_database_rejects_half_present_capacity_pair(telemetry) -> None:
    _, sessions, _, _ = telemetry
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            NodeTelemetrySample(
                node_id=NODE_A,
                boot_id=str(BOOT_A),
                sequence=1,
                observed_at=NOW,
                received_at=NOW,
                memory_total_bytes=1,
                memory_available_bytes=None,
                gap_samples=0,
                details={},
            )
        )


@pytest.mark.parametrize(
    "values",
    [
        {
            "memory_total_bytes": MAX_CAPACITY_BYTES + 1,
            "memory_available_bytes": MAX_CAPACITY_BYTES + 1,
        },
        {
            "disk_total_bytes": MAX_CAPACITY_BYTES + 1,
            "disk_free_bytes": MAX_CAPACITY_BYTES + 1,
        },
        {
            "gpu_memory_total_bytes": MAX_CAPACITY_BYTES + 1,
            "gpu_memory_free_bytes": MAX_CAPACITY_BYTES + 1,
        },
        {"network_receive_bytes_per_second": MAX_NETWORK_BYTES_PER_SECOND + 1},
        {"network_transmit_bytes_per_second": MAX_NETWORK_BYTES_PER_SECOND + 1},
    ],
)
def test_database_rejects_metrics_above_wire_maximums(
    telemetry, values: dict[str, object]
) -> None:
    _, sessions, _, _ = telemetry
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            NodeTelemetrySample(
                node_id=NODE_A,
                boot_id=str(BOOT_A),
                sequence=1,
                observed_at=NOW,
                received_at=NOW,
                gap_samples=0,
                details={},
                **values,
            )
        )


def test_latest_pointer_cannot_reference_a_sample_from_another_node() -> None:
    engine = create_engine("sqlite://")

    def enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    event.listen(engine, "connect", enable_foreign_keys)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            (
                AgentNode(node_id=NODE_A, state="active", capabilities=[]),
                AgentNode(node_id=NODE_B, state="active", capabilities=[]),
            )
        )
        row = NodeTelemetrySample(
            node_id=NODE_A,
            boot_id=str(BOOT_A),
            sequence=1,
            observed_at=NOW,
            received_at=NOW,
            gap_samples=0,
            details={},
        )
        session.add(row)
        session.flush()
        sample_id = row.id

    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(NodeTelemetryLatest(node_id=NODE_B, sample_id=sample_id))


def test_record_batch_locks_node_before_reading_latest_projection(telemetry) -> None:
    repository, sessions, _, _ = telemetry
    observed: list[tuple[type[object], object]] = []

    def capture_statement(state) -> None:
        if not state.is_select:
            return
        for description in state.statement.column_descriptions:
            entity = description.get("entity")
            if entity in {AgentNode, NodeTelemetryLatest}:
                observed.append((entity, state.statement))
                break

    event.listen(sessions.class_, "do_orm_execute", capture_statement)
    try:
        repository.record_batch(NODE_A, (sample(sequence=1),))
    finally:
        event.remove(sessions.class_, "do_orm_execute", capture_statement)

    assert [entity for entity, _ in observed[:2]] == [AgentNode, NodeTelemetryLatest]
    lock_statement = observed[0][1]
    compiled = str(lock_statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF agent_nodes" in compiled


def test_record_batch_rejects_time_window_before_opening_transaction(
    telemetry,
) -> None:
    repository, _, engine, _ = telemetry
    transactions = 0

    def began(_connection) -> None:
        nonlocal transactions
        transactions += 1

    event.listen(engine, "begin", began)
    try:
        with pytest.raises(ValueError, match="accepted window"):
            repository.record_batch(
                NODE_A,
                (
                    sample(
                        sequence=1,
                        observed_at=NOW - timedelta(minutes=5, microseconds=1),
                    ),
                ),
            )
        with pytest.raises(ValueError, match="accepted window"):
            repository.record_batch(
                NODE_A,
                (
                    sample(
                        sequence=1,
                        observed_at=NOW + timedelta(seconds=30, microseconds=1),
                    ),
                ),
            )
    finally:
        event.remove(engine, "begin", began)
    assert transactions == 0


def test_record_batch_rejects_more_than_sixteen_or_duplicate_samples(
    telemetry,
) -> None:
    repository, _, _, _ = telemetry
    with pytest.raises(ValueError, match="between 1 and 16"):
        repository.record_batch(NODE_A, tuple(sample(sequence=i) for i in range(17)))
    with pytest.raises(ValueError, match="duplicated"):
        repository.record_batch(NODE_A, (sample(sequence=1), sample(sequence=1)))


def test_record_batch_rejects_regressing_sequence_or_observation_time(
    telemetry,
) -> None:
    repository, _, _, _ = telemetry
    with pytest.raises(ValueError, match="observation times must increase"):
        repository.record_batch(
            NODE_A,
            (
                sample(sequence=1, observed_at=START + timedelta(seconds=2)),
                sample(sequence=2, observed_at=START + timedelta(seconds=1)),
            ),
        )
    with pytest.raises(ValueError, match="sequences must increase"):
        repository.record_batch(
            NODE_A,
            (
                sample(sequence=2, observed_at=START + timedelta(seconds=1)),
                sample(sequence=1, observed_at=START + timedelta(seconds=2)),
            ),
        )

    repository.record_batch(NODE_A, (sample(sequence=5),))
    with pytest.raises(ValueError, match="regresses stored sequence"):
        repository.record_batch(NODE_A, (sample(sequence=3),))


def test_conflicting_replay_is_rejected(telemetry) -> None:
    repository, _, _, _ = telemetry
    repository.record_batch(NODE_A, (sample(sequence=4),))

    with pytest.raises(ValueError, match="conflicts with stored sample"):
        repository.record_batch(
            NODE_A,
            (replace(sample(sequence=4), cpu_utilization_percent=99.0),),
        )


def test_history_rejects_invalid_or_unbounded_windows(telemetry) -> None:
    repository, _, _, _ = telemetry
    with pytest.raises(ValueError, match="maximum points"):
        repository.history(NODE_A, START, NOW, 1_501)
    with pytest.raises(ValueError, match="history window"):
        repository.history(NODE_A, NOW, START, 1_500)
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.history(NODE_A, START.replace(tzinfo=None), NOW, 1_500)


def test_history_and_latest_writes_are_one_transaction(telemetry) -> None:
    repository, sessions, _, _ = telemetry

    def fail_latest(_session, _flush_context, _instances) -> None:
        if any(isinstance(value, NodeTelemetryLatest) for value in _session.new):
            raise RuntimeError("latest projection failed")

    event.listen(sessions.class_, "before_flush", fail_latest)
    try:
        with pytest.raises(RuntimeError, match="latest projection failed"):
            repository.record_batch(NODE_A, (sample(sequence=1),))
    finally:
        event.remove(sessions.class_, "before_flush", fail_latest)

    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(NodeTelemetrySample)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(NodeTelemetryLatest)) == 0
        )
