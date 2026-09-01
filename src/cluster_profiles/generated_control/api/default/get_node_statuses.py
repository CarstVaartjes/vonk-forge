from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.fleet_status_response import FleetStatusResponse
from typing import cast



def _get_kwargs(

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/nodes/status",
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, FleetStatusResponse]]:
    if response.status_code == 200:
        response_200 = FleetStatusResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, FleetStatusResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,

) -> Response[Union[BoundedErrorResponse, FleetStatusResponse]]:
    """ Read explicit node health-probe evidence

     Returns the legacy node health-probe projection. Its stale fields refer only to explicit node.probe
    compute-gate evidence, not aggregate Fleet readiness. Use /api/v1/fleet for live connection,
    inventory, and telemetry readiness.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, FleetStatusResponse]]
     """


    kwargs = _get_kwargs(

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,

) -> Optional[Union[BoundedErrorResponse, FleetStatusResponse]]:
    """ Read explicit node health-probe evidence

     Returns the legacy node health-probe projection. Its stale fields refer only to explicit node.probe
    compute-gate evidence, not aggregate Fleet readiness. Use /api/v1/fleet for live connection,
    inventory, and telemetry readiness.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, FleetStatusResponse]
     """


    return sync_detailed(
        client=client,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,

) -> Response[Union[BoundedErrorResponse, FleetStatusResponse]]:
    """ Read explicit node health-probe evidence

     Returns the legacy node health-probe projection. Its stale fields refer only to explicit node.probe
    compute-gate evidence, not aggregate Fleet readiness. Use /api/v1/fleet for live connection,
    inventory, and telemetry readiness.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, FleetStatusResponse]]
     """


    kwargs = _get_kwargs(

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,

) -> Optional[Union[BoundedErrorResponse, FleetStatusResponse]]:
    """ Read explicit node health-probe evidence

     Returns the legacy node health-probe projection. Its stale fields refer only to explicit node.probe
    compute-gate evidence, not aggregate Fleet readiness. Use /api/v1/fleet for live connection,
    inventory, and telemetry readiness.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, FleetStatusResponse]
     """


    return (await asyncio_detailed(
        client=client,

    )).parsed
