from threading import Barrier, Event

import httpx
import pytest
from vonk_control.model_cache_ranges import (
    RangeResponseError,
    RangeTruncatedError,
    download_ranges,
    range_partial_bytes,
)


def response(start, end, total, content, status=206, content_range=None):
    return httpx.Response(
        status,
        content=content,
        headers={"content-range": content_range or f"bytes {start}-{end}/{total}"},
        request=httpx.Request("GET", "https://example.com/model"),
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
        ("bytes 0-9/10", b"012"),
    ],
)
def test_rejects_incorrect_range_without_publishing(tmp_path, header, body):
    target = tmp_path / "model.part"
    with pytest.raises((RangeResponseError, RangeTruncatedError)):
        download_ranges(
            target,
            10,
            lambda start, end: response(start, end, 10, body, content_range=header),
            Event(),
            lambda value: None,
            workers=1,
        )
    assert not target.exists()


def test_truncated_response_resumes_its_valid_prefix(tmp_path):
    target = tmp_path / "model.part"
    with pytest.raises((RangeResponseError, RangeTruncatedError)):
        download_ranges(
            target,
            10,
            lambda start, end: response(start, end, 10, b"012"),
            Event(),
            lambda value: None,
            workers=1,
        )
    assert range_partial_bytes(target, 10, workers=1) == 3
    requested = []

    def resume(start, end):
        requested.append((start, end))
        return response(start, end, 10, b"3456789")

    assert download_ranges(target, 10, resume, Event(), lambda value: None, workers=1)
    assert requested == [(3, 9)]
    assert target.read_bytes() == b"0123456789"


def test_interruption_keeps_received_ranges_for_resume(tmp_path):
    target = tmp_path / "model.part"
    data = b"a" * (3 * 1024 * 1024)
    stop = Event()

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(data), 1024 * 1024):
                yield data[offset : offset + 1024 * 1024]

    def streaming(start, end):
        return httpx.Response(
            206,
            stream=Chunks(),
            headers={"content-range": f"bytes {start}-{end}/{len(data)}"},
            request=httpx.Request("GET", "https://example.com/model"),
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
    with pytest.raises((RangeResponseError, RangeTruncatedError)):
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


def test_precancelled_download_never_opens_http(tmp_path):
    stop = Event()
    stop.set()

    def unexpected(start, end):
        raise AssertionError("cancelled transfer opened HTTP")

    with pytest.raises(InterruptedError):
        download_ranges(
            tmp_path / "model.part", 100, unexpected, stop, lambda value: None
        )


@pytest.mark.parametrize(
    "name", ["model.part", "model.part.range-0-24", "model.part.range-assembly"]
)
def test_symlink_cache_paths_cannot_touch_external_file(tmp_path, name):
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    (tmp_path / name).symlink_to(outside)
    with pytest.raises(RangeResponseError, match="symlink"):
        download_ranges(
            tmp_path / "model.part",
            100,
            lambda s, e: response(s, e, 100, b"a" * (e - s + 1)),
            Event(),
            lambda value: None,
        )
    assert outside.read_bytes() == b"keep"
