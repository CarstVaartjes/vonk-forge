from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.get_artifact_job_result_response_getartifactjobresult import GetArtifactJobResultResponseGetartifactjobresult
from ...models.http_validation_error import HTTPValidationError
from typing import cast



def _get_kwargs(
    job_id: str,

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/artifact-jobs/{job_id}/result".format(job_id=job_id,),
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]]:
    if response.status_code == 200:
        response_200 = GetArtifactJobResultResponseGetartifactjobresult.from_dict(response.json())



        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]]:
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

) -> Response[Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]]:
    """ Result Metadata

    Args:
        job_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        job_id=job_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    job_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]]:
    """ Result Metadata

    Args:
        job_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]
     """


    return sync_detailed(
        job_id=job_id,
client=client,

    ).parsed

async def asyncio_detailed(
    job_id: str,
    *,
    client: AuthenticatedClient,

) -> Response[Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]]:
    """ Result Metadata

    Args:
        job_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        job_id=job_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    job_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]]:
    """ Result Metadata

    Args:
        job_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[GetArtifactJobResultResponseGetartifactjobresult, HTTPValidationError]
     """


    return (await asyncio_detailed(
        job_id=job_id,
client=client,

    )).parsed
