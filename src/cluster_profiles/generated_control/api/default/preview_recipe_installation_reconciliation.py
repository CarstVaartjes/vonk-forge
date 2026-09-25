from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.request_validation_problem import RequestValidationProblem
from ...models.run_switch_cleanup_preview_request import RunSwitchCleanupPreviewRequest
from ...models.run_switch_plan import RunSwitchPlan
from typing import cast



def _get_kwargs(
    installation_id: str,
    *,
    body: RunSwitchCleanupPreviewRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/recipe/installations/{installation_id}/reconcile/preview".format(installation_id=installation_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]]:
    if response.status_code == 200:
        response_200 = RunSwitchPlan.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]]:
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
    body: RunSwitchCleanupPreviewRequest,

) -> Response[Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]]:
    """ Preview Installation Reconciliation

    Args:
        installation_id (str):
        body (RunSwitchCleanupPreviewRequest): Ask Run/Switch to remove one installation that is
            no longer desired.

            Cleanup is authorized by the installation's own uninstall assessment, so it
            never requires launch readiness: removing work must not depend on being able
            to start work.  Run/Switch still owns the sequencing, the child reference
            and the retry budget for the removal.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]]
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
    body: RunSwitchCleanupPreviewRequest,

) -> Optional[Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]]:
    """ Preview Installation Reconciliation

    Args:
        installation_id (str):
        body (RunSwitchCleanupPreviewRequest): Ask Run/Switch to remove one installation that is
            no longer desired.

            Cleanup is authorized by the installation's own uninstall assessment, so it
            never requires launch readiness: removing work must not depend on being able
            to start work.  Run/Switch still owns the sequencing, the child reference
            and the retry budget for the removal.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]
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
    body: RunSwitchCleanupPreviewRequest,

) -> Response[Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]]:
    """ Preview Installation Reconciliation

    Args:
        installation_id (str):
        body (RunSwitchCleanupPreviewRequest): Ask Run/Switch to remove one installation that is
            no longer desired.

            Cleanup is authorized by the installation's own uninstall assessment, so it
            never requires launch readiness: removing work must not depend on being able
            to start work.  Run/Switch still owns the sequencing, the child reference
            and the retry budget for the removal.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]]
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
    body: RunSwitchCleanupPreviewRequest,

) -> Optional[Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]]:
    """ Preview Installation Reconciliation

    Args:
        installation_id (str):
        body (RunSwitchCleanupPreviewRequest): Ask Run/Switch to remove one installation that is
            no longer desired.

            Cleanup is authorized by the installation's own uninstall assessment, so it
            never requires launch readiness: removing work must not depend on being able
            to start work.  Run/Switch still owns the sequencing, the child reference
            and the retry budget for the removal.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, RequestValidationProblem, RunSwitchPlan]
     """


    return (await asyncio_detailed(
        installation_id=installation_id,
client=client,
body=body,

    )).parsed
