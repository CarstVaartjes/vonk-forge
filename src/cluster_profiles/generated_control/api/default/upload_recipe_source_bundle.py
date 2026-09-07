from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.catalog_problem import CatalogProblem
from ...models.source_bundle_response import SourceBundleResponse
from typing import cast



def _get_kwargs(
    sha256: str,

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/catalog/source-bundles/{sha256}".format(sha256=sha256,),
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[CatalogProblem, SourceBundleResponse]]:
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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[CatalogProblem, SourceBundleResponse]]:
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

) -> Response[Union[CatalogProblem, SourceBundleResponse]]:
    """ Upload Source Bundle

    Args:
        sha256 (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogProblem, SourceBundleResponse]]
     """


    kwargs = _get_kwargs(
        sha256=sha256,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    sha256: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[CatalogProblem, SourceBundleResponse]]:
    """ Upload Source Bundle

    Args:
        sha256 (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogProblem, SourceBundleResponse]
     """


    return sync_detailed(
        sha256=sha256,
client=client,

    ).parsed

async def asyncio_detailed(
    sha256: str,
    *,
    client: AuthenticatedClient,

) -> Response[Union[CatalogProblem, SourceBundleResponse]]:
    """ Upload Source Bundle

    Args:
        sha256 (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogProblem, SourceBundleResponse]]
     """


    kwargs = _get_kwargs(
        sha256=sha256,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    sha256: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[CatalogProblem, SourceBundleResponse]]:
    """ Upload Source Bundle

    Args:
        sha256 (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogProblem, SourceBundleResponse]
     """


    return (await asyncio_detailed(
        sha256=sha256,
client=client,

    )).parsed
