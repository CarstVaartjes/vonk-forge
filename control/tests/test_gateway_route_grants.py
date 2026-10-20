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


def test_route_publication_waits_for_key_grants_then_heals(
    tmp_path, postgres_engine, monkeypatch
):
    import httpx2
    import pytest
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError
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
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = RouteState.PENDING

    import asyncio

    from vonk_control.api.production import _keep_recipe_routes
    from vonk_control.recipe_operation_worker import RecipeOperationWorker

    from .test_recipe_routes import NOW

    reconcile = gateway.reconcile_route_grants

    async def api_restart():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        observations = 0

        def observed():
            nonlocal observations
            observations += 1
            if observations == 1:
                raise SQLAlchemyError("injected database observation outage")
            result = reconcile()
            loop.call_soon_threadsafe(stop.set)
            return result

        monkeypatch.setattr(gateway, "reconcile_route_grants", observed)
        # New API scheduler, same durable intent. No worker has a gateway service.
        await asyncio.wait_for(
            _keep_recipe_routes(
                RecipeOperationWorker(service.sessions, service, clock=lambda: NOW),
                gateway,
                stop,
                interval_seconds=0.001,
            ),
            timeout=30,
        )
        assert observations == 2

    asyncio.run(api_restart())
    assert applied
    assert upstream.keys["sweep"]["key"] == original
    # Gateway drift after publication heals on an API tick after another restart.
    upstream.keys["sweep"]["allowed_routes"] = [GatewayInferenceRoute.OPENAI]
    asyncio.run(api_restart())
    assert upstream.keys["sweep"]["allowed_routes"] == [
        GatewayInferenceRoute.OPENAI,
        GatewayInferenceRoute.SYSTEMONE,
    ]
    assert upstream.keys["sweep"]["key"] == original


def test_worker_waits_for_api_publication_without_gateway_authority(tmp_path):
    import pytest
    from vonk_control.gateway_route_grants import require_existing_recipe_routes
    from vonk_control.recipe_operation_worker import RecipeOperationWorker
    from vonk_control.recipe_routes import RecipeRouteNotReady

    from .test_recipe_routes import NOW

    service, _, applied, run_id = setup(tmp_path)
    with service.sessions.begin() as session:
        _recipe_run(session, run_id).route_state = RouteState.PENDING
    prepare = lambda runs: require_existing_recipe_routes(service.sessions, runs)
    with pytest.raises(RecipeRouteNotReady):
        prepare(frozenset({run_id}))
    worker = RecipeOperationWorker(
        service.sessions, service, clock=lambda: NOW, reconcile_routes=False
    )
    worker.tick()
    worker.redrive_run(run_id)
    assert not applied
    # The API publishes; the worker can then retain it while withdrawing peers.
    service.publish_run(run_id)
    prepare(frozenset({run_id}))
    prepare(frozenset())
