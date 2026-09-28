from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.get_fleet_metrics_history_resolution import check_get_fleet_metrics_history_resolution
from ...models.get_fleet_metrics_history_resolution import GetFleetMetricsHistoryResolution
from ...models.request_validation_problem import RequestValidationProblem
from ...models.telemetry_history_response import TelemetryHistoryResponse
from ...types import UNSET, Unset
from typing import cast
import datetime



def _get_kwargs(
    selector: str,
    *,
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetFleetMetricsHistoryResolution,
    maximum_points: int | Unset = 1500,
    key: None | str | Unset = UNSET,
    device_id: None | str | Unset = UNSET,
    interface_name: None | str | Unset = UNSET,
    run_id: None | str | Unset = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_start = start.isoformat()
    params["start"] = json_start

    json_end = end.isoformat()
    params["end"] = json_end

    json_resolution: str = resolution
    params["resolution"] = json_resolution

    params["maximum_points"] = maximum_points

    json_key: None | str | Unset
    if isinstance(key, Unset):
        json_key = UNSET
    else:
        json_key = key
    params["key"] = json_key

    json_device_id: None | str | Unset
    if isinstance(device_id, Unset):
        json_device_id = UNSET
    else:
        json_device_id = device_id
    params["device_id"] = json_device_id

    json_interface_name: None | str | Unset
    if isinstance(interface_name, Unset):
        json_interface_name = UNSET
    else:
        json_interface_name = interface_name
    params["interface_name"] = json_interface_name

    json_run_id: None | str | Unset
    if isinstance(run_id, Unset):
        json_run_id = UNSET
    else:
        json_run_id = run_id
    params["run_id"] = json_run_id


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/fleet/{selector}/metrics/history".format(selector=quote(str(selector), safe=""),),
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse | None:
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
        response_422 = RequestValidationProblem.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    selector: str,
    *,
    client: AuthenticatedClient,
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetFleetMetricsHistoryResolution,
    maximum_points: int | Unset = 1500,
    key: None | str | Unset = UNSET,
    device_id: None | str | Unset = UNSET,
    interface_name: None | str | Unset = UNSET,
    run_id: None | str | Unset = UNSET,

) -> Response[BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse]:
    """ Fleet Metrics History

    Args:
        selector (str):
        start (datetime.datetime):
        end (datetime.datetime):
        resolution (GetFleetMetricsHistoryResolution):
        maximum_points (int | Unset):  Default: 1500.
        key (None | str | Unset):
        device_id (None | str | Unset):
        interface_name (None | str | Unset):
        run_id (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse]
     """


    kwargs = _get_kwargs(
        selector=selector,
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
    selector: str,
    *,
    client: AuthenticatedClient,
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetFleetMetricsHistoryResolution,
    maximum_points: int | Unset = 1500,
    key: None | str | Unset = UNSET,
    device_id: None | str | Unset = UNSET,
    interface_name: None | str | Unset = UNSET,
    run_id: None | str | Unset = UNSET,

) -> BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse | None:
    """ Fleet Metrics History

    Args:
        selector (str):
        start (datetime.datetime):
        end (datetime.datetime):
        resolution (GetFleetMetricsHistoryResolution):
        maximum_points (int | Unset):  Default: 1500.
        key (None | str | Unset):
        device_id (None | str | Unset):
        interface_name (None | str | Unset):
        run_id (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse
     """


    return sync_detailed(
        selector=selector,
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
    selector: str,
    *,
    client: AuthenticatedClient,
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetFleetMetricsHistoryResolution,
    maximum_points: int | Unset = 1500,
    key: None | str | Unset = UNSET,
    device_id: None | str | Unset = UNSET,
    interface_name: None | str | Unset = UNSET,
    run_id: None | str | Unset = UNSET,

) -> Response[BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse]:
    """ Fleet Metrics History

    Args:
        selector (str):
        start (datetime.datetime):
        end (datetime.datetime):
        resolution (GetFleetMetricsHistoryResolution):
        maximum_points (int | Unset):  Default: 1500.
        key (None | str | Unset):
        device_id (None | str | Unset):
        interface_name (None | str | Unset):
        run_id (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse]
     """


    kwargs = _get_kwargs(
        selector=selector,
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
    selector: str,
    *,
    client: AuthenticatedClient,
    start: datetime.datetime,
    end: datetime.datetime,
    resolution: GetFleetMetricsHistoryResolution,
    maximum_points: int | Unset = 1500,
    key: None | str | Unset = UNSET,
    device_id: None | str | Unset = UNSET,
    interface_name: None | str | Unset = UNSET,
    run_id: None | str | Unset = UNSET,

) -> BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse | None:
    """ Fleet Metrics History

    Args:
        selector (str):
        start (datetime.datetime):
        end (datetime.datetime):
        resolution (GetFleetMetricsHistoryResolution):
        maximum_points (int | Unset):  Default: 1500.
        key (None | str | Unset):
        device_id (None | str | Unset):
        interface_name (None | str | Unset):
        run_id (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RequestValidationProblem | TelemetryHistoryResponse
     """


    return (await asyncio_detailed(
        selector=selector,
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
