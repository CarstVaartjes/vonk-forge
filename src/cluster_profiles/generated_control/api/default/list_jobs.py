from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.jobs_response import JobsResponse
from ...models.request_validation_problem import RequestValidationProblem
from ...types import UNSET, Unset
from typing import cast



def _get_kwargs(
    *,
    cursor: None | str | Unset = UNSET,
    limit: int | Unset = 20,
    status: None | str | Unset = UNSET,
    target: None | str | Unset = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_cursor: None | str | Unset
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    params["limit"] = limit

    json_status: None | str | Unset
    if isinstance(status, Unset):
        json_status = UNSET
    else:
        json_status = status
    params["status"] = json_status

    json_target: None | str | Unset
    if isinstance(target, Unset):
        json_target = UNSET
    else:
        json_target = target
    params["target"] = json_target


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/jobs",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | JobsResponse | RequestValidationProblem | None:
    if response.status_code == 200:
        response_200 = JobsResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 422:
        response_422 = RequestValidationProblem.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | JobsResponse | RequestValidationProblem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    cursor: None | str | Unset = UNSET,
    limit: int | Unset = 20,
    status: None | str | Unset = UNSET,
    target: None | str | Unset = UNSET,

) -> Response[BoundedErrorResponse | JobsResponse | RequestValidationProblem]:
    """ Jobs View

    Args:
        cursor (None | str | Unset):
        limit (int | Unset):  Default: 20.
        status (None | str | Unset):
        target (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | JobsResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        cursor=cursor,
limit=limit,
status=status,
target=target,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    cursor: None | str | Unset = UNSET,
    limit: int | Unset = 20,
    status: None | str | Unset = UNSET,
    target: None | str | Unset = UNSET,

) -> BoundedErrorResponse | JobsResponse | RequestValidationProblem | None:
    """ Jobs View

    Args:
        cursor (None | str | Unset):
        limit (int | Unset):  Default: 20.
        status (None | str | Unset):
        target (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | JobsResponse | RequestValidationProblem
     """


    return sync_detailed(
        client=client,
cursor=cursor,
limit=limit,
status=status,
target=target,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    cursor: None | str | Unset = UNSET,
    limit: int | Unset = 20,
    status: None | str | Unset = UNSET,
    target: None | str | Unset = UNSET,

) -> Response[BoundedErrorResponse | JobsResponse | RequestValidationProblem]:
    """ Jobs View

    Args:
        cursor (None | str | Unset):
        limit (int | Unset):  Default: 20.
        status (None | str | Unset):
        target (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | JobsResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        cursor=cursor,
limit=limit,
status=status,
target=target,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    cursor: None | str | Unset = UNSET,
    limit: int | Unset = 20,
    status: None | str | Unset = UNSET,
    target: None | str | Unset = UNSET,

) -> BoundedErrorResponse | JobsResponse | RequestValidationProblem | None:
    """ Jobs View

    Args:
        cursor (None | str | Unset):
        limit (int | Unset):  Default: 20.
        status (None | str | Unset):
        target (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | JobsResponse | RequestValidationProblem
     """


    return (await asyncio_detailed(
        client=client,
cursor=cursor,
limit=limit,
status=status,
target=target,

    )).parsed
