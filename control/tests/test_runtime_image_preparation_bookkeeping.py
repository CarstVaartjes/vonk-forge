"""Runtime image storage: facts that no longer match are misses, storage faults retry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
    _atomic_json_replace,
)

from .runtime_image_fixtures import place_test_image
from .test_runtime_image_preparation import (
    ARCHIVE,
    ARCHIVE_DIGEST,
    BUILT_IMAGE_DIGEST,
    TinyTransport,
    _document,
    _prepare,
    _runtime,
    prepare_runtime_image,
)


def test_a_receipt_whose_size_differs_from_the_stored_image_is_a_scan_miss(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    build_input = "a" * 64
    receipt = prepare_runtime_image(
        _document("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        build_receipt={
            "state": "succeeded",
            "build_id": "build-1",
            "build_input_sha256": build_input,
            "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST,
            "image_bytes": len(ARCHIVE),
        },
    )
    lookup = {
        "expected_architecture": "linux/arm64",
        "expected_runtime_interface": "vonk.runtime.v1",
    }
    assert storage.find_build(build_input, **lookup) == receipt

    # The receipt now claims another size than the stored image has.
    path = storage.root / f"{ARCHIVE_DIGEST}.receipt.json"
    document = json.loads(path.read_text())
    document["image_bytes"] = len(ARCHIVE) + 1
    path.write_text(json.dumps(document))

    assert storage.find_build(build_input, **lookup) is None
    assert storage.find_verified(BUILT_IMAGE_DIGEST, **lookup) is None


def test_a_storage_fault_is_a_retryable_failure_not_a_terminal_one(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    # The lock directory cannot be created: a file sits where it should be.
    lock_root = storage.root / ".publication-locks"
    lock_root.write_text("not a directory")
    with (
        pytest.raises(RuntimeImagePreparationError),
        storage.publication_lock(ARCHIVE_DIGEST),
    ):
        pass

    with pytest.raises(RuntimeImagePreparationError):
        _atomic_json_replace(tmp_path / "missing-directory" / "x.receipt.json", {})

    # The failed owner leaves no lock behind; repaired storage admits a new
    # publication and a new receipt write through the same paths.
    lock_root.unlink()
    with storage.publication_lock(ARCHIVE_DIGEST):
        repaired = tmp_path / "missing-directory" / "x.receipt.json"
        repaired.parent.mkdir()
        _atomic_json_replace(repaired, {})
    assert json.loads(repaired.read_text()) == {}

    receipt = _prepare(storage=storage)
    assert storage.read_receipt(receipt.oci_archive_sha256) == receipt
