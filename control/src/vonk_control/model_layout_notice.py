"""Project current model-layout faults from the owning operation receipt."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as Operation
from vonk_agent_protocol import InstallationState, LifecycleState, ProjectionCode
from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.contracts import AgentFailureResult
from vonk_agent_protocol.lifecycle_vocabulary import FailureCode

from .models import AgentOperation, AgentOperationAttempt, RecipeInstallation
from .stored_json import read_row_column

if TYPE_CHECKING:
    from .fleet_projection.common import ProjectionReason


def model_layout_notices(
    session: Session, node_ids: Sequence[str]
) -> dict[str, tuple[ProjectionReason, ...]]:
    """A repaired installation clears its notice through normal success."""
    from .fleet_projection.common import ProjectionReason

    rows = session.execute(
        select(AgentOperation, AgentOperationAttempt)
        .join(
            AgentOperationAttempt,
            AgentOperationAttempt.operation_id == AgentOperation.id,
        )
        .join(
            RecipeInstallation,
            RecipeInstallation.id
            == AgentOperation.payload["installation_id"].as_string(),
        )
        .where(
            AgentOperation.node_id.in_(node_ids),
            AgentOperation.kind == Operation.RECIPE_INSTALL,
            AgentOperation.state != LifecycleState.SUCCEEDED,
            RecipeInstallation.state.not_in(
                (InstallationState.INSTALLED, InstallationState.UNINSTALLED)
            ),
        )
        .order_by(
            AgentOperation.updated_at.desc(),
            AgentOperationAttempt.attempt.desc(),
            AgentOperation.id,
        )
    )
    notices: dict[str, list[ProjectionReason]] = {}
    for operation, attempt in rows:
        if operation.node_id in notices:
            continue
        result = read_row_column(attempt, "result")
        if (
            not isinstance(result, AgentFailureResult)
            or result.error_code != FailureCode.MODEL_STORE_LAYOUT
        ):
            continue
        layout = (
            None
            if result.diagnostics is None
            else result.diagnostics.model_store_layout
        )
        detail = (
            f"{operation.node_id}: errno={layout.errno} path={layout.path}"
            if layout is not None
            else f"{operation.node_id}: {result.reason or FailureCode.MODEL_STORE_LAYOUT}"
        )
        notices.setdefault(operation.node_id, []).append(
            ProjectionReason(
                code=ProjectionCode.MODEL_STORE_LAYOUT,
                model_store_layout=layout,
                detail=detail[:256],
                severity=ProfileReasonSeverity.WARNING.value,
                recommendation="Place installations and distribution/models on one hardlink-capable filesystem; the original operation retries automatically.",
            )
        )
    return {node_id: tuple(values) for node_id, values in notices.items()}
