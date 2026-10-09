from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.capability_unavailable_reply import CapabilityUnavailableReply
from ...models.enrollment_grant_status import EnrollmentGrantStatus
from ...models.enrollment_observation_outcome import EnrollmentObservationOutcome
from ...models.request_validation_problem import RequestValidationProblem
from typing import cast



def _get_kwargs(
    grant_id: str,

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/fleet/enrollments/{grant_id}/revoke".format(grant_id=quote(str(grant_id), safe=""),),
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem | None:
    if response.status_code == 200:
        def _parse_response_200(data: object) -> EnrollmentGrantStatus | EnrollmentObservationOutcome:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_200_type_0 = EnrollmentGrantStatus.from_dict(data)



                return response_200_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_200_type_1 = EnrollmentObservationOutcome.from_dict(data)



            return response_200_type_1

        response_200 = _parse_response_200(response.json())

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
        def _parse_response_503(data: object) -> BoundedErrorResponse | CapabilityUnavailableReply:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_503_type_0 = BoundedErrorResponse.from_dict(data)



                return response_503_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_503_type_1 = CapabilityUnavailableReply.from_dict(data)



            return response_503_type_1

        response_503 = _parse_response_503(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    grant_id: str,
    *,
    client: AuthenticatedClient,

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem]:
    """ Revoke Enrollment

    Args:
        grant_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        grant_id=grant_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    grant_id: str,
    *,
    client: AuthenticatedClient,

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem | None:
    """ Revoke Enrollment

    Args:
        grant_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem
     """


    return sync_detailed(
        grant_id=grant_id,
client=client,

    ).parsed

async def asyncio_detailed(
    grant_id: str,
    *,
    client: AuthenticatedClient,

) -> Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem]:
    """ Revoke Enrollment

    Args:
        grant_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        grant_id=grant_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    grant_id: str,
    *,
    client: AuthenticatedClient,

) -> BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem | None:
    """ Revoke Enrollment

    Args:
        grant_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | BoundedErrorResponse | CapabilityUnavailableReply | EnrollmentGrantStatus | EnrollmentObservationOutcome | RequestValidationProblem
     """


    return (await asyncio_detailed(
        grant_id=grant_id,
client=client,

    )).parsed
