from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.capability_unavailable_reply import CapabilityUnavailableReply
from ...models.gateway_key_create_request import GatewayKeyCreateRequest
from ...models.gateway_key_created import GatewayKeyCreated
from ...models.http_transient import HttpTransient
from ...models.request_validation_problem import RequestValidationProblem
from ...models.unknown_error import UnknownError
from typing import cast



def _get_kwargs(
    *,
    body: GatewayKeyCreateRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/key",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError | None:
    if response.status_code == 201:
        def _parse_response_201(data: object) -> GatewayKeyCreated | UnknownError:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_201_type_0 = GatewayKeyCreated.from_dict(data)



                return response_201_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_201_type_1 = UnknownError.from_dict(data)



            return response_201_type_1

        response_201 = _parse_response_201(response.json())

        return response_201

    if response.status_code == 202:
        response_202 = UnknownError.from_dict(response.json())



        return response_202

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = BoundedErrorResponse.from_dict(response.json())



        return response_403

    if response.status_code == 422:
        response_422 = RequestValidationProblem.from_dict(response.json())



        return response_422

    if response.status_code == 429:
        response_429 = HttpTransient.from_dict(response.json())



        return response_429

    if response.status_code == 502:
        response_502 = BoundedErrorResponse.from_dict(response.json())



        return response_502

    if response.status_code == 503:
        def _parse_response_503(data: object) -> BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_503_type_0 = BoundedErrorResponse.from_dict(data)



                return response_503_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_503_type_1 = CapabilityUnavailableReply.from_dict(data)



                return response_503_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_503_type_2 = HttpTransient.from_dict(data)



            return response_503_type_2

        response_503 = _parse_response_503(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: GatewayKeyCreateRequest,

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError]:
    """ Create Gateway Key

    Args:
        body (GatewayKeyCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError]
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
    body: GatewayKeyCreateRequest,

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError | None:
    """ Create Gateway Key

    Args:
        body (GatewayKeyCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError
     """


    return sync_detailed(
        client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: GatewayKeyCreateRequest,

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError]:
    """ Create Gateway Key

    Args:
        body (GatewayKeyCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError]
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
    body: GatewayKeyCreateRequest,

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError | None:
    """ Create Gateway Key

    Args:
        body (GatewayKeyCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | GatewayKeyCreated | UnknownError | HttpTransient | RequestValidationProblem | UnknownError
     """


    return (await asyncio_detailed(
        client=client,
body=body,

    )).parsed
