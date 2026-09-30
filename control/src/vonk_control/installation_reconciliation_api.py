"""Typed preview, apply, and status routes for exact installation reconciliation."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, status

from .auth import MUTATION_ROLES, Actor
from .logging import redact_text
from .operation_api import _ADMIN_OPERATION_IDS, bounded_error_responses
from .run_switch_contract import (
    InstallationReconcileRequest,
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
    RunSwitchOperation,
    RunSwitchPlan,
)
from .run_switch_operations import RunSwitchOperationConflict

_UUID = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_PREVIEW_PATH = "/api/recipe/installations/{installation_id}/reconcile/preview"
_APPLY_PATH = "/api/recipe/installations/{installation_id}/reconcile"
_OPERATION_PATH = "/api/run-switch/operations/{operation_id}"

INSTALLATION_RECONCILIATION_OPERATION_IDS = {
    ("post", _PREVIEW_PATH): "previewRecipeInstallationReconciliation",
    ("post", _APPLY_PATH): "applyRecipeInstallationReconciliation",
    ("get", _OPERATION_PATH): "getRunSwitchOperation",
}


def install_installation_reconciliation_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    operations: Any | None,
) -> None:
    """Expose the existing Run/Switch owner without adding another planner."""

    _ADMIN_OPERATION_IDS.update(INSTALLATION_RECONCILIATION_OPERATION_IDS)
    authenticated = actor_dependency

    def service() -> Any:
        if operations is None:
            raise HTTPException(
                status_code=503, detail="Run/Switch operation service unavailable"
            )
        return operations

    def require_apply(actor: Actor) -> None:
        if actor.role not in MUTATION_ROLES[("POST", _APPLY_PATH)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    def refusal_detail(error: RunSwitchOperationConflict) -> str:
        detail = redact_text(str(error))[:256]
        return detail or "Run/Switch rejected installation reconciliation"

    def validate_plan(plan: RunSwitchPlan, installation_id: str) -> RunSwitchPlan:
        if (
            plan.action != "cleanup"
            or plan.installation_id != installation_id
            or plan.cleanup_mode != "reconcile"
            or (
                plan.reconciliation_authority is not None
                and plan.reconciliation_authority.installation_id != installation_id
            )
            or (
                plan.allowed
                and plan.reconciliation_authority is None
                and plan.cleanup_disposition != "abandon"
            )
        ):
            raise HTTPException(
                status_code=503,
                detail="Run/Switch returned a mismatched reconciliation plan",
            )
        return plan

    def validate_operation(
        operation: RunSwitchOperation,
        *,
        operation_id: str | None = None,
        request_key: str | None = None,
        installation_id: str | None = None,
    ) -> RunSwitchOperation:
        if (
            (operation_id is not None and operation.operation_id != operation_id)
            or (request_key is not None and operation.request_key != request_key)
            or operation.kind != "recipe.cleanup.v2"
            or operation.action != "cleanup"
            or operation.cleanup_mode != "reconcile"
            or (
                installation_id is not None
                and operation.installation_id != installation_id
            )
        ):
            raise HTTPException(
                status_code=503,
                detail="Run/Switch returned a mismatched cleanup operation",
            )
        return operation

    @app.post(
        _PREVIEW_PATH,
        response_model=RunSwitchPlan,
        responses=bounded_error_responses(401, 404, 409, 422, 503),
        operation_id="previewRecipeInstallationReconciliation",
    )
    def preview_installation_reconciliation(
        installation_id: Annotated[str, Path(pattern=_UUID)],
        actor: Actor = authenticated,
    ) -> RunSwitchPlan:
        try:
            plan = service().preview_cleanup(
                RunSwitchCleanupPreviewRequest(
                    installation_id=installation_id, cleanup_mode="reconcile"
                ),
                actor=actor.subject,
            )
            return validate_plan(plan, installation_id)
        except HTTPException:
            raise
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Recipe installation not found"
            ) from None
        except RunSwitchOperationConflict as error:
            raise HTTPException(status_code=409, detail=refusal_detail(error)) from None

    @app.post(
        _APPLY_PATH,
        response_model=RunSwitchOperation,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="applyRecipeInstallationReconciliation",
    )
    def apply_installation_reconciliation(
        installation_id: Annotated[str, Path(pattern=_UUID)],
        body: InstallationReconcileRequest,
        actor: Actor = authenticated,
    ) -> RunSwitchOperation:
        require_apply(actor)
        try:
            operation = service().apply_cleanup(
                RunSwitchCleanupApplyRequest(
                    installation_id=installation_id,
                    cleanup_mode="reconcile",
                    request_key=body.request_key,
                ),
                actor=actor.subject,
            )
            operation = validate_operation(
                operation,
                request_key=body.request_key,
                installation_id=installation_id,
            )
        except HTTPException:
            raise
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Recipe installation not found"
            ) from None
        except RunSwitchOperationConflict as error:
            raise HTTPException(status_code=409, detail=refusal_detail(error)) from None
        return operation

    @app.get(
        _OPERATION_PATH,
        response_model=RunSwitchOperation,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getRunSwitchOperation",
    )
    def run_switch_operation(
        operation_id: Annotated[str, Path(pattern=_UUID)],
        _actor: Actor = authenticated,
    ) -> RunSwitchOperation:
        try:
            operation = service().get(operation_id)
            if operation.operation_id != operation_id:
                raise HTTPException(
                    status_code=503,
                    detail="Run/Switch returned a mismatched operation identity",
                )
            return operation
        except HTTPException:
            raise
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Run/Switch operation not found"
            ) from None
