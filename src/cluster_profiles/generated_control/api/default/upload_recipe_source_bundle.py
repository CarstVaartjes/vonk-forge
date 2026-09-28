from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.catalog_problem import CatalogProblem
from ...models.source_bundle_response import SourceBundleResponse
from ...types import File, FileTypes
from io import BytesIO
from typing import cast



def _get_kwargs(
    sha256: str,
    *,
    body: File,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/catalog/source-bundles/{sha256}".format(sha256=quote(str(sha256), safe=""),),
    }

    _kwargs["content"] = body.payload
    headers["Content-Type"] = "application/octet-stream"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> CatalogProblem | SourceBundleResponse | None:
    if response.status_code == 200:
        response_200 = SourceBundleResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = CatalogProblem.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = CatalogProblem.from_dict(response.json())



        return response_403

    if response.status_code == 409:
        response_409 = CatalogProblem.from_dict(response.json())



        return response_409

    if response.status_code == 422:
        response_422 = CatalogProblem.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[CatalogProblem | SourceBundleResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    sha256: str,
    *,
    client: AuthenticatedClient,
    body: File,

) -> Response[CatalogProblem | SourceBundleResponse]:
    """ Upload Source Bundle

    Args:
        sha256 (str):
        body (File):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CatalogProblem | SourceBundleResponse]
     """


    kwargs = _get_kwargs(
        sha256=sha256,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    sha256: str,
    *,
    client: AuthenticatedClient,
    body: File,

) -> CatalogProblem | SourceBundleResponse | None:
    """ Upload Source Bundle

    Args:
        sha256 (str):
        body (File):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CatalogProblem | SourceBundleResponse
     """


    return sync_detailed(
        sha256=sha256,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    sha256: str,
    *,
    client: AuthenticatedClient,
    body: File,

) -> Response[CatalogProblem | SourceBundleResponse]:
    """ Upload Source Bundle

    Args:
        sha256 (str):
        body (File):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CatalogProblem | SourceBundleResponse]
     """


    kwargs = _get_kwargs(
        sha256=sha256,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    sha256: str,
    *,
    client: AuthenticatedClient,
    body: File,

) -> CatalogProblem | SourceBundleResponse | None:
    """ Upload Source Bundle

    Args:
        sha256 (str):
        body (File):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CatalogProblem | SourceBundleResponse
     """


    return (await asyncio_detailed(
        sha256=sha256,
client=client,
body=body,

    )).parsed
