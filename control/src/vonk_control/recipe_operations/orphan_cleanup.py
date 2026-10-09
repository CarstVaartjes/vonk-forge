"""Consume exact cleanup effects after the foreground or owner projection ends."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentOperation as OperationKind,
)
from vonk_agent_protocol import (
    LifecycleState,
    RecipeReconcilePayload,
    RecipeStopPayload,
    RecipeUninstallPayload,
    ReservationState,
)

from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    AgentOperation,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from ..recipe_lifecycle_contract import (
    LifecycleNodeResult,
    RecipeOperationCancellationResult,
    RecipeOperationProgressResult,
    RecipeOperationResult,
)
from ..recipe_progress import _parent_identity
from ..stored_json import read_row_column
from ..strict_json import read_stored_model, serialize_json_value
from .results import _recorded_result


def consume_orphan_cleanup(
    session: Session,
    job: Job,
    operation: AgentOperation,
    *,
    succeeded: bool,
    evidence: LifecycleNodeResult,
    now: datetime,
) -> bool:
    """Retain a receipt and release only its confirmed rank, without an owner row.

    The queue authenticated and fenced the peer before this consumer. Missing
    SQL bookkeeping cannot veto that receipt or discard it after a parent-end
    deadline. Incomplete target evidence still retains capacity.
    """
    owner_id = _parent_identity(job, "owner_id")
    if owner_id is None:
        return False
    if job.kind == OperationKind.RECIPE_STOP:
        owner_kind = "run"
        owner = session.get(RecipeRun, owner_id)
    elif job.kind in {OperationKind.RECIPE_UNINSTALL, OperationKind.RECIPE_RECONCILE}:
        owner_kind = "installation"
        owner = session.get(RecipeInstallation, owner_id)
    else:
        return False
    if owner is not None:
        return False
    exact = False
    try:
        if job.kind == OperationKind.RECIPE_STOP:
            target = read_stored_model(
                RecipeStopPayload, operation.payload, from_json=True
            )
            exact = target.run_id == owner_id
        elif job.kind == OperationKind.RECIPE_UNINSTALL:
            removal = read_stored_model(
                RecipeUninstallPayload, operation.payload, from_json=True
            )
            exact = removal.installation_id == owner_id
        else:
            cleanup = read_stored_model(
                RecipeReconcilePayload, operation.payload, from_json=True
            )
            exact = cleanup.installation_id == owner_id
    except (TypeError, ValueError):
        # The valid peer receipt remains recorded; its target is re-observed by
        # later exact owner reconciliation, never counted as freed capacity.
        pass
    exact = (
        exact
        and operation.kind == job.kind
        and operation.authority_revision == job.authority_revision
    )
    if succeeded and exact:
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == owner_kind,
                ResourceReservation.owner_id == owner_id,
                ResourceReservation.node_id == operation.node_id,
                ResourceReservation.state == ReservationState.ACTIVE,
            )
        ):
            reservation.state = ReservationState.RELEASED
            reservation.released_at = now
    recorded = _recorded_result(
        job.kind, read_row_column(job, "result"), subject=job.id
    )
    projected = isinstance(
        recorded,
        (
            RecipeOperationResult,
            RecipeOperationProgressResult,
            RecipeOperationCancellationResult,
        ),
    )
    prior = (
        recorded.node_evidence
        if isinstance(
            recorded,
            (
                RecipeOperationResult,
                RecipeOperationProgressResult,
                RecipeOperationCancellationResult,
            ),
        )
        else None
    )
    receipts: dict[str, LifecycleNodeResult] = dict(prior or {})
    receipts[operation.node_id] = evidence
    children = tuple(
        session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
    )
    successes = sorted(
        {child.node_id for child in children if child.state == LifecycleState.SUCCEEDED}
    )
    failures = sorted(
        {
            child.node_id
            for child in children
            if child.state in {LifecycleState.FAILED, LifecycleState.CANCELLED}
        }
    )
    terminal = {
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
    }
    if (
        children
        and all(child.state in terminal for child in children)
        and job.state not in terminal
    ):
        RecipeOperationAdapter().finish(
            job,
            now,
            failed=bool(failures) or not exact,
            reason=None
            if not failures and exact
            else "cleanup target observation ended",
            keep=False,
        )
    if projected and recorded is not None:
        job.result = serialize_json_value(
            recorded.model_copy(update={"node_evidence": receipts})
        )
    else:
        job.result = serialize_json_value(
            RecipeOperationResult(
                successful_nodes=successes,
                failed_nodes=failures,
                node_evidence=receipts,
                recovery_error=None
                if exact
                else "cleanup target observation unavailable",
            )
        )
    return True
