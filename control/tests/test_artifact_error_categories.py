"""Artifact storage, gate and job errors carry their contract category."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    ErrorCategory,
    InvalidRequestReason,
    SecurityRefusalReason,
    WaitReason,
)
from vonk_control.artifact_blob_store import (
    ArtifactBlobDigestMismatch,
    ArtifactBlobInvalid,
    ArtifactBlobQuotaExhausted,
    ArtifactBlobStore,
    ArtifactBlobStoreError,
    ArtifactBlobUnavailable,
    ArtifactBlobUnsafePath,
)
from vonk_control.artifact_jobs import _translate_blob_error
from vonk_control.artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    ArtifactReferenceUnsettled,
    ArtifactRemovalFenceLost,
)


def _store(tmp_path: Path, **kwargs: int) -> ArtifactBlobStore:
    return ArtifactBlobStore(tmp_path / "blobs", **kwargs)


def test_upload_digest_mismatch_is_a_security_refusal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ArtifactBlobDigestMismatch) as caught:
        store.put_bytes("0" * 64, b"content", maximum_bytes=100)
    assert caught.value.category is ErrorCategory.SECURITY_REFUSAL
    assert isinstance(caught.value, ArtifactBlobStoreError)
    assert caught.value.typed_error() is not None


def test_oversized_upload_and_quota_are_invalid_requests(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"content").hexdigest()
    with pytest.raises(ArtifactBlobInvalid) as too_big:
        _store(tmp_path).put_bytes(digest, b"content", maximum_bytes=3)
    assert too_big.value.typed_reason is InvalidRequestReason.LIMIT_EXCEEDED
    with pytest.raises(ArtifactBlobQuotaExhausted) as quota:
        _store(tmp_path / "small", max_stored_bytes=3).put_bytes(
            digest, b"content", maximum_bytes=100
        )
    assert quota.value.category is ErrorCategory.INVALID_REQUEST
    assert isinstance(quota.value, ValueError)


def test_unsafe_storage_key_is_a_security_refusal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    digest = hashlib.sha256(b"x").hexdigest()
    with pytest.raises(ArtifactBlobUnsafePath) as caught:
        store.resolve("../escape", digest, 1)
    assert caught.value.typed_reason is SecurityRefusalReason.UNSAFE_PATH


def test_missing_bytes_are_unknown_and_still_file_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_bytes(hashlib.sha256(b"x").hexdigest(), b"x", maximum_bytes=10)
    digest = hashlib.sha256(b"y").hexdigest()
    with pytest.raises(ArtifactBlobUnavailable) as caught:
        store.resolve(f"{digest[:2]}/{digest}", digest, 1)
    assert caught.value.category is ErrorCategory.UNKNOWN
    assert isinstance(caught.value, FileNotFoundError)


def test_blob_failures_keep_their_category_through_the_job_service(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with pytest.raises(ArtifactBlobDigestMismatch) as digest_error:
        store.put_bytes("0" * 64, b"content", maximum_bytes=100)
    with pytest.raises(Exception) as translated:
        _translate_blob_error(digest_error.value)
    assert translated.value.category is ErrorCategory.SECURITY_REFUSAL  # type: ignore[attr-defined]
    assert translated.value.__cause__ is digest_error.value


def test_gate_busy_is_unknown_and_fence_loss_is_a_refusal() -> None:
    busy = ArtifactReferenceUnsettled("artifact.reference_busy", "busy", retryable=True)
    assert busy.category is ErrorCategory.UNKNOWN
    assert busy.typed_reason is WaitReason.OBSERVATION_UNAVAILABLE
    assert busy.retryable and isinstance(busy, ArtifactLifecycleError)
    lost = ArtifactRemovalFenceLost("artifact.deletion_fence_lost", "lost")
    assert lost.category is ErrorCategory.SECURITY_REFUSAL
    assert not lost.retryable and isinstance(lost, ArtifactLifecycleError)


def test_identity_misuse_is_an_invalid_value() -> None:
    with pytest.raises(ValueError) as caught:
        ArtifactIdentity("model-set", "not-a-digest")
    assert caught.value.category is ErrorCategory.INVALID_REQUEST  # type: ignore[attr-defined]
