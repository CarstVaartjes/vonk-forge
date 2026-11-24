"""Api: the fleet event stream, operator projection routes and activity detail."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
)
from starlette.responses import StreamingResponse

from vonk_control.api.common import _FleetEventStreamResponse
from vonk_control.auth import Actor
from vonk_control.bounded_json import BoundedJSONError
from vonk_control.control_limits import MAX_CONTROL_DOCUMENT_BYTES
from vonk_control.failure_evidence import FailureEvidenceService
from vonk_control.fleet_stream import parse_last_event_id
from vonk_control.fleet_stream_contract import FLEET_SSE_EVENTS, FleetStreamEvent
from vonk_control.metrics.prometheus_api import PrometheusReader
from vonk_control.operation_api.contracts import (
    OperationDetailResponse,
    bounded_error_responses,
)
from vonk_control.operation_api.responses import operation_detail_response
from vonk_control.operations.contract import OperationRecoveryAction
from vonk_control.operations.item_contract import (
    OperationOwnerReference,
    OperationRow,
    operation_item,
)
from vonk_control.operations.summary import UNREADABLE_SUMMARY
from vonk_control.operator_projection_api.routes import (
    install_operator_projection_routes,
)
from vonk_control.operator_projection_api.services import FleetOperatorServices
from vonk_control.strict_json import read_stored_model, warn_unreadable_once


def register_activity_routes(
    app: FastAPI,
    *,
    authenticated_actor: Any,
    fleet_stream: Any | None,
    fleet_projection: Any | None,
    fleet_services: FleetOperatorServices | None,
    library_projection: Any | None,
    prometheus: PrometheusReader,
    failure_evidence: FailureEvidenceService | None,
) -> Callable[..., OperationDetailResponse]:
    @app.get(
        "/api/fleet/stream",
        response_class=_FleetEventStreamResponse,
        responses={
            200: {
                "model": FleetStreamEvent,
                "description": (
                    "Durable Fleet event stream. The schema describes the JSON "
                    "data in each refresh notice, telemetry, or change SSE frame. "
                    "A refresh notice requires a verified complete Fleet read."
                ),
            },
            **bounded_error_responses(400, 401, 503),
        },
        openapi_extra={
            "x-vonk-streaming-transport": True,
            "x-vonk-response-frame-max-bytes": MAX_CONTROL_DOCUMENT_BYTES,
            "x-vonk-sse-events": FLEET_SSE_EVENTS,
        },
        operation_id="streamFleetEvents",
    )
    async def fleet_event_stream(
        request: Request,
        _documented_last_event_id: Annotated[
            str | None,
            Header(
                alias="Last-Event-ID",
                description=(
                    "Optional durable Fleet cursor; duplicate and numeric validity "
                    "are checked from the raw header list."
                ),
            ),
        ] = None,
        _actor: Actor = authenticated_actor,
    ) -> StreamingResponse:
        try:
            last_event_id = parse_last_event_id(
                request.headers.getlist("last-event-id")
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        if fleet_stream is None:
            raise HTTPException(status_code=503, detail="Fleet stream unavailable")
        return _FleetEventStreamResponse(
            fleet_stream.events(last_event_id),
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    # Register literal stream/progress/artifact paths before selector routes.
    install_operator_projection_routes(
        app,
        actor_dependency=authenticated_actor,
        fleet_projection=fleet_projection,
        library_projection=library_projection,
        fleet_services=fleet_services,
        prometheus=prometheus,
    )

    def activity_detail(
        row: OperationRow,
        *,
        tolerate_unreadable: bool = False,
        projected_at: datetime | None = None,
    ) -> OperationDetailResponse:
        """Expose recovery only when its family route is installed."""
        try:
            item = operation_item(row)
            if failure_evidence is not None:
                item = failure_evidence.decorate(item)
            available_actions = (
                (OperationRecoveryAction.RESUME,)
                if item.supported_actions and "resume" in item.supported_actions
                else ()
            )
            return operation_detail_response(
                item, available_actions=available_actions, now=projected_at
            )
        except BoundedJSONError, OSError, RuntimeError, TypeError, ValueError:
            if not tolerate_unreadable:
                raise
            # A corrupt row keeps its durable identity and timestamp visible;
            # its broken historical details do not hide other current work.
            raw = dict(row) if isinstance(row, Mapping) else row.model_dump(mode="json")
            operation_id = raw.get("id")
            created_at = raw.get("created_at")
            if (
                not isinstance(operation_id, str)
                or not operation_id
                or len(operation_id) > 128
                or not isinstance(created_at, str)
                or not created_at
                or len(created_at) > 64
            ):
                raise
            warn_unreadable_once("operation", operation_id)
            raw_nodes = raw.get("node_ids")
            node_ids = (
                [
                    node
                    for node in raw_nodes
                    if isinstance(node, str) and re.fullmatch(r"spk_[0-9a-f]{32}", node)
                ][:1024]
                if isinstance(raw_nodes, (list, tuple))
                else []
            )
            raw_owner = raw.get("owner")
            owner = None
            if isinstance(raw_owner, Mapping):
                try:
                    owner = read_stored_model(OperationOwnerReference, raw_owner)
                except ValueError:
                    owner = None
            return OperationDetailResponse(
                id=operation_id,
                node_ids=node_ids,
                kind="unreadable",
                state="unavailable",
                summary=UNREADABLE_SUMMARY,
                attempt=0,
                created_at=created_at,
                owner=owner,
                status_reason="Stored operation history is malformed.",
            )

    return activity_detail
