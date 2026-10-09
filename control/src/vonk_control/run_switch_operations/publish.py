"""Image publication."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    LifecycleState,
)

from .. import job_states
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from ..content_identity import ImageContent, differing_image_fields
from ..models import (
    AgentNode,
)
from ..profile_capacity import (
    accepted_profile_runtime_image,
)
from ..run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchRuntimeImageReferenceIntent,
)
from ..run_switch_observation_contract import (
    RunSwitchObservedImageIdentity,
)
from ..runtime_image_preparation import (
    RuntimeImagePreparationUnknown,
)
from ..runtime_image_preparation import (
    RuntimeImageReceipt as RuntimeImageReceiptDocument,
)
from ..stored_json import read_row_column
from ..strict_json import (
    read_stored_model,
)
from .errors import (
    RunSwitchOperationConflict,
    _RuntimeImageIdentityUnknown,
    _RuntimeImageOwnerChanged,
)
from .identity_helpers import _stated_bytes, _string_or_none
from .image_receipts import _require_profile_runtime_image
from .ownership import _lock_phase_owner
from .planning_helpers import _now, _run_switch_payload, _stored_job_plan
from .result_helpers import (
    _bound_workload_intent,
    _persisted_result,
    _progress_damaged,
    _read_progress,
)


def _persist_run_switch_runtime_image_reference(
    sessions: sessionmaker[Session],
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
    *,
    item_index: int,
    actor: str,
    request_key: str,
    progress: RunSwitchOperationResult,
    execution_keys: Sequence[str],
    receipt: RuntimeImageReceiptDocument,
    clock: Callable[[], datetime],
) -> None:
    """Commit this current phase's exact image reference before publication.

    The caller holds the nonblocking lock for ``receipt.oci_archive_sha256``.
    This transaction rechecks the durable RunSwitch checkpoint and current
    workload claim, then commits the reference intent while that lock is
    still held. It never waits for image work or storage from inside SQL.
    """
    from .retry_holds import RetryHoldsMixin

    try:
        parsed_receipt = read_stored_model(
            RuntimeImageReceiptDocument, receipt, strict=True
        )
    except (TypeError, ValueError) as error:
        raise _RuntimeImageIdentityUnknown(
            "verified runtime image receipt is invalid"
        ) from error
    try:
        ordinal = _bound_workload_intent(progress)
    except RunSwitchOperationConflict as error:
        raise _RuntimeImageIdentityUnknown(
            "RunSwitch phase workload claim observation is unavailable"
        ) from error
    profile_application_id = _string_or_none(progress.profile_application_id)
    target_nodes = tuple(sorted(node.node_id for node in plan.spark_group.nodes))
    recipe_revision_id = plan.recipe_revision_id
    if (
        phase.kind != "prepare"
        or phase.subphase != "runtime-image"
        or item_index != 0
        or phase.index >= len(plan.phases)
        or plan.phases[phase.index] != phase
        or not target_nodes
        or not execution_keys
        or recipe_revision_id is None
    ):
        raise _RuntimeImageIdentityUnknown(
            "runtime image callback is outside its phase"
        )
    canonical_execution_keys = tuple(sorted(set(execution_keys)))
    if len(canonical_execution_keys) != len(execution_keys):
        raise _RuntimeImageIdentityUnknown(
            "runtime image execution identities are not unique"
        )

    now = _now(clock)
    with sessions.begin() as session:
        try:
            nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(target_nodes))
                    .order_by(AgentNode.node_id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            )
        except DBAPIError as error:
            state = getattr(error.orig, "sqlstate", None) or getattr(
                error.orig, "pgcode", None
            )
            if state == "42501" or (isinstance(state, str) and state.startswith("28")):
                raise
            raise RuntimeImagePreparationUnknown(
                ArtifactLifecycleCode.REFERENCE_BUSY,
                "RunSwitch target ownership is changing; image publication will retry",
                retryable=True,
            ) from error
        if len(nodes) != len(target_nodes) or any(
            node.state != "active" and node.revoked_at is None for node in nodes
        ):
            raise _RuntimeImageIdentityUnknown(
                "RunSwitch target observation is unavailable"
            )
        if any(
            node.revoked_at is not None or node.workload_intent_ordinal != ordinal
            for node in nodes
        ):
            raise _RuntimeImageOwnerChanged(
                "RunSwitch Spark scope or workload claim changed"
            )

        try:
            require_reference_open(
                session,
                (ArtifactIdentity("runtime-image", parsed_receipt.oci_archive_sha256),),
                now=now,
            )
        except ArtifactLifecycleError as error:
            raise RuntimeImagePreparationUnknown(
                error.code, error.detail, retryable=error.retryable
            ) from error

        try:
            job = _lock_phase_owner(session, request_key, phase.index, item_index)
        except DBAPIError as error:
            state = getattr(error.orig, "sqlstate", None) or getattr(
                error.orig, "pgcode", None
            )
            if state == "42501" or (isinstance(state, str) and state.startswith("28")):
                raise
            raise RuntimeImagePreparationUnknown(
                ArtifactLifecycleCode.REFERENCE_BUSY,
                "RunSwitch operation ownership is changing; image publication will retry",
                retryable=True,
            ) from error
        if (
            job is None
            or job.kind != "recipe.run-switch.v2"
            or job.actor != actor
            # Background preparation publishes while its operation waits on
            # it; the exact checkpoint below still binds ownership.
            or job.state
            not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.OBSERVING
            )
            or tuple(sorted(job.targets)) != target_nodes
            or (payload := _run_switch_payload(job)) is None
            or payload.workload_intent_ordinal != ordinal
            or payload.plan_digest != plan.plan_digest
        ):
            raise _RuntimeImageOwnerChanged(
                "RunSwitch operation no longer owns image publication"
            )
        persisted_plan = _stored_job_plan(job)
        if persisted_plan is None:
            raise _RuntimeImageIdentityUnknown("persisted RunSwitch plan is invalid")
        if persisted_plan != plan:
            raise _RuntimeImageIdentityUnknown(
                "RunSwitch plan changed before image publication"
            )

        if _progress_damaged(read_row_column(job, "result")):
            raise _RuntimeImageIdentityUnknown(
                "RunSwitch progress no longer owns image publication"
            )
        try:
            current = _read_progress(read_row_column(job, "result"))
            current_ordinal = _bound_workload_intent(current)
        except RunSwitchOperationConflict as error:
            raise _RuntimeImageIdentityUnknown(
                "RunSwitch workload claim observation is unavailable"
            ) from error
        if (
            current_ordinal != ordinal
            or _string_or_none(current.profile_application_id) != profile_application_id
            or current.cancellation is not None
            # The operation waits on this very background preparation, so the
            # same phase/item checkpoint also owns publication while waiting.
            or job.state
            not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.OBSERVING
            )
            or current.phase_index != phase.index
            or current.item_index != item_index
            or current.child_operation_id is not None
            or RetryHoldsMixin._scope_intent_status(session, job) != "current"
        ):
            raise _RuntimeImageOwnerChanged(
                "RunSwitch phase was cancelled or superseded"
            )

        # An image is its content: the publisher, slug, revision and build a
        # stored receipt first recorded are provenance of whoever asked first,
        # not a property of this plan, so they are not compared.
        expected_layout = (
            plan.runtime_storage.oci_layout_sha256 or plan.build.oci_layout_sha256
        )
        for label, approved in (
            ("image", ImageContent(image_digest=plan.image_digest)),
            (
                "platform",
                ImageContent(
                    image_digest=plan.runtime_storage.image_digest,
                    archive_sha256=expected_layout,
                    image_bytes=_stated_bytes(plan.runtime_storage.image_bytes),
                ),
            ),
            (
                "build",
                ImageContent(
                    image_digest=plan.build.image_digest,
                    archive_sha256=plan.build.oci_layout_sha256,
                    image_bytes=_stated_bytes(plan.build.image_bytes),
                ),
            ),
        ):
            differing = differing_image_fields(approved, parsed_receipt)
            if differing:
                raise _RuntimeImageIdentityUnknown(
                    f"runtime image differs from the approved {label}: "
                    + ", ".join(differing)
                )

        if profile_application_id is not None:
            try:
                expected_image = accepted_profile_runtime_image(
                    session,
                    profile_application_id,
                    recipe_revision_id,
                    target_nodes,
                )
                _require_profile_runtime_image(
                    expected_image,
                    RunSwitchObservedImageIdentity(
                        image_digest=parsed_receipt.image_digest,
                        oci_layout_sha256=parsed_receipt.oci_archive_sha256,
                        image_bytes=parsed_receipt.image_bytes,
                        architecture=parsed_receipt.architecture,
                        runtime_interface=parsed_receipt.runtime_interface,
                    ),
                )
            except (RunSwitchOperationConflict, ValueError) as error:
                raise _RuntimeImageIdentityUnknown(
                    "runtime image observation differs from the accepted Fleet profile"
                ) from error

        intent = RunSwitchRuntimeImageReferenceIntent(
            owner_kind="run-switch-job",
            operation_id=job.id,
            request_key=job.request_id,
            actor=job.actor,
            plan_digest=plan.plan_digest,
            phase_index=phase.index,
            item_index=item_index,
            workload_intent_ordinal=ordinal,
            recipe_revision_id=recipe_revision_id,
            profile_application_id=profile_application_id,
            execution_keys=list(canonical_execution_keys),
            image_digest=parsed_receipt.image_digest,
            archive_sha256=parsed_receipt.oci_archive_sha256,
            image_bytes=parsed_receipt.image_bytes,
            build_id=parsed_receipt.build_id,
            build_input_sha256=parsed_receipt.build_input_sha256,
        )
        prior_intent = current.runtime_image_reference_intent
        if prior_intent is not None:
            try:
                parsed_prior = prior_intent
            except (TypeError, ValueError) as error:
                raise _RuntimeImageIdentityUnknown(
                    "stored RunSwitch image reference is invalid"
                ) from error
            if parsed_prior == intent:
                return
            # A re-plan leaves the earlier plan's reference behind; this plan's
            # phase now owns the image. Within one plan it never changes.
            if parsed_prior.plan_digest == intent.plan_digest:
                raise _RuntimeImageIdentityUnknown(
                    "RunSwitch image reference changed during publication"
                )
        current.runtime_image_reference_intent = intent
        job.result = _persisted_result(current)
        job.updated_at = now
