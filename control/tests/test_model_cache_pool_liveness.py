"""A stuck or large transfer never starves the Controller download pool.

Real threads, real files and a real HTTP client against a mock transport: the
faults are transfers that hang inside the pool and an object lock another
writer holds.
"""

from __future__ import annotations

import fcntl
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
from vonk_agent_protocol import LifecycleState, ModelCacheCode
from vonk_control.bounded_json import text

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
