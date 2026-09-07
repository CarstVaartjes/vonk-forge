from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.operations_response import OperationsResponse
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    state: Union[None, Unset, str] = UNSET,
    node_id: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_cursor: Union[None, Unset, str]
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    params["limit"] = limit

    json_state: Union[None, Unset, str]
    if isinstance(state, Unset):
        json_state = UNSET
    else:
        json_state = state
    params["state"] = json_state

    json_node_id: Union[None, Unset, str]
    if isinstance(node_id, Unset):
        json_node_id = UNSET
    else:
        json_node_id = node_id
    params["node_id"] = json_node_id


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/operations",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, OperationsResponse]]:
    if response.status_code == 200:
        response_200 = OperationsResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 422:
        response_422 = BoundedErrorResponse.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, OperationsResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    state: Union[None, Unset, str] = UNSET,
    node_id: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, OperationsResponse]]:
    """ Operations View

    Args:
        cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.
        state (Union[None, Unset, str]):
        node_id (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, OperationsResponse]]
     """


    kwargs = _get_kwargs(
        cursor=cursor,
limit=limit,
state=state,
node_id=node_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    state: Union[None, Unset, str] = UNSET,
    node_id: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, OperationsResponse]]:
    """ Operations View

    Args:
        cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.
        state (Union[None, Unset, str]):
        node_id (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, OperationsResponse]
     """


    return sync_detailed(
        client=client,
cursor=cursor,
limit=limit,
state=state,
node_id=node_id,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    state: Union[None, Unset, str] = UNSET,
    node_id: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, OperationsResponse]]:
    """ Operations View

    Args:
        cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.
        state (Union[None, Unset, str]):
        node_id (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, OperationsResponse]]
     """


    kwargs = _get_kwargs(
        cursor=cursor,
limit=limit,
state=state,
node_id=node_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,
    state: Union[None, Unset, str] = UNSET,
    node_id: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, OperationsResponse]]:
    """ Operations View

    Args:
        cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.
        state (Union[None, Unset, str]):
        node_id (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, OperationsResponse]
     """


    return (await asyncio_detailed(
        client=client,
cursor=cursor,
limit=limit,
state=state,
node_id=node_id,

    )).parsed
