"""Image receipts."""

from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    ProfileReasonCode,
    RunSwitchCode,
    WaitReason,
)

from ..content_identity import differing_image_fields
from ..models import (
    RecipeBuild,
)
from ..preparation_contract import (
    RuntimeImageIdentity,
)
from ..run_switch_contract import (
    RunSwitchContainerBuildResult,
    RunSwitchOperationResult,
    RunSwitchPlan,
)
from ..run_switch_observation_contract import (
    RunSwitchEffectiveBuildReceipt,
    RunSwitchObservedImageIdentity,
)
from .errors import RunSwitchRetryLater
from .identity_helpers import _is_hex_digest, _is_oci_digest, _node_missing_bytes


def _planned_transfer_bytes(
    plan: RunSwitchPlan,
) -> tuple[int | None, dict[str, int | None]]:
    """Return the exact transfer envelope represented by the persisted plan.

    The model and OCI image are independent preparation inputs.  A missing
    byte count on either input keeps the aggregate unknown; silently treating
    an unknown cache manifest as zero would make the progress contract lie.
    """

    node_ids = list(_plan_target_node_ids(plan))
    if plan.action == "stop":
        return 0, {node_id: 0 for node_id in node_ids}
    model_download_bytes = plan.storage.missing_nas_bytes
    model_bytes = plan.storage.missing_spark_bytes
    image_bytes = plan.runtime_storage.missing_image_distribution_bytes
    target_bytes = (
        model_bytes + image_bytes
        if model_bytes is not None and image_bytes is not None
        else None
    )
    total = (
        model_download_bytes + target_bytes
        if model_download_bytes is not None and target_bytes is not None
        else None
    )
    per_node: dict[str, int | None] = {}
    for node_id in node_ids:
        model_each = _node_missing_bytes(
            plan.storage.missing_spark_bytes_by_node,
            node_id,
            model_bytes,
            len(node_ids),
        )
        image_each = _node_missing_bytes(
            plan.runtime_storage.missing_image_distribution_bytes_by_node,
            node_id,
            image_bytes,
            len(node_ids),
        )
        per_node[node_id] = (
            model_each + image_each
            if model_each is not None and image_each is not None
            else None
        )
    return total, per_node


def _plan_target_node_ids(plan: RunSwitchPlan) -> tuple[str, ...]:
    """Return physical targets while retaining the full reviewed topology."""

    if plan.profile_stop_scope is not None:
        return tuple(plan.profile_stop_scope.target_node_ids)
    return tuple(node.node_id for node in plan.spark_group.nodes)


def _planned_transfer_parts(
    plan: RunSwitchPlan,
) -> tuple[int | None, int | None, int | None]:
    """Return Controller model download, target copy, and aggregate bytes."""

    model_download = plan.storage.missing_nas_bytes
    model_copy = plan.storage.missing_spark_bytes
    image_copy = plan.runtime_storage.missing_image_distribution_bytes
    target_copy = (
        model_copy + image_copy
        if model_copy is not None and image_copy is not None
        else None
    )
    aggregate = (
        model_download + target_copy
        if model_download is not None and target_copy is not None
        else None
    )
    return model_download, target_copy, aggregate


def effective_build_receipt(
    plan: RunSwitchPlan,
    progress: RunSwitchOperationResult,
) -> RunSwitchEffectiveBuildReceipt | None:
    """Select the exact durable build output without changing the reviewed plan."""
    expected_build_id = plan.recipe_build_id
    expected_input = plan.build.build_input_sha256
    candidates: list[RunSwitchEffectiveBuildReceipt] = []
    planned_image = plan.image_digest
    planned_layout = plan.build.oci_layout_sha256
    planned_bytes = plan.build.image_bytes
    if (
        planned_image is not None
        and planned_layout is not None
        and planned_bytes is not None
    ):
        try:
            candidates.append(
                RunSwitchEffectiveBuildReceipt(
                    build_id=expected_build_id,
                    build_input_sha256=expected_input,
                    image_digest=planned_image,
                    oci_layout_sha256=planned_layout,
                    image_bytes=planned_bytes,
                )
            )
        except ValidationError:
            pass
    for result in reversed(progress.phase_results):
        if (
            not isinstance(result, RunSwitchContainerBuildResult)
            or result.state != LifecycleState.SUCCEEDED.value
        ):
            continue
        image_digest = result.image_digest
        layout_digest = result.oci_layout_sha256
        image_bytes = result.image_bytes
        if image_digest is None or layout_digest is None or image_bytes is None:
            continue
        candidates.append(
            RunSwitchEffectiveBuildReceipt(
                build_id=result.build_id,
                build_input_sha256=result.build_input_sha256,
                image_digest=image_digest,
                oci_layout_sha256=layout_digest,
                image_bytes=image_bytes,
            )
        )
    for result in candidates:
        if expected_input is not None and result.build_input_sha256 not in {
            None,
            expected_input,
        }:
            continue
        return result
    return None


def _require_profile_runtime_image(
    expected: RuntimeImageIdentity, observed: RunSwitchObservedImageIdentity
) -> None:
    """Observe a projection without interpreting damage as changed consent.

    This record is not an ingress verification of bytes. The phase owner
    re-reads its managed image on a retry; its immutable request deadline ends
    unresolved observation without changing the accepted image or live routes.
    """
    changes = differing_image_fields(expected, observed)
    if changes:
        raise RunSwitchRetryLater(
            f"{ProfileReasonCode.RUNTIME_IMAGE_CHANGED}: " + ", ".join(changes),
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )


def _build_receipt_in_session(
    session: Session,
    plan: RunSwitchPlan,
    *,
    expected_image: RuntimeImageIdentity | None = None,
) -> RunSwitchContainerBuildResult:
    """Read and validate the successful build receipt for a pending plan."""

    build_id = plan.recipe_build_id or plan.build.build_id
    if build_id is None:
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_RECEIPT_UNAVAILABLE,
            reason=WaitReason.RECEIPT_MISSING,
        )
    build = session.get(RecipeBuild, build_id)
    if (
        build is None
        or build.state != LifecycleState.SUCCEEDED.value
        or build.build_input_sha256 != plan.build.build_input_sha256
        or not _is_oci_digest(build.image_digest)
        or not _is_hex_digest(build.oci_layout_sha256)
        or type(build.image_bytes) is not int
        or build.image_bytes < 1
    ):
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_RECEIPT_UNAVAILABLE,
            reason=WaitReason.RECEIPT_MISSING,
        )
    if expected_image is not None:
        _require_profile_runtime_image(
            expected_image,
            RunSwitchObservedImageIdentity(
                build_id=build.id,
                image_digest=build.image_digest,
                oci_layout_sha256=build.oci_layout_sha256,
                image_bytes=build.image_bytes,
            ),
        )
    return RunSwitchContainerBuildResult(
        phase="prepare",
        subphase="container-build",
        build_id=build.id,
        build_input_sha256=build.build_input_sha256,
        image_digest=build.image_digest,
        oci_layout_sha256=build.oci_layout_sha256,
        image_bytes=build.image_bytes,
        state=LifecycleState.SUCCEEDED.value,
    )
