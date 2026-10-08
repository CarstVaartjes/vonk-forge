"""The categorized errors keep their builtin and legacy bases and report a category."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_control import model_cache
from vonk_control.artifact_lifecycle import ArtifactIdentity, reserve_removal
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

from .test_model_cache import cache  # noqa: F401 - pytest fixture
from .test_model_cache_lifecycle import _queue


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


def test_model_cache_artifact_lifecycle_leaves_accept_retryable(
    cache,  # noqa: F811 - imported fixture
    tmp_path: Path,
) -> None:
    """A missing stored removal owner heals without refusing a fresh download."""
    service, sessions = cache
    accepted, _artifact = _queue(service, tmp_path, str(uuid4()))
    service.run_pending()
    assert service.get_operation(accepted.id).state == LifecycleState.SUCCEEDED
    assert accepted.artifact_set_sha256 is not None
    with sessions.begin() as session:
        scope = service._model_removal_scope_for_sets(
            session, (accepted.artifact_set_sha256,)
        )
        reserve_removal(
            session,
            (ArtifactIdentity("model-set", accepted.artifact_set_sha256),),
            owner_kind="model-cache-operation",
            owner_id=str(uuid4()),
            fence=str(uuid4()),
            now=datetime.now(UTC),
        )
    with (
        sessions() as session,
        pytest.raises(model_cache.ModelCacheRemovalOwnerInvalid) as caught,
    ):
        service.removal_owner_findings_in_session(session, scope)
    assert caught.value.retryable
    assert isinstance(caught.value, UnknownOutcomeError)
    assert caught.value.typed_reason is WaitReason.OBSERVATION_UNAVAILABLE
    assert service.reconcile_removal_gates() == 1
    fresh, _artifact = _queue(service, tmp_path, str(uuid4()))
    service.run_pending()
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED

    fence = model_cache.ModelCacheDeletionFenceLost(
        ArtifactLifecycleCode.DELETION_FENCE_LOST, "x", retryable=True
    )
    assert isinstance(fence, SecurityRefusalError)
    assert fence.retryable is True


def test_writer_busy_is_unknown_and_resumes() -> None:
    busy = model_cache._ArtifactWriterBusy("a" * 64)
    assert isinstance(busy, UnknownOutcomeError)
    assert busy.recovery == "resume"
    assert busy.retry_after_seconds == model_cache._RETRY_BASE_SECONDS


def _only(error: Exception, category: type[Exception]) -> None:
    others = {SecurityRefusalError, InvalidRequestError, UnknownOutcomeError} - {
        category
    }
    assert isinstance(error, category)
    assert not any(isinstance(error, other) for other in others)


def test_runtime_image_and_availability_leaves_keep_their_keywords() -> None:
    from vonk_control import (
        recipe_image_availability as availability,
    )
    from vonk_control import (
        runtime_image_preparation as preparation,
    )

    refused = preparation.RuntimeImagePreparationRefused(
        "runtime_image.digest_mismatch", "differs", retryable=False
    )
    _only(refused, SecurityRefusalError)
    assert isinstance(refused, preparation.RuntimeImagePreparationError)
    unknown = preparation.RuntimeImagePreparationUnknown(
        "runtime_image.lock_unavailable",
        "later",
        retryable=True,
        recovery_actions=("retry",),
        reason=WaitReason.OBSERVATION_UNAVAILABLE,
    )
    _only(unknown, UnknownOutcomeError)
    assert unknown.retryable is True
    assert unknown.recovery_actions == ("retry",)
    assert unknown.code == "runtime_image.lock_unavailable"
    lost = availability._AvailabilityClaimLost()
    _only(lost, UnknownOutcomeError)

    invalid = availability.RecipeImageAvailabilityInvalid(
        "recipe_image.request_key_reused",
        "reused",
        retryable=False,
        reason=InvalidRequestReason.CONFLICT,
    )
    _only(invalid, InvalidRequestError)
    assert isinstance(invalid, availability.RecipeImageAvailabilityError)
    assert invalid.retryable is False
    assert invalid.typed_reason is InvalidRequestReason.CONFLICT
    queue = availability._ModelQueueFailed(RuntimeError("down"), "queueing failed")
    _only(queue, UnknownOutcomeError)
    assert queue.retryable is True
    assert queue.code == "recipe_image.model_cache_unavailable"


def test_build_stop_and_admission_leaves() -> None:
    from vonk_control import recipe_build_cancellation, recipe_builds, run_admission
    from vonk_control import recipe_stop_payloads as stop

    refused = recipe_builds.RecipeBuildRefused("build.input_mismatch", "differs")
    _only(refused, SecurityRefusalError)
    assert refused.code == "build.input_mismatch"
    _only(
        recipe_builds.RecipeBuildInvalid("build.source_invalid", "x"),
        InvalidRequestError,
    )
    _only(recipe_builds.RecipeBuildAdmissionBusy(), UnknownOutcomeError)
    _only(
        recipe_build_cancellation.BuildConsumerError(
            "build.consumer_busy", "busy", retryable=True
        ),
        UnknownOutcomeError,
    )
    _only(
        stop.StopPayloadRefused("recipe Stop identity is stale"), SecurityRefusalError
    )
    _only(
        stop.StopPayloadInvalid("recipe Stop target set is empty"), InvalidRequestError
    )
    _only(
        stop.StopPayloadUnknown("recipe Start phases are missing"), UnknownOutcomeError
    )
    assert isinstance(stop.StopPayloadRefused("x"), stop.RecipeStopAuthorityError)
    _only(
        run_admission.RunAdmissionBusy("busy", code="run.capacity_busy"),
        UnknownOutcomeError,
    )
    plan = run_admission.RunPlanInvalid("run.plan_stale")
    _only(plan, InvalidRequestError)
    assert plan.typed_reason is InvalidRequestReason.CONFLICT


def test_run_switch_leaves_keep_the_definite_flag_and_the_legacy_conflict() -> None:
    from vonk_control import run_switch_operations as run_switch

    retry = run_switch.RunSwitchRetryLater("run-switch.phase-waiting")
    _only(retry, UnknownOutcomeError)
    assert isinstance(retry, run_switch.RunSwitchOperationConflict)
    assert retry.definite is False
    invalid = run_switch.RunSwitchRequestInvalid(
        "x", reason=InvalidRequestReason.SUPERSEDED
    )
    _only(invalid, InvalidRequestError)
    refused = run_switch.RunSwitchRefused(
        SecurityRefusalReason.RUN_SWITCH_ARTIFACT_DIGEST_VERIFICATION_FAILED.value
    )
    _only(refused, SecurityRefusalError)
    assert (
        refused.typed_reason
        is SecurityRefusalReason.RUN_SWITCH_ARTIFACT_DIGEST_VERIFICATION_FAILED
    )
    definite = run_switch._RunSwitchDefiniteConflict("x")
    _only(definite, InvalidRequestError)
    assert definite.definite is True
    pending = run_switch.RunSwitchPostStopEvidencePending("later")
    _only(pending, UnknownOutcomeError)
    owner = run_switch._RuntimeImageOwnerChanged("moved")
    _only(owner, UnknownOutcomeError)
    assert owner.typed_reason is WaitReason.SCOPE_CHANGED
    mismatch = run_switch._RuntimeImageIdentityMismatch("differs")
    _only(mismatch, SecurityRefusalError)


def test_recipe_operation_leaves_for_first_half_classes() -> None:
    from vonk_control import recipe_operations
    from vonk_control.distributed_lifecycle import (
        DistributedLifecycleError,
        DistributedRecoveryInvalid,
    )
    from vonk_control.install_admission import InstallAdmissionBusy

    busy = InstallAdmissionBusy(
        "install.capacity_busy", reason=WaitReason.OBSERVATION_UNAVAILABLE
    )
    assert isinstance(busy, InstallAdmissionBusy)
    assert busy.typed_error() is not None
    _only(busy, UnknownOutcomeError)
    superseded = DistributedRecoveryInvalid("newer intent")
    assert isinstance(superseded, DistributedLifecycleError)
    _only(superseded, InvalidRequestError)
    _only(recipe_operations._RouteNotWithdrawn("run"), UnknownOutcomeError)
