"""Resumable bounded HTTP ranges for one immutable model-cache object.

The caller owns HTTP authentication, retry policy, identity, and final publication.
An ignored Range returns False so the caller can use its sequential downloader.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from threading import Event, Lock

import httpx2
from vonk_agent_protocol import UnknownOutcomeError, WaitReason


class RangeResponseError(ValueError):
    """A range response does not describe the requested object bytes."""


class RangeTruncatedError(UnknownOutcomeError, httpx2.RemoteProtocolError):
    """A valid range ended early; retry may resume its retained prefix."""


class _RangeIgnored(Exception):
    pass


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RangeResponseError("range cache path must not be a symlink")


def _segments(target: Path, expected_bytes: int, workers: int):
    if expected_bytes <= 0 or workers <= 0:
        raise ValueError("expected_bytes and workers must be positive")
    width = (expected_bytes + workers - 1) // workers
    for start in range(0, expected_bytes, width):
        end = min(start + width, expected_bytes) - 1
        yield start, end, target.with_name(f"{target.name}.range-{start}-{end}")


def range_partial_bytes(target: Path, expected_bytes: int, *, workers: int = 4) -> int:
    """Count reusable bytes, without counting the contiguous prefix twice."""
    _reject_symlink(target)
    prefix = min(target.stat().st_size, expected_bytes) if target.exists() else 0
    for _, _, path in _segments(target, expected_bytes, workers):
        _reject_symlink(path)
    return sum(
        max(
            min(path.stat().st_size, end - start + 1) if path.exists() else 0,
            max(0, min(prefix, end + 1) - start),
        )
        for start, end, path in _segments(target, expected_bytes, workers)
    )


def cleanup_ranges(target: Path, expected_bytes: int, *, workers: int = 4) -> None:
    for _, _, path in _segments(target, expected_bytes, workers):
        path.unlink(missing_ok=True)
    target.with_name(f"{target.name}.range-assembly").unlink(missing_ok=True)


def download_ranges(
    target: Path,
    expected_bytes: int,
    open_range: Callable[[int, int], httpx2.Response],
    stop_event: Event,
    on_progress: Callable[[int], None],
    *,
    workers: int = 4,
    stream_gate: Callable[[], AbstractContextManager[object]] | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> bool:
    """Complete target atomically, or return False for sequential fallback.

    Responses must be streaming and are always closed here. Progress is an
    absolute byte count and the callback is serialized across range threads.
    Interrupted/truncated transfers retain valid segment prefixes for retry.
    Assembly temporarily needs space for another full object alongside segments.
    The caller must serialize attempts for this target and use stable workers.
    """
    _reject_symlink(target)
    segments = list(_segments(target, expected_bytes, workers))
    for _, _, path in segments:
        _reject_symlink(path)
    _reject_symlink(target.with_name(f"{target.name}.range-assembly"))
    target.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[int, int] = {}
    # Reuse any existing sequential prefix without changing its fallback file.
    for start, end, path in segments:
        size = path.stat().st_size if path.exists() else 0
        if size > end - start + 1:
            path.unlink()
            size = 0
        prefix = min(target.stat().st_size, end + 1) if target.exists() else 0
        available = max(0, prefix - start)
        if available > size:
            with target.open("rb") as source, path.open("ab") as output:
                source.seek(start + size)
                remaining = available - size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RangeResponseError(
                            "contiguous partial changed while seeding ranges"
                        )
                    output.write(chunk)
                    remaining -= len(chunk)
            size = available
        counts[start] = size
    lock = Lock()
    abort = Event()
    fallback = Event()

    def interrupted() -> bool:
        return stop_event.is_set() or abort.is_set()

    def transfer(start: int, end: int, path: Path) -> None:
        if interrupted():
            raise InterruptedError("range transfer interrupted")
        # Three observations bound a short-body retry. Backoff holds neither
        # the stream permit nor a connection; every attempt resumes bytes that
        # the preceding response flushed durably.
        for pause in (0.0, 0.05, 0.1):
            if stop_event.wait(pause) or interrupted():
                raise InterruptedError("range transfer interrupted")
            offset = start + counts[start]
            if offset > end:
                return
            try:
                with stream_gate() if stream_gate is not None else nullcontext():
                    _transfer_body(start, end, path, offset)
                return
            except RangeTruncatedError:
                continue
        # The sequential downloader is already the request's bounded fallback.
        # Retained range prefixes remain reusable on a later range observation.
        fallback.set()

    def _transfer_body(start: int, end: int, path: Path, offset: int) -> None:
        response = open_range(offset, end)
        invalid_body = False
        try:
            if response.status_code == 200:
                raise _RangeIgnored()
            response.raise_for_status()
            match = re.fullmatch(
                r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", "")
            )
            if (
                response.status_code != 206
                or match is None
                or tuple(map(int, match.groups())) != (offset, end, expected_bytes)
            ):
                raise RangeResponseError(
                    "response Content-Range does not match requested bytes"
                )
            if (
                response.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise RangeResponseError(
                    "range response must use identity content encoding"
                )
            remaining = end - offset + 1
            with path.open("ab") as output:
                try:
                    for chunk in response.iter_bytes():
                        if interrupted():
                            raise InterruptedError("range transfer interrupted")
                        if len(chunk) > remaining:
                            invalid_body = True
                            raise RangeResponseError(
                                "range response exceeds requested bytes"
                            )
                        output.write(chunk)
                        remaining -= len(chunk)
                        if on_bytes is not None:
                            on_bytes(len(chunk))
                        with lock:
                            counts[start] += len(chunk)
                            on_progress(sum(counts.values()))
                    if remaining:
                        raise RangeTruncatedError(
                            "range response ended before requested bytes arrived",
                            reason=WaitReason.OBSERVATION_UNAVAILABLE,
                        )
                finally:
                    output.flush()
                    os.fsync(output.fileno())
        finally:
            response.close()
            if invalid_body:
                path.unlink(missing_ok=True)

    on_progress(sum(counts.values()))
    failure: Exception | None = None
    ignored = False
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="model-range"
    ) as pool:
        futures = [pool.submit(transfer, *segment) for segment in segments]
        for future in as_completed(futures):
            try:
                future.result()
            except _RangeIgnored:
                ignored = True
                abort.set()
            except Exception as exc:  # noqa: BLE001 - cancel peers, then re-raise the original failure
                if failure is None:
                    failure = exc
                abort.set()
    if ignored:
        cleanup_ranges(target, expected_bytes, workers=workers)
        return False
    if failure is not None:
        raise failure
    if stop_event.is_set():
        raise InterruptedError("range transfer interrupted")
    if fallback.is_set():
        return False
    assembly = target.with_name(f"{target.name}.range-assembly")
    try:
        with assembly.open("wb") as output:
            for start, end, path in segments:
                if path.stat().st_size != end - start + 1:
                    raise RangeResponseError("range partial has unexpected length")
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        if stop_event.is_set():
                            raise InterruptedError("range assembly interrupted")
                        output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(assembly, target)
    finally:
        assembly.unlink(missing_ok=True)
    cleanup_ranges(target, expected_bytes, workers=workers)
    return True
