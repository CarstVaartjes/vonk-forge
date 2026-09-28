from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.artifact_job_capabilities_response import ArtifactJobCapabilitiesResponse
from ...models.bounded_error_response import BoundedErrorResponse
from ...models.request_validation_problem import RequestValidationProblem
from typing import cast



def _get_kwargs(

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/artifact-jobs/capabilities",
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem | None:
    if response.status_code == 200:
        response_200 = ArtifactJobCapabilitiesResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 422:
        response_422 = RequestValidationProblem.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,

) -> Response[ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem]:
    """ Capabilities

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,

) -> ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem | None:
    """ Capabilities

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem
     """


    return sync_detailed(
        client=client,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,

) -> Response[ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem]:
    """ Capabilities

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,

) -> ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem | None:
    """ Capabilities

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ArtifactJobCapabilitiesResponse | BoundedErrorResponse | RequestValidationProblem
     """


    return (await asyncio_detailed(
        client=client,

    )).parsed
