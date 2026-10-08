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
    with pytest.raises(ArtifactBlobStoreError):
        store.resolve("00/" + digest, digest, len(content))
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

    with pytest.raises(ArtifactBlobStoreError):
        store.put_bytes(_digest(content), content, maximum_bytes=1024)
    assert store.usage().in_flight_uploads == 0
    fresh = store.put_bytes(_digest(b"ok"), b"ok", maximum_bytes=4)
    assert fresh.path.read_bytes() == b"ok"


@pytest.mark.parametrize("boundary", ("reserve", "commit"))
def test_capacity_attempt_releases_claims_and_fresh_upload_recovers(tmp_path, boundary):
    """Catches quota exhaustion retaining temporary claims or rejecting reuse."""
    from vonk_control.artifact_blob_store import ArtifactBlobQuotaExhausted

    store = _store(tmp_path, quota=8)
    occupied = store.put_bytes(_digest(b"12345"), b"12345", maximum_bytes=8)
    payload = b"abcd"

    async def chunks():
        yield payload
        if boundary == "commit":
            # A concurrent unreserved transfer consumes the available space.
            (tmp_path / "blobs" / ".tmp" / "competing.part").write_bytes(b"12345")

    if boundary == "commit":
        store.delete(occupied.storage_key, occupied.sha256)
    with pytest.raises(ArtifactBlobQuotaExhausted):
        import asyncio

        asyncio.run(
            store.put_stream(
                _digest(payload), chunks(), expected_bytes=len(payload), maximum_bytes=8
            )
        )
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
