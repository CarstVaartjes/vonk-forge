from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.agent_upgrade_apply_request import AgentUpgradeApplyRequest
from ...models.apply_agent_upgrade_response_apply_agent_upgrade_api_v1_agents_upgrades_post import ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost
from ...models.http_validation_error import HTTPValidationError
from typing import cast



def _get_kwargs(
    *,
    body: AgentUpgradeApplyRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/agents/upgrades",
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]]:
    if response.status_code == 202:
        response_202 = ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost.from_dict(response.json())



        return response_202

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: AgentUpgradeApplyRequest,

) -> Response[Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]]:
    """ Apply Agent Upgrade

    Args:
        body (AgentUpgradeApplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]]
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
    body: AgentUpgradeApplyRequest,

) -> Optional[Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]]:
    """ Apply Agent Upgrade

    Args:
        body (AgentUpgradeApplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]
     """


    return sync_detailed(
        client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: AgentUpgradeApplyRequest,

) -> Response[Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]]:
    """ Apply Agent Upgrade

    Args:
        body (AgentUpgradeApplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]]
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
    body: AgentUpgradeApplyRequest,

) -> Optional[Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]]:
    """ Apply Agent Upgrade

    Args:
        body (AgentUpgradeApplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost, HTTPValidationError]
     """


    return (await asyncio_detailed(
        client=client,
body=body,

    )).parsed
