from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.request_validation_problem import RequestValidationProblem
from ...models.telemetry_capabilities_response import TelemetryCapabilitiesResponse
from typing import cast



def _get_kwargs(
    selector: str,

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/fleet/{selector}/metrics/capabilities".format(selector=selector,),
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]]:
    if response.status_code == 200:
        response_200 = TelemetryCapabilitiesResponse.from_dict(response.json())



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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]]:
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

) -> Response[Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]]:
    """ Fleet Metrics Capabilities

    Args:
        selector (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]]
     """


    kwargs = _get_kwargs(
        selector=selector,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    selector: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]]:
    """ Fleet Metrics Capabilities

    Args:
        selector (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]
     """


    return sync_detailed(
        selector=selector,
client=client,

    ).parsed

async def asyncio_detailed(
    selector: str,
    *,
    client: AuthenticatedClient,

) -> Response[Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]]:
    """ Fleet Metrics Capabilities

    Args:
        selector (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]]
     """


    kwargs = _get_kwargs(
        selector=selector,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    selector: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]]:
    """ Fleet Metrics Capabilities

    Args:
        selector (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, RequestValidationProblem, TelemetryCapabilitiesResponse]
     """


    return (await asyncio_detailed(
        selector=selector,
client=client,

    )).parsed
