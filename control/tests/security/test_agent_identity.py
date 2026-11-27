from __future__ import annotations

import asyncio
import threading
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from tests.token_support import TokenCodec
from vonk_control.agent_api import common
from vonk_control.api import middleware
from vonk_control.api.application import create_app
from vonk_control.auth import (
    AgentIdentity,
    AgentSource,
    AuthError,
    TrustedProxyAgentIdentityMiddleware,
    agent_identity_from_scope,
    agent_source_from_scope,
    validated_agent_identity_from_scope,
)

NODE = "spk_" + "a" * 32


def test_agent_scope_identity_must_be_typed_and_verified() -> None:
    assert agent_identity_from_scope({"vonk.agent_identity": {"node_id": NODE}}) is None
    identity = AgentIdentity(NODE, "serial", "fingerprint", True)
    assert agent_identity_from_scope({"vonk.agent_identity": identity}) == identity


def test_agent_scope_source_must_be_typed_and_bound_to_identity() -> None:
    identity = AgentIdentity(NODE, "serial", "fingerprint", True)
    source = AgentSource(identity=identity, management_address="10.0.0.42")

    assert agent_source_from_scope({"vonk.agent_source": source}) is None
    assert (
        agent_source_from_scope(
            {"vonk.agent_identity": identity, "vonk.agent_source": source}
        )
        == source
    )
    assert agent_source_from_scope({"vonk.agent_source": "10.0.0.42"}) is None
    assert (
        agent_source_from_scope(
            {
                "vonk.agent_identity": identity,
                "vonk.agent_source": AgentSource(
                    identity=AgentIdentity(NODE, "other", "other", True),
                    management_address="10.0.0.42",
                ),
            }
        )
        is None
    )


@pytest.mark.parametrize(("node", "verified"), [("not-a-node", True), (NODE, False)])
def test_agent_identity_rejects_noncanonical_or_unverified_values(
    node: str, verified: bool
) -> None:
    with pytest.raises(AuthError):
        AgentIdentity(node, "serial", "fingerprint", verified)


def test_non_secret_caller_on_any_network_cannot_populate_agent_scope() -> None:
    received = []

    async def app(scope, receive, send) -> None:
        received.append(scope)

    middleware = TrustedProxyAgentIdentityMiddleware(app, trusted_proxy_auth=b"p" * 32)
    scope = {
        "type": "http",
        "path": "/ordinary",
        "client": ("arbitrary-network-peer", 443),
        "headers": (
            (b"x-vonk-agent-node", NODE.encode()),
            (b"x-vonk-agent-serial", b"123"),
            (b"x-vonk-agent-fingerprint", b"fingerprint"),
            (b"x-vonk-agent-verified", b"1"),
            (b"x-vonk-agent-proxy-auth", b"wrong-secret"),
            (b"x-vonk-agent-source", b"10.0.0.42"),
        ),
    }

    asyncio.run(middleware(scope, lambda: None, lambda _: None))

    assert agent_identity_from_scope(received[0]) is None
    assert agent_source_from_scope(received[0]) is None
    assert received[0]["headers"] == ()


def test_trusted_proxy_builds_one_typed_source_and_strips_forwarded_headers() -> None:
    received = []

    async def app(scope, receive, send) -> None:
        received.append(scope)

    middleware = TrustedProxyAgentIdentityMiddleware(
        app,
        trusted_proxy_auth=b"p" * 32,
    )
    scope = {
        "type": "http",
        "path": "/ordinary",
        "headers": (
            (b"x-vonk-agent-node", NODE.encode()),
            (b"x-vonk-agent-serial", b"123"),
            (b"x-vonk-agent-fingerprint", b"fingerprint"),
            (b"x-vonk-agent-verified", b"1"),
            (b"x-vonk-agent-proxy-auth", b"p" * 32),
            (b"x-vonk-agent-source", b"10.0.0.42"),
        ),
    }

    asyncio.run(middleware(scope, lambda: None, lambda _: None))

    identity = AgentIdentity(NODE, "123", "fingerprint", True)
    assert agent_identity_from_scope(received[0]) == identity
    assert agent_source_from_scope(received[0]) == AgentSource(
        identity=identity,
        management_address="10.0.0.42",
    )
    assert received[0]["headers"] == ()


def test_repaired_proxy_ingress_admits_effect_without_exposing_forwarded_secret() -> (
    None
):
    effects = []
    exposed = []

    async def app(scope, receive, send) -> None:
        exposed.extend(scope["headers"])
        identity = agent_identity_from_scope(scope)
        if identity is not None:
            effects.append(identity.node_id)

    middleware = TrustedProxyAgentIdentityMiddleware(app, trusted_proxy_auth=b"p" * 32)

    def scope(secret: bytes):
        return {
            "type": "http",
            "path": "/ordinary",
            "headers": (
                (b"x-vonk-agent-node", NODE.encode()),
                (b"x-vonk-agent-serial", b"123"),
                (b"x-vonk-agent-fingerprint", b"fingerprint"),
                (b"x-vonk-agent-source", b"10.0.0.42"),
                (b"x-vonk-agent-verified", b"1"),
                (b"x-vonk-agent-proxy-auth", secret),
            ),
        }

    asyncio.run(middleware(scope(b"forged-secret"), lambda: None, lambda _: None))
    assert not effects
    assert not exposed
    asyncio.run(middleware(scope(b"p" * 32), lambda: None, lambda _: None))
    assert effects == [NODE]
    assert not exposed


def test_slow_identity_database_validation_does_not_park_other_requests() -> None:
    """A pooled-connection wait in agent auth must not block the ASGI loop."""
    entered, release = threading.Event(), threading.Event()
    received = []

    def validate(identity):
        entered.set()
        assert release.wait(10), "injected database wait was not released"
        return False  # Offloading must still refuse revoked authority.

    async def app(scope, receive, send):
        received.append(scope["path"])

    middleware = TrustedProxyAgentIdentityMiddleware(
        app, trusted_proxy_auth=b"p" * 32, agent_identity_validator=validate
    )
    scope = {
        "type": "http",
        "path": "/agent/telemetry",
        "headers": (
            (b"x-vonk-agent-node", NODE.encode()),
            (b"x-vonk-agent-serial", b"123"),
            (b"x-vonk-agent-fingerprint", b"fingerprint"),
            (b"x-vonk-agent-verified", b"1"),
            (b"x-vonk-agent-proxy-auth", b"p" * 32),
            (b"x-vonk-agent-source", b"10.0.0.42"),
        ),
    }
    replies = []

    async def send(message):
        replies.append(message)

    async def exercise():
        task = asyncio.create_task(middleware(scope, lambda: None, send))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            assert not task.done(), "database validation blocked the event loop"
            await asyncio.wait_for(
                middleware(
                    {"type": "http", "path": "/api/platform", "headers": ()},
                    lambda: None,
                    send,
                ),
                10,
            )
            assert received == ["/api/platform"]
        finally:
            release.set()
            await task

    asyncio.run(exercise())
    assert replies[0]["status"] == 401


def test_agent_identity_is_validated_once_and_handlers_reuse_it() -> None:
    lookups, seen = [], []

    def validate(identity):
        lookups.append(identity.node_id)
        return True

    async def app(scope, receive, send):
        seen.append(validated_agent_identity_from_scope(scope))

    middleware = TrustedProxyAgentIdentityMiddleware(
        app, trusted_proxy_auth=b"p" * 32, agent_identity_validator=validate
    )
    headers = (
        (b"x-vonk-agent-node", NODE.encode()),
        (b"x-vonk-agent-serial", b"123"),
        (b"x-vonk-agent-fingerprint", b"fingerprint"),
        (b"x-vonk-agent-verified", b"1"),
        (b"x-vonk-agent-proxy-auth", b"p" * 32),
        (b"x-vonk-agent-source", b"10.0.0.42"),
    )
    scope = {"type": "http", "path": "/agent/telemetry", "headers": headers}
    asyncio.run(middleware(scope, lambda: None, lambda _: None))
    assert lookups == [NODE]
    assert seen[0] is not None
    assert seen[0].node_id == NODE

    # A request that skipped the middleware is unvalidated.
    identity = AgentIdentity(NODE, "123", "fingerprint", True)
    assert (
        validated_agent_identity_from_scope({"vonk.agent_identity": identity}) is None
    )

    # A forged marker on a path the middleware does not validate is stripped.
    forged = {
        "type": "http",
        "path": "/agent/enroll",
        "headers": headers,
        "vonk.agent_validated": identity,
    }
    asyncio.run(middleware(forged, lambda: None, lambda _: None))
    assert seen[1] is None
    assert lookups == [NODE]


def test_agent_handler_reuses_the_middleware_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    lookups: list[object] = []

    def count(*_: object) -> bool:
        lookups.append(1)
        return True

    monkeypatch.setattr(middleware, "active_agent_identity", count)
    monkeypatch.setattr(common, "active_agent_identity", count)
    app = create_app(
        jobs=cast(Any, object()),
        tokens=TokenCodec(b"t" * 32),
        agent=cast(common.AgentApiServices, object()),
        trusted_agent_proxy_auth=b"test",
    )
    headers = {
        "x-vonk-agent-proxy-auth": "test",
        "x-vonk-agent-node": NODE,
        "x-vonk-agent-serial": "serial",
        "x-vonk-agent-fingerprint": "fingerprint",
        "x-vonk-agent-verified": "1",
        "x-vonk-agent-source": "192.0.2.1",
    }
    path = "/agent/artifacts/" + "a" * 64
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get(path, headers=headers)
        assert len(lookups) == 1
        # Without the proxy identity the middleware refuses before any handler.
        assert client.get(path).status_code == 401
        assert len(lookups) == 1
