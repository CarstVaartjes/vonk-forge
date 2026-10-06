"""The categorized errors keep their builtin and legacy bases and report a category."""

from __future__ import annotations

import pytest
from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_control import model_cache
from vonk_control.categorized_errors import (
    BookkeepingUnknown,
    InvalidType,
    MissingRecord,
)
from vonk_control.categorized_faults import (
    OperationInterrupted,
    StoredStateKeyMissing,
    StoredStateTypeDamaged,
)


@pytest.mark.parametrize(
    ("error", "builtin", "category"),
    [
        (BookkeepingUnknown("x"), ValueError, UnknownOutcomeError),
        (StoredStateTypeDamaged("x"), TypeError, UnknownOutcomeError),
        (StoredStateKeyMissing("x"), KeyError, UnknownOutcomeError),
        (OperationInterrupted("x"), InterruptedError, UnknownOutcomeError),
        (InvalidType("x"), TypeError, InvalidRequestError),
        (MissingRecord("x"), KeyError, InvalidRequestError),
    ],
)
def test_builtin_faults_keep_their_builtin_and_one_category(
    error: Exception, builtin: type[Exception], category: type[Exception]
) -> None:
    assert isinstance(error, builtin)
    assert isinstance(error, category)
    others = {SecurityRefusalError, InvalidRequestError, UnknownOutcomeError} - {
        category
    }
    assert not any(isinstance(error, other) for other in others)


def test_stored_state_is_unknown_with_a_contract_reason() -> None:
    error = StoredStateTypeDamaged("persisted payload is invalid")
    typed = error.typed_error()
    assert typed is not None
    assert typed.reason is WaitReason.OBSERVATION_UNAVAILABLE


def test_model_cache_leaves_carry_their_category_base_and_keep_the_keywords() -> None:
    refused = model_cache.ModelCacheStorageRefused(
        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_DENIED.value,
        "denied",
        retry_after_seconds=7,
        recovery="resume",
    )
    assert isinstance(refused, SecurityRefusalError)
    assert isinstance(refused, model_cache.ModelCacheStorageError)
    assert refused.retry_after_seconds == 7
    assert refused.recovery == "resume"
    assert refused.typed_reason is SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_DENIED
    unnamed = model_cache.ModelCacheStorageRefused("model_cache.digest_mismatch", "x")
    assert unnamed.typed_error() is None

    invalid = model_cache.ModelCacheNotFoundInvalid("model_cache.entry_missing", "x")
    assert isinstance(invalid, InvalidRequestError)
    assert isinstance(invalid, model_cache.ModelCacheNotFound)
    assert invalid.typed_reason is InvalidRequestReason.NOT_FOUND

    unknown = model_cache.ModelCacheStorageUnknown(
        "model_cache.source_unavailable", "later", retry_after_seconds=5
    )
    assert isinstance(unknown, UnknownOutcomeError)
    assert unknown.retry_after_seconds == 5
    assert unknown.typed_reason is WaitReason.OBSERVATION_UNAVAILABLE


def test_model_cache_artifact_lifecycle_leaves_accept_retryable() -> None:
    owner = model_cache.ModelCacheRemovalOwnerInvalid(
        "artifact.removal_owner_invalid", "x", retryable=True
    )
    assert isinstance(owner, InvalidRequestError)
    assert owner.retryable is True
    fence = model_cache.ModelCacheDeletionFenceLost(
        "artifact.deletion_fence_lost", "x", retryable=True
    )
    assert isinstance(fence, SecurityRefusalError)
    assert fence.retryable is True


def test_writer_busy_is_unknown_and_resumes() -> None:
    busy = model_cache._ArtifactWriterBusy("a" * 64)
    assert isinstance(busy, UnknownOutcomeError)
    assert busy.recovery == "resume"
    assert busy.retry_after_seconds == model_cache._RETRY_BASE_SECONDS
