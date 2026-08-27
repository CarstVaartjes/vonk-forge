from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.agent_upgrade_preview_request import AgentUpgradePreviewRequest
from ...models.http_validation_error import HTTPValidationError
from ...models.preview_agent_upgrade_response_preview_agent_upgrade_api_v1_agents_upgrades_preview_post import PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost
from typing import cast



def _get_kwargs(
    *,
    body: AgentUpgradePreviewRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/agents/upgrades/preview",
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]]:
    if response.status_code == 200:
        response_200 = PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost.from_dict(response.json())



        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: AgentUpgradePreviewRequest,

) -> Response[Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]]:
    """ Preview Agent Upgrade

    Args:
        body (AgentUpgradePreviewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]]
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
    body: AgentUpgradePreviewRequest,

) -> Optional[Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]]:
    """ Preview Agent Upgrade

    Args:
        body (AgentUpgradePreviewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]
     """


    return sync_detailed(
        client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: AgentUpgradePreviewRequest,

) -> Response[Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]]:
    """ Preview Agent Upgrade

    Args:
        body (AgentUpgradePreviewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]]
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
    body: AgentUpgradePreviewRequest,

) -> Optional[Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]]:
    """ Preview Agent Upgrade

    Args:
        body (AgentUpgradePreviewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost]
     """


    return (await asyncio_detailed(
        client=client,
body=body,

    )).parsed
