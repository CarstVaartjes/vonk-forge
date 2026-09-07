"""Authenticated, side-effect-free deployment provenance endpoint."""

from typing import Any

from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from .auth import Actor
from .deployment_provenance_contract import DeploymentProvenance
from .operation_api import bounded_error_responses


def install_deployment_provenance_routes(
    app: FastAPI, *, actor_dependency: Any, provenance: Any | None
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS[("get", "/api/v1/deployment-provenance")] = (
        "getDeploymentProvenance"
    )

    @app.get(
        "/api/v1/deployment-provenance",
        response_model=DeploymentProvenance,
        operation_id="getDeploymentProvenance",
        responses=bounded_error_responses(401, 503),
    )
    def deployment_provenance(_actor: Actor = actor_dependency) -> DeploymentProvenance:
        if provenance is None:
            raise HTTPException(
                status_code=503, detail="Deployment provenance unavailable"
            )
        try:
            return provenance.snapshot()
        except (OSError, ValueError, TypeError, SQLAlchemyError):
            raise HTTPException(
                status_code=503, detail="Deployment evidence is unavailable or invalid"
            ) from None
