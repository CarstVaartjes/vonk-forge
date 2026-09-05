"""HTTP routes for the high-level Run/Switch Controller operation."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse

from .audit import AuditRecord
from .auth import Actor
from .operation_api import bounded_error_responses
from .run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchOperation,
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    RunSwitchStopApplyRequest,
    RunSwitchStopPreviewRequest,
)
from .run_switch_operations import (
    RunSwitchOperationConflict,
    RunSwitchOperationService,
)

RUN_SWITCH_OPERATION_IDS = {
    (
        "post",
        "/api/v1/recipes/run-switch-plans/preview",
    ): "previewRecipeRunSwitch",
    ("post", "/api/v1/recipes/run-switches"): "applyRecipeRunSwitch",
    (
        "get",
        "/api/v1/recipes/run-switches/{operation_id}",
    ): "getRecipeRunSwitchOperation",
    (
        "post",
        "/api/v1/recipes/run-switch-stops/preview",
    ): "previewRecipeRunSwitchStop",
    ("post", "/api/v1/recipes/run-switch-stops"): "applyRecipeRunSwitchStop",
}

_UUID = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def install_run_switch_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    audits: Any,
    service: RunSwitchOperationService | None,
) -> None:
    """Install typed routes while keeping operation authority in the service."""

    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(RUN_SWITCH_OPERATION_IDS)
    authenticated = actor_dependency

    def runs() -> RunSwitchOperationService:
        if service is None:
            raise HTTPException(status_code=503, detail="run/switch operations unavailable")
        return service

    def administrator(actor: Actor) -> None:
        if actor.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")

    def conflict(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "code": "run-switch.operation_conflict",
                "detail": str(error)[:256],
                "request_id": request.state.request_id,
            },
        )

    @app.post(
        "/api/v1/recipes/run-switch-plans/preview",
        response_model=RunSwitchPlan,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="previewRecipeRunSwitch",
    )
    def preview_run_switch(
        body: RunSwitchPreviewRequest, actor: Actor = authenticated
    ) -> RunSwitchPlan:
        administrator(actor)
        try:
            return runs().preview(body, actor=actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe revision not found") from None
        except RunSwitchOperationConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise HTTPException(status_code=503, detail=f"run/switch preview unavailable: {error}") from None

    @app.post(
        "/api/v1/recipes/run-switches",
        response_model=RunSwitchOperation,
        status_code=status.HTTP_202_ACCEPTED,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="applyRecipeRunSwitch",
    )
    def apply_run_switch(
        body: RunSwitchApplyRequest,
        request: Request,
        actor: Actor = authenticated,
    ) -> RunSwitchOperation:
        administrator(actor)
        try:
            result = runs().apply(body, actor=actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe revision not found") from None
        except RunSwitchOperationConflict as error:
            return conflict(request, error)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise HTTPException(status_code=503, detail=f"run/switch apply unavailable: {error}") from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.run-switch.apply",
                None,
                (result.operation_id, result.plan_digest, *result.node_ids),
            )
        )
        return result

    @app.get(
        "/api/v1/recipes/run-switches/{operation_id}",
        response_model=RunSwitchOperation,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getRecipeRunSwitchOperation",
    )
    def get_run_switch(
        operation_id: str = Path(pattern=_UUID), _actor: Actor = authenticated
    ) -> RunSwitchOperation:
        try:
            return runs().get(operation_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run/switch operation not found") from None
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise HTTPException(status_code=503, detail=f"run/switch operation unavailable: {error}") from None

    @app.post(
        "/api/v1/recipes/run-switch-stops/preview",
        response_model=RunSwitchPlan,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="previewRecipeRunSwitchStop",
    )
    def preview_run_switch_stop(
        body: RunSwitchStopPreviewRequest, actor: Actor = authenticated
    ) -> RunSwitchPlan:
        administrator(actor)
        try:
            return runs().preview_stop(body, actor=actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe run not found") from None
        except RunSwitchOperationConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise HTTPException(status_code=503, detail=f"run/switch stop preview unavailable: {error}") from None

    @app.post(
        "/api/v1/recipes/run-switch-stops",
        response_model=RunSwitchOperation,
        status_code=status.HTTP_202_ACCEPTED,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="applyRecipeRunSwitchStop",
    )
    def apply_run_switch_stop(
        body: RunSwitchStopApplyRequest,
        request: Request,
        actor: Actor = authenticated,
    ) -> RunSwitchOperation:
        administrator(actor)
        try:
            result = runs().apply_stop(body, actor=actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe run not found") from None
        except RunSwitchOperationConflict as error:
            return conflict(request, error)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise HTTPException(status_code=503, detail=f"run/switch stop apply unavailable: {error}") from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.run-switch.stop",
                None,
                (result.operation_id, result.plan_digest, *result.node_ids),
            )
        )
        return result


__all__ = ["RUN_SWITCH_OPERATION_IDS", "install_run_switch_routes"]
