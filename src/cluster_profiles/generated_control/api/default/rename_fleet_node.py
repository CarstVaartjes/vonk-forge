from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.fleet_node_identity import FleetNodeIdentity
from ...models.fleet_rename_request import FleetRenameRequest
from ...models.request_validation_problem import RequestValidationProblem
from typing import cast



def _get_kwargs(
    selector: str,
    *,
    body: FleetRenameRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/fleet/{selector}/rename".format(selector=selector,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]]:
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
        response_422 = RequestValidationProblem.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]]:
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
    body: FleetRenameRequest,

) -> Response[Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]]:
    """ Fleet Rename

    Args:
        selector (str):
        body (FleetRenameRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        selector=selector,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    selector: str,
    *,
    client: AuthenticatedClient,
    body: FleetRenameRequest,

) -> Optional[Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]]:
    """ Fleet Rename

    Args:
        selector (str):
        body (FleetRenameRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]
     """


    return sync_detailed(
        selector=selector,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    selector: str,
    *,
    client: AuthenticatedClient,
    body: FleetRenameRequest,

) -> Response[Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]]:
    """ Fleet Rename

    Args:
        selector (str):
        body (FleetRenameRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        selector=selector,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    selector: str,
    *,
    client: AuthenticatedClient,
    body: FleetRenameRequest,

) -> Optional[Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]]:
    """ Fleet Rename

    Args:
        selector (str):
        body (FleetRenameRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, FleetNodeIdentity, RequestValidationProblem]
     """


    return (await asyncio_detailed(
        selector=selector,
client=client,
body=body,

    )).parsed
