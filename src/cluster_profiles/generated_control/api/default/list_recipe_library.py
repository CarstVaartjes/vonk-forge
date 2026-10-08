from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.capability_unavailable_reply import CapabilityUnavailableReply
from ...models.list_recipe_library_sort import check_list_recipe_library_sort
from ...models.list_recipe_library_sort import ListRecipeLibrarySort
from ...models.recipe_library_response import RecipeLibraryResponse
from ...models.request_validation_problem import RequestValidationProblem
from ...types import UNSET, Unset
from typing import cast
import datetime



def _get_kwargs(
    *,
    limit: int | Unset = 100,
    cursor: None | str | Unset = UNSET,
    model: list[str] | None | Unset = UNSET,
    cached: bool | Unset = False,
    ready: bool | None | Unset = UNSET,
    fits_fleet: bool | None | Unset = UNSET,
    assess: bool | Unset = True,
    usage: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    sparks: list[int] | None | Unset = UNSET,
    engine: list[str] | None | Unset = UNSET,
    creator: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListRecipeLibrarySort | Unset = 'updated',

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    params["limit"] = limit

    json_cursor: None | str | Unset
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    json_model: list[str] | None | Unset
    if isinstance(model, Unset):
        json_model = UNSET
    elif isinstance(model, list):
        json_model = model


    else:
        json_model = model
    params["model"] = json_model

    params["cached"] = cached

    json_ready: bool | None | Unset
    if isinstance(ready, Unset):
        json_ready = UNSET
    else:
        json_ready = ready
    params["ready"] = json_ready

    json_fits_fleet: bool | None | Unset
    if isinstance(fits_fleet, Unset):
        json_fits_fleet = UNSET
    else:
        json_fits_fleet = fits_fleet
    params["fits_fleet"] = json_fits_fleet

    params["assess"] = assess

    json_usage: list[str] | None | Unset
    if isinstance(usage, Unset):
        json_usage = UNSET
    elif isinstance(usage, list):
        json_usage = usage


    else:
        json_usage = usage
    params["usage"] = json_usage

    json_publisher: list[str] | None | Unset
    if isinstance(publisher, Unset):
        json_publisher = UNSET
    elif isinstance(publisher, list):
        json_publisher = publisher


    else:
        json_publisher = publisher
    params["publisher"] = json_publisher

    json_alignment: list[str] | None | Unset
    if isinstance(alignment, Unset):
        json_alignment = UNSET
    elif isinstance(alignment, list):
        json_alignment = alignment


    else:
        json_alignment = alignment
    params["alignment"] = json_alignment

    json_sparks: list[int] | None | Unset
    if isinstance(sparks, Unset):
        json_sparks = UNSET
    elif isinstance(sparks, list):
        json_sparks = sparks


    else:
        json_sparks = sparks
    params["sparks"] = json_sparks

    json_engine: list[str] | None | Unset
    if isinstance(engine, Unset):
        json_engine = UNSET
    elif isinstance(engine, list):
        json_engine = engine


    else:
        json_engine = engine
    params["engine"] = json_engine

    json_creator: list[str] | None | Unset
    if isinstance(creator, Unset):
        json_creator = UNSET
    elif isinstance(creator, list):
        json_creator = creator


    else:
        json_creator = creator
    params["creator"] = json_creator

    json_search: None | str | Unset
    if isinstance(search, Unset):
        json_search = UNSET
    else:
        json_search = search
    params["search"] = json_search

    json_updated_since: None | str | Unset
    if isinstance(updated_since, Unset):
        json_updated_since = UNSET
    elif isinstance(updated_since, datetime.datetime):
        json_updated_since = updated_since.isoformat()
    else:
        json_updated_since = updated_since
    params["updated_since"] = json_updated_since

    json_sort: str | Unset = UNSET
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



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem | None:
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
        def _parse_response_503(data: object) -> BoundedErrorResponse | CapabilityUnavailableReply:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_503_type_0 = BoundedErrorResponse.from_dict(data)



                return response_503_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_503_type_1 = CapabilityUnavailableReply.from_dict(data)



            return response_503_type_1

        response_503 = _parse_response_503(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = 100,
    cursor: None | str | Unset = UNSET,
    model: list[str] | None | Unset = UNSET,
    cached: bool | Unset = False,
    ready: bool | None | Unset = UNSET,
    fits_fleet: bool | None | Unset = UNSET,
    assess: bool | Unset = True,
    usage: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    sparks: list[int] | None | Unset = UNSET,
    engine: list[str] | None | Unset = UNSET,
    creator: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListRecipeLibrarySort | Unset = 'updated',

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem]:
    """ Recipe Library

    Args:
        limit (int | Unset):  Default: 100.
        cursor (None | str | Unset):
        model (list[str] | None | Unset):
        cached (bool | Unset):  Default: False.
        ready (bool | None | Unset):
        fits_fleet (bool | None | Unset):
        assess (bool | Unset):  Default: True.
        usage (list[str] | None | Unset):
        publisher (list[str] | None | Unset):
        alignment (list[str] | None | Unset):
        sparks (list[int] | None | Unset):
        engine (list[str] | None | Unset):
        creator (list[str] | None | Unset):
        search (None | str | Unset):
        updated_since (datetime.datetime | None | Unset):
        sort (ListRecipeLibrarySort | Unset):  Default: 'updated'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        limit=limit,
cursor=cursor,
model=model,
cached=cached,
ready=ready,
fits_fleet=fits_fleet,
assess=assess,
usage=usage,
publisher=publisher,
alignment=alignment,
sparks=sparks,
engine=engine,
creator=creator,
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
    limit: int | Unset = 100,
    cursor: None | str | Unset = UNSET,
    model: list[str] | None | Unset = UNSET,
    cached: bool | Unset = False,
    ready: bool | None | Unset = UNSET,
    fits_fleet: bool | None | Unset = UNSET,
    assess: bool | Unset = True,
    usage: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    sparks: list[int] | None | Unset = UNSET,
    engine: list[str] | None | Unset = UNSET,
    creator: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListRecipeLibrarySort | Unset = 'updated',

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem | None:
    """ Recipe Library

    Args:
        limit (int | Unset):  Default: 100.
        cursor (None | str | Unset):
        model (list[str] | None | Unset):
        cached (bool | Unset):  Default: False.
        ready (bool | None | Unset):
        fits_fleet (bool | None | Unset):
        assess (bool | Unset):  Default: True.
        usage (list[str] | None | Unset):
        publisher (list[str] | None | Unset):
        alignment (list[str] | None | Unset):
        sparks (list[int] | None | Unset):
        engine (list[str] | None | Unset):
        creator (list[str] | None | Unset):
        search (None | str | Unset):
        updated_since (datetime.datetime | None | Unset):
        sort (ListRecipeLibrarySort | Unset):  Default: 'updated'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem
     """


    return sync_detailed(
        client=client,
limit=limit,
cursor=cursor,
model=model,
cached=cached,
ready=ready,
fits_fleet=fits_fleet,
assess=assess,
usage=usage,
publisher=publisher,
alignment=alignment,
sparks=sparks,
engine=engine,
creator=creator,
search=search,
updated_since=updated_since,
sort=sort,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = 100,
    cursor: None | str | Unset = UNSET,
    model: list[str] | None | Unset = UNSET,
    cached: bool | Unset = False,
    ready: bool | None | Unset = UNSET,
    fits_fleet: bool | None | Unset = UNSET,
    assess: bool | Unset = True,
    usage: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    sparks: list[int] | None | Unset = UNSET,
    engine: list[str] | None | Unset = UNSET,
    creator: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListRecipeLibrarySort | Unset = 'updated',

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem]:
    """ Recipe Library

    Args:
        limit (int | Unset):  Default: 100.
        cursor (None | str | Unset):
        model (list[str] | None | Unset):
        cached (bool | Unset):  Default: False.
        ready (bool | None | Unset):
        fits_fleet (bool | None | Unset):
        assess (bool | Unset):  Default: True.
        usage (list[str] | None | Unset):
        publisher (list[str] | None | Unset):
        alignment (list[str] | None | Unset):
        sparks (list[int] | None | Unset):
        engine (list[str] | None | Unset):
        creator (list[str] | None | Unset):
        search (None | str | Unset):
        updated_since (datetime.datetime | None | Unset):
        sort (ListRecipeLibrarySort | Unset):  Default: 'updated'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        limit=limit,
cursor=cursor,
model=model,
cached=cached,
ready=ready,
fits_fleet=fits_fleet,
assess=assess,
usage=usage,
publisher=publisher,
alignment=alignment,
sparks=sparks,
engine=engine,
creator=creator,
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
    limit: int | Unset = 100,
    cursor: None | str | Unset = UNSET,
    model: list[str] | None | Unset = UNSET,
    cached: bool | Unset = False,
    ready: bool | None | Unset = UNSET,
    fits_fleet: bool | None | Unset = UNSET,
    assess: bool | Unset = True,
    usage: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    sparks: list[int] | None | Unset = UNSET,
    engine: list[str] | None | Unset = UNSET,
    creator: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListRecipeLibrarySort | Unset = 'updated',

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem | None:
    """ Recipe Library

    Args:
        limit (int | Unset):  Default: 100.
        cursor (None | str | Unset):
        model (list[str] | None | Unset):
        cached (bool | Unset):  Default: False.
        ready (bool | None | Unset):
        fits_fleet (bool | None | Unset):
        assess (bool | Unset):  Default: True.
        usage (list[str] | None | Unset):
        publisher (list[str] | None | Unset):
        alignment (list[str] | None | Unset):
        sparks (list[int] | None | Unset):
        engine (list[str] | None | Unset):
        creator (list[str] | None | Unset):
        search (None | str | Unset):
        updated_since (datetime.datetime | None | Unset):
        sort (ListRecipeLibrarySort | Unset):  Default: 'updated'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | RecipeLibraryResponse | RequestValidationProblem
     """


    return (await asyncio_detailed(
        client=client,
limit=limit,
cursor=cursor,
model=model,
cached=cached,
ready=ready,
fits_fleet=fits_fleet,
assess=assess,
usage=usage,
publisher=publisher,
alignment=alignment,
sparks=sparks,
engine=engine,
creator=creator,
search=search,
updated_since=updated_since,
sort=sort,

    )).parsed
