from __future__ import annotations

import asyncio

import pytest
from vonk_control.auth import (
    AgentIdentity,
    AgentSource,
    AuthError,
    TrustedProxyAgentIdentityMiddleware,
    agent_identity_from_scope,
    agent_source_from_scope,
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
    # The raw mapping is deliberately the wrong type: the assertion is that the
    # frozen dataclass refuses an untyped identity at runtime. This one pyright
    # error is the point of the case, so it is recorded in the baseline rather
    # than silenced; the `# type: ignore` that used to sit above it was on the
    # wrong line and suppressed nothing.
    with pytest.raises(AuthError):
        AgentSource(
            identity={"node_id": NODE},
            management_address="10.0.0.42",
        )


@pytest.mark.parametrize("node,verified", (("not-a-node", True), (NODE, False)))
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
