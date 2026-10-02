import threading
import time
import types
from threading import Event

import httpx2
import pytest
from vonk_control.model_cache_ranges import download_ranges
from vonk_control.model_cache_streams import (
    PLATEAU_HOLD_WINDOWS,
    WINDOW_SECONDS,
    StreamGovernor,
)

from .test_model_cache import (  # noqa: F401
    _download,
    _http_artifact,
    _http_cache_service,
    cache,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _saturate(governor: StreamGovernor, count: int):
    stack = []
    for _ in range(count):
        manager = governor.stream(lambda: False)
        manager.__enter__()
        stack.append(manager)
    return stack


def _release(stack) -> None:
    for manager in stack:
        manager.__exit__(None, None, None)


def _window(governor, clock, rate_bytes_per_second: float) -> None:
    governor.record_bytes(int(rate_bytes_per_second * WINDOW_SECONDS))
    clock.now += WINDOW_SECONDS
    governor.tick()


def test_streams_never_exceed_the_limit() -> None:
    governor = StreamGovernor(4, initial_streams=2)
    live = 0
    peak = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal live, peak
        with governor.stream(lambda: False):
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.02)
            with lock:
                live -= 1

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert peak <= 2
    assert governor.status().active == 0


def test_waiting_stream_stops_when_asked() -> None:
    governor = StreamGovernor(1, initial_streams=1)
    held = _saturate(governor, 1)
    try:
        with pytest.raises(InterruptedError), governor.stream(lambda: True):
            raise AssertionError("must not start")
    finally:
        _release(held)


def test_ramps_while_throughput_grows_then_holds_at_plateau() -> None:
    clock = Clock()
    governor = StreamGovernor(16, initial_streams=4, clock=clock)
    held = _saturate(governor, 4)
    _window(governor, clock, 40e6)
    assert governor.status().limit == 6
    _release(held)
    held = _saturate(governor, 6)
    _window(governor, clock, 60e6)  # grew by 50%: keep climbing
    assert governor.status().limit == 8
    _release(held)
    held = _saturate(governor, 8)
    _window(governor, clock, 60e6)  # no growth: step back and hold
    status = governor.status()
    assert status.limit == 6
    assert "plateau" in status.reason
    _release(held)
    for _ in range(PLATEAU_HOLD_WINDOWS):
        held = _saturate(governor, 6)
        _window(governor, clock, 60e6)
        _release(held)
        assert governor.status().limit == 6


def test_never_climbs_past_the_cap() -> None:
    clock = Clock()
    governor = StreamGovernor(6, initial_streams=4, clock=clock)
    rate = 10e6
    for _ in range(6):
        held = _saturate(governor, governor.status().limit)
        rate *= 2
        _window(governor, clock, rate)
        _release(held)
    assert governor.status().limit == 6


def test_throttle_halves_limit_and_holds_the_ramp() -> None:
    clock = Clock()
    governor = StreamGovernor(16, initial_streams=8, clock=clock)
    governor.throttled(45, "Hugging Face answered 429 (rate limited)")
    status = governor.status()
    assert status.limit == 4
    assert "429" in status.reason
    for _ in range(PLATEAU_HOLD_WINDOWS):
        held = _saturate(governor, 4)
        _window(governor, clock, 100e6)
        _release(held)
        assert governor.status().limit == 4
    held = _saturate(governor, 4)
    _window(governor, clock, 100e6)
    _release(held)
    assert governor.status().limit == 6  # ramping resumes after the hold


def test_range_streams_respect_the_gate(tmp_path) -> None:
    data = bytes(range(200))
    governor = StreamGovernor(2, initial_streams=2)
    live = 0
    peak = 0
    lock = threading.Lock()

    def open_range(start, end):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return httpx2.Response(
            206,
            content=data[start : end + 1],
            headers={"content-range": f"bytes {start}-{end}/{len(data)}"},
            request=httpx2.Request("GET", "https://example.com/model"),
        )

    target = tmp_path / "model.part"
    assert download_ranges(
        target,
        len(data),
        open_range,
        Event(),
        lambda value: None,
        stream_gate=lambda: governor.stream(lambda: False),
        on_bytes=governor.record_bytes,
    )
    assert target.read_bytes() == data
    assert peak <= 2


def test_huggingface_429_and_5xx_back_off_the_streams(cache, tmp_path) -> None:  # noqa: F811
    _existing, sessions = cache
    statuses = iter([429, 503])

    def handler(request):
        return httpx2.Response(
            next(statuses), headers={"Retry-After": "20"}, request=request
        )

    service, client = _http_cache_service(tmp_path, sessions, handler)
    try:
        url = "https://huggingface.co/org/repo/resolve/" + "a" * 40 + "/w.bin"
        before = service._streams.status().limit
        with pytest.raises(Exception) as first:
            service._open_http_response(client, url, {})
        assert getattr(first.value, "retry_after_seconds", None) == 20
        halved = service._streams.status().limit
        assert halved == before // 2
        assert service._hf_cooldown_until is not None  # Retry-After honoured
        service._hf_cooldown_until = None
        with pytest.raises(Exception) as second:
            service._open_http_response(client, url, {})
        assert getattr(second.value, "retry_after_seconds", None) == 20
        assert service._streams.status().limit == halved // 2
    finally:
        service.close()
        client.close()


def test_ranged_download_scans_the_bytes_once(cache, tmp_path, monkeypatch) -> None:  # noqa: F811
    import vonk_control.model_cache as module

    _existing, sessions = cache
    payload = bytes(range(256)) * 256
    monkeypatch.setattr(module, "_PARALLEL_RANGE_MIN_BYTES", 1)

    def handler(request):
        start, end = map(
            int, request.headers["range"].removeprefix("bytes=").split("-")
        )
        return httpx2.Response(
            206,
            request=request,
            content=payload[start : end + 1],
            headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
        )

    service, client = _http_cache_service(tmp_path, sessions, handler)
    import vonk_control.cached_file_verification as verification

    calls = []
    real = verification.hashlib.sha256
    monkeypatch.setattr(
        verification,
        "hashlib",
        types.SimpleNamespace(
            sha256=lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs)
        ),
    )
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000001999",
        )
        assert operation.state == "succeeded", operation.last_error
        assert len(calls) == 1
    finally:
        service.close()
        client.close()
