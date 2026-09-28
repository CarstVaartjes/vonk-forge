from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.fleet_log_response import FleetLogResponse
from ...models.get_fleet_log_info_source_type_0 import check_get_fleet_log_info_source_type_0
from ...models.get_fleet_log_info_source_type_0 import GetFleetLogInfoSourceType0
from ...models.request_validation_problem import RequestValidationProblem
from ...types import UNSET, Unset
from typing import cast
import datetime



def _get_kwargs(
    selector: str,
    *,
    since: datetime.datetime | None | Unset = UNSET,
    lines: int | Unset = 100,
    recipe: None | str | Unset = UNSET,
    source: GetFleetLogInfoSourceType0 | None | Unset = UNSET,
    follow: bool | Unset = False,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_since: None | str | Unset
    if isinstance(since, Unset):
        json_since = UNSET
    elif isinstance(since, datetime.datetime):
        json_since = since.isoformat()
    else:
        json_since = since
    params["since"] = json_since

    params["lines"] = lines

    json_recipe: None | str | Unset
    if isinstance(recipe, Unset):
        json_recipe = UNSET
    else:
        json_recipe = recipe
    params["recipe"] = json_recipe

    json_source: None | str | Unset
    if isinstance(source, Unset):
        json_source = UNSET
    elif isinstance(source, str):
        json_source = source
    else:
        json_source = source
    params["source"] = json_source

    params["follow"] = follow


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/fleet/{selector}/loginfo".format(selector=quote(str(selector), safe=""),),
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> BoundedErrorResponse | FleetLogResponse | RequestValidationProblem | None:
    if response.status_code == 200:
        response_200 = FleetLogResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 404:
        response_404 = BoundedErrorResponse.from_dict(response.json())



        return response_404

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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx2.Response) -> Response[BoundedErrorResponse | FleetLogResponse | RequestValidationProblem]:
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
    since: datetime.datetime | None | Unset = UNSET,
    lines: int | Unset = 100,
    recipe: None | str | Unset = UNSET,
    source: GetFleetLogInfoSourceType0 | None | Unset = UNSET,
    follow: bool | Unset = False,

) -> Response[BoundedErrorResponse | FleetLogResponse | RequestValidationProblem]:
    """ Fleet Loginfo

    Args:
        selector (str):
        since (datetime.datetime | None | Unset):
        lines (int | Unset):  Default: 100.
        recipe (None | str | Unset):
        source (GetFleetLogInfoSourceType0 | None | Unset):
        follow (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | FleetLogResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        selector=selector,
since=since,
lines=lines,
recipe=recipe,
source=source,
follow=follow,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    selector: str,
    *,
    client: AuthenticatedClient,
    since: datetime.datetime | None | Unset = UNSET,
    lines: int | Unset = 100,
    recipe: None | str | Unset = UNSET,
    source: GetFleetLogInfoSourceType0 | None | Unset = UNSET,
    follow: bool | Unset = False,

) -> BoundedErrorResponse | FleetLogResponse | RequestValidationProblem | None:
    """ Fleet Loginfo

    Args:
        selector (str):
        since (datetime.datetime | None | Unset):
        lines (int | Unset):  Default: 100.
        recipe (None | str | Unset):
        source (GetFleetLogInfoSourceType0 | None | Unset):
        follow (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | FleetLogResponse | RequestValidationProblem
     """


    return sync_detailed(
        selector=selector,
client=client,
since=since,
lines=lines,
recipe=recipe,
source=source,
follow=follow,

    ).parsed

async def asyncio_detailed(
    selector: str,
    *,
    client: AuthenticatedClient,
    since: datetime.datetime | None | Unset = UNSET,
    lines: int | Unset = 100,
    recipe: None | str | Unset = UNSET,
    source: GetFleetLogInfoSourceType0 | None | Unset = UNSET,
    follow: bool | Unset = False,

) -> Response[BoundedErrorResponse | FleetLogResponse | RequestValidationProblem]:
    """ Fleet Loginfo

    Args:
        selector (str):
        since (datetime.datetime | None | Unset):
        lines (int | Unset):  Default: 100.
        recipe (None | str | Unset):
        source (GetFleetLogInfoSourceType0 | None | Unset):
        follow (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BoundedErrorResponse | FleetLogResponse | RequestValidationProblem]
     """


    kwargs = _get_kwargs(
        selector=selector,
since=since,
lines=lines,
recipe=recipe,
source=source,
follow=follow,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    selector: str,
    *,
    client: AuthenticatedClient,
    since: datetime.datetime | None | Unset = UNSET,
    lines: int | Unset = 100,
    recipe: None | str | Unset = UNSET,
    source: GetFleetLogInfoSourceType0 | None | Unset = UNSET,
    follow: bool | Unset = False,

) -> BoundedErrorResponse | FleetLogResponse | RequestValidationProblem | None:
    """ Fleet Loginfo

    Args:
        selector (str):
        since (datetime.datetime | None | Unset):
        lines (int | Unset):  Default: 100.
        recipe (None | str | Unset):
        source (GetFleetLogInfoSourceType0 | None | Unset):
        follow (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx2.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BoundedErrorResponse | FleetLogResponse | RequestValidationProblem
     """


    return (await asyncio_detailed(
        selector=selector,
client=client,
since=since,
lines=lines,
recipe=recipe,
source=source,
follow=follow,

    )).parsed
