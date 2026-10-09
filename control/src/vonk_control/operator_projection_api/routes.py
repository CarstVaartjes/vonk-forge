"""Operator projection api: routes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Query, status
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response
from vonk_agent_protocol import (
    ModelCacheOperatorStatus,
)
from vonk_agent_protocol.http_failure import HttpRefusalReason

from vonk_control.http_errors import SecurityHTTPError

from ..agent_upgrade_contract import AgentUpgradeRequestIntent
from ..auth import Actor
from ..enrollment_contract import (
    ENROLLMENT_ID_PATTERN,
    EnrollmentGrantStatus,
    EnrollmentObservationOutcome,
)
from ..fleet_projection import (
    FleetNode,
    FleetNodeIdentity,
    FleetSnapshot,
)
from ..observation_transfer import (
    ObservationTransferRecord,
    ObservationTransferResponse,
    observation_openapi,
    observation_response,
)
from ..operation_api import bounded_error_responses
from ..platform_observation_errors import (
    ObservationCaptureUnavailable,
    observation_capture_unavailable_response,
)
from .contracts import (
    _SELECTOR_PATTERN,
    FLEET_OPERATION_IDS,
    FleetActionResponse,
    FleetEnrollRequest,
    FleetLocksResponse,
    FleetLogResponse,
    FleetReenrollRequest,
    FleetRenameRequest,
    FleetUpgradeRequest,
    _fleet_work_state,
)
from .errors import _node, _operator_error, _require_mutation
from .services import FleetOperatorServices


def install_operator_projection_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    fleet_projection: Any | None,
    library_projection: Any | None,
    fleet_services: FleetOperatorServices | None = None,
) -> None:
    """Install the singular operator route hierarchy.

    ``fleet_services`` is deliberately an adapter boundary.  The parent wires
    it to enrollment, signed upgrades and retained authenticated evidence; this
    module never falls back to SSH or synthetic values.
    """

    from ..library_api import install_library_routes
    from ..operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(FLEET_OPERATION_IDS)
    install_library_routes(
        app, actor_dependency=actor_dependency, projection=library_projection
    )
    authenticated = actor_dependency

    def fleet() -> Any:
        if fleet_projection is None:
            raise HTTPException(status_code=503, detail="fleet projection unavailable")
        return fleet_projection

    def snapshot() -> FleetSnapshot:
        try:
            return fleet().read()
        except HTTPException:
            raise
        except SQLAlchemyError as error:
            raise ObservationCaptureUnavailable(phase="database-capture") from error
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    def selected(selector: str) -> FleetNode:
        try:
            return _node(snapshot(), selector)
        except ObservationCaptureUnavailable:
            raise
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet",
        response_class=ObservationTransferResponse,
        response_model=None,
        responses={
            200: {"model": ObservationTransferRecord},
            **bounded_error_responses(401, 503),
        },
        openapi_extra=observation_openapi("FleetSnapshot"),
        operation_id="getFleetStatus",
    )
    def fleet_status(
        _actor: Actor = authenticated,
    ) -> ObservationTransferResponse | Response:
        try:
            captured = snapshot()
        except ObservationCaptureUnavailable as error:
            return observation_capture_unavailable_response(
                error, operation="getFleetStatus", endpoint="/api/fleet"
            )
        return observation_response(captured, resource="fleet")

    @app.get(
        "/api/fleet/locks",
        response_model=FleetLocksResponse,
        responses=bounded_error_responses(401, 403, 503),
        operation_id="getFleetAdmissionLocks",
    )
    def fleet_locks(actor: Actor = authenticated) -> FleetLocksResponse:
        if actor.role != "administrator":
            raise SecurityHTTPError(
                reason=HttpRefusalReason.AUTHORITY_DENIED,
                status_code=403,
                detail="insufficient role",
            )
        if fleet_services is None or fleet_services.sessions is None:
            raise HTTPException(status_code=503, detail="fleet locks unavailable")
        from ..admission_locking import report_admission_locks

        try:
            with fleet_services.sessions() as session:
                report = report_admission_locks(session)
                session.rollback()
            return FleetLocksResponse.model_validate(
                {
                    "held": [asdict(item) for item in report.held],
                    "open_transactions": [
                        asdict(item) for item in report.open_transactions
                    ],
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError, SQLAlchemyError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/{selector}/loginfo",
        response_model=FleetLogResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetLogInfo",
    )
    def fleet_loginfo(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        since: Annotated[datetime | None, Query()] = None,
        lines: Annotated[int, Query(ge=1, le=1_000)] = 100,
        recipe: Annotated[str | None, Query(max_length=256)] = None,
        source: Annotated[
            Literal["client", "monitor", "runtime", "job"] | None, Query()
        ] = None,
        follow: Annotated[bool, Query()] = False,
        _actor: Actor = authenticated,
    ) -> FleetLogResponse:
        node = selected(selector)
        if fleet_services is None or fleet_services.logs is None:
            raise HTTPException(
                status_code=503, detail="fleet log evidence unavailable"
            )
        try:
            value = fleet_services.logs.list(
                node.id,
                since=since,
                lines=lines,
                recipe=recipe,
                source=source,
                follow=follow,
            )
            return value
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/{selector}",
        response_model=FleetNode,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetNode",
    )
    def fleet_detail(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        _actor: Actor = authenticated,
    ) -> FleetNode:
        return selected(selector)

    @app.post(
        "/api/fleet/{selector}/rename",
        response_model=FleetNodeIdentity,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="renameFleetNode",
    )
    def fleet_rename(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        body: FleetRenameRequest,
        actor: Actor = authenticated,
    ) -> FleetNodeIdentity:
        _require_mutation(actor, "POST", "/api/fleet/{selector}/rename")
        node = selected(selector)
        try:
            result = fleet().update_display_name(node.id, body.display_name)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None
        return result

    @app.post(
        "/api/fleet/enroll",
        response_model=FleetActionResponse,
        status_code=status.HTTP_201_CREATED,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        operation_id="enrollFleetNode",
    )
    def fleet_enroll(
        body: FleetEnrollRequest,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/enroll")
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            value = fleet_services.enrollment.create_named(
                name=body.name,
                actor=actor.subject,
                request_id=body.request_key,
            )
            return value
        except (OSError, RuntimeError, TypeError, ValueError, SQLAlchemyError) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/{selector}/re-enroll",
        response_model=FleetActionResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="reenrollFleetNode",
    )
    def fleet_reenroll(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        body: FleetReenrollRequest,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/{selector}/re-enroll")
        node = selected(selector)
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            value = fleet_services.enrollment.create_reenrollment(
                node.id, actor.subject, body.request_key
            )
            return value
        except (OSError, RuntimeError, TypeError, ValueError, SQLAlchemyError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/enrollments/{grant_id}",
        response_model=EnrollmentGrantStatus,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="getFleetEnrollment",
    )
    def get_enrollment(
        grant_id: Annotated[str, Path(pattern=ENROLLMENT_ID_PATTERN)],
        actor: Actor = authenticated,
    ) -> EnrollmentGrantStatus:
        _require_mutation(actor, "POST", "/api/fleet/enroll")
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            return fleet_services.enrollment.grant_status(grant_id, actor=actor.subject)
        except (
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/enrollments/{grant_id}/revoke",
        openapi_extra={"x-vonk-request-body": "none"},
        response_model=EnrollmentGrantStatus | EnrollmentObservationOutcome,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="revokeFleetEnrollment",
    )
    def revoke_enrollment(
        grant_id: Annotated[str, Path(pattern=ENROLLMENT_ID_PATTERN)],
        actor: Actor = authenticated,
    ) -> EnrollmentGrantStatus | EnrollmentObservationOutcome:
        _require_mutation(actor, "POST", "/api/fleet/enrollments/{grant_id}/revoke")
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            result = fleet_services.enrollment.revoke_grant(
                grant_id, actor=actor.subject
            )
            return result
        except (
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/{selector}/remove",
        openapi_extra={"x-vonk-request-body": "none"},
        response_model=FleetActionResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 503),
        operation_id="removeFleetNode",
    )
    def fleet_remove(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/{selector}/remove")
        node = selected(selector)
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet removal unavailable")
        try:
            revocation = fleet_services.enrollment.revoke_node(node.id, actor.subject)
            result = FleetActionResponse(
                action="remove",
                state=ModelCacheOperatorStatus.ACCEPTED,
                node_id=node.id,
                revocation=revocation,
            )
            return result
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/upgrade",
        response_model=FleetActionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="upgradeFleet",
    )
    def fleet_upgrade(
        body: FleetUpgradeRequest,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/upgrade")
        if fleet_services is None or fleet_services.upgrades is None:
            raise HTTPException(status_code=503, detail="fleet upgrades unavailable")
        if body.all == (body.selectors is not None):
            raise HTTPException(status_code=422, detail="choose all or selectors")
        try:
            request_intent = AgentUpgradeRequestIntent(
                all=body.all, selectors=body.selectors
            )
            existing = fleet_services.upgrades.get_request(
                body.request_key,
                actor=actor.subject,
                request_intent=request_intent,
            )
            if existing is not None:
                result = FleetActionResponse(
                    action="upgrade",
                    state=_fleet_work_state(existing.state),
                    operation_id=str(getattr(existing, "id", "")) or None,
                    plan_digest=str(getattr(existing, "payload_digest", "")) or None,
                    request_key=body.request_key,
                    targets=list(getattr(existing, "targets", ())),
                )
                return result
            fleet_snapshot = snapshot()
            nodes = (
                list(fleet_snapshot.nodes)
                if body.all
                else [_node(fleet_snapshot, value) for value in body.selectors or []]
            )
            node_ids = list(dict.fromkeys(node.id for node in nodes))
            package = fleet_services.upgrades.current_package()
            plan = fleet_services.upgrades.preview(
                node_ids,
                package,
                request_intent=request_intent,
            )
            job = fleet_services.upgrades.apply(
                node_ids,
                package,
                plan_digest=plan.plan_digest,
                actor=actor.subject,
                request_id=body.request_key,
                request_intent=request_intent,
            )
            result = FleetActionResponse(
                action="upgrade",
                state=_fleet_work_state(job.state),
                operation_id=str(getattr(job, "id", "")) or None,
                plan_digest=str(getattr(job, "payload_digest", plan.plan_digest)),
                request_key=body.request_key,
                targets=list(getattr(job, "targets", node_ids)),
            )
            return result
        except ObservationCaptureUnavailable:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None
