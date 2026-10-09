"""Artifact reference scan: intents."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    WaitReason,
    canonical_message,
)

from ..artifact_lifecycle import (
    ArtifactReferenceUnverified,
)
from ..content_identity import ImageContent, differing_image_fields
from ..fleet_profile_contract import (
    FleetProfilePreview,
)
from ..models import (
    Job,
)
from ..run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchPlan,
    RunSwitchRuntimeImageReferenceIntent,
)
from ..strict_json import read_stored_model


def _profile_plan(value: object) -> FleetProfilePreview:
    try:
        return read_stored_model(
            FleetProfilePreview,
            canonical_message(value),
            strict=True,
            from_json=True,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ArtifactReferenceUnverified(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
            "accepted profile plan is malformed; removal was deferred",
            retryable=True,
        ) from error


def _run_switch_plan(payload: Mapping[str, object]) -> RunSwitchPlan:
    try:
        return read_stored_model(
            RunSwitchPlan,
            canonical_message(payload.get("plan")),
            strict=True,
            from_json=True,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ArtifactReferenceUnverified(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
            "accepted Run/Switch plan is malformed; removal was deferred",
            retryable=True,
        ) from error


def _stated(value: int | None) -> int | None:
    return value if value else None


def _approved_images(plan: RunSwitchPlan) -> tuple[ImageContent, ...]:
    """What the plan approved about the image, per source. Unstated is absent."""

    return (
        ImageContent(image_digest=plan.image_digest),
        ImageContent(
            image_digest=plan.runtime_storage.image_digest,
            archive_sha256=plan.runtime_storage.oci_layout_sha256,
            image_bytes=_stated(plan.runtime_storage.image_bytes),
        ),
        ImageContent(
            image_digest=plan.build.image_digest,
            archive_sha256=plan.build.oci_layout_sha256,
            image_bytes=_stated(plan.build.image_bytes),
        ),
    )


def _run_switch_runtime_image_intent(
    operation: Job, plan: RunSwitchPlan
) -> RunSwitchRuntimeImageReferenceIntent | None:
    """Read one active job's strict, exact prepublication artifact reference."""

    if operation.result is None:
        return None
    try:
        result = read_stored_model(
            RunSwitchOperationResult,
            canonical_message(operation.result),
            strict=True,
            from_json=True,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ArtifactReferenceUnverified(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
            "active RunSwitch progress is malformed; image removal was deferred",
            retryable=True,
        ) from error
    intent = result.runtime_image_reference_intent
    if intent is None:
        return None
    phase = (
        plan.phases[intent.phase_index]
        if intent.phase_index < len(plan.phases)
        else None
    )
    target_nodes = tuple(sorted(node.node_id for node in plan.spark_group.nodes))
    expected_build_id = plan.recipe_build_id or plan.build.build_id
    valid = (
        operation.kind == "recipe.run-switch.v2"
        and intent.owner_kind == "run-switch-job"
        and intent.operation_id == operation.id
        and intent.request_key == operation.request_id
        and intent.actor == operation.actor
        and intent.plan_digest == operation.payload.get("plan_digest")
        and intent.plan_digest == plan.plan_digest
        and tuple(sorted(operation.targets)) == target_nodes
        and operation.payload.get("workload_intent_ordinal")
        == intent.workload_intent_ordinal
        and result.workload_intent_ordinal == intent.workload_intent_ordinal
        and result.profile_application_id == intent.profile_application_id
        and intent.recipe_revision_id == plan.recipe_revision_id
        and phase is not None
        and phase.kind == "prepare"
        and phase.subphase == "runtime-image"
        and intent.item_index == 0
        and intent.build_id == expected_build_id
        and intent.build_input_sha256 == plan.build.build_input_sha256
        and not any(
            differing_image_fields(approved, intent)
            for approved in _approved_images(plan)
        )
    )
    if not valid:
        raise ArtifactReferenceUnverified(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
            "active RunSwitch image reference does not match its owner plan; image removal was deferred",
            retryable=True,
            reason=WaitReason.SCOPE_CHANGED,
        )
    return intent


def _run_switch_kinds() -> frozenset[str]:
    from ..run_switch_operations import _OPERATION_KINDS

    return frozenset(_OPERATION_KINDS)
