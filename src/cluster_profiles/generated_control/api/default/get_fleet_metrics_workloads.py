from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.request_validation_problem import RequestValidationProblem
from ...models.telemetry_workloads_response import TelemetryWorkloadsResponse
from ...types import UNSET, Unset
from typing import cast



def _get_kwargs(
    selector: str,
    *,
    run_id: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_run_id: None | str | Unset
    if isinstance(run_id, Unset):
        json_run_id = UNSET
    else:
        json_run_id = run_id
    params["run_id"] = json_run_id

    json_state: None | str | Unset
    if isinstance(state, Unset):
        json_state = UNSET
    else:
        json_state = state
    params["state"] = json_state


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/fleet/{selector}/metrics/workloads".format(selector=quote(str(selector), safe=""),),
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse | None:
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
        response_422 = RequestValidationProblem.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse]:
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
    run_id: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,

) -> Response[BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse]:
    """ Fleet Metrics Workloads

    Args:
        selector (str):
        run_id (None | str | Unset):
        state (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse]
     """


    kwargs = _get_kwargs(
        selector=selector,
run_id=run_id,
state=state,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    selector: str,
    *,
    client: AuthenticatedClient,
    run_id: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,

) -> BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse | None:
    """ Fleet Metrics Workloads

    Args:
        selector (str):
        run_id (None | str | Unset):
        state (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse
     """


    return sync_detailed(
        selector=selector,
client=client,
run_id=run_id,
state=state,

    ).parsed

async def asyncio_detailed(
    selector: str,
    *,
    client: AuthenticatedClient,
    run_id: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,

) -> Response[BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse]:
    """ Fleet Metrics Workloads

    Args:
        selector (str):
        run_id (None | str | Unset):
        state (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse]
     """


    kwargs = _get_kwargs(
        selector=selector,
run_id=run_id,
state=state,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    selector: str,
    *,
    client: AuthenticatedClient,
    run_id: None | str | Unset = UNSET,
    state: None | str | Unset = UNSET,

) -> BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse | None:
    """ Fleet Metrics Workloads

    Args:
        selector (str):
        run_id (None | str | Unset):
        state (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RequestValidationProblem | TelemetryWorkloadsResponse
     """


    return (await asyncio_detailed(
        selector=selector,
client=client,
run_id=run_id,
state=state,

    )).parsed
