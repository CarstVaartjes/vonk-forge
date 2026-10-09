"""Explicit enrollment revocation denies route effects, regardless of observations."""

from ..models import AgentNode
from ..recipe_routes.shared import RecipeEndpointAuthorityRefused


def require_route_authority(agent: AgentNode | None, run_id: str) -> None:
    if agent is not None and agent.revoked_at is not None:
        raise RecipeEndpointAuthorityRefused(
            "recipe rank node is revoked", run_id=run_id
        )
