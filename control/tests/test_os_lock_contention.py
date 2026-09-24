"""OS-lock contention guarantees across independent Controller processes.

Each test here holds a kernel ``flock`` from a **separate process** and proves
that the module under test reports the contention inside a bounded budget
instead of parking a worker thread. An independent holder exercises the actual
worker-process boundary, including kernel release when that holder exits.

These belong to the lane tier because ``flock`` semantics are the guarantee
being tested: the claim, the contention, and the release are all kernel
behaviour, and the modules under test are production Controller code rather than
fixtures. A mocked acquisition would establish nothing here.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from vonk_control import artifact_blob_store, route_runtime, runtime_image_preparation
from vonk_control.artifact_blob_store import ArtifactBlobStore, ArtifactBlobStoreError
from vonk_control.route_runtime import AtomicRouteBundlePublisher, RouteRuntimeError

pytestmark = pytest.mark.lane

_HOLDER = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
    "time.sleep(60)\n"
)


def _hold(path: Path) -> subprocess.Popen[str]:
    """Start a separate process holding an exclusive lock on ``path``."""

    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


def test_reference_fence_claim_is_bounded_when_another_process_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contended reference fence returns a failure instead of blocking.

    It fails on the wrong implementation that calls a blocking ``flock``,
    because that call never returns while the holder lives.
    """

    store = ArtifactBlobStore(tmp_path / "blobs")
    store._prepare_root()
    monkeypatch.setattr(artifact_blob_store, "_REFERENCE_LOCK_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(artifact_blob_store, "_REFERENCE_LOCK_RETRY_SECONDS", 0.01)
    holder = _hold(store._root / ".references.lock")
    try:
        with (
            pytest.raises(ArtifactBlobStoreError, match="being reconciled"),
            store.reference_attachment(),
        ):
            pass
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    # With the holder gone both fences are available again.
    with store.reference_attachment():
        pass
    with store.reference_reconciliation():
        pass


def test_oci_layer_lock_claim_is_bounded_when_another_writer_holds_it(
    tmp_path: Path,
) -> None:
    """A contended OCI layer lock fails retryably instead of parking the worker.

    It fails on the wrong implementation that calls a blocking ``flock``, and
    it also pins that the failure is retryable, because a preparation that
    cannot take the lock must be rescheduled rather than reported as terminal.
    """

    lock_path = tmp_path / "layer.lock"
    holder = _hold(lock_path)
    try:
        started = time.monotonic()
        with (
            lock_path.open("a+b") as lock,
            pytest.raises(
                runtime_image_preparation.RuntimeImagePreparationError,
                match="same OCI index",
            ) as failure,
        ):
            runtime_image_preparation._claim_registry_layer_lock(
                lock, reference="registry.example/vonk/tiny"
            )
        assert failure.value.retryable is True
        assert failure.value.recovery_actions == ("retry",)
        assert time.monotonic() - started < 0.1, (
            "OCI contention parked an image slot instead of rescheduling"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    with lock_path.open("a+b") as lock:
        runtime_image_preparation._claim_registry_layer_lock(
            lock, reference="registry.example/vonk/tiny"
        )


def test_publication_lock_claim_is_bounded_when_another_publisher_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contended publication lock returns a failure instead of blocking.

    It fails on the wrong implementation that calls a blocking ``flock``,
    because that call never returns while the holder lives.
    """

    publisher = AtomicRouteBundlePublisher(
        tmp_path / "runtime", clock=lambda: datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    )
    root = publisher._root
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(route_runtime, "_PUBLICATION_LOCK_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(route_runtime, "_PUBLICATION_LOCK_RETRY_SECONDS", 0.01)
    holder = _hold(root / ".publication.lock")
    try:
        with (
            pytest.raises(RouteRuntimeError, match="held by another publisher"),
            publisher._locked(),
        ):
            pass
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    with publisher._locked():
        pass


def test_runtime_image_publication_lock_is_bounded_and_released_on_process_death(
    tmp_path: Path,
) -> None:
    """Image publication cannot park a worker and kernel death frees its fence."""

    storage = runtime_image_preparation.FilesystemRuntimeImageStorage(tmp_path)
    archive_sha256 = "a" * 64
    lock_root = storage.root / ".publication-locks"
    lock_root.mkdir(parents=True)
    lock_path = lock_root / f"{archive_sha256}.lock"
    holder = _hold(lock_path)
    try:
        started = time.monotonic()
        with (
            pytest.raises(
                runtime_image_preparation.RuntimeImagePreparationError,
                match="another owner to finish this image publication",
            ) as failure,
            storage.publication_lock(archive_sha256),
        ):
            pass
        assert failure.value.code == "runtime_image.publication_contended"
        assert failure.value.retryable is True
        assert failure.value.recovery_actions == ("retry",)
        assert time.monotonic() - started < 0.1, (
            "image publication contention parked a worker"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    # The kernel releases flock when the publisher process dies. The next
    # owner can fence and reconcile the exact archive without a lease guess.
    with storage.publication_lock(archive_sha256):
        pass
