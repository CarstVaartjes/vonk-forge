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
    assert store.usage()["in_flight_uploads"] == 0


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

    with pytest.raises(FileNotFoundError):
        store.resolve(stored.storage_key, stored.sha256, len(content))


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


def test_an_upload_over_the_quota_is_refused_at_ingress(tmp_path: Path) -> None:
    store = _store(tmp_path, quota=4)
    content = b"larger than four bytes"

    with pytest.raises(ArtifactBlobStoreError):
        store.put_bytes(_digest(content), content, maximum_bytes=1024)
