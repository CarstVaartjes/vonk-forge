"""Unrelated unavailable metadata must not obstruct request-led preparation."""

from pathlib import Path

import pytest
from vonk_agent_protocol import SecurityRefusalReason
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationRefused,
)

from .test_runtime_image_preparation import (
    ARCHIVE_DIGEST,
    BUILT_IMAGE_DIGEST,
    _prepare,
)


def test_unreadable_unrelated_receipt_is_a_miss_then_fresh_preparation_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path)
    stale = storage.root / f"{'0' * 64}.receipt.json"
    stale.write_text("{}")
    original = Path.read_text

    def unavailable(path: Path, *args, **kwargs):
        if path == stale:
            raise OSError("temporarily unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unavailable)
    assert (
        storage.find_verified(
            BUILT_IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    for archive_sha256 in (None, ARCHIVE_DIGEST):
        assert (
            storage.find_build(
                "a" * 64,
                expected_architecture="linux/arm64",
                expected_runtime_interface="vonk.runtime.v1",
                expected_archive_sha256=archive_sha256,
            )
            is None
        )
    receipt = _prepare(storage=storage)
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt


def test_preparation_does_not_swallow_denied_receipt_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path)

    def denied(_digest: str):
        raise RuntimeImagePreparationRefused(
            SecurityRefusalReason.PERMISSION_DENIED,
            "receipt access denied",
            reason=SecurityRefusalReason.PERMISSION_DENIED,
        )

    monkeypatch.setattr(storage, "read_receipt", denied)
    with pytest.raises(RuntimeImagePreparationRefused):
        _prepare(storage=storage)


def test_missing_exact_build_receipt_is_a_lookup_miss(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path)
    assert (
        storage.find_build(
            "a" * 64,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
            expected_archive_sha256=ARCHIVE_DIGEST,
        )
        is None
    )


def test_unknown_receipt_attempt_ends_without_blocking_fresh_preparation(
    tmp_path: Path,
) -> None:
    from vonk_agent_protocol import LifecycleState, RuntimeImageCode, WaitReason
    from vonk_agent_protocol.wire_model import WireModel
    from vonk_control.runtime_image_preparation import RuntimeImagePreparationUnknown

    from .non_blocking import assert_ended_without_blocking

    class AttemptReceipt(WireModel):
        request_key: str
        state: LifecycleState
        reason_code: RuntimeImageCode | None = None

    storage = FilesystemRuntimeImageStorage(tmp_path)
    original = AttemptReceipt(
        request_key="missing-receipt", state=LifecycleState.QUEUED
    )

    def end(_operation: AttemptReceipt) -> AttemptReceipt:
        with pytest.raises(RuntimeImagePreparationUnknown) as missing:
            storage.read_receipt(ARCHIVE_DIGEST)
        assert missing.value.retryable
        assert missing.value.typed_reason == WaitReason.RECEIPT_MISSING
        return AttemptReceipt(
            request_key=original.request_key,
            state=LifecycleState.FAILED,
            reason_code=RuntimeImageCode.RECEIPT_UNAVAILABLE,
        )

    def fresh(_world: object) -> AttemptReceipt:
        receipt = _prepare(storage=storage)
        assert storage.read_receipt(ARCHIVE_DIGEST) == receipt
        return AttemptReceipt(
            request_key="fresh-preparation", state=LifecycleState.SUCCEEDED
        )

    def released() -> None:
        with storage.publication_lock(ARCHIVE_DIGEST):
            pass

    assert_ended_without_blocking(
        storage, original, end=end, fresh=fresh, assert_released=released
    )
