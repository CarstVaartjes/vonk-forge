"""Authenticated saved Fleet profile and profile-application routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Request, Response, status

from .audit import AuditRecord
from .auth import MUTATION_ROLES, Actor
from .fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileApplyRequest,
    FleetProfileCaptureInput,
    FleetProfileDuplicateInput,
    FleetProfileInput,
    FleetProfileList,
    FleetProfilePreparePreviewRequest,
    FleetProfilePrepareRequest,
    FleetProfilePreview,
    FleetProfilePreviewRequest,
    FleetProfileStatusView,
    FleetProfileView,
)
from .fleet_profiles import FleetProfileConflict
from .operation_api import bounded_error_responses

FLEET_PROFILE_OPERATION_IDS = {
    ("get", "/api/v1/fleet-profiles"): "listFleetProfiles",
    ("post", "/api/v1/fleet-profiles"): "createFleetProfile",
    ("post", "/api/v1/fleet-profiles/capture-current"): (
        "captureCurrentFleetProfile"
    ),
    ("get", "/api/v1/fleet-profiles/{profile_id}"): "getFleetProfile",
    ("put", "/api/v1/fleet-profiles/{profile_id}"): "updateFleetProfile",
    ("delete", "/api/v1/fleet-profiles/{profile_id}"): "deleteFleetProfile",
    ("post", "/api/v1/fleet-profiles/{profile_id}/preview"): "previewFleetProfile",
    ("post", "/api/v1/fleet-profiles/{profile_id}/apply"): "applyFleetProfile",
    ("post", "/api/v1/fleet-profiles/{profile_id}/duplicate"): (
        "duplicateFleetProfile"
    ),
    ("get", "/api/v1/fleet-profiles/{profile_id}/status"): (
        "getFleetProfileStatus"
    ),
    ("post", "/api/v1/fleet-profiles/{profile_id}/prepare"): (
        "prepareFleetProfile"
    ),
    ("post", "/api/v1/fleet-profiles/{profile_id}/prepare/preview"): (
        "previewFleetProfilePreparation"
    ),
    ("post", "/api/v1/fleet-profiles/{profile_id}/switch"): "switchFleetProfile",
    (
        "get",
        "/api/v1/fleet-profile-applications/{application_id}",
    ): "getFleetProfileApplication",
}
_UUID = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


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
            raise HTTPException(status_code=503, detail="Fleet profiles unavailable")
        return profiles

    def require_mutation(actor: Actor, method: str, route: str) -> None:
        if actor.role not in MUTATION_ROLES[(method, route)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    def audit(
        request: Request, actor: Actor, action: str, targets: tuple[str, ...]
    ) -> None:
        audits.append(
            AuditRecord(request.state.request_id, actor.subject, action, None, targets)
        )

    @app.get(
        "/api/v1/fleet-profiles",
        response_model=FleetProfileList,
        responses=bounded_error_responses(401, 503),
        operation_id="listFleetProfiles",
    )
    def list_profiles(_actor: Actor = authenticated) -> FleetProfileList:
        try:
            return service().list()
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profiles unavailable"
            ) from None

    @app.post(
        "/api/v1/fleet-profiles",
        response_model=FleetProfileView,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        status_code=status.HTTP_201_CREATED,
        operation_id="createFleetProfile",
    )
    def create_profile(
        request: Request, body: FleetProfileInput, actor: Actor = authenticated
    ) -> FleetProfileView:
        require_mutation(actor, "POST", "/api/v1/fleet-profiles")
        try:
            result = service().create(body, actor=actor.subject)
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile create unavailable"
            ) from None
        audit(request, actor, "fleet-profile.create", (result.id,))
        return result

    @app.post(
        "/api/v1/fleet-profiles/capture-current",
        response_model=FleetProfileView,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        status_code=status.HTTP_201_CREATED,
        operation_id="captureCurrentFleetProfile",
    )
    def capture_current_profile(
        request: Request,
        body: FleetProfileCaptureInput,
        actor: Actor = authenticated,
    ) -> FleetProfileView:
        require_mutation(actor, "POST", "/api/v1/fleet-profiles/capture-current")
        try:
            result = service().capture_current(
                name=body.name,
                description=body.description,
                installation_policy=body.installation_policy,
                labels=body.labels,
                favorite=body.favorite,
                actor=actor.subject,
            )
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile capture unavailable"
            ) from None
        audit(request, actor, "fleet-profile.capture-current", (result.id,))
        return result

    @app.get(
        "/api/v1/fleet-profiles/{profile_id}",
        response_model=FleetProfileView,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetProfile",
    )
    def get_profile(
        profile_id: Annotated[str, Path(pattern=_UUID)], _actor: Actor = authenticated
    ) -> FleetProfileView:
        try:
            return service().get(profile_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile unavailable"
            ) from None

    @app.put(
        "/api/v1/fleet-profiles/{profile_id}",
        response_model=FleetProfileView,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="updateFleetProfile",
    )
    def update_profile(
        request: Request,
        profile_id: Annotated[str, Path(pattern=_UUID)],
        body: FleetProfileInput,
        actor: Actor = authenticated,
    ) -> FleetProfileView:
        route = "/api/v1/fleet-profiles/{profile_id}"
        require_mutation(actor, "PUT", route)
        try:
            result = service().update(profile_id, body, actor=actor.subject)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile update unavailable"
            ) from None
        audit(request, actor, "fleet-profile.update", (profile_id,))
        return result

    @app.delete(
        "/api/v1/fleet-profiles/{profile_id}",
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="deleteFleetProfile",
    )
    def delete_profile(
        request: Request,
        profile_id: Annotated[str, Path(pattern=_UUID)],
        actor: Actor = authenticated,
    ) -> Response:
        route = "/api/v1/fleet-profiles/{profile_id}"
        require_mutation(actor, "DELETE", route)
        try:
            service().delete(profile_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile delete unavailable"
            ) from None
        audit(request, actor, "fleet-profile.delete", (profile_id,))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/v1/fleet-profiles/{profile_id}/preview",
        response_model=FleetProfilePreview,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="previewFleetProfile",
    )
    def preview_profile(
        profile_id: Annotated[str, Path(pattern=_UUID)],
        body: FleetProfilePreviewRequest,
        actor: Actor = authenticated,
    ) -> FleetProfilePreview:
        del body
        route = "/api/v1/fleet-profiles/{profile_id}/preview"
        require_mutation(actor, "POST", route)
        try:
            return service().preview(profile_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile preview unavailable"
            ) from None

    @app.get(
        "/api/v1/fleet-profiles/{profile_id}/status",
        response_model=FleetProfileStatusView,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetProfileStatus",
    )
    def profile_status(
        profile_id: Annotated[str, Path(pattern=_UUID)],
        _actor: Actor = authenticated,
    ) -> FleetProfileStatusView:
        try:
            return service().status(profile_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile status unavailable"
            ) from None

    @app.post(
        "/api/v1/fleet-profiles/{profile_id}/duplicate",
        response_model=FleetProfileView,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_201_CREATED,
        operation_id="duplicateFleetProfile",
    )
    def duplicate_profile(
        request: Request,
        profile_id: Annotated[str, Path(pattern=_UUID)],
        body: FleetProfileDuplicateInput,
        actor: Actor = authenticated,
    ) -> FleetProfileView:
        require_mutation(
            actor, "POST", "/api/v1/fleet-profiles/{profile_id}/duplicate"
        )
        try:
            result = service().duplicate(
                profile_id,
                name=body.name,
                description=body.description,
                actor=actor.subject,
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile duplicate unavailable"
            ) from None
        audit(request, actor, "fleet-profile.duplicate", (profile_id, result.id))
        return result
    @app.post(
        "/api/v1/fleet-profiles/{profile_id}/apply",
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="applyFleetProfile",
    )
    def apply_profile(
        request: Request,
        profile_id: Annotated[str, Path(pattern=_UUID)],
        body: FleetProfileApplyRequest,
        actor: Actor = authenticated,
    ) -> FleetProfileApplicationView:
        route = "/api/v1/fleet-profiles/{profile_id}/apply"
        require_mutation(actor, "POST", route)
        try:
            result = service().apply(
                profile_id,
                plan_digest=body.plan_digest,
                request_key=body.request_key,
                actor=actor.subject,
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile application unavailable"
            ) from None
        audit(request, actor, "fleet-profile.apply", (profile_id, result.id))
        return result

    @app.post(
        "/api/v1/fleet-profiles/{profile_id}/prepare/preview",
        response_model=FleetProfilePreview,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="previewFleetProfilePreparation",
    )
    def prepare_preview_profile(
        profile_id: Annotated[str, Path(pattern=_UUID)],
        body: FleetProfilePreparePreviewRequest,
        actor: Actor = authenticated,
    ) -> FleetProfilePreview:
        del body
        route = "/api/v1/fleet-profiles/{profile_id}/prepare/preview"
        require_mutation(actor, "POST", route)
        try:
            return service().prepare_preview(profile_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile preparation preview unavailable"
            ) from None

    @app.post(
        "/api/v1/fleet-profiles/{profile_id}/prepare",
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="prepareFleetProfile",
    )
    def prepare_profile(
        request: Request,
        profile_id: Annotated[str, Path(pattern=_UUID)],
        body: FleetProfilePrepareRequest,
        actor: Actor = authenticated,
    ) -> FleetProfileApplicationView:
        require_mutation(
            actor, "POST", "/api/v1/fleet-profiles/{profile_id}/prepare"
        )
        try:
            result = service().prepare(
                profile_id,
                plan_digest=body.plan_digest,
                request_key=body.request_key,
                actor=actor.subject,
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile preparation unavailable"
            ) from None
        audit(request, actor, "fleet-profile.prepare", (profile_id, result.id))
        return result

    @app.post(
        "/api/v1/fleet-profiles/{profile_id}/switch",
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="switchFleetProfile",
    )
    def switch_profile(
        request: Request,
        profile_id: Annotated[str, Path(pattern=_UUID)],
        body: FleetProfileApplyRequest,
        actor: Actor = authenticated,
    ) -> FleetProfileApplicationView:
        require_mutation(
            actor, "POST", "/api/v1/fleet-profiles/{profile_id}/switch"
        )
        try:
            result = service().switch(
                profile_id,
                plan_digest=body.plan_digest,
                request_key=body.request_key,
                actor=actor.subject,
            )
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile not found"
            ) from None
        except FleetProfileConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile switch unavailable"
            ) from None
        audit(request, actor, "fleet-profile.switch", (profile_id, result.id))
        return result

    @app.get(
        "/api/v1/fleet-profile-applications/{application_id}",
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetProfileApplication",
    )
    def get_application(
        application_id: Annotated[str, Path(pattern=_UUID)],
        _actor: Actor = authenticated,
    ) -> FleetProfileApplicationView:
        try:
            return service().application(application_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Fleet profile application not found"
            ) from None
        except HTTPException:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Fleet profile application unavailable"
            ) from None


__all__ = ["FLEET_PROFILE_OPERATION_IDS", "install_fleet_profile_routes"]
