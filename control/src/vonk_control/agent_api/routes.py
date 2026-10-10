"""Agent api: routes concerns."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from vonk_agent_protocol.enrollment import EnrollmentSubmitRequest, ExpiredRenewRequest

from ..openapi_numbers import install_canonical_openapi
from ..strict_json import ControllerAPIRoute
from .artifacts import install_artifacts_routes
from .authority import install_authority_routes
from .common import AgentApiServices, EnrollmentRateLimiter
from .enrollment import install_enrollment_routes
from .observations import install_observations_routes


def install_agent_routes(
    app: Any,
    *,
    services: AgentApiServices | None,
    enrollment_rate_limiter: EnrollmentRateLimiter | None = None,
) -> None:
    agent = APIRouter(prefix="/agent", route_class=ControllerAPIRoute)
    limiter = enrollment_rate_limiter or EnrollmentRateLimiter()

    install_enrollment_routes(agent, services, limiter)
    install_observations_routes(agent, services)
    # Proof recovery has its own bounded, non-client-keyed budget. A burst of
    # tens of expired Sparks fits without consuming operator enrollment slots;
    # Retry-After and agent jitter bound retries when this window is full.
    recovery_limiter = EnrollmentRateLimiter(maximum=120)
    install_authority_routes(agent, services, recovery_limiter)
    install_artifacts_routes(agent, services)

    app.include_router(agent)

    # Enrollment reads a bounded raw body before validation so an invalid
    # submission still consumes its identifiable one-use grant. Document that
    # input from the very same model used above; a Request parameter alone
    # would otherwise hide the request contract from OpenAPI consumers.
    install_canonical_openapi(app)
    standard_openapi = app.openapi

    def openapi_with_enrollment_contract() -> dict[str, object]:
        document = standard_openapi()
        components = document.setdefault("components", {}).setdefault("schemas", {})
        for path, model in (
            ("/agent/enroll", EnrollmentSubmitRequest),
            ("/agent/renew/expired", ExpiredRenewRequest),
        ):
            request_schema = model.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
            components.update(request_schema.pop("$defs", {}))
            components[model.__name__] = request_schema
            document["paths"][path]["post"]["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{model.__name__}"}
                    }
                },
            }
        return document

    app.openapi = openapi_with_enrollment_contract
