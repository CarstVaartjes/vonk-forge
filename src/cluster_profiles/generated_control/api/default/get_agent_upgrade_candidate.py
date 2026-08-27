from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.get_agent_upgrade_candidate_response_current_agent_upgrade_api_v1_agents_upgrades_candidate_get import GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet
from typing import cast



def _get_kwargs(

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/agents/upgrades/candidate",
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet]:
    if response.status_code == 200:
        response_200 = GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet.from_dict(response.json())



        return response_200

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,

) -> Response[GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet]:
    """ Current Agent Upgrade

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet]
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

) -> Optional[GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet]:
    """ Current Agent Upgrade

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet
     """


    return sync_detailed(
        client=client,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,

) -> Response[GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet]:
    """ Current Agent Upgrade

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet]
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

) -> Optional[GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet]:
    """ Current Agent Upgrade

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet
     """


    return (await asyncio_detailed(
        client=client,

    )).parsed
