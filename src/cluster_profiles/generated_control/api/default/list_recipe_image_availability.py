from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.http_validation_error import HTTPValidationError
from ...models.list_recipe_image_availability_state_type_0 import check_list_recipe_image_availability_state_type_0
from ...models.list_recipe_image_availability_state_type_0 import ListRecipeImageAvailabilityStateType0
from ...models.recipe_image_availability_list_response import RecipeImageAvailabilityListResponse
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    recipe_revision_id: Union[None, Unset, str] = UNSET,
    state: Union[ListRecipeImageAvailabilityStateType0, None, Unset] = UNSET,
    limit: Union[Unset, int] = 50,
    cursor: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_recipe_revision_id: Union[None, Unset, str]
    if isinstance(recipe_revision_id, Unset):
        json_recipe_revision_id = UNSET
    else:
        json_recipe_revision_id = recipe_revision_id
    params["recipe_revision_id"] = json_recipe_revision_id

    json_state: Union[None, Unset, str]
    if isinstance(state, Unset):
        json_state = UNSET
    elif isinstance(state, str):
        json_state = state
    else:
        json_state = state
    params["state"] = json_state

    params["limit"] = limit

    json_cursor: Union[None, Unset, str]
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/library/recipe-image-availability",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[HTTPValidationError, RecipeImageAvailabilityListResponse]]:
    if response.status_code == 200:
        response_200 = RecipeImageAvailabilityListResponse.from_dict(response.json())



        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[HTTPValidationError, RecipeImageAvailabilityListResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    recipe_revision_id: Union[None, Unset, str] = UNSET,
    state: Union[ListRecipeImageAvailabilityStateType0, None, Unset] = UNSET,
    limit: Union[Unset, int] = 50,
    cursor: Union[None, Unset, str] = UNSET,

) -> Response[Union[HTTPValidationError, RecipeImageAvailabilityListResponse]]:
    """ List Operations

    Args:
        recipe_revision_id (Union[None, Unset, str]):
        state (Union[ListRecipeImageAvailabilityStateType0, None, Unset]):
        limit (Union[Unset, int]):  Default: 50.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, RecipeImageAvailabilityListResponse]]
     """


    kwargs = _get_kwargs(
        recipe_revision_id=recipe_revision_id,
state=state,
limit=limit,
cursor=cursor,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    recipe_revision_id: Union[None, Unset, str] = UNSET,
    state: Union[ListRecipeImageAvailabilityStateType0, None, Unset] = UNSET,
    limit: Union[Unset, int] = 50,
    cursor: Union[None, Unset, str] = UNSET,

) -> Optional[Union[HTTPValidationError, RecipeImageAvailabilityListResponse]]:
    """ List Operations

    Args:
        recipe_revision_id (Union[None, Unset, str]):
        state (Union[ListRecipeImageAvailabilityStateType0, None, Unset]):
        limit (Union[Unset, int]):  Default: 50.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, RecipeImageAvailabilityListResponse]
     """


    return sync_detailed(
        client=client,
recipe_revision_id=recipe_revision_id,
state=state,
limit=limit,
cursor=cursor,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    recipe_revision_id: Union[None, Unset, str] = UNSET,
    state: Union[ListRecipeImageAvailabilityStateType0, None, Unset] = UNSET,
    limit: Union[Unset, int] = 50,
    cursor: Union[None, Unset, str] = UNSET,

) -> Response[Union[HTTPValidationError, RecipeImageAvailabilityListResponse]]:
    """ List Operations

    Args:
        recipe_revision_id (Union[None, Unset, str]):
        state (Union[ListRecipeImageAvailabilityStateType0, None, Unset]):
        limit (Union[Unset, int]):  Default: 50.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, RecipeImageAvailabilityListResponse]]
     """


    kwargs = _get_kwargs(
        recipe_revision_id=recipe_revision_id,
state=state,
limit=limit,
cursor=cursor,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    recipe_revision_id: Union[None, Unset, str] = UNSET,
    state: Union[ListRecipeImageAvailabilityStateType0, None, Unset] = UNSET,
    limit: Union[Unset, int] = 50,
    cursor: Union[None, Unset, str] = UNSET,

) -> Optional[Union[HTTPValidationError, RecipeImageAvailabilityListResponse]]:
    """ List Operations

    Args:
        recipe_revision_id (Union[None, Unset, str]):
        state (Union[ListRecipeImageAvailabilityStateType0, None, Unset]):
        limit (Union[Unset, int]):  Default: 50.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, RecipeImageAvailabilityListResponse]
     """


    return (await asyncio_detailed(
        client=client,
recipe_revision_id=recipe_revision_id,
state=state,
limit=limit,
cursor=cursor,

    )).parsed
