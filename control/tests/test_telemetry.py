from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.models import (
    AgentNode,
    Base,
    NodeEngineSample,
    NodeTelemetryLatest,
    NodeTelemetrySample,
    RecipeRun,
)
from vonk_control.telemetry import (
    ENGINE_LIVE_WINDOW,
    TelemetryRepository,
    TelemetrySample,
)

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
BOOT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
BOOT_B = uuid.UUID("00000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
START = NOW - timedelta(minutes=5)
MAX_CAPACITY_BYTES = 16 * 1024**4


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
                AgentNode(node_id=NODE_A, state="active"),
                AgentNode(node_id=NODE_B, state="active"),
            )
        )
    clock = Clock()
    return TelemetryRepository(sessions, clock=clock), sessions, engine, clock


def sample(
    *,
    sequence: int = 1,
    observed_at: datetime | None = None,
    boot_id: uuid.UUID = BOOT_A,
) -> TelemetrySample:
    return TelemetrySample.model_validate_json(
        canonical_message(
            {
                "boot_id": str(boot_id),
                "observed_at": observed_at or START + timedelta(seconds=sequence),
                "memory_total_bytes": 128_000_000_000,
                "memory_available_bytes": 64_000_000_000,
                "disk_total_bytes": 1_000_000_000_000,
                "disk_free_bytes": 750_000_000_000,
                "gpu_utilization_percent": 25.0,
                "gpu_memory_total_bytes": 128_000_000_000,
                "gpu_memory_free_bytes": 63_000_000_000,
            }
        )
    )


def _stored(sessions) -> list[datetime]:
    with sessions() as session:
        return [
            value.replace(tzinfo=UTC)
            for value in session.scalars(
                select(NodeTelemetrySample.observed_at).order_by(
                    NodeTelemetrySample.observed_at
                )
            )
        ]


def test_newer_telemetry_replaces_latest_and_replay_does_not(telemetry) -> None:
    repository, sessions, _, _ = telemetry
    repository.record_batch(NODE_A, (sample(sequence=4), sample(sequence=5)))

    repository.record_batch(NODE_A, (sample(sequence=4),))

    assert repository.latest((NODE_A,))[NODE_A].observed_at == START + timedelta(
        seconds=5
    )
    assert [observed_at for observed_at in _stored(sessions)] == [
        START + timedelta(seconds=4),
        START + timedelta(seconds=5),
    ]


def test_new_boot_only_newer_observation_advances_latest(
    telemetry,
) -> None:
    repository, sessions, _, _ = telemetry
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
    assert latest[NODE_A].boot_id == str(BOOT_B)
    assert latest[NODE_A].observed_at == START + timedelta(seconds=10)
    assert NODE_B not in latest
    assert [observed_at for observed_at in _stored(sessions)] == [
        START + timedelta(seconds=8),
        START + timedelta(seconds=9),
        START + timedelta(seconds=10),
    ]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"gpu_utilization_percent": math.nan}, "GPU utilization"),
        ({"gpu_utilization_percent": 100.01}, "GPU utilization"),
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
        ({"memory_total_bytes": MAX_CAPACITY_BYTES + 1}, "memory total"),
        ({"disk_total_bytes": MAX_CAPACITY_BYTES + 1}, "disk total"),
        ({"gpu_memory_total_bytes": MAX_CAPACITY_BYTES + 1}, "GPU memory total"),
    ],
)
def test_sample_rejects_non_finite_negative_and_out_of_range_values(
    changes: dict[str, object], message: str
) -> None:
    original = sample(sequence=1)
    with pytest.raises(Exception):  # noqa: B017 -- invalid sample produces no value; valid sample remains usable
        type(original).model_validate({**original.model_dump(), **changes})
    assert type(original).model_validate({**original.model_dump()}) == original


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
    original = sample(sequence=1)
    with pytest.raises(Exception):  # noqa: B017 -- invalid sample produces no value; valid sample remains usable
        type(original).model_validate({**original.model_dump(), **changes})
    assert type(original).model_validate({**original.model_dump()}) == original


def test_sample_rejects_nil_boot_id() -> None:
    with pytest.raises(Exception):  # noqa: B017 -- invalid identity produces no value; valid sample remains usable
        type(sample(sequence=1)).model_validate(
            {**sample(sequence=1).model_dump(), "boot_id": uuid.UUID(int=0)}
        )
    assert sample(sequence=1).boot_id == str(BOOT_A)


def test_resource_store_rejects_invalid_measurement_without_partial_sample(
    telemetry,
) -> None:
    repository, sessions, _, _ = telemetry
    from vonk_agent_protocol.telemetry import HostResourceSample
    from vonk_control.models import NodeInventoryResourceSnapshot
    from vonk_control.stored_json import write_guard_mode

    repository.record_batch(NODE_A, (sample(sequence=1),))
    with sessions() as session:
        resource = session.scalar(select(NodeInventoryResourceSnapshot))
        assert resource is not None
        identifier = resource.id
    # Controller initialization selects warning mode for production writes;
    # this rejection assertion owns its strict scope instead of test order.
    with (
        write_guard_mode(strict=True),
        pytest.raises(ValueError),
        sessions.begin() as session,
    ):
        resource = session.get(NodeInventoryResourceSnapshot, identifier)
        assert resource is not None
        resource.host_resources = HostResourceSample.model_construct(
            memory_total_bytes=1,
            memory_available_bytes=2,
            disk_total_bytes=None,
            disk_free_bytes=None,
        )
    assert repository.latest((NODE_A,))[NODE_A].memory_available_bytes == 64_000_000_000


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
                AgentNode(node_id=NODE_A, state="active"),
                AgentNode(node_id=NODE_B, state="active"),
            )
        )
        row = NodeTelemetrySample(
            node_id=NODE_A,
            boot_id=str(BOOT_A),
            observed_at=NOW,
            received_at=NOW,
        )
        session.add(row)
        session.flush()
        sample_id = row.id

    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(NodeTelemetryLatest(node_id=NODE_B, sample_id=sample_id))


def test_record_batch_does_not_reread_what_it_wrote(telemetry) -> None:
    repository, _sessions, engine, _ = telemetry
    statements: list[str] = []

    def capture(_conn, _cursor, statement, *_rest) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        views = repository.record_batch(
            NODE_A, tuple(sample(sequence=n) for n in range(1, 5))
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert [view.observed_at for view in views] == [
        START + timedelta(seconds=n) for n in range(1, 5)
    ]
    reads = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "node_telemetry_samples" in statement
    ]
    # One head read for the boot; no sample is read back after its insert.
    assert len(reads) == 1
    inserts = [
        s for s in statements if s.startswith("INSERT INTO node_telemetry_samples")
    ]
    assert len(inserts) == 4


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
        with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
            repository.record_batch(
                NODE_A,
                (
                    sample(
                        observed_at=NOW - timedelta(minutes=5, microseconds=1),
                    ),
                ),
            )
        with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
            repository.record_batch(
                NODE_A,
                (
                    sample(
                        observed_at=NOW + timedelta(seconds=30, microseconds=1),
                    ),
                ),
            )
    finally:
        event.remove(engine, "begin", began)
    assert transactions == 0
    repository.record_batch(NODE_A, (sample(sequence=1),))
    assert repository.latest((NODE_A,))[NODE_A].observed_at == START + timedelta(
        seconds=1
    )


def test_record_batch_rejects_more_than_sixteen_or_duplicate_samples(
    telemetry,
) -> None:
    repository, sessions, _, _ = telemetry
    with pytest.raises(Exception):  # noqa: B017 -- invalid batch cannot write; fresh valid batch succeeds
        repository.record_batch(NODE_A, tuple(sample(sequence=i) for i in range(17)))
    with pytest.raises(Exception):  # noqa: B017 -- duplicate batch cannot write; fresh valid batch succeeds
        repository.record_batch(NODE_A, (sample(sequence=1), sample(sequence=1)))
    assert _stored(sessions) == []
    repository.record_batch(NODE_A, (sample(sequence=1),))
    assert _stored(sessions) == [START + timedelta(seconds=1)]


def test_record_batch_rejects_regressing_observation_time(
    telemetry,
) -> None:
    repository, _, _, _ = telemetry
    with pytest.raises(ValueError):
        repository.record_batch(
            NODE_A,
            (
                sample(sequence=1, observed_at=START + timedelta(seconds=2)),
                sample(sequence=2, observed_at=START + timedelta(seconds=1)),
            ),
        )
    repository.record_batch(NODE_A, (sample(sequence=5),))
    with pytest.raises(ValueError):
        repository.record_batch(
            NODE_A, (sample(sequence=3, observed_at=START + timedelta(seconds=3)),)
        )


def test_conflicting_replay_is_rejected(telemetry) -> None:
    repository, sessions, _, _ = telemetry
    repository.record_batch(NODE_A, (sample(sequence=4),))

    with pytest.raises(ValueError):
        repository.record_batch(
            NODE_A,
            (
                type(sample(sequence=4)).model_validate(
                    {**sample(sequence=4).model_dump(), "gpu_utilization_percent": 99.0}
                ),
            ),
        )

    assert _stored(sessions) == [START + timedelta(seconds=4)]
    assert repository.latest((NODE_A,))[NODE_A].gpu_utilization_percent == 25.0
    repository.record_batch(NODE_A, (sample(sequence=5),))
    assert _stored(sessions)[-1] == START + timedelta(seconds=5)


def test_sample_and_latest_writes_are_one_transaction(telemetry) -> None:
    repository, sessions, _, _ = telemetry

    # The latest pointer is a Core upsert, the last write of a sample.
    engine = sessions.kw["bind"]

    def fail_latest(_conn, _cursor, statement, *_rest) -> None:
        if statement.startswith("INSERT INTO node_telemetry_latest"):
            raise RuntimeError("latest projection failed")

    event.listen(engine, "before_cursor_execute", fail_latest)
    try:
        with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
            repository.record_batch(NODE_A, (sample(sequence=1),))
    finally:
        event.remove(engine, "before_cursor_execute", fail_latest)

    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(NodeTelemetrySample)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(NodeTelemetryLatest)) == 0
        )

    repository.record_batch(NODE_A, (sample(sequence=1),))
    assert _stored(sessions) == [START + timedelta(seconds=1)]
    assert repository.latest((NODE_A,))[NODE_A].observed_at == START + timedelta(
        seconds=1
    )


def test_gpu_failure_round_trips_and_next_sample_recovers(telemetry):
    # Catches losing the typed collection reason between wire, store and fleet.
    from vonk_agent_protocol.telemetry import GpuUnavailableReason, TelemetrySample
    from vonk_control.fleet_projection import telemetry_point

    repository, _, _, _ = telemetry
    wire = TelemetrySample(
        boot_id=str(BOOT_A),
        observed_at=sample().observed_at,
        memory_total_bytes=128_000_000_000,
        memory_available_bytes=64_000_000_000,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
        gpu_unavailable_reason=GpuUnavailableReason.COMMAND_FAILED,
    )
    decoded = TelemetrySample.model_validate_json(wire.model_dump_json())
    failed = type(sample()).model_validate(
        {
            **sample().model_dump(),
            "gpu_utilization_percent": None,
            "gpu_memory_total_bytes": None,
            "gpu_memory_free_bytes": None,
            "gpu_unavailable_reason": decoded.gpu_unavailable_reason,
        }
    )
    repository.record_batch(NODE_A, (failed,))
    point = telemetry_point(repository.latest((NODE_A,))[NODE_A])
    assert point.gpu_unavailable_reason is GpuUnavailableReason.COMMAND_FAILED
    assert point.gpu_utilization_percent is None
    recovered = type(sample(sequence=2)).model_validate(
        {
            **sample(sequence=2).model_dump(),
            "gpu_memory_total_bytes": None,
            "gpu_memory_free_bytes": None,
            "gpu_temperature_c": 61,
        }
    )
    repository.record_batch(NODE_A, (recovered,))
    point = telemetry_point(repository.latest((NODE_A,))[NODE_A])
    assert point.gpu_unavailable_reason is None
    assert point.gpu_utilization_percent == 25.0
    assert point.gpu_temperature_c == 61
    assert point.gpu_memory_total_bytes is None
    assert point.memory_total_bytes == recovered.memory_total_bytes


def _vector() -> dict:
    return json.loads(
        (
            files("vonk_agent_protocol") / "vectors" / "telemetry-sample-v1.json"
        ).read_text()
    )


def test_gpu_power_uptime_and_process_memory_are_stored_and_served(telemetry) -> None:
    from vonk_control.fleet_projection import telemetry_point

    repository, _sessions, _engine, _clock = telemetry
    vector = _vector()
    reported = TelemetrySample.model_validate_json(
        canonical_message(
            {**vector["sample"], "observed_at": START + timedelta(seconds=1)}
        )
    )
    repository.record_batch(NODE_A, (reported,))
    point = telemetry_point(repository.latest((NODE_A,))[NODE_A])
    assert (
        point.gpu_power_watts,
        point.uptime_seconds,
        point.gpu_process_memory_bytes,
        point.system_memory_bytes,
    ) == (9.14, 1804535, 113668784128, vector["system_memory_bytes"])

    # An older agent omits the readings: stored NULL, absent from the wire.
    older = sample(sequence=2)
    repository.record_batch(NODE_A, (older,))
    served = json.loads(
        canonical_message(telemetry_point(repository.latest((NODE_A,))[NODE_A]))
    )
    assert not {
        "gpu_power_watts",
        "uptime_seconds",
        "gpu_process_memory_bytes",
        "system_memory_bytes",
    } & set(served)


def _vector_v2() -> dict:
    return json.loads(
        (
            files("vonk_agent_protocol") / "vectors" / "telemetry-sample-v2.json"
        ).read_text()
    )


def test_host_readings_are_stored_and_served_and_engines_are_not_on_the_point(
    telemetry,
) -> None:
    from vonk_control.fleet_projection import telemetry_point

    repository, sessions, _engine, _clock = telemetry
    vector = _vector_v2()["sample"]
    reported = TelemetrySample.model_validate_json(
        canonical_message({**vector, "observed_at": START + timedelta(seconds=1)})
    )
    repository.record_batch(NODE_A, (reported,))
    served = json.loads(
        canonical_message(telemetry_point(repository.latest((NODE_A,))[NODE_A]))
    )
    new_readings = {
        k: v
        for k, v in vector.items()
        if k not in _vector()["sample"] and k != "engines"
    }
    assert new_readings
    assert {k: served[k] for k in new_readings} == new_readings
    assert "engines" not in served
    with sessions() as session:
        stored = session.scalars(select(NodeEngineSample)).all()
    assert {row.run_id: row.generation_tokens for row in stored} == {
        engine["run_id"]: engine.get("generation_tokens")
        for engine in vector["engines"]
    }

    # An older agent omits every 4.4 reading: NULL columns, absent on the wire.
    repository.record_batch(NODE_A, (sample(sequence=2),))
    older = json.loads(
        canonical_message(telemetry_point(repository.latest((NODE_A,))[NODE_A]))
    )
    assert not {"fan_rpm_max", "oom_kills", "cpu_busy_seconds"} & set(older)

    # A replay of the first sample stays idempotent and adds no engine rows.
    repository.record_batch(NODE_A, (reported,))
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(NodeEngineSample)) == 2


def _run(session, run_id: str, alias: str, route_state: str) -> None:
    session.add(
        RecipeRun(
            id=run_id,
            installation_id=str(uuid.uuid4()),
            mapping_id=str(uuid.uuid4()),
            mapping_generation=1,
            run_generation=1,
            alias=alias,
            plan_digest="a" * 64,
            plan={},
            state="running",
            route_state=route_state,
            actor="admin",
            created_at=NOW,
            updated_at=NOW,
        )
    )


def test_live_engine_readings_are_the_newest_per_run_with_a_published_alias(
    telemetry,
) -> None:
    repository, sessions, _engine, _clock = telemetry
    first, second = (e["run_id"] for e in _vector_v2()["sample"]["engines"])
    unpublished = "44444444-4444-4444-8444-444444444444"
    with sessions.begin() as session:
        _run(session, first, "glm-5-flash", "published")
        _run(session, second, "other-model", "published")
        _run(session, unpublished, "withdrawn-model", "withdrawn")

    def reported(second_offset: int, tokens: int, *runs: str) -> TelemetrySample:
        return TelemetrySample.model_validate_json(
            canonical_message(
                {
                    **sample(sequence=second_offset).model_dump(mode="json"),
                    "engines": [
                        {"run_id": run, "format": "vllm", "generation_tokens": tokens}
                        for run in runs
                    ],
                }
            )
        )

    repository.record_batch(NODE_A, (reported(1, 10, first, second, unpublished),))
    repository.record_batch(NODE_A, (reported(10, 20, first, unpublished),))

    # `second` answered slowly: it is missing from the newest sample but its
    # earlier reading still stands inside the live window.
    live = repository.live_engine_readings(START + timedelta(seconds=11))
    assert {(item.alias, item.reading.generation_tokens) for item in live} == {
        ("glm-5-flash", 20),
        ("other-model", 10),
    }

    # Past the window nothing stands for it.
    past = repository.live_engine_readings(
        START + timedelta(seconds=10) + ENGINE_LIVE_WINDOW + timedelta(seconds=1)
    )
    assert past == []
