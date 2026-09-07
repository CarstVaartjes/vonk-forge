from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.artifact_job_create import ArtifactJobCreate
from ...models.artifact_job_response import ArtifactJobResponse
from ...models.bounded_error_response import BoundedErrorResponse
from typing import cast



def _get_kwargs(
    run_id: str,
    *,
    body: ArtifactJobCreate,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/recipes/runs/{run_id}/artifact-jobs".format(run_id=run_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[ArtifactJobResponse, BoundedErrorResponse]]:
    if response.status_code == 201:
        response_201 = ArtifactJobResponse.from_dict(response.json())



        return response_201

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = BoundedErrorResponse.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = BoundedErrorResponse.from_dict(response.json())



        return response_404

    if response.status_code == 409:
        response_409 = BoundedErrorResponse.from_dict(response.json())



        return response_409

    if response.status_code == 422:
        response_422 = BoundedErrorResponse.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[ArtifactJobResponse, BoundedErrorResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    run_id: str,
    *,
    client: AuthenticatedClient,
    body: ArtifactJobCreate,

) -> Response[Union[ArtifactJobResponse, BoundedErrorResponse]]:
    """ Create Job

    Args:
        run_id (str):
        body (ArtifactJobCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[ArtifactJobResponse, BoundedErrorResponse]]
     """


    kwargs = _get_kwargs(
        run_id=run_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    run_id: str,
    *,
    client: AuthenticatedClient,
    body: ArtifactJobCreate,

) -> Optional[Union[ArtifactJobResponse, BoundedErrorResponse]]:
    """ Create Job

    Args:
        run_id (str):
        body (ArtifactJobCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[ArtifactJobResponse, BoundedErrorResponse]
     """


    return sync_detailed(
        run_id=run_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    run_id: str,
    *,
    client: AuthenticatedClient,
    body: ArtifactJobCreate,

) -> Response[Union[ArtifactJobResponse, BoundedErrorResponse]]:
    """ Create Job

    Args:
        run_id (str):
        body (ArtifactJobCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[ArtifactJobResponse, BoundedErrorResponse]]
     """


    kwargs = _get_kwargs(
        run_id=run_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    run_id: str,
    *,
    client: AuthenticatedClient,
    body: ArtifactJobCreate,

) -> Optional[Union[ArtifactJobResponse, BoundedErrorResponse]]:
    """ Create Job

    Args:
        run_id (str):
        body (ArtifactJobCreate):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[ArtifactJobResponse, BoundedErrorResponse]
     """


    return (await asyncio_detailed(
        run_id=run_id,
client=client,
body=body,

    )).parsed
