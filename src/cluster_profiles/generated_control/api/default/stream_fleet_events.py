from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.http_validation_error import HTTPValidationError
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    last_event_id: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(last_event_id, Unset):
        headers["Last-Event-ID"] = last_event_id







    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/fleet/stream",
    }


    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, HTTPValidationError, str]]:
    if response.status_code == 200:
        response_200 = response.text
        return response_200

    if response.status_code == 400:
        response_400 = BoundedErrorResponse.from_dict(response.json())



        return response_400

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, HTTPValidationError, str]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    last_event_id: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, HTTPValidationError, str]]:
    """ Fleet Event Stream

    Args:
        last_event_id (Union[None, Unset, str]): Optional durable Fleet cursor; duplicate and
            numeric validity are checked from the raw header list.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, HTTPValidationError, str]]
     """


    kwargs = _get_kwargs(
        last_event_id=last_event_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    last_event_id: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, HTTPValidationError, str]]:
    """ Fleet Event Stream

    Args:
        last_event_id (Union[None, Unset, str]): Optional durable Fleet cursor; duplicate and
            numeric validity are checked from the raw header list.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, HTTPValidationError, str]
     """


    return sync_detailed(
        client=client,
last_event_id=last_event_id,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    last_event_id: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, HTTPValidationError, str]]:
    """ Fleet Event Stream

    Args:
        last_event_id (Union[None, Unset, str]): Optional durable Fleet cursor; duplicate and
            numeric validity are checked from the raw header list.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, HTTPValidationError, str]]
     """


    kwargs = _get_kwargs(
        last_event_id=last_event_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    last_event_id: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, HTTPValidationError, str]]:
    """ Fleet Event Stream

    Args:
        last_event_id (Union[None, Unset, str]): Optional durable Fleet cursor; duplicate and
            numeric validity are checked from the raw header list.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, HTTPValidationError, str]
     """


    return (await asyncio_detailed(
        client=client,
last_event_id=last_event_id,

    )).parsed
