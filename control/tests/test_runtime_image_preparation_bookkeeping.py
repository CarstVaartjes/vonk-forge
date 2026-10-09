"""Runtime image storage: facts that no longer match are misses, storage faults retry."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import vonk_control.runtime_image_preparation as module
from vonk_agent_protocol import RuntimeImageCode
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
    RuntimeImagePreparationUnknown,
    _atomic_json_replace,
)

from .runtime_image_fixtures import place_test_image
from .test_runtime_image_preparation import (
    ARCHIVE,
    ARCHIVE_DIGEST,
    BUILT_IMAGE_DIGEST,
    TinyTransport,
    _document,
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
        pytest.raises(RuntimeImagePreparationError) as unavailable,
        storage.publication_lock(ARCHIVE_DIGEST),
    ):
        pass
    assert unavailable.value.code == RuntimeImageCode.LOCK_UNAVAILABLE
    assert unavailable.value.retryable is True

    with pytest.raises(RuntimeImagePreparationError) as unwritten:
        _atomic_json_replace(tmp_path / "missing-directory" / "x.receipt.json", {})
    assert unwritten.value.code == RuntimeImageCode.RECEIPT_WRITE_FAILED
    assert unwritten.value.retryable is True

    # The failed owner leaves no lock behind; repaired storage admits a new
    # publication and a new receipt write through the same paths.
    lock_root.unlink()
    with storage.publication_lock(ARCHIVE_DIGEST):
        repaired = tmp_path / "missing-directory" / "x.receipt.json"
        repaired.parent.mkdir()
        _atomic_json_replace(repaired, {})
    assert json.loads(repaired.read_text()) == {}


def test_unknown_storage_outcome_retries_by_default_but_keeps_explicit_end() -> None:
    """Catch a typed unknown inheriting the base error's terminal default."""
    unknown = RuntimeImagePreparationUnknown(
        RuntimeImageCode.RECEIPT_UNAVAILABLE, "receipt observation unavailable"
    )
    assert unknown.retryable is True
    ended = RuntimeImagePreparationUnknown(
        RuntimeImageCode.RECEIPT_UNAVAILABLE,
        "receipt observation budget exhausted",
        retryable=False,
    )
    assert ended.retryable is False


def _code_word(node: ast.expr) -> str | None:
    """The word of a literal or of a ``RuntimeImageCode.MEMBER`` argument."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "RuntimeImageCode"
    ):
        return RuntimeImageCode[node.attr].value
    return None


def test_every_raise_of_a_storage_fault_names_its_retry() -> None:
    """A storage-fault code never goes back to a terminal, non-retryable raise."""

    retryable_codes = {
        RuntimeImageCode.LOCK_UNAVAILABLE.value,
        RuntimeImageCode.RECEIPT_WRITE_FAILED.value,
        RuntimeImageCode.RECEIPT_PERSISTENCE_FAILED.value,
    }
    tree = ast.parse(
        "\n".join(
            path.read_text()
            for path in sorted(Path(module.__file__).parent.glob("*.py"))
        )
    )
    seen: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and node.exc.args
            and (word := _code_word(node.exc.args[0])) in retryable_codes
        ):
            assert word is not None
            seen.add(word)
            assert any(
                keyword.arg == "retryable"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.exc.keywords
            ), ast.unparse(node)
    assert seen == retryable_codes
