from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.catalog_entity_list_response import CatalogEntityListResponse
from ...models.catalog_problem import CatalogProblem
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    kind: Union[None, Unset, str] = UNSET,
    publisher: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    cursor: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_kind: Union[None, Unset, str]
    if isinstance(kind, Unset):
        json_kind = UNSET
    else:
        json_kind = kind
    params["kind"] = json_kind

    json_publisher: Union[None, Unset, str]
    if isinstance(publisher, Unset):
        json_publisher = UNSET
    else:
        json_publisher = publisher
    params["publisher"] = json_publisher

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
        "url": "/api/v1/catalog/entities",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[CatalogEntityListResponse, CatalogProblem]]:
    if response.status_code == 200:
        response_200 = CatalogEntityListResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = CatalogProblem.from_dict(response.json())



        return response_401

    if response.status_code == 422:
        response_422 = CatalogProblem.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[CatalogEntityListResponse, CatalogProblem]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    kind: Union[None, Unset, str] = UNSET,
    publisher: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    cursor: Union[None, Unset, str] = UNSET,

) -> Response[Union[CatalogEntityListResponse, CatalogProblem]]:
    """ List Entities

    Args:
        kind (Union[None, Unset, str]):
        publisher (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogEntityListResponse, CatalogProblem]]
     """


    kwargs = _get_kwargs(
        kind=kind,
publisher=publisher,
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
    kind: Union[None, Unset, str] = UNSET,
    publisher: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    cursor: Union[None, Unset, str] = UNSET,

) -> Optional[Union[CatalogEntityListResponse, CatalogProblem]]:
    """ List Entities

    Args:
        kind (Union[None, Unset, str]):
        publisher (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogEntityListResponse, CatalogProblem]
     """


    return sync_detailed(
        client=client,
kind=kind,
publisher=publisher,
limit=limit,
cursor=cursor,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    kind: Union[None, Unset, str] = UNSET,
    publisher: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    cursor: Union[None, Unset, str] = UNSET,

) -> Response[Union[CatalogEntityListResponse, CatalogProblem]]:
    """ List Entities

    Args:
        kind (Union[None, Unset, str]):
        publisher (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogEntityListResponse, CatalogProblem]]
     """


    kwargs = _get_kwargs(
        kind=kind,
publisher=publisher,
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
    kind: Union[None, Unset, str] = UNSET,
    publisher: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    cursor: Union[None, Unset, str] = UNSET,

) -> Optional[Union[CatalogEntityListResponse, CatalogProblem]]:
    """ List Entities

    Args:
        kind (Union[None, Unset, str]):
        publisher (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogEntityListResponse, CatalogProblem]
     """


    return (await asyncio_detailed(
        client=client,
kind=kind,
publisher=publisher,
limit=limit,
cursor=cursor,

    )).parsed
