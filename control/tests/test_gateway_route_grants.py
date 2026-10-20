"""Published contract route grants, exercised against the real database."""

from vonk_agent_protocol import RouteState
from vonk_control.gateway_route_grants import GatewayInferenceRoute, recipe_key_routes

from .test_recipe_routes import _recipe_run, setup


def test_native_grants_follow_published_accepted_recipe_and_public_alias(
    tmp_path, postgres_engine
):
    service, _, _, run_id = setup(
        tmp_path,
        engine=postgres_engine,
        run_alias="public-d3",
        settings={
            "kind": "decision",
            "context_tokens": {"value": 1024, "change_effect": "restart"},
        },
    )
    sessions = service.sessions
    assert recipe_key_routes(sessions, []) == [GatewayInferenceRoute.OPENAI]
    with sessions.begin() as session:
        run = _recipe_run(session, run_id)
        run.route_state = RouteState.PENDING
    assert recipe_key_routes(sessions, ["public-d3"]) == [
        GatewayInferenceRoute.OPENAI,
        GatewayInferenceRoute.SYSTEMONE,
    ]
    with sessions.begin() as session:
        _recipe_run(session, run_id).route_state = RouteState.PUBLISHED
    assert recipe_key_routes(sessions, []) == [
        GatewayInferenceRoute.OPENAI,
        GatewayInferenceRoute.SYSTEMONE,
    ]
    assert recipe_key_routes(sessions, ["public-d3"]) == [
        GatewayInferenceRoute.OPENAI,
        GatewayInferenceRoute.SYSTEMONE,
    ]
    assert recipe_key_routes(sessions, ["qwen"]) == [GatewayInferenceRoute.OPENAI]
    with sessions.begin() as session:
        _recipe_run(session, run_id).route_state = RouteState.WITHDRAWN
    assert recipe_key_routes(sessions, []) == [GatewayInferenceRoute.OPENAI]


def test_route_publication_waits_for_key_grants_then_heals(tmp_path, postgres_engine):
    import httpx2
    import pytest
    from sqlalchemy import event
    from vonk_agent_protocol import UnknownOutcomeError
    from vonk_control.gateway_keys import GatewayKeyCreated, GatewayKeyService

    from .test_gateway_keys import MASTER, FakeLiteLlm

    upstream = FakeLiteLlm()
    unavailable = True
    transactions = set()

    @event.listens_for(postgres_engine, "begin")
    def begin(connection):
        transactions.add(connection)

    @event.listens_for(postgres_engine, "commit")
    @event.listens_for(postgres_engine, "rollback")
    def end(connection):
        transactions.discard(connection)

    def handle(request):
        assert not transactions, "key HTTP must not hold a SQL transaction"
        if unavailable and request.url.path == "/key/update":
            return httpx2.Response(503)
        return upstream.handle(request)

    def activated(_content):
        assert upstream.keys["sweep"]["allowed_routes"] == [
            GatewayInferenceRoute.OPENAI,
            GatewayInferenceRoute.SYSTEMONE,
        ]
        return True

    service, _, applied, run_id = setup(
        tmp_path,
        engine=postgres_engine,
        validate=activated,
        settings={
            "kind": "decision",
            "context_tokens": {"value": 1024, "change_effect": "restart"},
        },
    )
    gateway = GatewayKeyService(
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(handle),
        intent_root=tmp_path / "key-mutations",
        route_grants=lambda models, runs: recipe_key_routes(
            service.sessions, models, include_run_ids=runs
        ),
    )
    assert isinstance(gateway.create("sweep"), GatewayKeyCreated)
    original = upstream.keys["sweep"]["key"]
    service._prepare_key_routes = gateway.prepare_recipe_routes
    with pytest.raises(UnknownOutcomeError):
        service.publish_run(run_id)
    assert not applied
    unavailable = False
    service.publish_run(run_id)
    assert applied
    assert upstream.keys["sweep"]["key"] == original
