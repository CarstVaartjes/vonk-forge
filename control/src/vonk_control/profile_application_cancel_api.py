"""Authenticated cancellation route for one numbered Fleet profile application."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Path, Request, status

from .audit import AuditRecord
from .auth import MUTATION_ROLES, Actor
from .fleet_profile_contract import (
    FleetProfileApplicationCancelRequest,
    FleetProfileApplicationView,
)
from .fleet_profiles import FleetProfileConflict, FleetProfilePermissionDenied
from .operation_api import bounded_error_responses

_CANCEL_PATH = "/api/profile/applications/{application_id}/cancel"
_CANCEL_RECEIPT_PATH = (
    "/api/profile/applications/{application_id}/cancellations/{request_key}"
)
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


def install_profile_application_cancel_route(
    app: FastAPI,
    *,
    actor_dependency: Any,
    profiles: Any | None,
    audits: Any,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

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
        application_id: str = Path(pattern=_UUID),
        request_key: str = Path(pattern=_UUID),
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
        except FleetProfileConflict:
            raise HTTPException(
                status_code=503, detail="Profile cancellation receipt unavailable"
            ) from None
        except (OSError, RuntimeError, TypeError, ValueError):
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
    def cancel_profile_application(
        request: Request,
        body: FleetProfileApplicationCancelRequest,
        application_id: str = Path(pattern=_UUID),
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
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Profile cancellation unavailable"
            ) from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "profile.application.cancel",
                None,
                (str(body.profile_number), result.id, body.request_key),
            )
        )
        return result
