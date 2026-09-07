from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.telemetry_workloads_response import TelemetryWorkloadsResponse
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    node_id: str,
    *,
    run_id: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_run_id: Union[None, Unset, str]
    if isinstance(run_id, Unset):
        json_run_id = UNSET
    else:
        json_run_id = run_id
    params["run_id"] = json_run_id

    json_state: Union[None, Unset, str]
    if isinstance(state, Unset):
        json_state = UNSET
    else:
        json_state = state
    params["state"] = json_state


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/nodes/{node_id}/telemetry/workloads".format(node_id=node_id,),
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, TelemetryWorkloadsResponse]]:
    if response.status_code == 200:
        response_200 = TelemetryWorkloadsResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, TelemetryWorkloadsResponse]]:
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
    run_id: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, TelemetryWorkloadsResponse]]:
    """ Node Telemetry Workloads

    Args:
        node_id (str):
        run_id (Union[None, Unset, str]):
        state (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, TelemetryWorkloadsResponse]]
     """


    kwargs = _get_kwargs(
        node_id=node_id,
run_id=run_id,
state=state,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    node_id: str,
    *,
    client: AuthenticatedClient,
    run_id: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, TelemetryWorkloadsResponse]]:
    """ Node Telemetry Workloads

    Args:
        node_id (str):
        run_id (Union[None, Unset, str]):
        state (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, TelemetryWorkloadsResponse]
     """


    return sync_detailed(
        node_id=node_id,
client=client,
run_id=run_id,
state=state,

    ).parsed

async def asyncio_detailed(
    node_id: str,
    *,
    client: AuthenticatedClient,
    run_id: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, TelemetryWorkloadsResponse]]:
    """ Node Telemetry Workloads

    Args:
        node_id (str):
        run_id (Union[None, Unset, str]):
        state (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, TelemetryWorkloadsResponse]]
     """


    kwargs = _get_kwargs(
        node_id=node_id,
run_id=run_id,
state=state,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    node_id: str,
    *,
    client: AuthenticatedClient,
    run_id: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, TelemetryWorkloadsResponse]]:
    """ Node Telemetry Workloads

    Args:
        node_id (str):
        run_id (Union[None, Unset, str]):
        state (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, TelemetryWorkloadsResponse]
     """


    return (await asyncio_detailed(
        node_id=node_id,
client=client,
run_id=run_id,
state=state,

    )).parsed
