from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.list_model_library_sort import check_list_model_library_sort
from ...models.list_model_library_sort import ListModelLibrarySort
from ...models.model_library_response import ModelLibraryResponse
from ...models.request_validation_problem import RequestValidationProblem
from ...types import UNSET, Unset
from typing import cast
import datetime



def _get_kwargs(
    *,
    limit: int | Unset = 100,
    cursor: None | str | Unset = UNSET,
    usage: list[str] | None | Unset = UNSET,
    family: list[str] | None | Unset = UNSET,
    version: list[str] | None | Unset = UNSET,
    quantization: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListModelLibrarySort | Unset = 'updated',
    local: bool | Unset = False,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    params["limit"] = limit

    json_cursor: None | str | Unset
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    json_usage: list[str] | None | Unset
    if isinstance(usage, Unset):
        json_usage = UNSET
    elif isinstance(usage, list):
        json_usage = usage


    else:
        json_usage = usage
    params["usage"] = json_usage

    json_family: list[str] | None | Unset
    if isinstance(family, Unset):
        json_family = UNSET
    elif isinstance(family, list):
        json_family = family


    else:
        json_family = family
    params["family"] = json_family

    json_version: list[str] | None | Unset
    if isinstance(version, Unset):
        json_version = UNSET
    elif isinstance(version, list):
        json_version = version


    else:
        json_version = version
    params["version"] = json_version

    json_quantization: list[str] | None | Unset
    if isinstance(quantization, Unset):
        json_quantization = UNSET
    elif isinstance(quantization, list):
        json_quantization = quantization


    else:
        json_quantization = quantization
    params["quantization"] = json_quantization

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

    params["local"] = local


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/model/library",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem | None:
    if response.status_code == 200:
        response_200 = ModelLibraryResponse.from_dict(response.json())



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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem]:
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
    usage: list[str] | None | Unset = UNSET,
    family: list[str] | None | Unset = UNSET,
    version: list[str] | None | Unset = UNSET,
    quantization: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListModelLibrarySort | Unset = 'updated',
    local: bool | Unset = False,

) -> Response[BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem]:
    """ Model Library

    Args:
        limit (int | Unset):  Default: 100.
        cursor (None | str | Unset):
        usage (list[str] | None | Unset):
        family (list[str] | None | Unset):
        version (list[str] | None | Unset):
        quantization (list[str] | None | Unset):
        publisher (list[str] | None | Unset):
        alignment (list[str] | None | Unset):
        search (None | str | Unset):
        updated_since (datetime.datetime | None | Unset):
        sort (ListModelLibrarySort | Unset):  Default: 'updated'.
        local (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        limit=limit,
cursor=cursor,
usage=usage,
family=family,
version=version,
quantization=quantization,
publisher=publisher,
alignment=alignment,
search=search,
updated_since=updated_since,
sort=sort,
local=local,

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
    usage: list[str] | None | Unset = UNSET,
    family: list[str] | None | Unset = UNSET,
    version: list[str] | None | Unset = UNSET,
    quantization: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListModelLibrarySort | Unset = 'updated',
    local: bool | Unset = False,

) -> BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem | None:
    """ Model Library

    Args:
        limit (int | Unset):  Default: 100.
        cursor (None | str | Unset):
        usage (list[str] | None | Unset):
        family (list[str] | None | Unset):
        version (list[str] | None | Unset):
        quantization (list[str] | None | Unset):
        publisher (list[str] | None | Unset):
        alignment (list[str] | None | Unset):
        search (None | str | Unset):
        updated_since (datetime.datetime | None | Unset):
        sort (ListModelLibrarySort | Unset):  Default: 'updated'.
        local (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem
     """


    return sync_detailed(
        client=client,
limit=limit,
cursor=cursor,
usage=usage,
family=family,
version=version,
quantization=quantization,
publisher=publisher,
alignment=alignment,
search=search,
updated_since=updated_since,
sort=sort,
local=local,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = 100,
    cursor: None | str | Unset = UNSET,
    usage: list[str] | None | Unset = UNSET,
    family: list[str] | None | Unset = UNSET,
    version: list[str] | None | Unset = UNSET,
    quantization: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListModelLibrarySort | Unset = 'updated',
    local: bool | Unset = False,

) -> Response[BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem]:
    """ Model Library

    Args:
        limit (int | Unset):  Default: 100.
        cursor (None | str | Unset):
        usage (list[str] | None | Unset):
        family (list[str] | None | Unset):
        version (list[str] | None | Unset):
        quantization (list[str] | None | Unset):
        publisher (list[str] | None | Unset):
        alignment (list[str] | None | Unset):
        search (None | str | Unset):
        updated_since (datetime.datetime | None | Unset):
        sort (ListModelLibrarySort | Unset):  Default: 'updated'.
        local (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        limit=limit,
cursor=cursor,
usage=usage,
family=family,
version=version,
quantization=quantization,
publisher=publisher,
alignment=alignment,
search=search,
updated_since=updated_since,
sort=sort,
local=local,

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
    usage: list[str] | None | Unset = UNSET,
    family: list[str] | None | Unset = UNSET,
    version: list[str] | None | Unset = UNSET,
    quantization: list[str] | None | Unset = UNSET,
    publisher: list[str] | None | Unset = UNSET,
    alignment: list[str] | None | Unset = UNSET,
    search: None | str | Unset = UNSET,
    updated_since: datetime.datetime | None | Unset = UNSET,
    sort: ListModelLibrarySort | Unset = 'updated',
    local: bool | Unset = False,

) -> BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem | None:
    """ Model Library

    Args:
        limit (int | Unset):  Default: 100.
        cursor (None | str | Unset):
        usage (list[str] | None | Unset):
        family (list[str] | None | Unset):
        version (list[str] | None | Unset):
        quantization (list[str] | None | Unset):
        publisher (list[str] | None | Unset):
        alignment (list[str] | None | Unset):
        search (None | str | Unset):
        updated_since (datetime.datetime | None | Unset):
        sort (ListModelLibrarySort | Unset):  Default: 'updated'.
        local (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | ModelLibraryResponse | RequestValidationProblem
     """


    return (await asyncio_detailed(
        client=client,
limit=limit,
cursor=cursor,
usage=usage,
family=family,
version=version,
quantization=quantization,
publisher=publisher,
alignment=alignment,
search=search,
updated_since=updated_since,
sort=sort,
local=local,

    )).parsed
