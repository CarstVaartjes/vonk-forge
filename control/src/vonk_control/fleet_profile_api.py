"""Authenticated numbered whole-fleet Profile operator routes."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Request, status

from .audit import AuditRecord
from .auth import MUTATION_ROLES, Actor
from .fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileInput,
    FleetProfileList,
    FleetProfileLoadRequest,
    FleetProfilePreview,
    FleetProfileView,
)
from .fleet_profiles import FleetProfileConflict
from .operation_api import bounded_error_responses

_PROFILE_PATH = "/api/profile/{number}"
FLEET_PROFILE_OPERATION_IDS = {
    ("get", "/api/profile"): "listProfiles",
    ("get", _PROFILE_PATH): "getProfile",
    ("put", _PROFILE_PATH): "autosaveProfile",
    ("post", "/api/profile/{number}/preview"): "previewProfile",
    ("post", "/api/profile/{number}/load"): "loadProfile",
    ("get", "/api/profile/{number}/progress"): "getProfileProgress",
}


def install_fleet_profile_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    profiles: Any | None,
    audits: Any,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(FLEET_PROFILE_OPERATION_IDS)
    authenticated = actor_dependency

    def service() -> Any:
        if profiles is None:
            raise HTTPException(status_code=503, detail="Profiles unavailable")
        return profiles

    def require_mutation(actor: Actor, method: str, route: str) -> None:
        if actor.role not in MUTATION_ROLES[(method, route)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    def audit(request: Request, actor: Actor, action: str, targets: tuple[str, ...]) -> None:
        audits.append(
            AuditRecord(request.state.request_id, actor.subject, action, None, targets)
        )

    @app.get(
        "/api/profile",
        response_model=FleetProfileList,
        responses=bounded_error_responses(401, 503),
        operation_id="listProfiles",
    )
    def list_profiles(_actor: Actor = authenticated) -> FleetProfileList:
        try:
            return service().list()
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="Profiles unavailable") from None

    @app.get(
        _PROFILE_PATH,
        response_model=FleetProfileView,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="getProfile",
    )
    def get_profile(
        number: Annotated[int, Path(ge=1)], _actor: Actor = authenticated
    ) -> FleetProfileView:
        try:
            return service().read_number(number)
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="Profile unavailable") from None

    @app.put(
        _PROFILE_PATH,
        response_model=FleetProfileView,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        operation_id="autosaveProfile",
    )
    def autosave_profile(
        request: Request,
        number: Annotated[int, Path(ge=1)],
        body: FleetProfileInput,
        actor: Actor = authenticated,
    ) -> FleetProfileView:
        require_mutation(actor, "PUT", _PROFILE_PATH)
        try:
            result = service().update_number(number, body, actor=actor.subject)
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except KeyError:
            raise HTTPException(status_code=422, detail="invalid profile number") from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="Profile save unavailable") from None
        audit(request, actor, "profile.autosave", (str(number),))
        return result

    @app.post(
        "/api/profile/{number}/preview",
        response_model=FleetProfilePreview,
        responses=bounded_error_responses(401, 404, 409, 422, 503),
        operation_id="previewProfile",
    )
    def preview_profile(
        number: Annotated[int, Path(ge=1)], _actor: Actor = authenticated
    ) -> FleetProfilePreview:
        try:
            profile = service().get_number(number)
            return service().preview(profile.id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Profile not found") from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="Profile preview unavailable") from None

    @app.post(
        "/api/profile/{number}/load",
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="loadProfile",
    )
    def load_profile(
        request: Request,
        number: Annotated[int, Path(ge=1)],
        body: FleetProfileLoadRequest,
        actor: Actor = authenticated,
    ) -> FleetProfileApplicationView:
        require_mutation(actor, "POST", "/api/profile/{number}/load")
        request_key = body.request_key or str(uuid.uuid4())
        try:
            result = service().load(number, actor=actor.subject, request_key=request_key)
        except KeyError:
            raise HTTPException(status_code=404, detail="Profile not found") from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="Profile load unavailable") from None
        audit(request, actor, "profile.load", (str(number), result.id))
        return result

    @app.get(
        "/api/profile/{number}/progress",
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getProfileProgress",
    )
    def profile_progress(
        number: Annotated[int, Path(ge=1)], _actor: Actor = authenticated
    ) -> FleetProfileApplicationView:
        try:
            return service().progress_number(number)
        except KeyError:
            raise HTTPException(status_code=404, detail="Profile progress not found") from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="Profile progress unavailable") from None


__all__ = ["FLEET_PROFILE_OPERATION_IDS", "install_fleet_profile_routes"]
