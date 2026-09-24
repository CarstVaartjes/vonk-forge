from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.cache_removal_review import CacheRemovalReview
from ...models.request_validation_problem import RequestValidationProblem
from typing import cast



def _get_kwargs(
    selector: str,
    *,
    with_model: bool,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    params["with_model"] = with_model


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/recipe/{selector}/remove-review".format(selector=selector,),
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]]:
    if response.status_code == 200:
        response_200 = CacheRemovalReview.from_dict(response.json())



        return response_200

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
        response_422 = RequestValidationProblem.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    selector: str,
    *,
    client: AuthenticatedClient,
    with_model: bool,

) -> Response[Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]]:
    """ Review Remove

    Args:
        selector (str):
        with_model (bool):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        selector=selector,
with_model=with_model,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    selector: str,
    *,
    client: AuthenticatedClient,
    with_model: bool,

) -> Optional[Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]]:
    """ Review Remove

    Args:
        selector (str):
        with_model (bool):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]
     """


    return sync_detailed(
        selector=selector,
client=client,
with_model=with_model,

    ).parsed

async def asyncio_detailed(
    selector: str,
    *,
    client: AuthenticatedClient,
    with_model: bool,

) -> Response[Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]]:
    """ Review Remove

    Args:
        selector (str):
        with_model (bool):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]]
     """


    kwargs = _get_kwargs(
        selector=selector,
with_model=with_model,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    selector: str,
    *,
    client: AuthenticatedClient,
    with_model: bool,

) -> Optional[Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]]:
    """ Review Remove

    Args:
        selector (str):
        with_model (bool):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, CacheRemovalReview, RequestValidationProblem]
     """


    return (await asyncio_detailed(
        selector=selector,
client=client,
with_model=with_model,

    )).parsed
