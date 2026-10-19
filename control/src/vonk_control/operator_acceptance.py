"""Resolve successful operator mutations through their existing durable owner.

This is a response policy, not a receipt store. Readers open the owner's normal
read boundary after the mutation returns (and its transaction has committed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from vonk_agent_protocol import (
    EnrollmentGrantState,
    InvalidRequestError,
    OperatorActionName,
    UnknownError,
    UnknownOutcomeError,
)

from .api_availability import AVAILABILITY_ERRORS
from .auth import Actor
from .operator_receipt_contract import (
    AcceptanceReceipt,
    ReceiptCode,
    ReceiptOwner,
    ReceiptPolicy,
)

if TYPE_CHECKING:
    from .api.common import JobQueue
    from .artifact_jobs import ArtifactJobService
    from .fleet_profiles import FleetProfileService
    from .fleet_projection import FleetProjection
    from .gateway_keys import GatewayKeyService
    from .model_cache import ModelCacheService
    from .operator_projection_api.services import FleetOperatorServices
    from .recipe_image_availability import RecipeImageAvailabilityService
    from .run_switch_operations import RunSwitchOperationService


RECEIPT_HEADER = "X-Vonk-Receipt"


def operator_acceptance(policy: ReceiptPolicy):
    """Declare semantics on the mounted endpoint, independently of its URL."""

    def decorate[T: Callable[..., Any]](endpoint: T) -> T:
        endpoint.__vonk_acceptance__ = policy  # type: ignore[attr-defined]
        return endpoint

    return decorate


def receipt_policy(endpoint: Callable[..., Any]) -> ReceiptPolicy | None:
    from .strict_json import route_endpoint

    value = getattr(route_endpoint(endpoint), "__vonk_acceptance__", None)
    return value if isinstance(value, ReceiptPolicy) else None


def _refused(detail: str, *, unavailable: bool = False) -> HTTPException:
    code = ReceiptCode.UNAVAILABLE if unavailable else ReceiptCode.MISMATCH
    return HTTPException(
        status_code=503 if unavailable else 409,
        detail=detail,
        headers={
            "x-vonk-error-code": code,
            **({"retry-after": "5"} if unavailable else {}),
        },
    )


def _require(condition: bool) -> None:
    if not condition:
        raise _refused("The owner has no matching acceptance receipt for this request")


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _refused("The response has no accepted request identity")
    return value


def _available[T](value: T | None) -> T:
    if value is None:
        raise _refused(
            "The receipt owner is unavailable; retry the same request", unavailable=True
        )
    return value


@dataclass(frozen=True)
class AcceptanceArguments:
    actor: Actor | None
    request_key: str | None
    request_id: str | None
    number: int | None
    http_request_id: str | None
    job_action: OperatorActionName


def acceptance_arguments(
    policy: ReceiptPolicy | None, **values: Any
) -> AcceptanceArguments | None:
    if policy is None or policy in {ReceiptPolicy.OBSERVATION, ReceiptPolicy.AUTH}:
        return None
    from .operation_api.contracts import JobResumeRequest

    actor = next((value for value in values.values() if isinstance(value, Actor)), None)
    request_key = getattr(values.get("body"), "request_key", None)
    request_id = values.get("request_id")
    number = values.get("number")
    if number is not None and not isinstance(number, int):
        raise _refused("The accepted profile number is invalid")
    request = next(
        (value for value in values.values() if isinstance(value, Request)), None
    )
    http_request_id = (
        None if request is None else getattr(request.state, "request_id", None)
    )
    body = values.get("body")
    job_action = (
        OperatorActionName.RETIRE
        if isinstance(body, JobResumeRequest)
        and body.disposition == OperatorActionName.RETIRE
        else OperatorActionName.RESUME
    )
    return AcceptanceArguments(
        actor,
        None if request_key is None else _identity(request_key),
        None if request_id is None else _identity(request_id),
        number,
        http_request_id,
        job_action,
    )


@dataclass(frozen=True)
class OperatorReceiptResolver:
    # These are the already installed service interfaces, not new persistence.
    jobs: JobQueue | None = None
    profiles: FleetProfileService | None = None
    models: ModelCacheService | None = None
    recipes: RecipeImageAvailabilityService | None = None
    artifacts: ArtifactJobService | None = None
    fleet: FleetProjection | None = None
    fleet_services: FleetOperatorServices | None = None
    keys: GatewayKeyService | None = None
    installations: RunSwitchOperationService | None = None

    def bind(self, app: FastAPI) -> None:
        from .strict_json import ControllerAPIRoute

        for route in app.routes:
            if isinstance(route, ControllerAPIRoute):
                previous = route.operator_receipts or OperatorReceiptResolver()
                route.operator_receipts = replace(
                    previous,
                    **{
                        field.name: getattr(self, field.name)
                        for field in fields(self)
                        if getattr(self, field.name) is not None
                    },
                )

    def resolve(
        self, policy: ReceiptPolicy, result: BaseModel, arguments: AcceptanceArguments
    ) -> AcceptanceReceipt:
        from .artifact_jobs import ArtifactJobResponse
        from .enrollment_contract import EnrollmentGrantStatus
        from .fleet_profile_contract import (
            FleetProfileApplicationView,
            FleetProfileView,
        )
        from .fleet_projection import FleetNodeIdentity
        from .gateway_keys import GatewayKeyCreated, GatewayKeyRevoked
        from .job_operator_receipts import read_job_operator_receipt
        from .model_cache_contract import ModelCacheOperatorResponse
        from .operation_api import JobResumeResponse
        from .operator_projection_api.contracts import FleetActionResponse
        from .recipe_image_availability_api import (
            RecipeImageAvailabilityResponse,
            RecipeOperatorResponse,
        )
        from .recipe_image_availability_view_contract import RecipeCacheRemovalStatus
        from .recipe_image_removal_contract import RecipeRemovalUnavailableView
        from .recipe_update_contract import RecipeUpdateResponse
        from .run_switch_contract import RunSwitchOperation

        actor = arguments.actor
        _require(actor is not None)
        assert actor is not None
        request_key = arguments.request_key
        if isinstance(result, UnknownError):
            raise _refused(
                "Acceptance is not yet observable; retry the same request",
                unavailable=True,
            )
        try:
            if policy == ReceiptPolicy.PROFILE_SAVE and isinstance(
                result, FleetProfileView
            ):
                saved = _available(self.profiles).definition_number(result.number)
                receipt = AcceptanceReceipt(
                    owner=ReceiptOwner.PROFILE,
                    id=result.id,
                    href=f"/api/profile/{result.number}/definition",
                    revision=result.revision,
                    content_digest=result.profile_digest,
                )
                # Identical content can reappear after an intervening save.
                observed = receipt.model_copy(
                    update={"id": saved.id, "revision": saved.revision}
                )
                _require(observed == receipt and saved.definition == result.definition)
                return receipt
            if policy == ReceiptPolicy.PROFILE_LOAD and isinstance(
                result, FleetProfileApplicationView
            ):
                saved = _available(self.profiles).application_by_request_key(
                    _identity(request_key), actor=actor.subject, number=arguments.number
                )
                _require(
                    saved.id == result.id and saved.plan_digest == result.plan_digest
                )
                return AcceptanceReceipt(
                    owner=ReceiptOwner.PROFILE_APPLICATION,
                    id=result.id,
                    href=f"/api/profile/{arguments.number}/requests/{request_key}",
                    request_id=request_key,
                    content_digest=result.plan_digest,
                )
            if policy == ReceiptPolicy.PROFILE_CANCEL and isinstance(
                result, FleetProfileApplicationView
            ):
                saved = _available(self.profiles).application_cancellation_by_request(
                    result.id, _identity(request_key), actor=actor.subject
                )
                _require(saved.id == result.id)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.PROFILE_APPLICATION,
                    id=result.id,
                    href=f"/api/profile/applications/{result.id}/cancellations/{request_key}",
                    request_id=request_key,
                    content_digest=result.plan_digest,
                )
            if isinstance(result, ModelCacheOperatorResponse):
                if policy == ReceiptPolicy.MODEL_CANCEL:
                    saved, action, _ = _available(
                        self.models
                    ).get_operator_cancellation(
                        _identity(result.operation_id),
                        _identity(request_key),
                        actor=actor.subject,
                    )
                    href = (
                        f"/api/model/operations/{saved.id}/cancellations/{request_key}"
                    )
                else:
                    _require(policy == ReceiptPolicy.MODEL_REQUEST)
                    saved, action, _ = _available(self.models).get_operator_request(
                        _identity(request_key), actor=actor.subject
                    )
                    href = f"/api/model/requests/{request_key}"
                _require(saved.id == result.operation_id and action == result.action)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.MODEL_CACHE,
                    id=saved.id,
                    href=href,
                    request_id=request_key,
                    content_digest=saved.plan_digest,
                )
            if isinstance(
                result,
                (
                    RecipeImageAvailabilityResponse,
                    RecipeOperatorResponse,
                    RecipeUpdateResponse,
                ),
            ):
                operation_id = (
                    result.operation_id
                    if isinstance(result, RecipeOperatorResponse)
                    else result.id
                )
                if policy == ReceiptPolicy.RECIPE_CANCEL:
                    saved = _available(self.recipes).cancellation_by_request(
                        operation_id, _identity(request_key), actor=actor.subject
                    )
                    href = f"/api/recipe/operations/{operation_id}/cancellations/{request_key}"
                else:
                    _require(policy == ReceiptPolicy.RECIPE_REQUEST)
                    saved = _available(self.recipes).get_operator_request(
                        _identity(request_key), actor=actor.subject
                    )
                    href = f"/api/recipe/requests/{request_key}"
                _require(
                    saved.operation_id == operation_id
                    if isinstance(
                        saved, (RecipeCacheRemovalStatus, RecipeRemovalUnavailableView)
                    )
                    else saved.id == operation_id
                )
                return AcceptanceReceipt(
                    owner=ReceiptOwner.RECIPE,
                    id=operation_id,
                    href=href,
                    request_id=request_key,
                )
            if isinstance(result, ArtifactJobResponse):
                saved = _available(self.artifacts).get(result.id)
                identity = arguments.request_id
                if policy == ReceiptPolicy.ARTIFACT_CREATE:
                    _require(
                        _available(self.artifacts)
                        .get_by_request_id(_identity(identity))
                        .id
                        == result.id
                    )
                elif policy == ReceiptPolicy.ARTIFACT_SUBMIT:
                    _require(saved.submit_request_id == identity)
                elif policy == ReceiptPolicy.ARTIFACT_CANCEL:
                    evidence = saved.result_evidence
                    _require(
                        evidence is not None
                        and evidence.cancel_request_id == identity
                        and evidence.cancel_actor == actor.subject
                    )
                else:
                    _require(policy == ReceiptPolicy.ARTIFACT_JOB)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.ARTIFACT_JOB,
                    id=result.id,
                    href=f"/api/artifact-jobs/{result.id}",
                    request_id=identity,
                    content_digest=saved.input_manifest_sha256,
                )
            if policy == ReceiptPolicy.JOB and isinstance(result, JobResumeResponse):
                saved = _available(self.jobs).get(result.id)
                receipt = read_job_operator_receipt(
                    saved, _identity(arguments.http_request_id)
                )
                _require(
                    receipt is not None
                    and receipt.request_id == arguments.http_request_id
                    and receipt.actor == actor.subject
                    and receipt.action == arguments.job_action
                )
                assert receipt is not None
                return AcceptanceReceipt(
                    owner=ReceiptOwner.JOB,
                    id=result.id,
                    href=f"/api/jobs/{result.id}/requests/{receipt.request_id}",
                    request_id=receipt.request_id,
                    content_digest=receipt.content_digest,
                )
            if policy == ReceiptPolicy.FLEET_NODE and isinstance(
                result, FleetNodeIdentity
            ):
                # Fleet identity is a synchronous save of the node's current revision.
                saved = next(
                    (
                        node
                        for node in _available(self.fleet).read().nodes
                        if node.id == result.id
                    ),
                    None,
                )
                _require(saved is not None)
                assert saved is not None
                _require(saved.display_name == result.display_name)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.NODE,
                    id=result.id,
                    href=f"/api/fleet/{result.id}",
                )
            if policy == ReceiptPolicy.ENROLLMENT_REVOKE and isinstance(
                result, EnrollmentGrantStatus
            ):
                saved = _available(
                    _available(self.fleet_services).enrollment
                ).grant_status(result.id, actor=actor.subject)
                _require(saved.state == EnrollmentGrantState.REVOKED)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.ENROLLMENT,
                    id=result.id,
                    href=f"/api/fleet/enrollments/{result.id}",
                )
            if policy == ReceiptPolicy.FLEET_ACTION and isinstance(
                result, FleetActionResponse
            ):
                if result.observation is not None:
                    raise _refused(
                        "Fleet acceptance is not observable; retry the same request",
                        unavailable=True,
                    )
                if result.grant is not None or result.grant_status is not None:
                    grant = result.grant or result.grant_status
                    assert grant is not None
                    identity = grant.id
                    _require(identity == request_key)
                    _available(_available(self.fleet_services).enrollment).grant_status(
                        identity, actor=actor.subject
                    )
                    return AcceptanceReceipt(
                        owner=ReceiptOwner.ENROLLMENT,
                        id=identity,
                        href=f"/api/fleet/enrollments/{identity}",
                        request_id=request_key,
                    )
                if result.operation_id is not None:
                    saved = _available(self.jobs).get(result.operation_id)
                    _require(
                        saved.request_id == request_key
                        and saved.actor == actor.subject
                        and saved.payload_digest == result.plan_digest
                    )
                    return AcceptanceReceipt(
                        owner=ReceiptOwner.JOB,
                        id=result.operation_id,
                        href=f"/api/jobs/{result.operation_id}",
                        request_id=request_key,
                        content_digest=result.plan_digest,
                    )
                _require(
                    result.node_id is not None
                    and result.revocation is not None
                    and result.revocation.local_denial_complete
                )
                identity = result.node_id
                assert identity is not None
                _available(
                    _available(self.fleet_services).enrollment
                ).revocation_status(identity)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.NODE,
                    id=identity,
                    href=f"/api/fleet/{identity}/revocation",
                )
            if policy == ReceiptPolicy.KEY and isinstance(
                result, (GatewayKeyCreated, GatewayKeyRevoked)
            ):
                _require(result.request_id is not None)
                identity = result.request_id
                assert identity is not None
                saved = _available(self.keys).receipt(
                    result.name, identity, actor=actor.subject
                )
                _require(saved.completed)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.KEY,
                    id=result.name,
                    request_id=identity,
                    href=f"/api/key/{quote(result.name, safe='')}/requests/{quote(identity, safe='')}",
                )
            if policy == ReceiptPolicy.INSTALLATION and isinstance(
                result, RunSwitchOperation
            ):
                saved = _available(self.installations).get(result.operation_id)
                _require(saved.request_key == request_key)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.RUN_SWITCH,
                    id=result.operation_id,
                    href=f"/api/run-switch/operations/{result.operation_id}",
                    request_id=request_key,
                    content_digest=result.plan_digest,
                )
        except InvalidRequestError as error:
            raise HTTPException(
                status_code=409,
                detail=str(error)[:256],
                headers={
                    "x-vonk-error-code": str(
                        getattr(error, "code", ReceiptCode.MISMATCH)
                    )
                },
            ) from None
        except UnknownOutcomeError:
            raise _refused(
                "The owner cannot yet observe its acceptance receipt", unavailable=True
            ) from None
        except (KeyError, *AVAILABILITY_ERRORS):
            raise _refused(
                "The acceptance receipt is unavailable; retry the same request",
                unavailable=True,
            ) from None
        raise _refused("The response does not identify a committed acceptance receipt")


def apply_receipt(
    policy: ReceiptPolicy | None,
    resolver: OperatorReceiptResolver | None,
    result: Any,
    response: Response,
    arguments: AcceptanceArguments | None,
) -> Response:
    if (
        policy is None
        or policy in {ReceiptPolicy.OBSERVATION, ReceiptPolicy.AUTH}
        or not 200 <= response.status_code < 300
    ):
        return response
    if resolver is None or not isinstance(result, BaseModel):
        raise _refused(
            "The mutation has no observable acceptance receipt", unavailable=True
        )
    if arguments is None:
        raise _refused("The mutation has no accepted request context", unavailable=True)
    receipt = resolver.resolve(policy, result, arguments)
    response.headers[RECEIPT_HEADER] = receipt.model_dump_json(exclude_none=True)
    response.headers["Link"] = f'<{receipt.href}>; rel="receipt"'
    return response
