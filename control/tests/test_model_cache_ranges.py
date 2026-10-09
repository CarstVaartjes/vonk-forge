from threading import Barrier, Event

import httpx2
import pytest
from vonk_control.model_cache_ranges import (
    RangeResponseError,
    download_ranges,
    range_partial_bytes,
)


def response(start, end, total, content, status=206, content_range=None):
    return httpx2.Response(
        status,
        content=content,
        headers={"content-range": content_range or f"bytes {start}-{end}/{total}"},
        request=httpx2.Request("GET", "https://example.com/model"),
    )


def test_parallel_ranges_assemble_and_reuse_contiguous_prefix(tmp_path):
    target = tmp_path / "model.part"
    data = bytes(range(100))
    target.write_bytes(data[:10])
    barrier = Barrier(4)
    requests = []
    progress = []

    def open_range(start, end):
        requests.append((start, end))
        barrier.wait(timeout=5)  # Fails if the actual transfers serialize.
        return response(start, end, len(data), data[start : end + 1])

    assert download_ranges(target, len(data), open_range, Event(), progress.append)
    assert target.read_bytes() == data
    assert sorted(requests) == [(10, 24), (25, 49), (50, 74), (75, 99)]
    assert progress == sorted(progress)
    assert progress[-1] == len(data)
    assert list(tmp_path.iterdir()) == [target]


def test_ignored_ranges_preserve_sequential_partial(tmp_path):
    target = tmp_path / "model.part"
    target.write_bytes(b"abc")
    assert not download_ranges(
        target,
        100,
        lambda start, end: response(start, end, 100, b"x" * 100, 200),
        Event(),
        lambda value: None,
    )
    assert target.read_bytes() == b"abc"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "header,body",
    [
        ("bytes 1-9/10", b"0123456789"),
        ("bytes 0-9/11", b"0123456789"),
        ("nonsense", b"0123456789"),
        ("bytes 0-9/10", b"01234567890"),
    ],
)
def test_rejects_incorrect_range_without_publishing(tmp_path, header, body):
    target = tmp_path / "model.part"
    with pytest.raises(RangeResponseError):
        download_ranges(
            target,
            10,
            lambda start, end: response(start, end, 10, body, content_range=header),
            Event(),
            lambda value: None,
            workers=1,
        )
    assert not target.exists()
    assert download_ranges(
        target,
        10,
        lambda start, end: response(start, end, 10, b"0123456789"),
        Event(),
        lambda value: None,
        workers=1,
    )
    assert target.read_bytes() == b"0123456789"


def test_truncated_response_resumes_its_valid_prefix(tmp_path):
    target = tmp_path / "model.part"
    data = b"0123456789"
    assert not download_ranges(
        target,
        10,
        lambda start, end: response(start, end, 10, data[start : start + 3]),
        Event(),
        lambda value: None,
        workers=1,
    )
    assert range_partial_bytes(target, 10, workers=1) == 9
    requested = []

    def resume(start, end):
        requested.append((start, end))
        return response(start, end, 10, data[start : end + 1])

    assert download_ranges(target, 10, resume, Event(), lambda value: None, workers=1)
    assert requested == [(9, 9)]
    assert target.read_bytes() == b"0123456789"


def test_interruption_keeps_received_ranges_for_resume(tmp_path):
    target = tmp_path / "model.part"
    data = b"a" * (3 * 1024 * 1024)
    stop = Event()

    class Chunks(httpx2.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(data), 1024 * 1024):
                yield data[offset : offset + 1024 * 1024]

    def streaming(start, end):
        return httpx2.Response(
            206,
            stream=Chunks(),
            headers={"content-range": f"bytes {start}-{end}/{len(data)}"},
            request=httpx2.Request("GET", "https://example.com/model"),
        )

    def progress(count):
        if count:
            stop.set()

    with pytest.raises(InterruptedError):
        download_ranges(
            target,
            len(data),
            streaming,
            stop,
            progress,
            workers=1,
        )
    received = range_partial_bytes(target, len(data), workers=1)
    assert received == 1024 * 1024
    assert not target.exists()
    stop.clear()
    assert download_ranges(
        target,
        len(data),
        lambda s, e: response(s, e, len(data), data[s : e + 1]),
        stop,
        lambda value: None,
        workers=1,
    )
    assert target.read_bytes() == data


def test_oversize_after_complete_prefix_cannot_be_reused(tmp_path):
    target = tmp_path / "model.part"
    size = 1024 * 1024
    with pytest.raises(RangeResponseError):
        download_ranges(
            target,
            size,
            lambda s, e: response(s, e, size, b"a" * (size + 1)),
            Event(),
            lambda value: None,
            workers=1,
        )
    assert range_partial_bytes(target, size, workers=1) == 0
    assert not target.exists()
    assert download_ranges(
        target,
        size,
        lambda s, e: response(s, e, size, b"a" * size),
        Event(),
        lambda value: None,
        workers=1,
    )
    assert target.read_bytes() == b"a" * size


def test_precancelled_download_never_opens_http(tmp_path):
    stop = Event()
    stop.set()

    def unexpected(start, end):
        raise AssertionError("cancelled transfer opened HTTP")

    with pytest.raises(InterruptedError):
        download_ranges(
            tmp_path / "model.part", 100, unexpected, stop, lambda value: None
        )
    stop.clear()
    assert download_ranges(
        tmp_path / "model.part",
        100,
        lambda s, e: response(s, e, 100, b"a" * (e - s + 1)),
        stop,
        lambda value: None,
    )


@pytest.mark.parametrize(
    "name", ["model.part", "model.part.range-0-24", "model.part.range-assembly"]
)
def test_symlink_cache_paths_cannot_touch_external_file(tmp_path, name):
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    (tmp_path / name).symlink_to(outside)
    assert range_partial_bytes(tmp_path / "model.part", 100) == 0
    assert download_ranges(
        tmp_path / "model.part",
        100,
        lambda s, e: response(s, e, 100, b"a" * (e - s + 1)),
        Event(),
        lambda value: None,
    )
    assert outside.read_bytes() == b"keep"
    assert (tmp_path / "model.part").read_bytes() == b"a" * 100


@pytest.mark.parametrize("resume_prefix", [0, 3 * 1024 * 1024 // 2])
@pytest.mark.parametrize("retained_segment", [0, 128 * 1024])
def test_resumed_range_peak_growth_fits_two_object_reservation(
    tmp_path, monkeypatch, resume_prefix, retained_segment
):
    import os

    data = bytes(range(256)) * (2 * 1024 * 1024 // 256)
    target = tmp_path / "model.part"
    target.write_bytes(data[:resume_prefix])
    if retained_segment:
        start = 3 * len(data) // 4
        segment = target.with_name(f"{target.name}.range-{start}-{len(data) - 1}")
        segment.write_bytes(data[start : start + retained_segment])
    initial_bytes = sum(path.stat().st_size for path in tmp_path.iterdir())
    observed_peak = []
    real_replace = os.replace

    def publish(assembly, destination):
        # Measure real files at maximum coexistence: retained contiguous prefix,
        # completed range segments and the fully written assembly all exist.
        observed_peak.append(sum(path.stat().st_size for path in tmp_path.iterdir()))
        return real_replace(assembly, destination)

    monkeypatch.setattr(os, "replace", publish)
    assert download_ranges(
        target,
        len(data),
        lambda start, end: response(start, end, len(data), data[start : end + 1]),
        Event(),
        lambda value: None,
    )
    assert target.read_bytes() == data
    assert observed_peak == [resume_prefix + 2 * len(data)]
    assert observed_peak[0] - initial_bytes == 2 * len(data) - retained_segment
    assert observed_peak[0] - initial_bytes <= 2 * len(data)
    assert list(tmp_path.iterdir()) == [target]


def test_lost_range_at_assembly_is_a_miss_and_fresh_attempt_repairs(tmp_path):
    target = tmp_path / "model.part"
    segment = target.with_name("model.part.range-0-9")

    def damage_after_transfer(count):
        if count == 10:
            segment.write_bytes(b"torn")

    assert not download_ranges(
        target,
        10,
        lambda s, e: response(s, e, 10, b"0123456789"),
        Event(),
        damage_after_transfer,
        workers=1,
    )
    assert not target.exists()
    assert download_ranges(
        target,
        10,
        lambda s, e: response(s, e, 10, b"0123456789"),
        Event(),
        lambda count: None,
        workers=1,
    )
    assert target.read_bytes() == b"0123456789"


def test_lost_contiguous_prefix_is_refetched(tmp_path, monkeypatch):
    from pathlib import Path

    target = tmp_path / "model.part"
    target.write_bytes(b"01234")
    original_open = Path.open
    damaged = False

    def lose_prefix(path, mode="r", *args, **kwargs):
        nonlocal damaged
        if path == target and mode == "rb" and not damaged:
            damaged = True
            target.write_bytes(b"")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", lose_prefix)
    requested = []

    def fetch(s, e):
        requested.append((s, e))
        return response(s, e, 10, b"0123456789"[s : e + 1])

    assert download_ranges(target, 10, fetch, Event(), lambda count: None, workers=1)
    assert requested == [(0, 9)]
    assert target.read_bytes() == b"0123456789"
