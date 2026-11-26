"""Administrator routes for external models and their credential bindings."""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi import Path as PathParameter

from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_agent_protocol.outcome import InvalidRequestError, UnknownOutcomeError
from vonk_control.api_availability import AVAILABILITY_ERRORS
from vonk_control.auth import MUTATION_ROLES, Actor
from vonk_control.gateway.alias_namespace import AliasTaken
from vonk_control.gateway.external_api_contract import (
    ExternalBindingAccepted,
    ExternalBindingAcceptRequest,
    ExternalBindingList,
    ExternalBindingPreviewRequest,
    ExternalBindingReview,
    ExternalBindingView,
    ExternalModelAccepted,
    ExternalModelAcceptRequest,
    ExternalModelList,
    ExternalModelPreviewRequest,
    ExternalModelReview,
    ExternalModelView,
    ExternalValidateRequest,
    ExternalValidation,
)
from vonk_control.gateway.external_contract import BINDING_NAME_PATTERN
from vonk_control.gateway.external_service import ExternalModelService
from vonk_control.operation_api.constants import _ADMIN_OPERATION_IDS
from vonk_control.operation_api.contracts import bounded_error_responses
from vonk_control.operator_acceptance.receipt_contract import ReceiptPolicy
from vonk_control.operator_acceptance.resolution import operator_acceptance

_MODELS = "/api/gateway/models"
_VALIDATE = "/api/gateway/models/validate"
_MODEL = "/api/gateway/models/{alias}"
_MODEL_PREVIEW = "/api/gateway/models/{alias}/preview"
_MODEL_ACCEPT = "/api/gateway/models/{alias}/accept"
_MODEL_RECEIPT = "/api/gateway/models/{alias}/requests"
_CREDENTIALS = "/api/gateway/credentials"
_CREDENTIAL_PREVIEW = "/api/gateway/credentials/{name}/preview"
_CREDENTIAL_ACCEPT = "/api/gateway/credentials/{name}/accept"
_CREDENTIAL_RECEIPT = "/api/gateway/credentials/{name}/requests"

EXTERNAL_OPERATION_IDS = {
    ("get", _MODELS): "listGatewayModels",
    ("post", _VALIDATE): "validateGatewayModel",
    ("get", _MODEL): "getGatewayModel",
    ("post", _MODEL_PREVIEW): "previewGatewayModel",
    ("post", _MODEL_ACCEPT): "acceptGatewayModel",
    ("get", _MODEL_RECEIPT): "getGatewayModelReceipt",
    ("get", _CREDENTIALS): "listGatewayCredentials",
    ("post", _CREDENTIAL_PREVIEW): "previewGatewayCredential",
    ("post", _CREDENTIAL_ACCEPT): "acceptGatewayCredential",
    ("get", _CREDENTIAL_RECEIPT): "getGatewayCredentialReceipt",
}

_ALIAS = PathParameter(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", max_length=128)
_NAME = PathParameter(pattern=f"^{BINDING_NAME_PATTERN}$", max_length=63)


def _refuse(error: InvalidRequestError) -> NoReturn:
    """Answer a refusal by what asking again does: a conflict or a missing name never succeeds as sent."""

    status = {
        InvalidRequestReason.NOT_FOUND: 404,
        InvalidRequestReason.CONFLICT: 409,
    }.get(error.typed_reason or InvalidRequestReason.MALFORMED, 422)
    code = getattr(error, "code", None)
    raise HTTPException(
        status_code=status,
        detail=str(error)[:256],
        headers={"x-vonk-error-code": str(code)} if code else None,
    ) from None


def _unavailable() -> NoReturn:
    raise HTTPException(
        status_code=503,
        detail="The external model owner is unavailable",
        headers={"retry-after": "5"},
    ) from None


def install_external_model_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    service: ExternalModelService | None,
) -> None:
    _ADMIN_OPERATION_IDS.update(EXTERNAL_OPERATION_IDS)

    def owner() -> ExternalModelService:
        if service is None:
            _unavailable()
        return service

    owned = Depends(owner)

    def read(actor: Actor) -> None:
        if actor.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")

    def mutate(actor: Actor, route: str) -> ExternalModelService:
        if actor.role not in MUTATION_ROLES[("POST", route)]:
            raise HTTPException(status_code=403, detail="insufficient role")
        return owner()

    @app.get(
        _MODELS,
        response_model=ExternalModelList,
        responses=bounded_error_responses(401, 403, 503),
        operation_id="listGatewayModels",
    )
    def list_models(
        actor: Actor = actor_dependency, models: ExternalModelService = owned
    ) -> ExternalModelList:
        read(actor)
        return models.list_models()

    @app.post(
        _VALIDATE,
        response_model=ExternalValidation,
        responses=bounded_error_responses(401, 403, 422, 503),
        operation_id="validateGatewayModel",
    )
    @operator_acceptance(ReceiptPolicy.OBSERVATION)
    def validate_model(
        body: ExternalValidateRequest, actor: Actor = actor_dependency
    ) -> ExternalValidation:
        try:
            return mutate(actor, _VALIDATE).validate(body.entry)
        except AVAILABILITY_ERRORS:
            _unavailable()

    @app.get(
        _MODEL,
        response_model=ExternalModelView,
        responses=bounded_error_responses(401, 403, 404, 503),
        operation_id="getGatewayModel",
    )
    def get_model(
        alias: Annotated[str, _ALIAS],
        actor: Actor = actor_dependency,
        models: ExternalModelService = owned,
    ) -> ExternalModelView:
        read(actor)
        try:
            return models.get_model(alias)
        except InvalidRequestError:
            raise HTTPException(status_code=404, detail="No such model") from None

    @app.post(
        _MODEL_PREVIEW,
        response_model=ExternalModelReview,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="previewGatewayModel",
    )
    @operator_acceptance(ReceiptPolicy.OBSERVATION)
    def preview_model(
        alias: Annotated[str, _ALIAS],
        body: ExternalModelPreviewRequest,
        actor: Actor = actor_dependency,
    ) -> ExternalModelReview:
        try:
            return mutate(actor, _MODEL_PREVIEW).preview_model(alias, body)
        except InvalidRequestError as error:
            _refuse(error)
        except AVAILABILITY_ERRORS:
            _unavailable()

    @app.post(
        _MODEL_ACCEPT,
        response_model=ExternalModelAccepted,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="acceptGatewayModel",
    )
    @operator_acceptance(ReceiptPolicy.GATEWAY_MODEL)
    def accept_model(
        alias: Annotated[str, _ALIAS],
        body: ExternalModelAcceptRequest,
        actor: Actor = actor_dependency,
    ) -> ExternalModelAccepted:
        try:
            return mutate(actor, _MODEL_ACCEPT).accept_model(alias, body)
        except AliasTaken as error:
            raise HTTPException(
                status_code=409,
                detail=str(error)[:256],
                headers={"x-vonk-error-code": str(error.code)},
            ) from None
        except InvalidRequestError as error:
            _refuse(error)
        except (UnknownOutcomeError, *AVAILABILITY_ERRORS):
            _unavailable()

    @app.get(
        _MODEL_RECEIPT,
        response_model=ExternalModelView,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="getGatewayModelReceipt",
    )
    def model_receipt(
        alias: Annotated[str, _ALIAS],
        request_id: Annotated[str, Query(min_length=1, max_length=128)],
        actor: Actor = actor_dependency,
        models: ExternalModelService = owned,
    ) -> ExternalModelView:
        read(actor)
        try:
            return models.receipt(alias, request_id)
        except InvalidRequestError:
            raise HTTPException(status_code=404, detail="No such request") from None

    @app.get(
        _CREDENTIALS,
        response_model=ExternalBindingList,
        responses=bounded_error_responses(401, 403, 503),
        operation_id="listGatewayCredentials",
    )
    def list_credentials(
        actor: Actor = actor_dependency, models: ExternalModelService = owned
    ) -> ExternalBindingList:
        read(actor)
        return models.list_bindings()

    @app.post(
        _CREDENTIAL_PREVIEW,
        response_model=ExternalBindingReview,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="previewGatewayCredential",
    )
    @operator_acceptance(ReceiptPolicy.OBSERVATION)
    def preview_credential(
        name: Annotated[str, _NAME],
        body: ExternalBindingPreviewRequest,
        actor: Actor = actor_dependency,
    ) -> ExternalBindingReview:
        try:
            return mutate(actor, _CREDENTIAL_PREVIEW).preview_binding(name, body)
        except InvalidRequestError as error:
            _refuse(error)
        except AVAILABILITY_ERRORS:
            _unavailable()

    @app.post(
        _CREDENTIAL_ACCEPT,
        response_model=ExternalBindingAccepted,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="acceptGatewayCredential",
    )
    @operator_acceptance(ReceiptPolicy.GATEWAY_CREDENTIAL)
    def accept_credential(
        name: Annotated[str, _NAME],
        body: ExternalBindingAcceptRequest,
        actor: Actor = actor_dependency,
    ) -> ExternalBindingAccepted:
        try:
            return mutate(actor, _CREDENTIAL_ACCEPT).accept_binding(
                name, body, actor=actor.subject
            )
        except InvalidRequestError as error:
            _refuse(error)
        except AVAILABILITY_ERRORS:
            _unavailable()

    @app.get(
        _CREDENTIAL_RECEIPT,
        response_model=ExternalBindingView,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="getGatewayCredentialReceipt",
    )
    def credential_receipt(
        name: Annotated[str, _NAME],
        request_id: Annotated[str, Query(min_length=1, max_length=128)],
        actor: Actor = actor_dependency,
        models: ExternalModelService = owned,
    ) -> ExternalBindingView:
        read(actor)
        try:
            return models.binding_receipt(name, request_id)
        except InvalidRequestError:
            raise HTTPException(status_code=404, detail="No such request") from None
