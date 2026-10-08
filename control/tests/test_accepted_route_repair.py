"""Damaged derived route bundles miss reuse and admit a fresh atomic generation."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from vonk_agent_protocol import GatewayRouteState
from vonk_control.litellm import LiteLlmPolicy, RouteState, render_config
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_routes import AtomicRecipeRoutePublisher
from vonk_control.recipe_routes.shared import _RecipeCandidate, _RecipeEndpoint
from vonk_control.route_bundle_contract import RouteBundleDocument
from vonk_control.route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AtomicRouteBundlePublisher,
    verify_active_route_bundle,
)


@pytest.mark.parametrize(
    "damage", ["generation", "ownership", "endpoint", "policy", "bytes"]
)
def test_accepted_projection_miss_is_repaired_without_unverified_reuse(
    tmp_path, damage
):
    """Catches a local projection refusal escaping instead of re-publishing authorized content."""
    run_id = str(uuid4())
    endpoint = _RecipeEndpoint(
        "spk_" + "a" * 32,
        "10.0.0.2",
        8000,
        datetime.now(UTC),
        f"recipe:{run_id}:rank:0",
    )
    state = RouteState({"model": endpoint.api_base}, "a" * 64)
    policy = LiteLlmPolicy(
        {
            "model": {
                "upstream_model": "model",
                "requests_per_minute": 10,
                "tokens_per_minute": 100,
            }
        }
    )
    candidate = _RecipeCandidate(
        state, frozenset({run_id}), policy, {"model": endpoint}
    )
    runtime = AtomicRouteBundlePublisher(tmp_path)
    publisher = AtomicRecipeRoutePublisher(runtime)
    address_policy = ManagementAddressPolicy.parse("10.0.0.0/24")
    route = endpoint.route_document()
    if damage == "endpoint":
        route = route.model_copy(update={"path": "/wrong"})
    routes = {"model": route}
    if damage == "ownership":
        routes["duplicate"] = route
    document = RouteBundleDocument(
        generation=99 if damage == "generation" else 1,
        routes=routes,
        schema_version=2,
        state=GatewayRouteState.PUBLISHED,
    )
    config = render_config(state, policy)
    if damage == "policy":
        config = config.replace(b"10.0.0.2:8000", b"10.0.0.2:8001")
    with runtime._locked() as uncertainty:
        assert uncertainty is None
        marker = runtime._activate(
            generation=1,
            state=GatewayRouteState.PUBLISHED,
            authority_id=RECIPE_ROUTE_AUTHORITY_ID,
            plan_digest=state.digest,
            evidence_set_digest=state.digest,
            routes=document.model_dump_json().encode(),
            litellm=config,
        )
    if damage == "bytes":
        (tmp_path / "generations" / marker.directory / "routes.json").write_bytes(
            b"broken"
        )
    assert publisher.accepted_run(run_id, address_policy) is None
    repaired = publisher.publish_recipe(candidate)
    assert repaired.generation > marker.generation
    assert publisher.accepted_run(run_id, address_policy) is not None
    verified = verify_active_route_bundle(tmp_path)
    assert verified.routes is not None
    assert verified.routes.routes["model"] == endpoint.route_document()
    # A healthy exact bundle is reused without allocating another generation.
    reused = publisher.publish_recipe(candidate)
    assert reused.generation == repaired.generation


def test_latest_explicit_route_intent_wins_over_older_alias_owner(tmp_path):
    """Catches a persisted alias collision blocking the next authorized publication."""
    from vonk_agent_protocol import RouteState as RunRouteState

    from .test_recipe_routes import (
        NOW,
        MutableClock,
        add_running_run,
        atomic_service,
        setup,
    )

    clock = MutableClock(NOW)
    base, _, _, first = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(first)
    second = add_running_run(
        base, first, alias="qwen", route_state=RunRouteState.PENDING, identity=3
    )
    service.publish_run(second)
    verified = verify_active_route_bundle(tmp_path / "live")
    assert verified.routes is not None
    assert list(verified.routes.routes) == ["qwen"]
    assert verified.routes.routes["qwen"].operation_id.startswith(f"recipe:{second}:")
    assert service._publisher.accepted_run(first, service._management_policy) is None
    assert (
        service._publisher.accepted_run(second, service._management_policy) is not None
    )
