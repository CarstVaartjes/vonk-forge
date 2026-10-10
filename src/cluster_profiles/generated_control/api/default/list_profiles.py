from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.capability_unavailable_reply import CapabilityUnavailableReply
from ...models.fleet_profile_list import FleetProfileList
from ...models.http_transient import HttpTransient
from ...models.request_validation_problem import RequestValidationProblem
from typing import cast



def _get_kwargs(

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/profile",
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem | None:
    if response.status_code == 200:
        response_200 = FleetProfileList.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 422:
        response_422 = RequestValidationProblem.from_dict(response.json())



        return response_422

    if response.status_code == 429:
        response_429 = HttpTransient.from_dict(response.json())



        return response_429

    if response.status_code == 503:
        def _parse_response_503(data: object) -> BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_503_type_0 = BoundedErrorResponse.from_dict(data)



                return response_503_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_503_type_1 = CapabilityUnavailableReply.from_dict(data)



                return response_503_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_503_type_2 = HttpTransient.from_dict(data)



            return response_503_type_2

        response_503 = _parse_response_503(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem]:
    """ List Profiles

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem]
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

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem | None:
    """ List Profiles

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem
     """


    return sync_detailed(
        client=client,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem]:
    """ List Profiles

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem]
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

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem | None:
    """ List Profiles

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileList | HttpTransient | RequestValidationProblem
     """


    return (await asyncio_detailed(
        client=client,

    )).parsed
