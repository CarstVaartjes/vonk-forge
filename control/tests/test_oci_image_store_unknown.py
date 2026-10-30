"""A store that cannot settle an answer reports it as unknown, never by raising.

The owner of the work observes again on its next pass; nothing is removed or
trusted half-stored in the meantime.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

from vonk_agent_protocol import ImageStoreCode
from vonk_control.oci_image_store import (
    Collection,
    OciImageStore,
    StoreUnknown,
)

_DIGEST = "a" * 64


def _failing_runner(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["skopeo"], 1, "", "registry unreachable\n")


def _hold_lock(store: OciImageStore) -> int:
    store.root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(store.root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return descriptor


def test_a_busy_store_answers_unknown_for_a_copy_and_a_collection(
    tmp_path: Path,
) -> None:
    store = OciImageStore(tmp_path, runner=_failing_runner)
    (store.root / "blobs" / "sha256").mkdir(parents=True)
    descriptor = _hold_lock(store)
    try:
        copied = store.import_archive(tmp_path / "image.tar")
        collected = store.collect(lambda: (), grace_seconds=0)
    finally:
        os.close(descriptor)
    assert isinstance(copied, StoreUnknown)
    assert isinstance(collected, StoreUnknown)
    assert store.collect(lambda: (), grace_seconds=0) == Collection(0, 0)


def test_a_copy_that_does_not_finish_is_unknown_with_the_tool_s_reason(
    tmp_path: Path,
) -> None:
    store = OciImageStore(tmp_path, runner=_failing_runner)
    answer = store.import_archive(tmp_path / "image.tar")
    assert isinstance(answer, StoreUnknown)
    assert store.collect(lambda: (), grace_seconds=0) == Collection(0, 0)


def test_a_damaged_referenced_manifest_proves_nothing_and_removes_nothing(
    tmp_path: Path,
) -> None:
    store = OciImageStore(tmp_path)
    blobs = store.root / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (blobs / _DIGEST).write_bytes(b"{not json")
    orphan = blobs / ("b" * 64)
    orphan.write_bytes(b"unreferenced")
    old = time.time() - 3600
    os.utime(orphan, (old, old))

    answer = store.collect(lambda: [_DIGEST], grace_seconds=60)

    assert isinstance(answer, StoreUnknown)
    assert orphan.read_bytes() == b"unreferenced"
    # A successful subsequent reference scan releases collection without a reset.
    assert store.collect(lambda: (), grace_seconds=60) == Collection(
        1, len(b"unreferenced")
    )
    assert not orphan.exists()


def test_an_unreadable_reference_list_is_passed_through_unremoved(
    tmp_path: Path,
) -> None:
    store = OciImageStore(tmp_path)
    blobs = store.root / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    orphan = blobs / ("c" * 64)
    orphan.write_bytes(b"unreferenced")
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    unknown = StoreUnknown(ImageStoreCode.REFERENCE_SCAN_FAILED, "cannot list")

    assert store.collect(lambda: unknown, grace_seconds=60) == unknown
    assert orphan.exists()
    assert store.collect(lambda: (), grace_seconds=60) == Collection(1, 12)


def _leave_stale_skopeo_files(store: OciImageStore) -> list[Path]:
    """What a killed skopeo leaves: a staged blob and scratch files."""

    scratch = store.root.parent / ".skopeo-tmp"
    (scratch / "unpack-123").mkdir(parents=True)
    store.root.mkdir(parents=True, exist_ok=True)
    leftovers = [
        store.root / "oci-put-blob1234567",
        scratch / "docker-tar-9",
        scratch / "unpack-123" / "layer.tar",
    ]
    for leftover in leftovers:
        leftover.write_bytes(b"half a layer")
    return [*leftovers, scratch / "unpack-123"]


def test_a_killed_copy_s_temporaries_go_at_the_next_collection_unless_the_lock_is_held(
    tmp_path: Path,
) -> None:
    store = OciImageStore(tmp_path, runner=_failing_runner)
    (store.root / "blobs" / "sha256").mkdir(parents=True)
    stale = _leave_stale_skopeo_files(store)

    descriptor = _hold_lock(store)  # a live copy owns the scratch space
    try:
        assert isinstance(store.collect(lambda: (), grace_seconds=0), StoreUnknown)
    finally:
        os.close(descriptor)
    assert all(path.exists() for path in stale)

    assert store.collect(lambda: (), grace_seconds=0) == Collection(0, 0)  # writer gone
    assert not any(path.exists() for path in stale)


def test_a_copy_starts_from_an_empty_scratch_directory_that_skopeo_uses(
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    def runner(*_args, env, **_kwargs) -> subprocess.CompletedProcess[str]:
        scratch = Path(env["TMPDIR"])
        seen["tmpdir"] = scratch
        seen["entries"] = sorted(path.name for path in scratch.iterdir())
        return subprocess.CompletedProcess(["skopeo"], 1, "", "registry unreachable\n")

    store = OciImageStore(tmp_path, runner=runner)
    stale = _leave_stale_skopeo_files(store)

    assert isinstance(store.import_archive(tmp_path / "image.tar"), StoreUnknown)
    assert seen["tmpdir"] == tmp_path / "image-cache" / ".skopeo-tmp"
    assert seen["entries"] == []
    assert not any(path.exists() for path in stale)


def test_a_temporary_that_cannot_be_removed_is_logged_and_the_rest_still_go(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    store = OciImageStore(tmp_path, runner=_failing_runner)
    (store.root / "blobs" / "sha256").mkdir(parents=True)
    stale = _leave_stale_skopeo_files(store)
    stuck = stale[0]
    real_unlink = Path.unlink

    def unlink(self: Path, *args, **kwargs) -> None:
        if self == stuck:
            raise PermissionError("read-only")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with caplog.at_level("INFO"):
        assert store.collect(lambda: (), grace_seconds=0) == Collection(0, 0)

    assert stuck.exists()
    assert not any(path.exists() for path in stale if path != stuck)
    assert ImageStoreCode.COLLECTION_DEFERRED in caplog.text
    assert "PermissionError" in caplog.text
