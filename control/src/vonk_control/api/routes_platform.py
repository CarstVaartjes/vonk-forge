"""Api: platform observation, health and metrics routes."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
)

from ..auth import (
    Actor,
)
from ..download_contract import download_responses
from ..metrics.registry import MetricsRegistry
from ..observation.platform import (
    PlatformObserver,
    api_only_observation,
)
from ..observation.platform_errors import (
    ObservationCaptureUnavailable,
    observation_capture_unavailable_response,
)
from ..observation.transfer import (
    ObservationTransferRecord,
    ObservationTransferResponse,
    observation_openapi,
    observation_response,
)
from ..operation_api import (
    HealthzResponse,
    ReadyzResponse,
    bounded_error_responses,
)


def register_platform_routes(
    app: FastAPI,
    *,
    authenticated_actor: Any,
    platform_observer: PlatformObserver | None,
    metrics: MetricsRegistry | None,
    metrics_refresh: Callable[[], None] | None,
    metrics_token: str | Callable[[], str] | None,
) -> None:
    @app.get(
        "/api/platform",
        response_class=ObservationTransferResponse,
        response_model=None,
        operation_id="getPlatformObservation",
        responses={
            200: {"model": ObservationTransferRecord},
            **bounded_error_responses(401, 503),
        },
        openapi_extra=observation_openapi("PlatformObservation"),
    )
    def platform_observation(
        _actor: Actor = authenticated_actor,
    ) -> ObservationTransferResponse | Response:
        try:
            observation = (
                api_only_observation()
                if platform_observer is None
                else platform_observer.read()
            )
        except ObservationCaptureUnavailable as error:
            return observation_capture_unavailable_response(
                error, operation="getPlatformObservation", endpoint="/api/platform"
            )
        return observation_response(observation, resource="platform")

    @app.get("/api/healthz", response_model=HealthzResponse)
    def healthz() -> HealthzResponse:
        return HealthzResponse(status="ok")

    @app.get("/api/readyz", response_model=ReadyzResponse)
    def readyz() -> ReadyzResponse:
        return ReadyzResponse(status="ready")

    @app.get(
        "/metrics",
        response_class=Response,
        responses=download_responses("text/plain"),
        openapi_extra={"x-vonk-streaming-transport": True},
    )
    def platform_metrics(request: Request) -> Response:
        if metrics is None or metrics_token is None:
            raise HTTPException(status_code=404, detail="not found")
        authorization = request.headers.get("authorization", "")
        expected_metrics_token = (
            metrics_token() if callable(metrics_token) else metrics_token
        )
        if not secrets.compare_digest(
            authorization, f"Bearer {expected_metrics_token}"
        ):
            raise HTTPException(status_code=401, detail="authentication required")
        if metrics_refresh is not None:
            metrics_refresh()
        return Response(
            metrics.render(),
            media_type="application/openmetrics-text; version=1.0.0; charset=utf-8",
        )
