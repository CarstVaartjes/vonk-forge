from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.http_validation_error import HTTPValidationError
from ...models.model_cache_updates_response import ModelCacheUpdatesResponse
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    artifact_set_sha256: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_artifact_set_sha256: Union[None, Unset, str]
    if isinstance(artifact_set_sha256, Unset):
        json_artifact_set_sha256 = UNSET
    else:
        json_artifact_set_sha256 = artifact_set_sha256
    params["artifact_set_sha256"] = json_artifact_set_sha256

    params["limit"] = limit

    json_cursor: Union[None, Unset, str]
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/model-cache/updates",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]]:
    if response.status_code == 200:
        response_200 = ModelCacheUpdatesResponse.from_dict(response.json())



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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    artifact_set_sha256: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]]:
    """ Get Updates

    Args:
        artifact_set_sha256 (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 100.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]]
     """


    kwargs = _get_kwargs(
        artifact_set_sha256=artifact_set_sha256,
limit=limit,
cursor=cursor,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    artifact_set_sha256: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]]:
    """ Get Updates

    Args:
        artifact_set_sha256 (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 100.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]
     """


    return sync_detailed(
        client=client,
artifact_set_sha256=artifact_set_sha256,
limit=limit,
cursor=cursor,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    artifact_set_sha256: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,

) -> Response[Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]]:
    """ Get Updates

    Args:
        artifact_set_sha256 (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 100.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]]
     """


    kwargs = _get_kwargs(
        artifact_set_sha256=artifact_set_sha256,
limit=limit,
cursor=cursor,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    artifact_set_sha256: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,
    cursor: Union[None, Unset, str] = UNSET,

) -> Optional[Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]]:
    """ Get Updates

    Args:
        artifact_set_sha256 (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 100.
        cursor (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, HTTPValidationError, ModelCacheUpdatesResponse]
     """


    return (await asyncio_detailed(
        client=client,
artifact_set_sha256=artifact_set_sha256,
limit=limit,
cursor=cursor,

    )).parsed
