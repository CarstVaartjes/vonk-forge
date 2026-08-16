from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.catalog_entity_revision_response import CatalogEntityRevisionResponse
from ...models.catalog_problem import CatalogProblem
from ...models.create_catalog_entity_request import CreateCatalogEntityRequest
from typing import cast



def _get_kwargs(
    *,
    body: CreateCatalogEntityRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/catalog/entities",
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    if response.status_code == 201:
        response_201 = CatalogEntityRevisionResponse.from_dict(response.json())



        return response_201

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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: CreateCatalogEntityRequest,

) -> Response[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    """ Create Entity

    Args:
        body (CreateCatalogEntityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogEntityRevisionResponse, CatalogProblem]]
     """


    kwargs = _get_kwargs(
        body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    body: CreateCatalogEntityRequest,

) -> Optional[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    """ Create Entity

    Args:
        body (CreateCatalogEntityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogEntityRevisionResponse, CatalogProblem]
     """


    return sync_detailed(
        client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: CreateCatalogEntityRequest,

) -> Response[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    """ Create Entity

    Args:
        body (CreateCatalogEntityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogEntityRevisionResponse, CatalogProblem]]
     """


    kwargs = _get_kwargs(
        body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    body: CreateCatalogEntityRequest,

) -> Optional[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    """ Create Entity

    Args:
        body (CreateCatalogEntityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogEntityRevisionResponse, CatalogProblem]
     """


    return (await asyncio_detailed(
        client=client,
body=body,

    )).parsed
