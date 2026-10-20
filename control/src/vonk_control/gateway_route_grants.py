"""Inference-only key grants from published accepted recipe interfaces."""

from enum import StrEnum

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import RouteState, RunState
from vonk_forge_contracts import read_recipe
from vonk_forge_contracts.recipe import RecipeDecisionSettings

from .models import CatalogDocumentRevision, RecipeInstallation, RecipeRun


class GatewayInferenceRoute(StrEnum):
    OPENAI = "openai_routes"
    SYSTEMONE = "/vllm/systemone"


def recipe_key_routes(
    sessions: sessionmaker[Session],
    models: list[str],
    *,
    include_run_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """Resolve native inference routes within the key's existing public model scope.

    Only accepted running interface intents grant native routes. The short read transaction
    ends before the caller contacts LiteLLM. Model ACLs still authorize each call.
    """
    routes = [GatewayInferenceRoute.OPENAI.value]
    statement = (
        select(RecipeRun.alias, CatalogDocumentRevision.document)
        .select_from(RecipeRun)
        .join(RecipeInstallation)
        .join(CatalogDocumentRevision)
        .where(
            RecipeRun.state == RunState.RUNNING,
            or_(
                RecipeRun.route_state.in_(
                    (RouteState.PENDING, RouteState.PUBLISHED, RouteState.FAILED)
                ),
                RecipeRun.id.in_(include_run_ids),
            ),
        )
    )
    if models:
        statement = statement.where(RecipeRun.alias.in_(models))
    with sessions() as session:
        documents = list(session.execute(statement))
    for _alias, document in documents:
        recipe = read_recipe(document)
        if isinstance(recipe.settings, RecipeDecisionSettings) and any(
            interface.adapter == "openai" for interface in recipe.interfaces
        ):
            routes.append(GatewayInferenceRoute.SYSTEMONE.value)
            break
    return routes
