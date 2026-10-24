"""A stuck or large transfer never starves the Controller download pool.

Real threads, real files and a real HTTP client against a mock transport: the
faults are transfers that hang inside the pool and an object lock another
writer holds.
"""

from __future__ import annotations

import fcntl
import itertools
import random
import threading
import time
import uuid
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx2
import pytest
from vonk_agent_protocol import LifecycleState, ModelCacheCode
from vonk_control.bounded_json import text
from vonk_control.model_cache.artifacts import ArtifactSetManifest, ArtifactSpec
from vonk_control.model_cache.views import _BackgroundTransfer

from .test_model_cache_availability_regressions import (
    _artifact,
    _database,
    _drain,
    _service,
    _start,
)


def _serve(gates: dict[str, threading.Event], started: list[str]):
    lock = threading.Lock()

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        with lock:
            started.append(path)
        gate = gates.get(path)
        if gate is not None:
            assert gate.wait(30)
        return httpx2.Response(200, request=request, content=path.encode())

    return handler


def _wait_for(predicate, *, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def test_a_stored_download_completes_on_its_tick_while_hung_transfers_hold_the_pool(
    tmp_path: Path,
) -> None:
    gates = {f"/weights-hog{index}": threading.Event() for index in range(3)}
    started: list[str] = []
    service, client = _service(
        tmp_path, _database(tmp_path), maximum=2, handler=_serve(gates, started)
    )
    stored = [
        _artifact(f"s{index}", f"/weights-s{index}".encode()) for index in range(8)
    ]
    first = _start(service, stored, "00000000-0000-4000-8000-000000002001")
    _drain(service, first.id)
    hog = _start(
        service,
        [
            _artifact(f"hog{index}", f"/weights-hog{index}".encode())
            for index in range(3)
        ],
        "00000000-0000-4000-8000-000000002002",
    )
    service.tick()
    _wait_for(lambda: sum(path in gates for path in started) == 2)

    # The same model again: every object is stored, so its planned bytes are
    # zero. It completes on the tick that claims it, though both transfer
    # slots are held by transfers that do not return.
    again = _start(service, stored, "00000000-0000-4000-8000-000000002003")
    service.tick()

    assert service.get_operation(again.id).state == LifecycleState.SUCCEEDED
    assert service.get_operation(hog.id).state == LifecycleState.RUNNING
    for gate in gates.values():
        gate.set()
    _drain(service, hog.id)
    service.close()
    assert client is not None
    client.close()


def test_freed_slots_go_to_waiting_downloads_before_a_large_one_refills(
    tmp_path: Path,
) -> None:
    gates = {f"/weights-h{index}": threading.Event() for index in range(6)}
    started: list[str] = []
    service, client = _service(
        tmp_path, _database(tmp_path), maximum=2, handler=_serve(gates, started)
    )
    hog = _start(
        service,
        [_artifact(f"h{index}", f"/weights-h{index}".encode()) for index in range(6)],
        "00000000-0000-4000-8000-000000002101",
    )
    service.tick()
    _wait_for(lambda: len(started) == 2)
    waiting = [
        _start(
            service,
            [_artifact(f"n{index}", f"/weights-n{index}".encode())],
            f"00000000-0000-4000-8000-00000000211{index}",
        )
        for index in range(3)
    ]

    # One of the large download's files ends; its other transfers stay hung.
    # The freed slot serves every waiting download in turn instead of the
    # large one taking it back for its next file.
    gates[started[0]].set()
    for operation in waiting:
        _drain(service, operation.id)
        assert service.get_operation(operation.id).state == LifecycleState.SUCCEEDED

    assert service.get_operation(hog.id).state == LifecycleState.RUNNING
    for gate in gates.values():
        gate.set()
    _drain(service, hog.id)
    assert service.get_operation(hog.id).state == LifecycleState.SUCCEEDED
    service.close()
    assert client is not None
    client.close()


def test_an_object_held_by_another_writer_is_a_named_wait_that_resumes(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 10, 24, 10, tzinfo=UTC)]
    started: list[str] = []
    service, client = _service(
        tmp_path,
        _database(tmp_path),
        maximum=2,
        handler=_serve({}, started),
        clock=lambda: now[0],
    )
    artifact = _artifact("held", b"/weights-held")
    sha256 = text(artifact["sha256"])
    assert sha256 is not None
    holder = "00000000-0000-4000-8000-0000000021ff"
    lock_path = service.root / "locks" / sha256
    with lock_path.open("a+b") as held:
        # Another writer (a stale thread, another Controller) owns the object.
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.write(holder.encode("ascii"))
        held.flush()
        operation = _start(service, [artifact], "00000000-0000-4000-8000-000000002201")
        _drain(service, operation.id, return_on_wait=True)

        waited = service.get_operation(operation.id)
        assert waited.state not in {LifecycleState.SUCCEEDED, LifecycleState.FAILED}
        assert waited.failure is not None
        assert waited.failure["code"] == ModelCacheCode.OBJECT_BUSY
        assert holder in str(waited.failure["detail"])
        assert waited.next_attempt_at is not None
        assert started == []
        # The wait re-plans: repeated ticks before the next attempt start
        # nothing and consume no attempt.
        for _ in range(5):
            service.tick()
        assert service.get_operation(operation.id).attempt == waited.attempt
        assert started == []

    now[0] += timedelta(minutes=1)
    _drain(service, operation.id)
    assert service.get_operation(operation.id).state == LifecycleState.SUCCEEDED
    assert started == ["/weights-held"]
    service.close()
    assert client is not None
    client.close()


def test_a_download_that_has_not_started_wins_the_tie_with_a_large_one_between_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {"/weights-hung": threading.Event()}
    started: list[str] = []
    service, client = _service(
        tmp_path, _database(tmp_path), maximum=2, handler=_serve(gates, started)
    )
    hung = _start(
        service,
        [_artifact("hung", b"/weights-hung")],
        "00000000-0000-4000-8000-000000002301",
    )
    service.tick()
    _wait_for(lambda: "/weights-hung" in started)

    def ids(prefix: str):
        counter = itertools.count()
        return lambda: uuid.UUID(f"{prefix}-{next(counter):012x}")

    # The large download's id sorts first, so an id tie-break would hand it
    # the one free slot every time it is back at zero files in flight.
    with monkeypatch.context() as patch:
        patch.setattr(uuid, "uuid4", ids("00000000-0000-4000-8000"))
        large = _start(
            service,
            [
                _artifact(f"x{index}", f"/weights-x{index}".encode())
                for index in range(12)
            ],
            "00000000-0000-4000-8000-000000002302",
        )
    service.tick()
    _wait_for(lambda: service._background_operations[large.id].pending() == 0)
    with monkeypatch.context() as patch:
        patch.setattr(uuid, "uuid4", ids("ffffffff-ffff-4fff-bfff"))
        small = _start(
            service,
            [_artifact("y", b"/weights-y")],
            "00000000-0000-4000-8000-000000002303",
        )
    assert large.id < small.id

    for _ in range(3):
        service.tick()
        if service.get_operation(small.id).state == LifecycleState.SUCCEEDED:
            break
        time.sleep(0.05)
    assert service.get_operation(small.id).state == LifecycleState.SUCCEEDED
    assert service.get_operation(large.id).state == LifecycleState.RUNNING

    gates["/weights-hung"].set()
    _drain(service, large.id)
    _drain(service, hung.id)
    service.close()
    assert client is not None
    client.close()


def test_a_stored_plan_whose_object_vanished_is_replanned_and_queues_for_a_slot(
    tmp_path: Path,
) -> None:
    gates = {"/weights-hog": threading.Event()}
    started: list[str] = []
    service, client = _service(
        tmp_path, _database(tmp_path), maximum=1, handler=_serve(gates, started)
    )
    stored = [
        _artifact(f"s{index}", f"/weights-s{index}".encode()) for index in range(2)
    ]
    first = _start(service, stored, "00000000-0000-4000-8000-000000002401")
    _drain(service, first.id)
    hog = _start(
        service,
        [_artifact("hog", b"/weights-hog")],
        "00000000-0000-4000-8000-000000002402",
    )
    service.tick()
    _wait_for(lambda: "/weights-hog" in started)
    again = _start(service, stored, "00000000-0000-4000-8000-000000002403")
    assert service._operation_transfer_snapshot(again.id)[0] == 0

    # The object goes after the plan was made; the only slot stays hung.
    vanished = text(stored[1]["sha256"])
    assert vanished is not None
    service._receipt_path(vanished).unlink()
    service._object_path(vanished).unlink()
    for _ in range(3):
        service.tick()

    # Re-planned instead of claimed: it waits queued for a slot, never as a
    # running download that has no transfer.
    assert service.get_operation(again.id).state == LifecycleState.QUEUED
    assert service._operation_transfer_snapshot(again.id)[0] == len(b"/weights-s1")
    assert again.id not in service._background_operations

    gates["/weights-hog"].set()
    _drain(service, again.id)
    assert service.get_operation(again.id).state == LifecycleState.SUCCEEDED
    assert started.count("/weights-s1") == 2
    _drain(service, hog.id)
    service.close()
    assert client is not None
    client.close()


class _HeldPool:
    """An executor whose transfers stay in flight until the test ends them."""

    def submit(self, *_args: object, **_kwargs: object) -> Future[None]:
        return Future()


def test_slot_sharing_never_serves_a_started_download_before_a_waiting_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The allocation rule over many random mixes: a free slot never goes to a
    # download that has a file in flight, or has already started more files,
    # while another with files left waits with fewer.
    service, _client = _service(tmp_path, _database(tmp_path), maximum=6)
    monkeypatch.setattr(service, "_executor", _HeldPool())
    shuffle = random.Random(1892)
    for _trial in range(300):
        service._background_operations.clear()
        for index in range(shuffle.randint(1, 6)):
            files = shuffle.randint(1, 8)
            started = shuffle.randint(0, files)
            in_flight: list[Future[None]] = []
            for _ in range(shuffle.randint(0, started)):
                in_flight.append(Future())
            record = _BackgroundTransfer(
                set_digest="0" * 64,
                manifest=cast(ArtifactSetManifest, None),
                force=False,
                specs=[
                    cast(ArtifactSpec, SimpleNamespace(key=f"f{n}"))
                    for n in range(files)
                ],
                planned_total=1,
                transfer_attempt=1,
                next_index=started,
                futures=in_flight,
            )
            service._background_operations[f"{index:08d}-op"] = record
        before = {
            key: (record.pending(), record.next_index)
            for key, record in service._background_operations.items()
        }
        service._share_transfer_slots()
        records = service._background_operations
        served = {
            key for key, record in records.items() if record.next_index > before[key][1]
        }
        left = [
            key
            for key, record in records.items()
            if record.next_index < len(record.specs)
        ]
        if left:
            assert service._available_transfer_slots() == 0
        for waiting in left:
            if waiting in served:
                continue
            for key in served:
                # The last slot the served download took was taken at a key
                # no larger than the waiting download's.
                last = (records[key].pending() - 1, records[key].next_index - 1)
                assert last <= before[waiting], (before, served)
