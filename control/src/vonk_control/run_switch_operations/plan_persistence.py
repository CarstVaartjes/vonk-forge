"""Plan persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import (
    Literal,
    get_args,
)

from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    canonical_message,
)

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from ..artifact_reference_scan import (
    require_model_sets_open,
)
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..models import (
    Job,
)
from ..run_switch_contract import (
    RunSwitchAction,
    RunSwitchPlacementAction,
    RunSwitchPlan,
)
from ..run_switch_observation_contract import (
    RunSwitchStoredIdentity,
)
from ..strict_json import (
    read_stored_document,
)
from .errors import RunSwitchRequestInvalid


def _view_identity(
    job: Job, plan: RunSwitchPlan | None
) -> tuple[RunSwitchAction, str, Literal["uninstall", "reconcile"] | None, str | None]:
    """Action, digest and cleanup identity of an operation, from its plan or,
    when the plan is unreadable, from the identity recorded beside it."""

    if plan is not None:
        cleanup = plan.action == "cleanup"
        return (
            plan.action,
            plan.plan_digest,
            plan.cleanup_mode if cleanup else None,
            plan.installation_id if cleanup else None,
        )
    try:
        recorded = RunSwitchStoredIdentity.model_validate_json(
            canonical_message(job.payload), strict=True
        )
    except (TypeError, ValueError):
        recorded = RunSwitchStoredIdentity()
    action: RunSwitchAction = "stop"
    for candidate in get_args(RunSwitchPlacementAction):
        if recorded.action == candidate:
            action = candidate
    return action, recorded.plan_digest or "0" * 64, None, None


def _load_plan(value: object) -> RunSwitchPlan | None:
    """The stored plan, or ``None`` once it cannot be read (retired as unknown).

    A damaged stored plan is never a reason to refuse: each caller either
    rebuilds from the accepted intent (re-plan), retires the one operation
    (``_reject_invalid_operation``), or carries on without the plan.
    """

    if not isinstance(value, Mapping):
        retire_as_unknown(
            "run-switch.plan",
            "stored-plan",
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            "stored plan is not a document",
        )
        return None
    # Job.payload is JSON, so strict validation must permit the RFC3339
    # timestamp representation when a worker restarts and reloads a plan.
    loaded = read_or_rebuild(
        kind="run-switch.plan",
        subject="stored-plan",
        read=lambda: read_stored_document(
            lambda document: RunSwitchPlan.model_validate_json(
                json.dumps(document), strict=True
            ),
            value,
        ),
    )
    return None if isinstance(loaded, Residue) else loaded


def _reserve_run_switch_assets(
    session: Session, plan: RunSwitchPlan, *, now: datetime
) -> None:
    """Keep exact accepted plan identities open through the Job commit."""

    model_set = plan.storage.artifact_set_sha256
    image_archive = plan.runtime_storage.oci_layout_sha256
    try:
        if model_set is not None:
            require_model_sets_open(
                session,
                (model_set,),
                now=now,
                object_digests=plan.storage.artifact_digests,
            )
        if image_archive is not None:
            require_reference_open(
                session,
                (ArtifactIdentity("runtime-image", image_archive),),
                now=now,
            )
    except ArtifactLifecycleError as error:
        raise RunSwitchRequestInvalid(f"{error.code}: {error.detail}") from error
