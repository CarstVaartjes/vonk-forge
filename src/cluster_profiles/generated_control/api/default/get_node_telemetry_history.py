from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.get_node_telemetry_history_resolution import check_get_node_telemetry_history_resolution
from ...models.get_node_telemetry_history_resolution import GetNodeTelemetryHistoryResolution
from ...models.telemetry_history_response import TelemetryHistoryResponse
from ...types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime



def _get_kwargs(
    node_id: str,
    *,
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetNodeTelemetryHistoryResolution,
    maximum_points: Union[Unset, int] = 1500,
    key: Union[None, Unset, str] = UNSET,
    device_id: Union[None, Unset, str] = UNSET,
    interface_name: Union[None, Unset, str] = UNSET,
    run_id: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_start = start.isoformat()
    params["start"] = json_start

    json_end = end.isoformat()
    params["end"] = json_end

    json_resolution: str = resolution
    params["resolution"] = json_resolution

    params["maximum_points"] = maximum_points

    json_key: Union[None, Unset, str]
    if isinstance(key, Unset):
        json_key = UNSET
    else:
        json_key = key
    params["key"] = json_key

    json_device_id: Union[None, Unset, str]
    if isinstance(device_id, Unset):
        json_device_id = UNSET
    else:
        json_device_id = device_id
    params["device_id"] = json_device_id

    json_interface_name: Union[None, Unset, str]
    if isinstance(interface_name, Unset):
        json_interface_name = UNSET
    else:
        json_interface_name = interface_name
    params["interface_name"] = json_interface_name

    json_run_id: Union[None, Unset, str]
    if isinstance(run_id, Unset):
        json_run_id = UNSET
    else:
        json_run_id = run_id
    params["run_id"] = json_run_id


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/nodes/{node_id}/telemetry".format(node_id=node_id,),
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, TelemetryHistoryResponse]]:
    if response.status_code == 200:
        response_200 = TelemetryHistoryResponse.from_dict(response.json())



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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, TelemetryHistoryResponse]]:
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
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetNodeTelemetryHistoryResolution,
    maximum_points: Union[Unset, int] = 1500,
    key: Union[None, Unset, str] = UNSET,
    device_id: Union[None, Unset, str] = UNSET,
    interface_name: Union[None, Unset, str] = UNSET,
    run_id: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, TelemetryHistoryResponse]]:
    """ Node Telemetry History

    Args:
        node_id (str):
        start (datetime.datetime):
        end (datetime.datetime):
        resolution (GetNodeTelemetryHistoryResolution):
        maximum_points (Union[Unset, int]):  Default: 1500.
        key (Union[None, Unset, str]):
        device_id (Union[None, Unset, str]):
        interface_name (Union[None, Unset, str]):
        run_id (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, TelemetryHistoryResponse]]
     """


    kwargs = _get_kwargs(
        node_id=node_id,
start=start,
end=end,
resolution=resolution,
maximum_points=maximum_points,
key=key,
device_id=device_id,
interface_name=interface_name,
run_id=run_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    node_id: str,
    *,
    client: AuthenticatedClient,
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetNodeTelemetryHistoryResolution,
    maximum_points: Union[Unset, int] = 1500,
    key: Union[None, Unset, str] = UNSET,
    device_id: Union[None, Unset, str] = UNSET,
    interface_name: Union[None, Unset, str] = UNSET,
    run_id: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, TelemetryHistoryResponse]]:
    """ Node Telemetry History

    Args:
        node_id (str):
        start (datetime.datetime):
        end (datetime.datetime):
        resolution (GetNodeTelemetryHistoryResolution):
        maximum_points (Union[Unset, int]):  Default: 1500.
        key (Union[None, Unset, str]):
        device_id (Union[None, Unset, str]):
        interface_name (Union[None, Unset, str]):
        run_id (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, TelemetryHistoryResponse]
     """


    return sync_detailed(
        node_id=node_id,
client=client,
start=start,
end=end,
resolution=resolution,
maximum_points=maximum_points,
key=key,
device_id=device_id,
interface_name=interface_name,
run_id=run_id,

    ).parsed

async def asyncio_detailed(
    node_id: str,
    *,
    client: AuthenticatedClient,
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetNodeTelemetryHistoryResolution,
    maximum_points: Union[Unset, int] = 1500,
    key: Union[None, Unset, str] = UNSET,
    device_id: Union[None, Unset, str] = UNSET,
    interface_name: Union[None, Unset, str] = UNSET,
    run_id: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, TelemetryHistoryResponse]]:
    """ Node Telemetry History

    Args:
        node_id (str):
        start (datetime.datetime):
        end (datetime.datetime):
        resolution (GetNodeTelemetryHistoryResolution):
        maximum_points (Union[Unset, int]):  Default: 1500.
        key (Union[None, Unset, str]):
        device_id (Union[None, Unset, str]):
        interface_name (Union[None, Unset, str]):
        run_id (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, TelemetryHistoryResponse]]
     """


    kwargs = _get_kwargs(
        node_id=node_id,
start=start,
end=end,
resolution=resolution,
maximum_points=maximum_points,
key=key,
device_id=device_id,
interface_name=interface_name,
run_id=run_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    node_id: str,
    *,
    client: AuthenticatedClient,
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetNodeTelemetryHistoryResolution,
    maximum_points: Union[Unset, int] = 1500,
    key: Union[None, Unset, str] = UNSET,
    device_id: Union[None, Unset, str] = UNSET,
    interface_name: Union[None, Unset, str] = UNSET,
    run_id: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, TelemetryHistoryResponse]]:
    """ Node Telemetry History

    Args:
        node_id (str):
        start (datetime.datetime):
        end (datetime.datetime):
        resolution (GetNodeTelemetryHistoryResolution):
        maximum_points (Union[Unset, int]):  Default: 1500.
        key (Union[None, Unset, str]):
        device_id (Union[None, Unset, str]):
        interface_name (Union[None, Unset, str]):
        run_id (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, TelemetryHistoryResponse]
     """


    return (await asyncio_detailed(
        node_id=node_id,
client=client,
start=start,
end=end,
resolution=resolution,
maximum_points=maximum_points,
key=key,
device_id=device_id,
interface_name=interface_name,
run_id=run_id,

    )).parsed
