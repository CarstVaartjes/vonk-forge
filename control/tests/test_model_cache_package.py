"""Package boundaries and real recovery handoffs for the model-cache owner."""

# ruff: noqa: F811 - pytest fixture is imported by name
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from vonk_control.model_cache.input_contracts import FixtureArtifact
from vonk_control.operation_contract import AvailabilityOperationFailure

from .non_blocking import assert_ended_without_blocking
from .test_model_cache import (
    _artifact,
    _download,
    _gone_handler,
    _http_artifact,
    _http_cache_service,
    cache,  # noqa: F401 - shared service fixture
)


def test_package_concerns_cannot_grow_back_into_a_monolith():
    """Catches moving unrelated concerns back into one large implementation."""
    import vonk_control.model_cache as package

    root = Path(package.__file__).parent
    sizes = {
        path.name: len(path.read_text().splitlines()) for path in root.glob("*.py")
    }
    assert max(sizes.values()) <= 1000, sizes


def test_source_recovers_within_the_same_request(cache, tmp_path):
    """The old single observation raises on the first 503 instead of recovering."""
    _existing, sessions = cache
    handler, payload, served = _gone_handler([503, 200])
    service, client = _http_cache_service(tmp_path, sessions, handler)
    try:
        response = service._open_http_response(
            client, "https://example.test/weights.bin", {}
        )
        try:
            assert response.read() == payload
        finally:
            response.close()
        assert served["count"] == 2
    finally:
        service.close()
        client.close()


def test_lost_receipt_observation_recovers_in_the_same_read(
    cache, tmp_path, monkeypatch
):
    """A single stale observation must not refuse an object whose receipt returns."""
    service, _sessions = cache
    artifact = _artifact(tmp_path, b"verified bytes")
    operation = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-00000000c001",
    )
    original = service._object_is_available
    observations = 0

    def available(digest, size):
        nonlocal observations
        observations += 1
        return False if observations == 1 else original(digest, size)

    monkeypatch.setattr(service, "_object_is_available", available)
    path, size, digest = service.cached_artifact_file(
        str(operation.artifact_set_sha256), str(artifact["sha256"]), "weights.bin"
    )
    assert path.read_bytes() == b"verified bytes"
    assert size == len(b"verified bytes")
    assert digest == artifact["sha256"]
    assert observations >= 2


def test_gone_source_ends_without_holding_up_a_fresh_download(cache, tmp_path):
    """An ended source-gone attempt cannot retain a claim or poison a new request."""
    _existing, sessions = cache
    handler, payload, _served = _gone_handler([404] * 5 + [200])
    service, client = _http_cache_service(tmp_path, sessions, handler)
    artifact = _http_artifact(payload)
    try:
        operation = _download(
            service,
            [artifact],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-00000000c002",
        )

        def end(receipt):
            for _ in range(4):
                service.run_pending()
            return service.get_operation(receipt.id)

        def fresh(_world):
            preview = service.download_preview(
                model_content_sha256="b" * 64, artifacts=[artifact]
            )
            return service.start_download(
                actor="test",
                request_key="00000000-0000-4000-8000-00000000c003",
                plan_digest=str(preview["plan_digest"]),
                model_content_sha256="b" * 64,
                artifacts=[artifact],
            )

        def reason(receipt):
            assert receipt.failure is not None
            failure = AvailabilityOperationFailure.model_validate_json(
                json.dumps(receipt.failure)
            )
            assert not failure.retryable
            assert failure.retry_time is None

        _ended, admitted = assert_ended_without_blocking(
            SimpleNamespace(sessions=sessions),
            operation,
            end=end,
            fresh=fresh,
            assert_reason=reason,
        )
        service.run_pending()
        assert service.get_operation(admitted.id).state == "succeeded"
    finally:
        service.close()
        client.close()


def test_fixture_ingress_never_coerces_integrity_metadata(tmp_path):
    """The old string/int casts admit booleans and wrong scalar types."""
    artifact = _artifact(tmp_path, b"strict")
    for value in (True, "6"):
        with pytest.raises(ValidationError):
            FixtureArtifact.model_validate({**artifact, "download_bytes": value})
