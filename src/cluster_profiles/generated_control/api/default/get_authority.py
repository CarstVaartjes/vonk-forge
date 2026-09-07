from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.authority_response import AuthorityResponse
from ...models.bounded_error_response import BoundedErrorResponse
from ...models.http_validation_error import HTTPValidationError
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    revision: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_revision: Union[None, Unset, str]
    if isinstance(revision, Unset):
        json_revision = UNSET
    else:
        json_revision = revision
    params["revision"] = json_revision


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/authority",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]]:
    if response.status_code == 200:
        response_200 = AuthorityResponse.from_dict(response.json())



        return response_200

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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    revision: Union[None, Unset, str] = UNSET,

) -> Response[Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]]:
    """ Authority View

    Args:
        revision (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        revision=revision,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    revision: Union[None, Unset, str] = UNSET,

) -> Optional[Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]]:
    """ Authority View

    Args:
        revision (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]
     """


    return sync_detailed(
        client=client,
revision=revision,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    revision: Union[None, Unset, str] = UNSET,

) -> Response[Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]]:
    """ Authority View

    Args:
        revision (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        revision=revision,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    revision: Union[None, Unset, str] = UNSET,

) -> Optional[Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]]:
    """ Authority View

    Args:
        revision (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[AuthorityResponse, BoundedErrorResponse, HTTPValidationError]
     """


    return (await asyncio_detailed(
        client=client,
revision=revision,

    )).parsed
