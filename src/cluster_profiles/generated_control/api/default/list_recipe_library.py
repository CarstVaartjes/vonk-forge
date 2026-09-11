from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.list_recipe_library_sort import check_list_recipe_library_sort
from ...models.list_recipe_library_sort import ListRecipeLibrarySort
from ...models.recipe_library_response import RecipeLibraryResponse
from ...models.request_validation_problem import RequestValidationProblem
from ...types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime



def _get_kwargs(
    *,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,
    model: Union[None, Unset, list[str]] = UNSET,
    all_models: Union[Unset, bool] = False,
    usage: Union[None, Unset, list[str]] = UNSET,
    publisher: Union[None, Unset, list[str]] = UNSET,
    alignment: Union[None, Unset, list[str]] = UNSET,
    sparks: Union[None, Unset, list[int]] = UNSET,
    search: Union[None, Unset, str] = UNSET,
    updated_since: Union[None, Unset, datetime.datetime] = UNSET,
    sort: Union[Unset, ListRecipeLibrarySort] = 'updated',

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    params["limit"] = limit

    json_cursor: Union[None, Unset, str]
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    json_model: Union[None, Unset, list[str]]
    if isinstance(model, Unset):
        json_model = UNSET
    elif isinstance(model, list):
        json_model = model


    else:
        json_model = model
    params["model"] = json_model

    params["all_models"] = all_models

    json_usage: Union[None, Unset, list[str]]
    if isinstance(usage, Unset):
        json_usage = UNSET
    elif isinstance(usage, list):
        json_usage = usage


    else:
        json_usage = usage
    params["usage"] = json_usage

    json_publisher: Union[None, Unset, list[str]]
    if isinstance(publisher, Unset):
        json_publisher = UNSET
    elif isinstance(publisher, list):
        json_publisher = publisher


    else:
        json_publisher = publisher
    params["publisher"] = json_publisher

    json_alignment: Union[None, Unset, list[str]]
    if isinstance(alignment, Unset):
        json_alignment = UNSET
    elif isinstance(alignment, list):
        json_alignment = alignment


    else:
        json_alignment = alignment
    params["alignment"] = json_alignment

    json_sparks: Union[None, Unset, list[int]]
    if isinstance(sparks, Unset):
        json_sparks = UNSET
    elif isinstance(sparks, list):
        json_sparks = sparks


    else:
        json_sparks = sparks
    params["sparks"] = json_sparks

    json_search: Union[None, Unset, str]
    if isinstance(search, Unset):
        json_search = UNSET
    else:
        json_search = search
    params["search"] = json_search

    json_updated_since: Union[None, Unset, str]
    if isinstance(updated_since, Unset):
        json_updated_since = UNSET
    elif isinstance(updated_since, datetime.datetime):
        json_updated_since = updated_since.isoformat()
    else:
        json_updated_since = updated_since
    params["updated_since"] = json_updated_since

    json_sort: Union[Unset, str] = UNSET
    if not isinstance(sort, Unset):
        json_sort = sort

    params["sort"] = json_sort


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/recipe/library",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]]:
    if response.status_code == 200:
        response_200 = RecipeLibraryResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,
    model: Union[None, Unset, list[str]] = UNSET,
    all_models: Union[Unset, bool] = False,
    usage: Union[None, Unset, list[str]] = UNSET,
    publisher: Union[None, Unset, list[str]] = UNSET,
    alignment: Union[None, Unset, list[str]] = UNSET,
    sparks: Union[None, Unset, list[int]] = UNSET,
    search: Union[None, Unset, str] = UNSET,
    updated_since: Union[None, Unset, datetime.datetime] = UNSET,
    sort: Union[Unset, ListRecipeLibrarySort] = 'updated',

) -> Response[Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]]:
    """ Recipe Library

    Args:
        limit (Union[Unset, int]):  Default: 100.
        cursor (Union[None, Unset, str]):
        model (Union[None, Unset, list[str]]):
        all_models (Union[Unset, bool]):  Default: False.
        usage (Union[None, Unset, list[str]]):
        publisher (Union[None, Unset, list[str]]):
        alignment (Union[None, Unset, list[str]]):
        sparks (Union[None, Unset, list[int]]):
        search (Union[None, Unset, str]):
        updated_since (Union[None, Unset, datetime.datetime]):
        sort (Union[Unset, ListRecipeLibrarySort]):  Default: 'updated'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        limit=limit,
cursor=cursor,
model=model,
all_models=all_models,
usage=usage,
publisher=publisher,
alignment=alignment,
sparks=sparks,
search=search,
updated_since=updated_since,
sort=sort,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,
    model: Union[None, Unset, list[str]] = UNSET,
    all_models: Union[Unset, bool] = False,
    usage: Union[None, Unset, list[str]] = UNSET,
    publisher: Union[None, Unset, list[str]] = UNSET,
    alignment: Union[None, Unset, list[str]] = UNSET,
    sparks: Union[None, Unset, list[int]] = UNSET,
    search: Union[None, Unset, str] = UNSET,
    updated_since: Union[None, Unset, datetime.datetime] = UNSET,
    sort: Union[Unset, ListRecipeLibrarySort] = 'updated',

) -> Optional[Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]]:
    """ Recipe Library

    Args:
        limit (Union[Unset, int]):  Default: 100.
        cursor (Union[None, Unset, str]):
        model (Union[None, Unset, list[str]]):
        all_models (Union[Unset, bool]):  Default: False.
        usage (Union[None, Unset, list[str]]):
        publisher (Union[None, Unset, list[str]]):
        alignment (Union[None, Unset, list[str]]):
        sparks (Union[None, Unset, list[int]]):
        search (Union[None, Unset, str]):
        updated_since (Union[None, Unset, datetime.datetime]):
        sort (Union[Unset, ListRecipeLibrarySort]):  Default: 'updated'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]
     """


    return sync_detailed(
        client=client,
limit=limit,
cursor=cursor,
model=model,
all_models=all_models,
usage=usage,
publisher=publisher,
alignment=alignment,
sparks=sparks,
search=search,
updated_since=updated_since,
sort=sort,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,
    model: Union[None, Unset, list[str]] = UNSET,
    all_models: Union[Unset, bool] = False,
    usage: Union[None, Unset, list[str]] = UNSET,
    publisher: Union[None, Unset, list[str]] = UNSET,
    alignment: Union[None, Unset, list[str]] = UNSET,
    sparks: Union[None, Unset, list[int]] = UNSET,
    search: Union[None, Unset, str] = UNSET,
    updated_since: Union[None, Unset, datetime.datetime] = UNSET,
    sort: Union[Unset, ListRecipeLibrarySort] = 'updated',

) -> Response[Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]]:
    """ Recipe Library

    Args:
        limit (Union[Unset, int]):  Default: 100.
        cursor (Union[None, Unset, str]):
        model (Union[None, Unset, list[str]]):
        all_models (Union[Unset, bool]):  Default: False.
        usage (Union[None, Unset, list[str]]):
        publisher (Union[None, Unset, list[str]]):
        alignment (Union[None, Unset, list[str]]):
        sparks (Union[None, Unset, list[int]]):
        search (Union[None, Unset, str]):
        updated_since (Union[None, Unset, datetime.datetime]):
        sort (Union[Unset, ListRecipeLibrarySort]):  Default: 'updated'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        limit=limit,
cursor=cursor,
model=model,
all_models=all_models,
usage=usage,
publisher=publisher,
alignment=alignment,
sparks=sparks,
search=search,
updated_since=updated_since,
sort=sort,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,
    model: Union[None, Unset, list[str]] = UNSET,
    all_models: Union[Unset, bool] = False,
    usage: Union[None, Unset, list[str]] = UNSET,
    publisher: Union[None, Unset, list[str]] = UNSET,
    alignment: Union[None, Unset, list[str]] = UNSET,
    sparks: Union[None, Unset, list[int]] = UNSET,
    search: Union[None, Unset, str] = UNSET,
    updated_since: Union[None, Unset, datetime.datetime] = UNSET,
    sort: Union[Unset, ListRecipeLibrarySort] = 'updated',

) -> Optional[Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]]:
    """ Recipe Library

    Args:
        limit (Union[Unset, int]):  Default: 100.
        cursor (Union[None, Unset, str]):
        model (Union[None, Unset, list[str]]):
        all_models (Union[Unset, bool]):  Default: False.
        usage (Union[None, Unset, list[str]]):
        publisher (Union[None, Unset, list[str]]):
        alignment (Union[None, Unset, list[str]]):
        sparks (Union[None, Unset, list[int]]):
        search (Union[None, Unset, str]):
        updated_since (Union[None, Unset, datetime.datetime]):
        sort (Union[Unset, ListRecipeLibrarySort]):  Default: 'updated'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, RecipeLibraryResponse, RequestValidationProblem]
     """


    return (await asyncio_detailed(
        client=client,
limit=limit,
cursor=cursor,
model=model,
all_models=all_models,
usage=usage,
publisher=publisher,
alignment=alignment,
sparks=sparks,
search=search,
updated_since=updated_since,
sort=sort,

    )).parsed
