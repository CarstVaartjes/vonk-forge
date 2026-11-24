"""Authenticated numbered whole-fleet Profile operator routes."""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from vonk_forge_contracts import ENDPOINT_ALIAS_PATTERN
from vonk_forge_contracts.endpoint_alias import ENDPOINT_ALIAS_MAX_LENGTH

from vonk_agent_protocol.integer_domains import MAX_DATABASE_INTEGER
from vonk_agent_protocol.outcome import InvalidRequestError
from vonk_control.api_availability import AVAILABILITY_ERRORS
from vonk_control.auth import MUTATION_ROLES, Actor
from vonk_control.endpoint_contract import inference_gateway_api_base
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileDefinitionView,
    FleetProfileDeleteRequest,
    FleetProfileDeletion,
    FleetProfileInput,
    FleetProfileList,
    FleetProfileReadView,
    FleetProfileView,
)
from vonk_control.fleet_profile_contract.endpoints import FleetProfileEndpointsView
from vonk_control.fleet_profile_contract.review import (
    FleetProfileApplicationView,
    FleetProfileLoadRequest,
    FleetProfilePreviewRequest,
    FleetProfilePreviewResponse,
)
from vonk_control.fleet_profiles.contracts import (
    FleetProfileConflict,
    FleetProfilePermissionDenied,
)
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.identifiers import UUID_PATTERN
from vonk_control.operation_api.constants import _ADMIN_OPERATION_IDS
from vonk_control.operation_api.contracts import bounded_error_responses
from vonk_control.operator_acceptance import (
    OperatorReceiptResolver,
    operator_acceptance,
)
from vonk_control.operator_receipt_contract import ReceiptPolicy

#: The canonical lowercase UUID shape every durable identity in this API uses.
_PROFILE_PATH = "/api/profile/{number}"
_APPLICATION_PATH = "/api/profile/applications/{application_id}"
_REQUEST_PATH = "/api/profile/{number}/requests/{request_key}"
FLEET_PROFILE_OPERATION_IDS = {
    ("get", "/api/profile"): "listProfiles",
    ("get", _PROFILE_PATH): "getProfile",
    ("get", "/api/profile/{number}/definition"): "getProfileDefinition",
    ("put", _PROFILE_PATH): "autosaveProfile",
    ("post", "/api/profile/{number}/preview"): "previewProfile",
    ("post", "/api/profile/{number}/load"): "loadProfile",
    ("post", "/api/profile/{number}/delete"): "deleteProfile",
    ("get", "/api/profile/{number}/progress"): "getProfileProgress",
    ("get", "/api/profile/{number}/endpoints"): "getProfileEndpoints",
    ("get", _APPLICATION_PATH): "getProfileApplication",
    ("get", _REQUEST_PATH): "getProfileApplicationByRequest",
}


def raise_profile_conflict(error: FleetProfileConflict) -> NoReturn:
    """Answer a Fleet profile conflict by what asking again does.

    A refusal never succeeds as sent: 409 naming its typed code, so a client
    fails fast with the detail. Anything else is a wait (a busy admission owner,
    lock contention, evidence that cannot settle here): it propagates, and the
    route policy answers it 503 with ``Retry-After``.
    """

    if isinstance(error, InvalidRequestError):
        code = getattr(error, "code", None)
        raise HTTPException(
            status_code=409,
            detail=str(error)[:256],
            headers={"x-vonk-error-code": str(code)} if code else None,
        ) from None
    raise error


def install_fleet_profile_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    profiles: Any | None,
    operations: Any | None = None,
) -> None:

    _ADMIN_OPERATION_IDS.update(FLEET_PROFILE_OPERATION_IDS)
    authenticated = actor_dependency

    def service() -> Any:
        if profiles is None:
            raise HTTPException(status_code=503, detail="Profiles unavailable")
        return profiles

    def require_mutation(actor: Actor, method: str, route: str) -> None:
        if actor.role not in MUTATION_ROLES[(method, route)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    profile_dependency = Depends(service)

    @app.get(
        "/api/profile",
        response_model=FleetProfileList,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="listProfiles",
    )
    def list_profiles(
        _actor: Actor = authenticated,
        profile_service: FleetProfileService = profile_dependency,
        after: Annotated[int, Query(ge=0, le=MAX_DATABASE_INTEGER)] = 0,
    ) -> FleetProfileList:
        try:
            return (
                profile_service.list(after=after) if after else profile_service.list()
            )
        except HTTPException:
            raise
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profiles unavailable"
            ) from None

    @app.get(
        _PROFILE_PATH,
        response_model=FleetProfileReadView,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="getProfile",
    )
    def get_profile(
        number: Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)],
        _actor: Actor = authenticated,
    ) -> FleetProfileReadView:
        try:
            return service().read_number(number)
        except HTTPException:
            raise
        except AVAILABILITY_ERRORS:
            raise HTTPException(status_code=503, detail="Profile unavailable") from None

    @app.get(
        "/api/profile/{number}/definition",
        response_model=FleetProfileDefinitionView,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="getProfileDefinition",
    )
    def get_profile_definition(
        number: Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)],
        _actor: Actor = authenticated,
    ) -> FleetProfileDefinitionView:
        try:
            return service().definition_number(number)
        except HTTPException:
            raise
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile definition unavailable"
            ) from None

    @app.get(
        "/api/profile/{number}/endpoints",
        response_model=FleetProfileEndpointsView,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getProfileEndpoints",
    )
    def profile_endpoints(
        request: Request,
        number: Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)],
        alias: Annotated[
            str | None,
            Query(pattern=ENDPOINT_ALIAS_PATTERN, max_length=ENDPOINT_ALIAS_MAX_LENGTH),
        ] = None,
        _actor: Actor = authenticated,
    ) -> FleetProfileEndpointsView:
        projection = getattr(operations, "profile_endpoint", None)
        if not callable(projection):
            raise HTTPException(
                status_code=503, detail="Profile endpoint projection unavailable"
            )
        try:
            gateway = inference_gateway_api_base(request.headers.get("host"))
            return FleetProfileEndpointsView.model_validate(
                projection(number, alias, gateway)
            )
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"endpoint alias is not part of profile {number}",
            ) from None
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile endpoint projection unavailable"
            ) from None

    @app.put(
        _PROFILE_PATH,
        response_model=FleetProfileView,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        operation_id="autosaveProfile",
    )
    @operator_acceptance(ReceiptPolicy.PROFILE_SAVE)
    def autosave_profile(
        number: Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)],
        body: FleetProfileInput,
        actor: Actor = authenticated,
    ) -> FleetProfileView:
        require_mutation(actor, "PUT", _PROFILE_PATH)
        try:
            result = service().update_number(number, body, actor=actor.subject)
        except FleetProfileConflict as error:
            raise_profile_conflict(error)
        except KeyError:
            raise HTTPException(
                status_code=422, detail="invalid profile number"
            ) from None
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile save unavailable"
            ) from None
        return result

    @app.post(
        "/api/profile/{number}/preview",
        response_model=FleetProfilePreviewResponse,
        responses=bounded_error_responses(401, 404, 409, 422, 503),
        operation_id="previewProfile",
    )
    @operator_acceptance(ReceiptPolicy.OBSERVATION)
    def preview_profile(
        number: Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)],
        body: FleetProfilePreviewRequest | None = None,
        actor: Actor = authenticated,
    ) -> FleetProfilePreviewResponse:
        require_mutation(actor, "POST", "/api/profile/{number}/preview")
        try:
            if body is not None:
                return FleetProfilePreviewResponse.of(
                    service().preview_draft(number, body.definition)
                )
            profile = service().definition_number(number)
            if profile.id is None:
                raise KeyError(number)
            return FleetProfilePreviewResponse.of(service().preview(profile.id))
        except KeyError:
            raise HTTPException(status_code=404, detail="Profile not found") from None
        except FleetProfileConflict as error:
            raise_profile_conflict(error)
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile preview unavailable"
            ) from None

    @app.post(
        "/api/profile/{number}/load",
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="loadProfile",
    )
    @operator_acceptance(ReceiptPolicy.PROFILE_LOAD)
    def load_profile(
        number: Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)],
        body: FleetProfileLoadRequest,
        actor: Actor = authenticated,
    ) -> FleetProfileApplicationView:
        require_mutation(actor, "POST", "/api/profile/{number}/load")
        try:
            result = service().load(
                number,
                actor=actor.subject,
                request_key=body.request_key,
                reviewed_effects_digest=(
                    body.review.effects_digest if body.review is not None else None
                ),
            )
        except FleetProfilePermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        except KeyError:
            raise HTTPException(status_code=404, detail="Profile not found") from None
        except FleetProfileConflict as error:
            raise_profile_conflict(error)
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile load unavailable"
            ) from None
        return result

    @app.post(
        "/api/profile/{number}/delete",
        response_model=FleetProfileDeletion,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        operation_id="deleteProfile",
    )
    @operator_acceptance(ReceiptPolicy.OBSERVATION)
    def delete_profile(
        number: Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)],
        body: FleetProfileDeleteRequest,
        actor: Actor = authenticated,
    ) -> FleetProfileDeletion:
        require_mutation(actor, "POST", "/api/profile/{number}/delete")
        try:
            return service().delete_number(
                number, actor=actor.subject, expected_revision=body.expected_revision
            )
        except FleetProfilePermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        except FleetProfileConflict as error:
            raise_profile_conflict(error)
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile delete unavailable"
            ) from None

    @app.get(
        _APPLICATION_PATH,
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getProfileApplication",
    )
    def profile_application(
        application_id: Annotated[str, Path(pattern=UUID_PATTERN)],
        _actor: Actor = authenticated,
    ) -> FleetProfileApplicationView:
        # A client that submitted one load must be able to follow that exact
        # application.  The numbered progress route answers with the profile's
        # latest application, which is a different durable operation as soon as
        # anyone loads the profile again.
        try:
            return service().application(application_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Profile application not found"
            ) from None
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile application unavailable"
            ) from None

    @app.get(
        _REQUEST_PATH,
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="getProfileApplicationByRequest",
    )
    def profile_application_by_request(
        number: Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)],
        request_key: Annotated[str, Path(pattern=UUID_PATTERN)],
        actor: Actor = authenticated,
    ) -> FleetProfileApplicationView:
        try:
            return service().application_by_request_key(
                request_key, actor=actor.subject, number=number
            )
        except FleetProfilePermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Profile request not found"
            ) from None
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile request unavailable"
            ) from None

    @app.get(
        "/api/profile/{number}/progress",
        response_model=FleetProfileApplicationView,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getProfileProgress",
    )
    def profile_progress(
        number: Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)],
        _actor: Actor = authenticated,
    ) -> FleetProfileApplicationView:
        try:
            return service().progress_number(number)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Profile progress not found"
            ) from None
        except AVAILABILITY_ERRORS:
            raise HTTPException(
                status_code=503, detail="Profile progress unavailable"
            ) from None

    OperatorReceiptResolver(profiles=profiles).bind(app)


__all__ = ["FLEET_PROFILE_OPERATION_IDS", "install_fleet_profile_routes"]
