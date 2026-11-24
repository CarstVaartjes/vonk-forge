"""Authenticated cancellation route for one numbered Fleet profile application."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, status

from vonk_control.api_availability import AVAILABILITY_ERRORS
from vonk_control.auth import MUTATION_ROLES, Actor
from vonk_control.fleet_profile_contract.review import (
    FleetProfileApplicationCancelRequest,
    FleetProfileApplicationView,
)
from vonk_control.fleet_profiles.api import raise_profile_conflict
from vonk_control.fleet_profiles.contracts import (
    FleetProfileConflict,
    FleetProfilePermissionDenied,
)
from vonk_control.identifiers import UUID_PATTERN
from vonk_control.operation_api.constants import _ADMIN_OPERATION_IDS
from vonk_control.operation_api.contracts import bounded_error_responses
from vonk_control.operator_acceptance import (
    OperatorReceiptResolver,
    operator_acceptance,
)
from vonk_control.operator_receipt_contract import ReceiptPolicy

_CANCEL_PATH = "/api/profile/applications/{application_id}/cancel"
_CANCEL_RECEIPT_PATH = (
    "/api/profile/applications/{application_id}/cancellations/{request_key}"
)


def install_profile_application_cancel_route(
    app: FastAPI,
    *,
    actor_dependency: Any,
    profiles: Any | None,
) -> None:

    _ADMIN_OPERATION_IDS[("post", _CANCEL_PATH)] = "cancelProfileApplication"
    _ADMIN_OPERATION_IDS[("get", _CANCEL_RECEIPT_PATH)] = (
        "getProfileApplicationCancellation"
    )

    @app.get(
        _CANCEL_RECEIPT_PATH,
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 403, 404, 409, 503),
        operation_id="getProfileApplicationCancellation",
    )
    def get_profile_application_cancellation(
        application_id: Annotated[str, Path(pattern=UUID_PATTERN)],
        request_key: Annotated[str, Path(pattern=UUID_PATTERN)],
        actor: Actor = actor_dependency,
    ) -> FleetProfileApplicationView:
        if actor.role not in MUTATION_ROLES[("POST", _CANCEL_PATH)]:
            raise HTTPException(status_code=403, detail="insufficient role")
        if profiles is None:
            raise HTTPException(status_code=503, detail="Profiles unavailable")
        try:
            return profiles.application_cancellation_by_request(
                application_id,
                request_key,
                actor=actor.subject,
            )
        except FleetProfilePermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Profile cancellation receipt not found"
            ) from None
        except FleetProfileConflict as error:
            raise_profile_conflict(error)
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile cancellation receipt unavailable"
            ) from None

    @app.post(
        _CANCEL_PATH,
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="cancelProfileApplication",
    )
    @operator_acceptance(ReceiptPolicy.PROFILE_CANCEL)
    def cancel_profile_application(
        body: FleetProfileApplicationCancelRequest,
        application_id: Annotated[str, Path(pattern=UUID_PATTERN)],
        actor: Actor = actor_dependency,
    ) -> FleetProfileApplicationView:
        if actor.role not in MUTATION_ROLES[("POST", _CANCEL_PATH)]:
            raise HTTPException(status_code=403, detail="insufficient role")
        if profiles is None:
            raise HTTPException(status_code=503, detail="Profiles unavailable")
        try:
            result = profiles.cancel(
                application_id,
                profile_number=body.profile_number,
                request_key=body.request_key,
                actor=actor.subject,
            )
        except FleetProfilePermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Profile application not found"
            ) from None
        except FleetProfileConflict as error:
            raise_profile_conflict(error)
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile cancellation unavailable"
            ) from None
        return result

    OperatorReceiptResolver(profiles=profiles).bind(app)
