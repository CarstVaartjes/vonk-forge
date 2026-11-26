"""Resolve successful operator mutations through their existing durable owner.

This is a response policy, not a receipt store. Readers open the owner's normal
read boundary after the mutation returns (and its transaction has committed).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.datastructures import DefaultPlaceholder
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel

from vonk_agent_protocol.lifecycle_vocabulary import (
    InvalidRequestReason,
    OperatorActionName,
)
from vonk_agent_protocol.outcome import (
    InvalidRequestError,
    UnknownError,
    UnknownOutcomeError,
)
from vonk_agent_protocol.state_machines import EnrollmentGrantState
from vonk_control.api_availability import AVAILABILITY_ERRORS
from vonk_control.artifact_jobs.contracts import ArtifactJobResponse
from vonk_control.auth import Actor
from vonk_control.enrollment.contract import EnrollmentGrantStatus
from vonk_control.fleet_profile_contract.definitions import FleetProfileView
from vonk_control.fleet_profile_contract.review import FleetProfileApplicationView
from vonk_control.fleet_projection.common import FleetNodeIdentity
from vonk_control.gateway.external_api_contract import (
    ExternalBindingAccepted,
    ExternalBindingAction,
    ExternalModelAccepted,
    ExternalModelAction,
)
from vonk_control.gateway.keys_contract import GatewayKeyCreated, GatewayKeyRevoked
from vonk_control.jobs.operator_receipts import read_job_operator_receipt
from vonk_control.model_cache.contract import ModelCacheOperatorResponse
from vonk_control.observation.platform_errors import (
    ObservationCaptureUnavailable,
    observation_capture_unavailable_response,
)
from vonk_control.operation_api.contracts import JobResumeRequest, JobResumeResponse
from vonk_control.operator_acceptance.agent import AGENT_POLICIES, resolve_agent_receipt
from vonk_control.operator_acceptance.receipt_checks import (
    _available,
    _identity,
    _refused,
    _require,
)
from vonk_control.operator_acceptance.receipt_contract import (
    AcceptanceReceipt,
    ReceiptCode,
    ReceiptOwner,
    ReceiptPolicy,
    SecurityEdge,
)
from vonk_control.operator_projection_api.contracts import FleetActionResponse
from vonk_control.recipe_image_availability.removal_contract import (
    RecipeRemovalUnavailableView,
)
from vonk_control.recipe_image_availability.response_contract import (
    RecipeImageAvailabilityResponse,
    RecipeOperatorResponse,
)
from vonk_control.recipe_image_availability.view_contract import (
    RecipeCacheRemovalStatus,
)
from vonk_control.recipe_update_batches.contract import RecipeUpdateResponse
from vonk_control.run_switch_contract.operations import RunSwitchOperation
from vonk_control.strict_json import (
    _retry_later_response,
    route_endpoint,
    serialize_json_value,
)

if TYPE_CHECKING:
    from vonk_control.agent_api.common import AgentApiServices
    from vonk_control.api.common import JobQueue
    from vonk_control.artifact_jobs.service import ArtifactJobService
    from vonk_control.fleet_profiles.service import FleetProfileService
    from vonk_control.fleet_projection.service import FleetProjection
    from vonk_control.gateway.external_service import ExternalModelService
    from vonk_control.gateway.keys import GatewayKeyService
    from vonk_control.model_cache.service import ModelCacheService
    from vonk_control.operator_projection_api.services import FleetOperatorServices
    from vonk_control.recipe_image_availability.service import (
        RecipeImageAvailabilityService,
    )
    from vonk_control.run_switch_operations.service import RunSwitchOperationService


RECEIPT_HEADER = "X-Vonk-Receipt"


def operator_acceptance(policy: ReceiptPolicy):
    """Declare semantics on the mounted endpoint, independently of its URL."""

    def decorate[T: Callable[..., Any]](endpoint: T) -> T:
        endpoint.__vonk_acceptance__ = policy  # type: ignore[attr-defined]
        return endpoint

    return decorate


def security_edge(edge: SecurityEdge):
    """Classify one reviewed credential endpoint, without a replay store."""

    def decorate[T: Callable[..., Any]](endpoint: T) -> T:
        if (endpoint.__module__, endpoint.__qualname__) != edge.endpoint:
            raise ValueError("security-edge exemption belongs to another endpoint")
        endpoint.__vonk_security_edge__ = edge  # type: ignore[attr-defined]
        return operator_acceptance(ReceiptPolicy.SECURITY_EDGE)(endpoint)

    return decorate


def security_edge_policy(endpoint: Callable[..., Any]) -> SecurityEdge | None:

    endpoint = route_endpoint(endpoint)
    edge = getattr(endpoint, "__vonk_security_edge__", None)
    if (
        isinstance(edge, SecurityEdge)
        and (endpoint.__module__, endpoint.__qualname__) == edge.endpoint
    ):
        return edge
    return None


def receipt_policy(endpoint: Callable[..., Any]) -> ReceiptPolicy | None:

    value = getattr(route_endpoint(endpoint), "__vonk_acceptance__", None)
    return value if isinstance(value, ReceiptPolicy) else None


@dataclass(frozen=True)
class AcceptanceArguments:
    actor: Actor | None
    request_key: str | None
    request_id: str | None
    number: int | None
    http_request_id: str | None
    job_action: OperatorActionName
    request: Request | None
    body: BaseModel | None


def acceptance_arguments(
    policy: ReceiptPolicy | None, **values: Any
) -> AcceptanceArguments | None:
    if policy is None or policy in {
        ReceiptPolicy.OBSERVATION,
        ReceiptPolicy.SECURITY_EDGE,
    }:
        return None

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
        request,
        body if isinstance(body, BaseModel) else None,
    )


@dataclass(frozen=True)
class OperatorReceiptResolver:
    # These are the already installed service interfaces, not new persistence.
    agent: AgentApiServices | None = None
    jobs: JobQueue | None = None
    profiles: FleetProfileService | None = None
    models: ModelCacheService | None = None
    recipes: RecipeImageAvailabilityService | None = None
    artifacts: ArtifactJobService | None = None
    fleet: FleetProjection | None = None
    fleet_services: FleetOperatorServices | None = None
    keys: GatewayKeyService | None = None
    external_models: ExternalModelService | None = None
    installations: RunSwitchOperationService | None = None

    def bind(self, app: FastAPI) -> None:

        for context in iter_route_contexts(app.routes):
            route = context.original_route
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

    def _external_receipt(
        self, policy: ReceiptPolicy, result: BaseModel | Response
    ) -> AcceptanceReceipt | None:
        """The receipt of an accepted external model or credential binding, if it is one."""

        if policy == ReceiptPolicy.GATEWAY_MODEL and isinstance(
            result, ExternalModelAccepted
        ):
            try:
                _available(self.external_models).receipt(
                    result.alias, result.request_id
                )
            except InvalidRequestError as error:
                # A removed model leaves once its withdrawal is confirmed: its
                # absence is then the evidence.
                if (
                    error.typed_reason is not InvalidRequestReason.NOT_FOUND
                    or result.action != ExternalModelAction.REMOVE
                ):
                    raise
            return AcceptanceReceipt(
                owner=ReceiptOwner.GATEWAY_MODEL,
                id=result.alias,
                request_id=result.request_id,
                revision=result.revision,
                href=f"/api/gateway/models/{quote(result.alias, safe='')}/requests?request_id={quote(result.request_id, safe='')}",
            )
        if policy == ReceiptPolicy.GATEWAY_CREDENTIAL and isinstance(
            result, ExternalBindingAccepted
        ):
            try:
                _available(self.external_models).binding_receipt(
                    result.name, result.request_id
                )
            except InvalidRequestError as error:
                if (
                    error.typed_reason is not InvalidRequestReason.NOT_FOUND
                    or result.action != ExternalBindingAction.REMOVE
                ):
                    raise
            return AcceptanceReceipt(
                owner=ReceiptOwner.GATEWAY_CREDENTIAL,
                id=result.name,
                request_id=result.request_id,
                revision=result.revision,
                href=f"/api/gateway/credentials/{quote(result.name, safe='')}/requests?request_id={quote(result.request_id, safe='')}",
            )
        return None

    def resolve(
        self,
        policy: ReceiptPolicy,
        result: BaseModel | Response,
        arguments: AcceptanceArguments,
    ) -> AcceptanceReceipt:

        if policy in AGENT_POLICIES:
            _require(isinstance(result, Response))
            assert isinstance(result, Response)
            try:
                return resolve_agent_receipt(self, policy, result, arguments)
            except (UnknownOutcomeError, *AVAILABILITY_ERRORS):
                raise _refused(
                    "The agent receipt owner is unavailable; retry the same request",
                    unavailable=True,
                ) from None
            except InvalidRequestError as error:
                if error.typed_reason is not InvalidRequestReason.NOT_FOUND:
                    raise
                raise _refused(
                    "The owner cannot yet observe its acceptance receipt",
                    unavailable=True,
                ) from None

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
                keys = _available(self.keys)
                try:
                    saved = keys.receipt(result.name, identity, actor=actor.subject)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                    raise _refused(
                        "The owner cannot yet observe its acceptance receipt",
                        unavailable=True,
                    ) from None
                _require(saved.completed)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.KEY,
                    id=result.name,
                    request_id=identity,
                    href=f"/api/key/{quote(result.name, safe='')}/requests?request_id={quote(identity, safe='')}",
                )
            external = self._external_receipt(policy, result)
            if external is not None:
                return external
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
            if error.typed_reason is InvalidRequestReason.NOT_FOUND:
                # The mutation succeeded but its owner row is not there (yet):
                # the outcome is unknown, so the same request retries.
                raise _refused(
                    "The owner cannot yet observe its acceptance receipt",
                    unavailable=True,
                ) from None
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
        or policy in {ReceiptPolicy.OBSERVATION, ReceiptPolicy.SECURITY_EDGE}
        or not 200 <= response.status_code < 300
    ):
        return response
    if resolver is None or not isinstance(result, BaseModel | Response):
        raise _refused(
            "The mutation has no observable acceptance receipt", unavailable=True
        )
    if arguments is None:
        raise _refused("The mutation has no accepted request context", unavailable=True)
    receipt = resolver.resolve(policy, result, arguments)
    response.headers[RECEIPT_HEADER] = receipt.model_dump_json(
        exclude_none=True, ensure_ascii=True
    )
    response.headers["Link"] = f'<{receipt.href}>; rel="receipt"'
    return response


_CONTROLLER_RESPONSE = "_controller_response"


def _presence_policy_endpoint(
    endpoint: Callable[..., Any], route: Callable[[], APIRoute]
) -> Callable[..., Any]:
    """Wrap an endpoint so its validated result is encoded with the presence policy.

    The wrapper carries the endpoint's own resolved signature, plus an injected
    ``Response`` when the endpoint does not declare one, so FastAPI analyses it
    exactly like the endpoint however the route is later included or mounted.
    """

    signature = inspect.signature(endpoint, eval_str=True)
    response_param_name = next(
        (
            name
            for name, parameter in signature.parameters.items()
            if isinstance(parameter.annotation, type)
            and issubclass(parameter.annotation, Response)
        ),
        None,
    )
    parameters = list(signature.parameters.values())
    if response_param_name is None:
        injected = inspect.Parameter(
            _CONTROLLER_RESPONSE, inspect.Parameter.KEYWORD_ONLY, annotation=Response
        )
        position = next(
            (
                index
                for index, parameter in enumerate(parameters)
                if parameter.kind is inspect.Parameter.VAR_KEYWORD
            ),
            len(parameters),
        )
        parameters.insert(position, injected)

    async def policy_endpoint(**values: Any) -> Any:
        injected_response = values.get(response_param_name or _CONTROLLER_RESPONSE)
        if response_param_name is None:
            values.pop(_CONTROLLER_RESPONSE, None)
        try:
            if inspect.iscoroutinefunction(endpoint):
                result = await endpoint(**values)
            else:
                result = await run_in_threadpool(endpoint, **values)
        except ObservationCaptureUnavailable as error:
            current = route()
            return observation_capture_unavailable_response(
                error,
                operation=current.operation_id or current.name,
                endpoint=current.path,
            )
        except UnknownOutcomeError as error:
            # An unknown outcome that no route handled is not a server fault and
            # not a refusal: the caller is told to ask again.
            return _retry_later_response(error)
        current = route()
        response_field = current.response_field
        if isinstance(result, Response) or response_field is None:
            response = result if isinstance(result, Response) else injected_response
            if not isinstance(response, Response):
                response = Response(status_code=current.status_code or 200)
            if response.status_code is None:  # pyright: ignore[reportUnnecessaryComparison]  # a None check pyright narrows away is kept unchanged
                response.status_code = current.status_code or 200
            checked = await run_in_threadpool(
                apply_receipt,
                receipt_policy(endpoint),
                getattr(current, "operator_receipts", None),
                result,
                response,
                acceptance_arguments(receipt_policy(endpoint), **values),
            )
            return checked if isinstance(result, Response) else result
        value, errors = response_field.validate(result, {}, loc=("response",))
        if errors:
            return result
        status_code = current.status_code or 200
        if isinstance(injected_response, Response) and injected_response.status_code:
            status_code = injected_response.status_code
        response_class = current.response_class
        if isinstance(response_class, DefaultPlaceholder):
            response_class = response_class.value
        response = response_class(
            content=serialize_json_value(
                value, by_alias=current.response_model_by_alias
            ),
            status_code=status_code,
        )
        if isinstance(injected_response, Response):
            response.headers.raw.extend(injected_response.headers.raw)
        return await run_in_threadpool(
            apply_receipt,
            receipt_policy(endpoint),
            getattr(current, "operator_receipts", None),
            value,
            response,
            acceptance_arguments(receipt_policy(endpoint), **values),
        )

    # Copy the identity FastAPI derives names, operation ids and descriptions
    # from, but no ``__wrapped__``: FastAPI must see an async endpoint.
    functools.update_wrapper(policy_endpoint, endpoint, updated=("__dict__",))
    del policy_endpoint.__wrapped__  # pyright: ignore[reportFunctionMemberAccess]  # update_wrapper set it just above
    policy_endpoint.__signature__ = signature.replace(parameters=parameters)
    policy_endpoint.__vonk_endpoint__ = endpoint  # type: ignore[attr-defined]
    return policy_endpoint


class ControllerAPIRoute(APIRoute):
    """Apply the presence policy after response validation for every JSON route."""

    operator_receipts: OperatorReceiptResolver | None = None

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if not hasattr(endpoint, "__vonk_endpoint__"):
            endpoint = _presence_policy_endpoint(endpoint, lambda: self)
        super().__init__(path, endpoint, **kwargs)
