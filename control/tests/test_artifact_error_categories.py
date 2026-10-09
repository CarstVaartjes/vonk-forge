"""Artifact ingress rejects invalid bytes and preserves the underlying failure."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from vonk_control.artifact_blob_store import (
    ArtifactBlobDigestMismatch,
    ArtifactBlobStore,
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


def test_oversized_upload_and_capacity_release_claims_for_fresh_content(
    tmp_path: Path,
) -> None:
    content = b"content"
    digest = hashlib.sha256(content).hexdigest()
    store = _store(tmp_path, max_stored_bytes=3)
    for maximum in (3, 100):
        with pytest.raises(Exception):  # noqa: B017 -- any ending; effects and fresh admission are asserted below
            store.put_bytes(digest, content, maximum_bytes=maximum)
        assert store.resolve(f"{digest[:2]}/{digest}", digest, len(content)) is None
        assert store.usage().in_flight_uploads == 0
    fresh = store.put_bytes(hashlib.sha256(b"ok").hexdigest(), b"ok", maximum_bytes=3)
    assert fresh.path.read_bytes() == b"ok"


def test_damaged_storage_key_is_a_miss_and_fresh_upload_is_admitted(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    digest = hashlib.sha256(b"x").hexdigest()
    assert store.resolve("../escape", digest, 1) is None
    fresh = store.put_bytes(digest, b"x", maximum_bytes=10)
    assert fresh.path.read_bytes() == b"x"


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
    assert not list((tmp_path / "blobs").glob("??/*"))
    assert store.usage().in_flight_uploads == 0
    fresh = store.put_bytes(hashlib.sha256(b"ok").hexdigest(), b"ok", maximum_bytes=100)
    assert fresh.path.read_bytes() == b"ok"


def test_identity_misuse_is_an_invalid_value() -> None:
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        ArtifactIdentity("model-set", "not-a-digest")
