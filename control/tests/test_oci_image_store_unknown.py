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
    STORE_BUSY,
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
    assert isinstance(copied, StoreUnknown) and copied.code == STORE_BUSY
    assert isinstance(collected, StoreUnknown) and collected.code == STORE_BUSY


def test_a_copy_that_does_not_finish_is_unknown_with_the_tool_s_reason(
    tmp_path: Path,
) -> None:
    store = OciImageStore(tmp_path, runner=_failing_runner)
    answer = store.import_archive(tmp_path / "image.tar")
    assert isinstance(answer, StoreUnknown)
    assert answer.code == ImageStoreCode.COPY_FAILED
    assert answer.detail == "registry unreachable"


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
    assert answer.code == ImageStoreCode.REFERENCED_MANIFEST_DAMAGED
    assert answer.address == _DIGEST
    assert orphan.exists()


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
