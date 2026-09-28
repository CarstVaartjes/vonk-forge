from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.recipe_image_availability_response import RecipeImageAvailabilityResponse
from ...models.recipe_operator_response import RecipeOperatorResponse
from ...models.recipe_update_response import RecipeUpdateResponse
from ...models.request_validation_problem import RequestValidationProblem
from typing import cast



def _get_kwargs(
    request_key: str,

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/recipe/requests/{request_key}".format(request_key=quote(str(request_key), safe=""),),
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem | None:
    if response.status_code == 200:
        def _parse_response_200(data: object) -> RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_200_type_0 = RecipeImageAvailabilityResponse.from_dict(data)



                return response_200_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_200_type_1 = RecipeOperatorResponse.from_dict(data)



                return response_200_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_200_type_2 = RecipeUpdateResponse.from_dict(data)



            return response_200_type_2

        response_200 = _parse_response_200(response.json())

        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 404:
        response_404 = BoundedErrorResponse.from_dict(response.json())



        return response_404

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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    request_key: str,
    *,
    client: AuthenticatedClient,

) -> Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem]:
    """ Get Request

    Args:
        request_key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        request_key=request_key,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    request_key: str,
    *,
    client: AuthenticatedClient,

) -> BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem | None:
    """ Get Request

    Args:
        request_key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem
     """


    return sync_detailed(
        request_key=request_key,
client=client,

    ).parsed

async def asyncio_detailed(
    request_key: str,
    *,
    client: AuthenticatedClient,

) -> Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem]:
    """ Get Request

    Args:
        request_key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        request_key=request_key,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    request_key: str,
    *,
    client: AuthenticatedClient,

) -> BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem | None:
    """ Get Request

    Args:
        request_key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RecipeImageAvailabilityResponse | RecipeOperatorResponse | RecipeUpdateResponse | RequestValidationProblem
     """


    return (await asyncio_detailed(
        request_key=request_key,
client=client,

    )).parsed
