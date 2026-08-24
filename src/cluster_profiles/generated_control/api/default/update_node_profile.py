from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.fleet_node_identity import FleetNodeIdentity
from ...models.node_profile_update_request import NodeProfileUpdateRequest
from typing import cast



def _get_kwargs(
    node_id: str,
    *,
    body: NodeProfileUpdateRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/api/v1/nodes/{node_id}/profile".format(node_id=node_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, FleetNodeIdentity]]:
    if response.status_code == 200:
        response_200 = FleetNodeIdentity.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = BoundedErrorResponse.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = BoundedErrorResponse.from_dict(response.json())



        return response_404

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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, FleetNodeIdentity]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    node_id: str,
    *,
    client: AuthenticatedClient,
    body: NodeProfileUpdateRequest,

) -> Response[Union[BoundedErrorResponse, FleetNodeIdentity]]:
    """ Update Node Profile

    Args:
        node_id (str):
        body (NodeProfileUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, FleetNodeIdentity]]
     """


    kwargs = _get_kwargs(
        node_id=node_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    node_id: str,
    *,
    client: AuthenticatedClient,
    body: NodeProfileUpdateRequest,

) -> Optional[Union[BoundedErrorResponse, FleetNodeIdentity]]:
    """ Update Node Profile

    Args:
        node_id (str):
        body (NodeProfileUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, FleetNodeIdentity]
     """


    return sync_detailed(
        node_id=node_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    node_id: str,
    *,
    client: AuthenticatedClient,
    body: NodeProfileUpdateRequest,

) -> Response[Union[BoundedErrorResponse, FleetNodeIdentity]]:
    """ Update Node Profile

    Args:
        node_id (str):
        body (NodeProfileUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, FleetNodeIdentity]]
     """


    kwargs = _get_kwargs(
        node_id=node_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    node_id: str,
    *,
    client: AuthenticatedClient,
    body: NodeProfileUpdateRequest,

) -> Optional[Union[BoundedErrorResponse, FleetNodeIdentity]]:
    """ Update Node Profile

    Args:
        node_id (str):
        body (NodeProfileUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, FleetNodeIdentity]
     """


    return (await asyncio_detailed(
        node_id=node_id,
client=client,
body=body,

    )).parsed
