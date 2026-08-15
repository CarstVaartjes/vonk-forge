from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.catalog_entity_revision_response import CatalogEntityRevisionResponse
from ...models.catalog_problem import CatalogProblem
from ...models.revise_catalog_entity_request import ReviseCatalogEntityRequest
from typing import cast



def _get_kwargs(
    entity_id: str,
    *,
    body: ReviseCatalogEntityRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/v1/catalog/entities/{entity_id}/draft".format(entity_id=entity_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    if response.status_code == 200:
        response_200 = CatalogEntityRevisionResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = CatalogProblem.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = CatalogProblem.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = CatalogProblem.from_dict(response.json())



        return response_404

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
    entity_id: str,
    *,
    client: AuthenticatedClient,
    body: ReviseCatalogEntityRequest,

) -> Response[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    """ Revise Entity

    Args:
        entity_id (str):
        body (ReviseCatalogEntityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogEntityRevisionResponse, CatalogProblem]]
     """


    kwargs = _get_kwargs(
        entity_id=entity_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    entity_id: str,
    *,
    client: AuthenticatedClient,
    body: ReviseCatalogEntityRequest,

) -> Optional[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    """ Revise Entity

    Args:
        entity_id (str):
        body (ReviseCatalogEntityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogEntityRevisionResponse, CatalogProblem]
     """


    return sync_detailed(
        entity_id=entity_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    entity_id: str,
    *,
    client: AuthenticatedClient,
    body: ReviseCatalogEntityRequest,

) -> Response[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    """ Revise Entity

    Args:
        entity_id (str):
        body (ReviseCatalogEntityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogEntityRevisionResponse, CatalogProblem]]
     """


    kwargs = _get_kwargs(
        entity_id=entity_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    entity_id: str,
    *,
    client: AuthenticatedClient,
    body: ReviseCatalogEntityRequest,

) -> Optional[Union[CatalogEntityRevisionResponse, CatalogProblem]]:
    """ Revise Entity

    Args:
        entity_id (str):
        body (ReviseCatalogEntityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogEntityRevisionResponse, CatalogProblem]
     """


    return (await asyncio_detailed(
        entity_id=entity_id,
client=client,
body=body,

    )).parsed
