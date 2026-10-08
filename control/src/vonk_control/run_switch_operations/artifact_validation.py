"""Artifact validation."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ValidationError
from vonk_agent_protocol import (
    RunSwitchCode,
    SecurityRefusalReason,
    WaitReason,
    canonical_message,
)

from ..content_identity import ImageContent, differing_image_fields
from ..preparation_contract import (
    RuntimeImageIdentity,
)
from ..run_switch_contract import (
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchVerifyResult,
)
from ..run_switch_observation_contract import (
    RunSwitchArtifactGuardEvidence,
    RunSwitchObservedImageIdentity,
)
from .errors import RunSwitchRefused, RunSwitchRetryLater
from .image_receipts import _require_profile_runtime_image
from .result_helpers import _phase_result


def _validate_artifact_execution(
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
    result: object,
    *,
    expected_image: RuntimeImageIdentity | None = None,
) -> None:
    """Enforce evidence required from an injected artifact phase adapter."""

    raw_result = (
        result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    )
    invalid_cause: Exception | None = None
    try:
        evidence = (
            RunSwitchArtifactGuardEvidence.model_validate_json(
                canonical_message(raw_result), strict=True
            )
            if isinstance(raw_result, Mapping)
            else None
        )
    except (TypeError, ValueError) as error:
        invalid_cause = error
        evidence = None
    if evidence is None:
        raise RunSwitchRetryLater(
            RunSwitchCode.RECEIPT_INVALID,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from invalid_cause
    if phase.kind == "prepare" and phase.subphase == "runtime-image":
        receipt = evidence.runtime_image
        if receipt is None:
            raise RunSwitchRetryLater(
                RunSwitchCode.RUNTIME_IMAGE_PREPARATION_RECEIPT_INVALID,
                reason=WaitReason.RECEIPT_MISSING,
            )
        image_digest = receipt.image_digest
        layout_digest = receipt.oci_archive_sha256
        if expected_image is not None:
            _require_profile_runtime_image(
                expected_image,
                RunSwitchObservedImageIdentity(
                    image_digest=image_digest,
                    oci_layout_sha256=layout_digest,
                    image_bytes=receipt.image_bytes,
                    architecture=receipt.architecture,
                    runtime_interface=receipt.runtime_interface,
                ),
            )
        differing = differing_image_fields(
            ImageContent(
                image_digest=plan.image_digest,
                archive_sha256=plan.build.oci_layout_sha256,
            ),
            receipt,
        )
        if "image_digest" in differing:
            raise RunSwitchRefused(
                SecurityRefusalReason.RUN_SWITCH_RUNTIME_IMAGE_PREPARATION_DIGEST_MISMATCH.value
            )
        if "archive_sha256" in differing:
            raise RunSwitchRetryLater(
                RunSwitchCode.RUNTIME_IMAGE_PREPARATION_LAYOUT_MISMATCH,
                reason=WaitReason.SCOPE_CHANGED,
            )
        return
    if phase.kind == "transfer" and phase.subphase == "model-download":
        preparation = plan.preparation
        expected_set = (
            preparation.model.artifact_set_sha256
            if preparation is not None
            else plan.storage.artifact_set_sha256
        )
        if evidence.artifact_set_sha256 != expected_set:
            raise RunSwitchRetryLater(
                RunSwitchCode.MODEL_DOWNLOAD_ARTIFACT_SET_MISMATCH,
                reason=WaitReason.SCOPE_CHANGED,
            )
        if evidence.coverage != "complete":
            raise RunSwitchRetryLater(RunSwitchCode.MODEL_DOWNLOAD_COVERAGE_INCOMPLETE)
        expected_bytes = (
            preparation.model.artifact_set_bytes
            if preparation is not None
            else plan.storage.artifact_set_bytes
        )
        completed = evidence.downloaded_bytes
        total = evidence.total_bytes
        planned_transfer = plan.storage.missing_nas_bytes
        if planned_transfer is None:
            # An unknown plan total must remain unknown all the way through a
            # terminal child result; a full manifest size is not a valid
            # substitute for a missing transfer estimate.
            valid_bytes = completed is None and total is None
        else:
            valid_bytes = (
                type(expected_bytes) is int
                and type(completed) is int
                and type(total) is int
                and completed >= 0
                and total >= 0
                and completed == total == planned_transfer
                and total <= expected_bytes
            )
        if not valid_bytes:
            raise RunSwitchRetryLater(
                RunSwitchCode.MODEL_DOWNLOAD_BYTE_EVIDENCE_MISMATCH,
                reason=WaitReason.SCOPE_CHANGED,
            )
        return
    if phase.kind == "verify":
        try:
            verification = RunSwitchVerifyResult.model_validate(
                _phase_result(result, phase=phase), strict=True
            )
        except (TypeError, ValidationError, RunSwitchRetryLater) as error:
            if (
                plan.recipe_build_id is not None
                and isinstance(raw_result, Mapping)
                and evidence.verified_build_id != plan.recipe_build_id
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.RUNTIME_BUILD_VERIFICATION_MISMATCH,
                    reason=WaitReason.SCOPE_CHANGED,
                ) from error
            raise RunSwitchRetryLater(
                RunSwitchCode.ARTIFACT_VERIFICATION_RESULT_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        if verification.verified is not True:
            raise RunSwitchRefused(
                SecurityRefusalReason.RUN_SWITCH_ARTIFACT_DIGEST_VERIFICATION_FAILED.value
            )
        if verification.verified_build_id != plan.recipe_build_id:
            # A build performed by the same high-level operation has no OCI
            # output digest at preview time.  The distribution adapter must
            # bind its verification receipt to the exact durable build row.
            raise RunSwitchRetryLater(
                RunSwitchCode.RUNTIME_BUILD_VERIFICATION_MISMATCH,
                reason=WaitReason.SCOPE_CHANGED,
            )
    elif phase.kind == "cleanup":
        if evidence.scope != "spark-local":
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_SCOPE_INVALID, reason=WaitReason.SCOPE_CHANGED
            )
        if evidence.nas_evicted is True:
            raise RunSwitchRefused(
                SecurityRefusalReason.RUN_SWITCH_CLEANUP_NAS_EVICTION_FORBIDDEN.value
            )
        reclaimed = evidence.reclaimed_bytes
        if type(reclaimed) is not int or reclaimed < 0:
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_RECLAIM_EVIDENCE_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        protected_bytes = evidence.protected_referenced_bytes
        if type(protected_bytes) is not int or protected_bytes < 0:
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_REFERENCE_PROTECTION_EVIDENCE_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        raw_reclaimed = evidence.reclaimed_digests
        raw_protected = evidence.protected_digests
        if (
            not isinstance(raw_reclaimed, list)
            or not isinstance(raw_protected, list)
            or len(set(raw_reclaimed)) != len(raw_reclaimed)
            or len(set(raw_protected)) != len(raw_protected)
        ):
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_REFERENCE_PROTECTION_EVIDENCE_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        reclaimed_digests = set(raw_reclaimed)
        protected_digests = set(raw_protected)
        if reclaimed_digests & protected_digests:
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_REFERENCE_PROTECTION_OVERLAP
            )
        allowed_reclaimable = set(plan.storage.reclaimable_digests)
        allowed_reclaimable.update(plan.runtime_storage.reclaimable_digests)
        if not reclaimed_digests <= allowed_reclaimable:
            raise RunSwitchRefused(
                SecurityRefusalReason.RUN_SWITCH_CLEANUP_RECLAIMED_DIGEST_NOT_PLANNED.value
            )
        maximum_reclaimable = (
            plan.storage.reclaimable_bytes + plan.runtime_storage.reclaimable_bytes
        )
        if reclaimed > maximum_reclaimable:
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_RECLAIMED_BYTES_EXCEED_PLAN,
                reason=WaitReason.STALE_PLAN,
            )
    elif phase.kind == "transfer":
        copied = evidence.copied_bytes
        if copied is not None and (type(copied) is not int or copied < 0):
            raise RunSwitchRetryLater(
                RunSwitchCode.TRANSFER_BYTE_EVIDENCE_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
