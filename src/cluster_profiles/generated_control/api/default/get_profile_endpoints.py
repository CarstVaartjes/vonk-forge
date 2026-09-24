from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.fleet_profile_endpoints_view import FleetProfileEndpointsView
from ...models.request_validation_problem import RequestValidationProblem
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    number: int,
    *,
    alias: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_alias: Union[None, Unset, str]
    if isinstance(alias, Unset):
        json_alias = UNSET
    else:
        json_alias = alias
    params["alias"] = json_alias


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/profile/{number}/endpoints".format(number=number,),
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]]:
    if response.status_code == 200:
        response_200 = FleetProfileEndpointsView.from_dict(response.json())



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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    number: int,
    *,
    client: AuthenticatedClient,
    alias: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]]:
    """ Profile Endpoints

    Args:
        number (int):
        alias (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        number=number,
alias=alias,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    number: int,
    *,
    client: AuthenticatedClient,
    alias: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]]:
    """ Profile Endpoints

    Args:
        number (int):
        alias (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]
     """


    return sync_detailed(
        number=number,
client=client,
alias=alias,

    ).parsed

async def asyncio_detailed(
    number: int,
    *,
    client: AuthenticatedClient,
    alias: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]]:
    """ Profile Endpoints

    Args:
        number (int):
        alias (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        number=number,
alias=alias,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    number: int,
    *,
    client: AuthenticatedClient,
    alias: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]]:
    """ Profile Endpoints

    Args:
        number (int):
        alias (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, FleetProfileEndpointsView, RequestValidationProblem]
     """


    return (await asyncio_detailed(
        number=number,
client=client,
alias=alias,

    )).parsed
