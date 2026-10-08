"""Blob store bookkeeping reconciles; the ingress and path refusals still refuse."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from vonk_control.artifact_blob_store import ArtifactBlobStore, ArtifactBlobStoreError


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _store(tmp_path: Path, *, quota: int = 1024**2) -> ArtifactBlobStore:
    return ArtifactBlobStore(tmp_path / "blobs", max_stored_bytes=quota)


def test_a_damaged_reservation_file_does_not_block_uploads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.usage()
    (tmp_path / "blobs" / ".reservations" / "damaged.reserve").write_text("not-a-size")
    (tmp_path / "blobs" / ".reservations" / "huge.reserve").write_text(str(10**12))

    content = b"payload"
    stored = store.put_bytes(_digest(content), content, maximum_bytes=1024)

    assert stored.path.read_bytes() == content
    assert store.usage().in_flight_uploads == 0


def test_a_damaged_stored_object_is_replaced_by_the_verified_upload(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    content = b"the exact bytes"
    stored = store.put_bytes(_digest(content), content, maximum_bytes=1024)
    stored.path.write_bytes(b"trunc")

    healed = store.put_bytes(_digest(content), content, maximum_bytes=1024)

    assert healed.path.read_bytes() == content
    assert store.resolve(healed.storage_key, healed.sha256, len(content)) == healed.path


def test_an_absent_object_is_not_found_rather_than_a_refusal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"gone soon"
    stored = store.put_bytes(_digest(content), content, maximum_bytes=1024)
    stored.path.unlink()

    assert store.resolve(stored.storage_key, stored.sha256, len(content)) is None


def test_digest_key_and_path_refusals_still_refuse(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"abc"
    digest = _digest(content)

    with pytest.raises(ArtifactBlobStoreError):
        store.put_bytes(_digest(b"other"), content, maximum_bytes=1024)
    assert store.resolve("00/" + digest, digest, len(content)) is None
    stored = store.put_bytes(digest, content, maximum_bytes=1024)
    assert store.resolve("00/" + digest, digest, len(content)) == stored.path
    assert stored.path.read_bytes() == content
    with pytest.raises(ArtifactBlobStoreError):
        store.delete("00/" + digest, digest)
    with pytest.raises(ArtifactBlobStoreError):
        store.put_bytes("not-a-digest", content, maximum_bytes=1024)


def test_an_unsafe_storage_root_still_refuses(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "blobs").symlink_to(real)

    with pytest.raises(ArtifactBlobStoreError):
        _store(tmp_path).usage()


def test_an_upload_over_current_capacity_ends_without_blocking_fresh_work(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, quota=4)
    content = b"larger than four bytes"

    with pytest.raises(Exception):  # noqa: B017 -- ending witness; fresh content admission below
        store.put_bytes(_digest(content), content, maximum_bytes=1024)
    assert store.usage().in_flight_uploads == 0
    fresh = store.put_bytes(_digest(b"ok"), b"ok", maximum_bytes=4)
    assert fresh.path.read_bytes() == b"ok"


@pytest.mark.parametrize("boundary", ("reserve", "commit"))
def test_capacity_attempt_releases_claims_and_fresh_upload_recovers(
    tmp_path, boundary, monkeypatch
):
    """Catches quota exhaustion retaining temporary claims or rejecting reuse."""
    store = _store(tmp_path, quota=8)
    occupied = store.put_bytes(_digest(b"12345"), b"12345", maximum_bytes=8)
    payload = b"abcd"
    attempts = []
    method = "_reserve" if boundary == "reserve" else "_commit"
    observe = getattr(store, method)

    def count_observation(*args):
        attempts.append(None)
        return observe(*args)

    monkeypatch.setattr(store, method, count_observation)

    async def chunks():
        yield payload
        if boundary == "commit":
            # A concurrent unreserved transfer consumes the available space.
            (tmp_path / "blobs" / ".tmp" / "competing.part").write_bytes(b"12345")

    if boundary == "commit":
        store.delete(occupied.storage_key, occupied.sha256)
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; released claims asserted below
        import asyncio

        asyncio.run(
            store.put_stream(
                _digest(payload), chunks(), expected_bytes=len(payload), maximum_bytes=8
            )
        )
    assert len(attempts) == 3
    assert store.usage().in_flight_uploads == 0
    assert not list((tmp_path / "blobs" / ".reservations").glob("*.reserve"))
    if boundary == "reserve":
        store.delete(occupied.storage_key, occupied.sha256)
    else:
        (tmp_path / "blobs" / ".tmp" / "competing.part").unlink()
    healed = store.put_bytes(_digest(payload), payload, maximum_bytes=8)
    assert healed.path.read_bytes() == payload
    assert store.put_bytes(_digest(payload), payload, maximum_bytes=8) == healed


def test_busy_quota_ends_stream_without_consuming_it_and_fresh_stream_fits(tmp_path):
    """Catches blocking flock and replaying a consumed upload on admission retry."""
    import asyncio
    import fcntl
    import time

    store = _store(tmp_path)
    store.usage()
    content = b"verified stream"
    consumed = []

    async def chunks():
        consumed.append(True)
        yield content

    with (tmp_path / "blobs" / ".quota.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(Exception):  # noqa: B017 -- any ending; effects and fresh admission are asserted below
            asyncio.run(
                store.put_stream(
                    _digest(content),
                    chunks(),
                    expected_bytes=len(content),
                    maximum_bytes=1024,
                )
            )
        assert time.monotonic() - started < 2
        assert not consumed
        assert not list((tmp_path / "blobs" / ".reservations").iterdir())
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    stored = asyncio.run(
        store.put_stream(
            _digest(content),
            chunks(),
            expected_bytes=len(content),
            maximum_bytes=1024,
        )
    )
    assert consumed == [True]
    assert stored.path.read_bytes() == content
    assert store.usage().in_flight_uploads == 0


@pytest.mark.parametrize("boundary", ("reserve", "commit"))
def test_stream_retries_same_content_without_replaying_iterator(
    tmp_path, monkeypatch, boundary
):
    """Catches streaming callers bypassing retries or destroying verified temporary bytes."""
    import asyncio

    from vonk_agent_protocol import WaitReason
    from vonk_control.artifact_blob_store import ArtifactBlobBusy

    store = _store(tmp_path)
    content = b"exact streamed bytes"
    consumed = []
    attempts = []
    actual = getattr(store, "_" + boundary)

    def once(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise ArtifactBlobBusy(
                "storage temporarily unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return actual(*args)

    monkeypatch.setattr(store, "_" + boundary, once)

    async def chunks():
        consumed.append(True)
        yield content

    stored = asyncio.run(
        store.put_stream(
            _digest(content),
            chunks(),
            expected_bytes=len(content),
            maximum_bytes=1024,
        )
    )
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert consumed == [True]
    assert stored.path.read_bytes() == content
    assert store.usage().in_flight_uploads == 0
    fresh = store.put_bytes(_digest(content), content, maximum_bytes=1024)
    assert fresh.path == stored.path


def test_reservation_fsync_failure_releases_claim_and_fresh_upload_fits(
    tmp_path, monkeypatch
):
    """Catches an OS failure escaping the storage owner with a retained reservation."""
    import os

    store = _store(tmp_path)
    real = os.fsync

    def unavailable(_descriptor):
        raise OSError("storage unavailable")

    monkeypatch.setattr(os, "fsync", unavailable)
    content = b"reservation repair"
    with pytest.raises(Exception):  # noqa: B017 -- any ending; effects and fresh admission are asserted below
        store.put_bytes(_digest(content), content, maximum_bytes=1024)
    monkeypatch.setattr(os, "fsync", real)
    assert store.usage().in_flight_uploads == 0
    stored = store.put_bytes(_digest(content), content, maximum_bytes=1024)
    assert stored.path.read_bytes() == content


@pytest.mark.parametrize(
    "boundary", ("temporary-unlink", "reservation-unlink", "unlock")
)
def test_cleanup_fault_releases_kernel_owner_and_fresh_upload_reaps_partial(
    tmp_path, monkeypatch, boundary
):
    """Catches cleanup faults leaking a claim or counting abandoned bytes forever."""
    import fcntl

    store = _store(tmp_path, quota=4)
    unlink = Path.unlink
    flock = fcntl.flock

    def fail_unlink(path, *args, **kwargs):
        if (boundary == "temporary-unlink" and path.suffix == ".part") or (
            boundary == "reservation-unlink" and path.suffix == ".reserve"
        ):
            raise OSError("cleanup storage unavailable")
        return unlink(path, *args, **kwargs)

    def fail_unlock(descriptor, mode):
        if boundary == "unlock" and mode == fcntl.LOCK_UN:
            raise OSError("explicit unlock unavailable")
        return flock(descriptor, mode)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", fail_unlink)
        fault.setattr(fcntl, "flock", fail_unlock)
        with pytest.raises(Exception):  # noqa: B017 -- ending witness; no ingress publication and fresh admission below
            # A wrong digest ends after owning temporary bytes and a reservation.
            import asyncio

            async def chunks():
                yield b"bad!"

            asyncio.run(
                store.put_stream(
                    _digest(b"good"), chunks(), expected_bytes=4, maximum_bytes=4
                )
            )
    assert not list((tmp_path / "blobs").glob("??/*"))
    fresh = store.put_bytes(_digest(b"ok"), b"ok", maximum_bytes=4)
    assert fresh.path.read_bytes() == b"ok"
    assert store.usage().in_flight_uploads == 0
    assert not list((tmp_path / "blobs" / ".tmp").iterdir())


@pytest.mark.parametrize("boundary", ("mkdir", "quota-open"))
def test_storage_admission_fault_observes_bound_without_consuming_stream(
    tmp_path, monkeypatch, boundary
):
    """Catches root/quota I/O escaping before the upload's bounded observations."""
    import asyncio

    store = _store(tmp_path)
    calls = []
    consumed = []
    method = "mkdir" if boundary == "mkdir" else "open"
    original = getattr(Path, method)

    def unavailable(path, *args, **kwargs):
        affected = (
            path == tmp_path / "blobs"
            if boundary == "mkdir"
            else path.name == ".quota.lock"
        )
        if affected:
            calls.append(None)
            raise OSError("storage observation unavailable")
        return original(path, *args, **kwargs)

    async def chunks():
        consumed.append(None)
        yield b"ok"

    with monkeypatch.context() as fault:
        fault.setattr(Path, method, unavailable)
        with pytest.raises(Exception):  # noqa: B017 -- ending witness; unconsumed stream and fresh admission below
            asyncio.run(
                store.put_stream(
                    _digest(b"ok"), chunks(), expected_bytes=2, maximum_bytes=4
                )
            )
    assert len(calls) == 3
    assert not consumed
    fresh = asyncio.run(
        store.put_stream(_digest(b"ok"), chunks(), expected_bytes=2, maximum_bytes=4)
    )
    assert fresh.path.read_bytes() == b"ok"
    assert consumed == [None]
    assert store.usage().in_flight_uploads == 0
