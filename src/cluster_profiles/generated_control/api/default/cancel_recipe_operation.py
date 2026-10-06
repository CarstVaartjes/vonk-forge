from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.recipe_cancellation_request import RecipeCancellationRequest
from ...models.recipe_image_availability_response import RecipeImageAvailabilityResponse
from ...models.recipe_operator_response import RecipeOperatorResponse
from ...models.recipe_removal_unavailable_view import RecipeRemovalUnavailableView
from ...models.recipe_update_response import RecipeUpdateResponse
from ...models.request_validation_problem import RequestValidationProblem
from typing import cast



def _get_kwargs(
    operation_id: str,
    *,
    body: RecipeCancellationRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/recipe/operations/{operation_id}/cancel".format(operation_id=quote(str(operation_id), safe=""),),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem | None:
    if response.status_code == 202:
        def _parse_response_202(data: object) -> RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_202_type_0 = RecipeImageAvailabilityResponse.from_dict(data)



                return response_202_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_202_type_1 = RecipeOperatorResponse.from_dict(data)



                return response_202_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_202_type_2 = RecipeUpdateResponse.from_dict(data)



                return response_202_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_202_type_3 = RecipeRemovalUnavailableView.from_dict(data)



            return response_202_type_3

        response_202 = _parse_response_202(response.json())

        return response_202

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = BoundedErrorResponse.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = BoundedErrorResponse.from_dict(response.json())



        return response_404

    if response.status_code == 409:
        response_409 = BoundedErrorResponse.from_dict(response.json())



        return response_409

    if response.status_code == 422:
        response_422 = RequestValidationProblem.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    operation_id: str,
    *,
    client: AuthenticatedClient,
    body: RecipeCancellationRequest,

) -> Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem]:
    """ Cancel Operation

    Args:
        operation_id (str):
        body (RecipeCancellationRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        operation_id=operation_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    operation_id: str,
    *,
    client: AuthenticatedClient,
    body: RecipeCancellationRequest,

) -> BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem | None:
    """ Cancel Operation

    Args:
        operation_id (str):
        body (RecipeCancellationRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem
     """


    return sync_detailed(
        operation_id=operation_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    operation_id: str,
    *,
    client: AuthenticatedClient,
    body: RecipeCancellationRequest,

) -> Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem]:
    """ Cancel Operation

    Args:
        operation_id (str):
        body (RecipeCancellationRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        operation_id=operation_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    operation_id: str,
    *,
    client: AuthenticatedClient,
    body: RecipeCancellationRequest,

) -> BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem | None:
    """ Cancel Operation

    Args:
        operation_id (str):
        body (RecipeCancellationRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeRemovalUnavailableView | RecipeUpdateResponse | RequestValidationProblem
     """


    return (await asyncio_detailed(
        operation_id=operation_id,
client=client,
body=body,

    )).parsed
