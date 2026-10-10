from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.capability_unavailable_reply import CapabilityUnavailableReply
from ...models.fleet_profile_application_cancel_request import FleetProfileApplicationCancelRequest
from ...models.fleet_profile_application_view import FleetProfileApplicationView
from ...models.http_transient import HttpTransient
from ...models.request_validation_problem import RequestValidationProblem
from typing import cast



def _get_kwargs(
    application_id: str,
    *,
    body: FleetProfileApplicationCancelRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/profile/applications/{application_id}/cancel".format(application_id=quote(str(application_id), safe=""),),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem | None:
    if response.status_code == 202:
        response_202 = FleetProfileApplicationView.from_dict(response.json())



        return response_202

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

    if response.status_code == 429:
        response_429 = HttpTransient.from_dict(response.json())



        return response_429

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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    application_id: str,
    *,
    client: AuthenticatedClient,
    body: FleetProfileApplicationCancelRequest,

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem]:
    """ Cancel Profile Application

    Args:
        application_id (str):
        body (FleetProfileApplicationCancelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        application_id=application_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    application_id: str,
    *,
    client: AuthenticatedClient,
    body: FleetProfileApplicationCancelRequest,

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem | None:
    """ Cancel Profile Application

    Args:
        application_id (str):
        body (FleetProfileApplicationCancelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem
     """


    return sync_detailed(
        application_id=application_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    application_id: str,
    *,
    client: AuthenticatedClient,
    body: FleetProfileApplicationCancelRequest,

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem]:
    """ Cancel Profile Application

    Args:
        application_id (str):
        body (FleetProfileApplicationCancelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        application_id=application_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    application_id: str,
    *,
    client: AuthenticatedClient,
    body: FleetProfileApplicationCancelRequest,

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem | None:
    """ Cancel Profile Application

    Args:
        application_id (str):
        body (FleetProfileApplicationCancelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | HttpTransient | FleetProfileApplicationView | HttpTransient | RequestValidationProblem
     """


    return (await asyncio_detailed(
        application_id=application_id,
client=client,
body=body,

    )).parsed
