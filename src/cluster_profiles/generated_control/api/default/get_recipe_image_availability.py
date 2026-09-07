from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.http_validation_error import HTTPValidationError
from ...models.recipe_image_availability_error_response import RecipeImageAvailabilityErrorResponse
from ...models.recipe_image_availability_response import RecipeImageAvailabilityResponse
from typing import cast



def _get_kwargs(
    operation_id: str,

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/library/recipe-image-availability/{operation_id}".format(operation_id=operation_id,),
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]]:
    if response.status_code == 200:
        response_200 = RecipeImageAvailabilityResponse.from_dict(response.json())



        return response_200

    if response.status_code == 409:
        response_409 = RecipeImageAvailabilityErrorResponse.from_dict(response.json())



        return response_409

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]]:
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

) -> Response[Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]]:
    """ Get

    Args:
        operation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]]
     """


    kwargs = _get_kwargs(
        operation_id=operation_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    operation_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]]:
    """ Get

    Args:
        operation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]
     """


    return sync_detailed(
        operation_id=operation_id,
client=client,

    ).parsed

async def asyncio_detailed(
    operation_id: str,
    *,
    client: AuthenticatedClient,

) -> Response[Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]]:
    """ Get

    Args:
        operation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]]
     """


    kwargs = _get_kwargs(
        operation_id=operation_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    operation_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]]:
    """ Get

    Args:
        operation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, RecipeImageAvailabilityErrorResponse, RecipeImageAvailabilityResponse]
     """


    return (await asyncio_detailed(
        operation_id=operation_id,
client=client,

    )).parsed
