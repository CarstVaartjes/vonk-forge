from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.job_detail_response import JobDetailResponse
from ...models.request_validation_problem import RequestValidationProblem
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    job_id: str,
    *,
    operation_cursor: Union[None, Unset, str] = UNSET,
    target_cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_operation_cursor: Union[None, Unset, str]
    if isinstance(operation_cursor, Unset):
        json_operation_cursor = UNSET
    else:
        json_operation_cursor = operation_cursor
    params["operation_cursor"] = json_operation_cursor

    json_target_cursor: Union[None, Unset, str]
    if isinstance(target_cursor, Unset):
        json_target_cursor = UNSET
    else:
        json_target_cursor = target_cursor
    params["target_cursor"] = json_target_cursor

    params["limit"] = limit


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/jobs/{job_id}".format(job_id=job_id,),
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]]:
    if response.status_code == 200:
        response_200 = JobDetailResponse.from_dict(response.json())



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

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    job_id: str,
    *,
    client: AuthenticatedClient,
    operation_cursor: Union[None, Unset, str] = UNSET,
    target_cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> Response[Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]]:
    """ Job View

    Args:
        job_id (str):
        operation_cursor (Union[None, Unset, str]):
        target_cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        job_id=job_id,
operation_cursor=operation_cursor,
target_cursor=target_cursor,
limit=limit,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    job_id: str,
    *,
    client: AuthenticatedClient,
    operation_cursor: Union[None, Unset, str] = UNSET,
    target_cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> Optional[Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]]:
    """ Job View

    Args:
        job_id (str):
        operation_cursor (Union[None, Unset, str]):
        target_cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]
     """


    return sync_detailed(
        job_id=job_id,
client=client,
operation_cursor=operation_cursor,
target_cursor=target_cursor,
limit=limit,

    ).parsed

async def asyncio_detailed(
    job_id: str,
    *,
    client: AuthenticatedClient,
    operation_cursor: Union[None, Unset, str] = UNSET,
    target_cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> Response[Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]]:
    """ Job View

    Args:
        job_id (str):
        operation_cursor (Union[None, Unset, str]):
        target_cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        job_id=job_id,
operation_cursor=operation_cursor,
target_cursor=target_cursor,
limit=limit,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    job_id: str,
    *,
    client: AuthenticatedClient,
    operation_cursor: Union[None, Unset, str] = UNSET,
    target_cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> Optional[Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]]:
    """ Job View

    Args:
        job_id (str):
        operation_cursor (Union[None, Unset, str]):
        target_cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, JobDetailResponse, RequestValidationProblem]
     """


    return (await asyncio_detailed(
        job_id=job_id,
client=client,
operation_cursor=operation_cursor,
target_cursor=target_cursor,
limit=limit,

    )).parsed
