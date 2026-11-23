"""Api: application concerns."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import (
    Depends,
    FastAPI,
)
from vonk_agent_protocol.capability_reply import ControllerCapability

from ..agent_api.common import AgentApiServices, EnrollmentRateLimiter
from ..agent_api.routes import install_agent_routes
from ..artifact_job_api import (
    install_artifact_job_routes,
)
from ..artifact_jobs.service import ArtifactJobService
from ..auth import (
    CursorCodec,
    TokenCodec,
)
from ..browser_auth import BrowserAuthService
from ..catalog.api import install_catalog_routes
from ..catalog.service import CatalogService
from ..failure_evidence import FailureEvidenceService
from ..failure_evidence_api import install_failure_evidence_routes
from ..fleet_profile_api import install_fleet_profile_routes
from ..gateway.keys import GatewayKeyService, install_gateway_key_routes
from ..installation_reconciliation_api import install_installation_reconciliation_routes
from ..metrics.prometheus_api import PrometheusReader
from ..metrics.registry import MetricsRegistry
from ..model_cache_api import install_model_operator_routes
from ..observation.platform import (
    PlatformObserver,
    api_only_capture,
)
from ..operation_api.contracts import OperationApiServices, bounded_error_responses
from ..operator_acceptance import (
    OperatorReceiptResolver,
)
from ..operator_projection_api.services import FleetOperatorServices
from ..profile_application_cancel_api import install_profile_application_cancel_route
from ..recipe_operations.service import RecipeOperationService
from ..run_switch_operations.service import RunSwitchOperationService
from ..strict_json import ControllerAPIRoute
from .common import (
    JobQueue,
)
from .errors import install_error_handlers
from .middleware import install_request_boundaries
from .roles import make_actor_dependency
from .routes_activity import register_activity_routes
from .routes_jobs import register_job_routes
from .routes_platform import register_platform_routes


def create_app(
    *,
    jobs: JobQueue,
    tokens: TokenCodec,
    fleet_projection: Any | None = None,
    fleet_services: FleetOperatorServices | None = None,
    failure_evidence: FailureEvidenceService | None = None,
    fleet_stream: Any | None = None,
    library_projection: Any | None = None,
    now: Callable[[], int] = lambda: int(time.time()),
    metrics: MetricsRegistry | None = None,
    prometheus: PrometheusReader | None = None,
    prometheus_url: str | None = None,
    metrics_token: str | Callable[[], str] | None = None,
    metrics_refresh: Callable[[], None] | None = None,
    agent: AgentApiServices | None = None,
    trusted_agent_proxy_auth: bytes | Callable[[], bytes] = b"",
    enrollment_rate_limiter: EnrollmentRateLimiter | None = None,
    operations: OperationApiServices | None = None,
    catalog: CatalogService | None = None,
    managed_catalog_sync: Any | None = None,
    recipe_operations: RecipeOperationService | None = None,
    run_switch_operations: RunSwitchOperationService | None = None,
    artifact_jobs: ArtifactJobService | None = None,
    fleet_profiles: Any | None = None,
    agent_upgrades: Any | None = None,
    browser_auth: BrowserAuthService | None = None,
    model_cache: Any | None = None,
    recipe_image_availability: Any | None = None,
    gateway_keys: GatewayKeyService | None = None,
    lifespan: Any | None = None,
    platform_observer: PlatformObserver | None = None,
) -> FastAPI:
    prometheus = prometheus or PrometheusReader(prometheus_url)

    @asynccontextmanager
    async def monitoring_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with prometheus.lifespan():
            if lifespan is None:
                yield
            else:
                async with lifespan(app):
                    yield

    app = FastAPI(
        title="Vonk Forge Control",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        responses=bounded_error_responses(422, 429, 500, 503),
        lifespan=monitoring_lifespan,
    )
    app.router.route_class = ControllerAPIRoute
    from ..capabilities import RecoveringService

    cursor_codec = cast(
        CursorCodec,
        RecoveringService(
            ControllerCapability.CURSOR_AUTH,
            CursorCodec,
            tokens.cursor_codec,
            lambda: datetime.now(UTC),
        ),
    )

    install_error_handlers(app)

    install_request_boundaries(
        app,
        agent=agent,
        trusted_agent_proxy_auth=trusted_agent_proxy_auth,
        metrics=metrics,
    )

    actor = make_actor_dependency(tokens=tokens, browser_auth=browser_auth, now=now)

    install_agent_routes(
        app,
        services=agent,
        enrollment_rate_limiter=enrollment_rate_limiter,
    )
    authenticated_actor = Depends(actor)

    if browser_auth is not None:
        from ..auth_api import install_auth_routes

        install_auth_routes(
            app,
            browser_auth,
            authenticated_actor,
            tokens=tokens,
            now=now,
        )

    install_catalog_routes(
        app,
        actor_dependency=authenticated_actor,
        service=catalog,
        managed_sync=managed_catalog_sync,
    )
    install_fleet_profile_routes(
        app,
        actor_dependency=authenticated_actor,
        profiles=fleet_profiles,
        operations=operations,
    )
    install_profile_application_cancel_route(
        app,
        actor_dependency=authenticated_actor,
        profiles=fleet_profiles,
    )
    install_gateway_key_routes(
        app, actor_dependency=authenticated_actor, service=gateway_keys
    )
    install_failure_evidence_routes(
        app,
        actor_dependency=authenticated_actor,
        service=failure_evidence,
    )
    install_artifact_job_routes(
        app,
        actor_dependency=authenticated_actor,
        service=artifact_jobs,
    )
    install_model_operator_routes(
        app,
        actor_dependency=authenticated_actor,
        service=model_cache,
    )
    from ..recipe_image_availability_api import install_recipe_operator_routes

    install_recipe_operator_routes(
        app,
        actor_dependency=authenticated_actor,
        service=recipe_image_availability,
    )
    install_installation_reconciliation_routes(
        app,
        actor_dependency=authenticated_actor,
        operations=run_switch_operations,
    )

    from ..cli_update_contract import install_cli_update_contract_routes

    install_cli_update_contract_routes(
        app,
        actor_dependency=authenticated_actor,
        capture=api_only_capture
        if platform_observer is None
        else platform_observer.capture,
    )

    register_platform_routes(
        app,
        authenticated_actor=authenticated_actor,
        platform_observer=platform_observer,
        metrics=metrics,
        metrics_refresh=metrics_refresh,
        metrics_token=metrics_token,
    )

    activity_detail = register_activity_routes(
        app,
        authenticated_actor=authenticated_actor,
        fleet_stream=fleet_stream,
        fleet_projection=fleet_projection,
        fleet_services=fleet_services,
        library_projection=library_projection,
        prometheus=prometheus,
        failure_evidence=failure_evidence,
    )

    register_job_routes(
        app,
        authenticated_actor=authenticated_actor,
        activity_detail=activity_detail,
        cursor_codec=cursor_codec,
        operations=operations,
        jobs=jobs,
        failure_evidence=failure_evidence,
    )

    receipts = OperatorReceiptResolver(
        agent=agent,
        jobs=jobs,
        profiles=fleet_profiles,
        models=model_cache,
        recipes=recipe_image_availability,
        artifacts=artifact_jobs,
        fleet=fleet_projection,
        fleet_services=fleet_services,
        keys=gateway_keys,
        installations=run_switch_operations,
    )
    receipts.bind(app)
    return app
