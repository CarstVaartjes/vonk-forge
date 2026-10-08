"""Artifact ingress rejects invalid bytes and preserves the underlying failure."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from vonk_control.artifact_blob_store import (
    ArtifactBlobDigestMismatch,
    ArtifactBlobInvalid,
    ArtifactBlobQuotaExhausted,
    ArtifactBlobStore,
    ArtifactBlobUnsafePath,
)
from vonk_control.artifact_jobs import _translate_blob_error
from vonk_control.artifact_lifecycle import (
    ArtifactIdentity,
)


def _store(tmp_path: Path, **kwargs: int) -> ArtifactBlobStore:
    return ArtifactBlobStore(tmp_path / "blobs", **kwargs)


def test_upload_digest_mismatch_is_a_security_refusal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ArtifactBlobDigestMismatch):
        store.put_bytes("0" * 64, b"content", maximum_bytes=100)
    assert store.resolve("00/" + "0" * 64, "0" * 64, len(b"content")) is None


def test_oversized_upload_and_quota_are_invalid_requests(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"content").hexdigest()
    with pytest.raises(ArtifactBlobInvalid):
        _store(tmp_path).put_bytes(digest, b"content", maximum_bytes=3)
    with pytest.raises(ArtifactBlobQuotaExhausted) as quota:
        _store(tmp_path / "small", max_stored_bytes=3).put_bytes(
            digest, b"content", maximum_bytes=100
        )
    assert isinstance(quota.value, ValueError)


def test_unsafe_storage_key_is_a_security_refusal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    digest = hashlib.sha256(b"x").hexdigest()
    with pytest.raises(ArtifactBlobUnsafePath):
        store.resolve("../escape", digest, 1)


def test_missing_bytes_are_unknown_and_still_file_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_bytes(hashlib.sha256(b"x").hexdigest(), b"x", maximum_bytes=10)
    digest = hashlib.sha256(b"y").hexdigest()
    assert store.resolve(f"{digest[:2]}/{digest}", digest, 1) is None


def test_blob_failure_preserves_the_cause_through_the_job_service(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with pytest.raises(ArtifactBlobDigestMismatch) as digest_error:
        store.put_bytes("0" * 64, b"content", maximum_bytes=100)
    with pytest.raises(Exception) as translated:
        _translate_blob_error(digest_error.value)
    assert translated.value.__cause__ is digest_error.value


def test_identity_misuse_is_an_invalid_value() -> None:
    with pytest.raises(ValueError):
        ArtifactIdentity("model-set", "not-a-digest")
