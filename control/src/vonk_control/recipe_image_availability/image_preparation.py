"""Image preparation for exact recipe image availability."""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    OperationProgress,
    RecipeImageCode,
    WaitReason,
)
from vonk_forge_contracts import RecipeDefinition

from .. import job_states
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from ..content_identity import same_image
from ..job_documents import (
    AvailabilityJobPayload,
    AvailabilityRuntime,
)
from ..models import (
    Job,
)
from ..operation_blockers import (
    OperationBlocker,
    bound_blockers,
)
from ..recipe_image_availability_contract import (
    AvailabilityBuildReceipt,
)
from ..runtime_image_preparation import (
    RuntimeImagePreparationRefused,
    RuntimeImageReceipt,
    RuntimeImageReferenceIntent,
    prepare_runtime_image,
)
from ..stored_json import Residue
from ..strict_json import serialize_json_value
from .contracts import (
    _LOGGER,
    SCHEMA_VERSION,
    BuildUnsettled,
    RecipeImageAvailabilityClaim,
    RecipeImageAvailabilityInvalid,
    RecipeImageAvailabilityUnknown,
    _AvailabilityClaimLost,
    _progress,
    _read,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def _prepare_claimed_image(
    self: RecipeImageAvailabilityService,
    claim: RecipeImageAvailabilityClaim,
    payload: AvailabilityJobPayload,
    recipe: RecipeDefinition,
    runtime: AvailabilityRuntime,
) -> RuntimeImageReceipt | BuildUnsettled:
    force_rebuild = payload.force_rebuild is True

    def persist_provisional_reference(
        receipt: RuntimeImageReceipt,
    ) -> None:
        self._persist_provisional_image_reference(
            claim,
            receipt=receipt,
        )

    if self._builder is None:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.BUILD_UNAVAILABLE,
            "no canonical recipe build executor is configured",
            reason=InvalidRequestReason.NOT_FOUND,
        )
    build_input_sha256 = payload.build_input_sha256
    dispatch_identity_missing = not isinstance(build_input_sha256, str)
    if dispatch_identity_missing:
        build_input_sha256 = ""
    if self._builder_admission is not None:
        self._builder_admission(recipe, runtime.model_dump(mode="json"))
    self._update_progress(claim, "build", total_bytes=None)

    def report(value: object) -> None:
        progress = _read(OperationProgress, value, subject=claim.operation_id)
        if progress is not None:
            self._update_progress(claim, progress.phase, detail=progress)

    # The builder re-resolves the exact executable identity and reuses
    # a verified filesystem receipt itself, so queue-time and
    # dispatch-time cache hits take the same path.
    build_receipt = self._builder(
        recipe,
        runtime.model_dump(mode="json"),
        claim=claim,
        build_input_sha256=build_input_sha256,
        force=force_rebuild,
        progress=report,
    )
    if isinstance(build_receipt, BuildUnsettled):
        return build_receipt
    receipt = _read(AvailabilityBuildReceipt, build_receipt, subject=claim.operation_id)
    if receipt is None:
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.BUILD_INVALID,
            "builder returned no receipt",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if dispatch_identity_missing:
        resolved_input = receipt.build_input_sha256
        if resolved_input is None:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.BUILD_INPUT_MISSING,
                "dispatch did not bind an exact build input identity",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        with self._sessions.begin() as session:
            operation = self._require_claim(session, claim)
            assigned = self._payload(operation)
            if isinstance(assigned, Residue):
                return BuildUnsettled(
                    RecipeImageCode.OPERATION_INVALID,
                    "availability bookkeeping is unknown",
                    WaitReason.OBSERVATION_UNAVAILABLE,
                    retryable=True,
                )
            runtime_update = {"build_input_sha256": resolved_input}
            if receipt.builder_node_id is not None:
                runtime_update["builder_node_id"] = receipt.builder_node_id
            assigned_runtime = assigned.runtime.model_copy(update=runtime_update)
            operation.payload = serialize_json_value(
                assigned.model_copy(
                    update={
                        "build_input_sha256": resolved_input,
                        "identity_key": resolved_input,
                        "runtime": assigned_runtime,
                    }
                )
            )
    self._update_progress(claim, "verify")
    return prepare_runtime_image(
        recipe.model_dump(mode="json"),
        runtime=runtime.model_dump(mode="json"),
        storage=self._storage,
        transport=self._transport,
        build_receipt=receipt.model_dump(mode="json"),
        now=self._clock(),
        before_publish=persist_provisional_reference,
    )


def _renew_claim_loop(
    self: RecipeImageAvailabilityService,
    claim: RecipeImageAvailabilityClaim,
    stop: threading.Event,
) -> None:
    interval = max(1.0, self._claim_lease_seconds / 3)
    while not stop.wait(timeout=interval):
        if not self._renew_claim(claim):
            return


def _renew_claim(
    self: RecipeImageAvailabilityService, claim: RecipeImageAvailabilityClaim
) -> bool:
    now = self._clock()
    now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    with self._sessions.begin() as session:
        operation = self._claim_operation(
            session,
            claim,
            allowed_states=job_states.words(
                LifecycleState.RUNNING, LifecycleState.OBSERVING
            ),
        )
        if operation is None:
            return False
        payload = self._payload(operation)
        if isinstance(payload, Residue):
            return False
        operation.payload = serialize_json_value(
            payload.model_copy(
                update={
                    "claim_until": now + timedelta(seconds=self._claim_lease_seconds),
                }
            )
        )
        operation.updated_at = now
        return True


def _persist_receipt(
    self: RecipeImageAvailabilityService,
    claim: RecipeImageAvailabilityClaim,
    receipt: RuntimeImageReceipt,
) -> None:
    with self._sessions.begin() as session:
        operation = self._require_claim(session, claim)
        operation_payload = self._payload(operation)
        if isinstance(operation_payload, Residue):
            return
        reference = self._image_reference_intent_for_claim(operation_payload, claim)
        if operation_payload.image_reference_intent is not None and reference is None:
            raise _AvailabilityClaimLost()
        try:
            require_reference_open(
                session,
                (ArtifactIdentity("runtime-image", receipt.oci_archive_sha256),),
                now=self._clock(),
            )
        except ArtifactLifecycleError as error:
            raise RuntimeImagePreparationRefused(
                error.code, error.detail, retryable=error.retryable
            ) from error
        operation.payload = serialize_json_value(
            operation_payload.model_copy(
                update={"image_result": receipt, "image_reference_intent": None}
            )
        )
        self._set_progress(
            operation,
            "available",
            total_bytes=receipt.image_bytes,
            completed_bytes=receipt.image_bytes,
        )
        operation.updated_at = self._clock()


def _persist_provisional_image_reference(
    self: RecipeImageAvailabilityService,
    claim: RecipeImageAvailabilityClaim,
    *,
    receipt: RuntimeImageReceipt,
) -> None:
    """Bind exact output identity before managed storage publication."""

    now = self._clock()
    reference = RuntimeImageReferenceIntent(
        schema_version=SCHEMA_VERSION,
        recipe_revision_id=claim.recipe_revision_id,
        oci_archive_sha256=receipt.oci_archive_sha256,
        image_digest=receipt.image_digest,
        image_bytes=receipt.image_bytes,
        operation_id=claim.operation_id,
        attempt=claim.execution_attempt,
        claim_owner=claim.claim_owner,
    )
    with self._sessions.begin() as session:
        operation = self._require_claim(
            session,
            claim,
            allowed_states=job_states.words(
                LifecycleState.RUNNING, LifecycleState.OBSERVING
            ),
        )
        try:
            require_reference_open(
                session,
                (ArtifactIdentity("runtime-image", receipt.oci_archive_sha256),),
                now=now,
            )
        except ArtifactLifecycleError as error:
            raise RuntimeImagePreparationRefused(
                error.code, error.detail, retryable=error.retryable
            ) from error
        payload = self._payload(operation)
        if isinstance(payload, Residue):
            return
        stored = serialize_json_value(payload)
        if stored != operation.payload:
            operation.payload = stored
            operation.updated_at = now
        existing = payload.image_reference_intent
        if existing is None:
            payload = payload.model_copy(update={"image_reference_intent": reference})
            operation.payload = serialize_json_value(payload)
            operation.updated_at = now
        else:
            existing_reference = existing
            if existing_reference != reference:
                if not same_image(existing_reference, reference):
                    _LOGGER.warning(
                        "availability operation %s now publishes archive %s "
                        "instead of %s",
                        reference.operation_id,
                        reference.oci_archive_sha256,
                        existing_reference.oci_archive_sha256,
                    )
                # The current attempt's output leads (only the current
                # claim reaches here). This callback runs under the exact
                # archive publication lock. The prior attempt can no
                # longer commit after this owner transfer; the exact bytes
                # remain protected without opening a gap between
                # provisional references.
                payload = payload.model_copy(
                    update={"image_reference_intent": reference}
                )
                operation.payload = serialize_json_value(payload)
                operation.updated_at = now


def _image_reference_intent_for_claim(
    payload: AvailabilityJobPayload, claim: RecipeImageAvailabilityClaim
) -> RuntimeImageReferenceIntent | None:
    raw_reference = payload.image_reference_intent
    if raw_reference is None:
        return None
    reference = raw_reference
    if not reference.belongs_to(
        operation_id=claim.operation_id,
        recipe_revision_id=claim.recipe_revision_id,
        attempt=claim.execution_attempt,
        claim_owner=claim.claim_owner,
    ):
        return None
    return reference


def _set_progress(
    self: RecipeImageAvailabilityService,
    operation: Job,
    phase: str,
    *,
    total_bytes: int | None = None,
    completed_bytes: int = 0,
    bytes_per_second: float | None = None,
    eta_seconds: float | None = None,
    detail: OperationProgress | None = None,
) -> None:
    progress = _progress(
        phase,
        total_bytes=total_bytes,
        completed_bytes=completed_bytes,
        bytes_per_second=bytes_per_second,
        eta_seconds=eta_seconds,
    )
    payload = self._payload(operation)
    if isinstance(payload, Residue):
        return
    payload = payload.model_copy(update={"progress": progress})
    operation.payload = serialize_json_value(payload)


def _update_progress(
    self: RecipeImageAvailabilityService,
    claim: RecipeImageAvailabilityClaim,
    phase: str,
    *,
    total_bytes: int | None = None,
    completed_bytes: int = 0,
    detail: OperationProgress | None = None,
) -> None:
    with self._sessions.begin() as session:
        operation = self._require_claim(session, claim)
        if detail is not None:
            completed_bytes = detail.completed_bytes
            total_bytes = detail.total_bytes
        payload = self._payload(operation)
        if isinstance(payload, Residue):
            return
        previous = payload.progress
        self._set_progress(
            operation,
            phase,
            total_bytes=total_bytes,
            completed_bytes=max(previous.completed_bytes, completed_bytes),
            bytes_per_second=detail.bytes_per_second if detail else None,
            eta_seconds=detail.eta_seconds if detail else None,
        )
        operation.updated_at = self._clock()


def _record_blockers(
    operation: Job,
    payload: AvailabilityJobPayload,
    blockers: Sequence[OperationBlocker],
) -> AvailabilityJobPayload:
    """Return the typed payload; log wait reasons only when they change."""
    before = {(item.code, tuple(item.node_ids)) for item in payload.blockers or []}
    stored = bound_blockers(blockers)
    after = {(item.code, tuple(item.node_ids)) for item in stored}
    if before != after and stored:
        _LOGGER.log(
            logging.WARNING
            if any(item.severity == "error" for item in stored)
            else logging.INFO,
            "recipe image preparation %s is waiting: %s",
            operation.id,
            "; ".join(f"{item.code}: {item.detail}" for item in stored[:4]),
        )
    return payload.model_copy(update={"blockers": stored})
