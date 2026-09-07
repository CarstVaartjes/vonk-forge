from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.fleet_profile_retry_request import FleetProfileRetryRequest
from ...models.library_placement_application import LibraryPlacementApplication
from ...models.request_validation_problem import RequestValidationProblem
from typing import cast



def _get_kwargs(
    placement_id: str,
    *,
    body: FleetProfileRetryRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/library/placements/{placement_id}/retry".format(placement_id=placement_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]]:
    if response.status_code == 202:
        response_202 = LibraryPlacementApplication.from_dict(response.json())



        return response_202

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = BoundedErrorResponse.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = BoundedErrorResponse.from_dict(response.json())



        return response_404

    if response.status_code == 409:
        response_409 = BoundedErrorResponse.from_dict(response.json())



        return response_409

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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    placement_id: str,
    *,
    client: AuthenticatedClient,
    body: FleetProfileRetryRequest,

) -> Response[Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]]:
    """ Retry Application

    Args:
        placement_id (str):
        body (FleetProfileRetryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        placement_id=placement_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    placement_id: str,
    *,
    client: AuthenticatedClient,
    body: FleetProfileRetryRequest,

) -> Optional[Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]]:
    """ Retry Application

    Args:
        placement_id (str):
        body (FleetProfileRetryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]
     """


    return sync_detailed(
        placement_id=placement_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    placement_id: str,
    *,
    client: AuthenticatedClient,
    body: FleetProfileRetryRequest,

) -> Response[Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]]:
    """ Retry Application

    Args:
        placement_id (str):
        body (FleetProfileRetryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        placement_id=placement_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    placement_id: str,
    *,
    client: AuthenticatedClient,
    body: FleetProfileRetryRequest,

) -> Optional[Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]]:
    """ Retry Application

    Args:
        placement_id (str):
        body (FleetProfileRetryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, LibraryPlacementApplication, RequestValidationProblem]
     """


    return (await asyncio_detailed(
        placement_id=placement_id,
client=client,
body=body,

    )).parsed
