from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.installation_reconcile_request import InstallationReconcileRequest
from ...models.request_validation_problem import RequestValidationProblem
from ...models.run_switch_operation import RunSwitchOperation
from typing import cast



def _get_kwargs(
    installation_id: str,
    *,
    body: InstallationReconcileRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/recipe/installations/{installation_id}/reconcile".format(installation_id=quote(str(installation_id), safe=""),),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation | None:
    if response.status_code == 202:
        response_202 = RunSwitchOperation.from_dict(response.json())



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

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    installation_id: str,
    *,
    client: AuthenticatedClient,
    body: InstallationReconcileRequest,

) -> Response[BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation]:
    """ Apply Installation Reconciliation

    Args:
        installation_id (str):
        body (InstallationReconcileRequest): Idempotency key for reconciling the installation
            named by the path.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation]
     """


    kwargs = _get_kwargs(
        installation_id=installation_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    installation_id: str,
    *,
    client: AuthenticatedClient,
    body: InstallationReconcileRequest,

) -> BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation | None:
    """ Apply Installation Reconciliation

    Args:
        installation_id (str):
        body (InstallationReconcileRequest): Idempotency key for reconciling the installation
            named by the path.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation
     """


    return sync_detailed(
        installation_id=installation_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    installation_id: str,
    *,
    client: AuthenticatedClient,
    body: InstallationReconcileRequest,

) -> Response[BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation]:
    """ Apply Installation Reconciliation

    Args:
        installation_id (str):
        body (InstallationReconcileRequest): Idempotency key for reconciling the installation
            named by the path.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation]
     """


    kwargs = _get_kwargs(
        installation_id=installation_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    installation_id: str,
    *,
    client: AuthenticatedClient,
    body: InstallationReconcileRequest,

) -> BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation | None:
    """ Apply Installation Reconciliation

    Args:
        installation_id (str):
        body (InstallationReconcileRequest): Idempotency key for reconciling the installation
            named by the path.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | RequestValidationProblem | RunSwitchOperation
     """


    return (await asyncio_detailed(
        installation_id=installation_id,
client=client,
body=body,

    )).parsed
