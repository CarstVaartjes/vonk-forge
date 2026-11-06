"""A busy-owner 409 names its code and tells the caller to retry."""

from __future__ import annotations

from vonk_control.model_cache.errors import (
    ModelCacheConflictInvalid,
    ModelCacheConflictUnknown,
)
from vonk_control.model_cache_api import model_cache_http_error

from cluster_profiles.control_client.errors import ControlHTTPError


def test_busy_owner_conflict_carries_code_and_retry() -> None:
    reply = model_cache_http_error(
        ModelCacheConflictUnknown("artifact.deletion_busy", "has another removal owner")
    )

    assert reply.status_code == 409
    assert reply.headers is not None
    assert reply.headers["x-vonk-error-code"] == "artifact.deletion_busy"
    after = int(reply.headers["retry-after"])
    assert after > 0
    client_error = ControlHTTPError(409, str(reply.detail), after, code="x")
    assert client_error.retryable is True


def test_invalid_conflict_stays_terminal() -> None:
    reply = model_cache_http_error(ModelCacheConflictInvalid("model.bad", "no"))

    assert "retry-after" not in (reply.headers or {})
    assert ControlHTTPError(409, "no", None).retryable is False
