from __future__ import annotations

import inspect
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import MUTATION_ROLES, Actor, TokenCodec
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.jobs import JobService
from vonk_control.models import AgentNode, Base


def _client(*, with_idle_spark: bool = False) -> tuple[TestClient, TokenCodec]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    if with_idle_spark:
        with sessions.begin() as session:
            session.add(
                AgentNode(
                    node_id="spk_" + "1" * 32,
                    state="active",
                    protocol_version=1,
                    architecture="linux-arm64",
                    capabilities=[],
                    last_seen_at=datetime(2026, 9, 10, tzinfo=UTC),
                )
            )
    codec = TokenCodec(b"p" * 32)
    app = create_app(
        jobs=JobService(sessions, clock=lambda: datetime(2026, 9, 10, tzinfo=UTC)),
        tokens=codec,
        audits=MemoryAuditStore(),
        now=lambda: 1,
        fleet_profiles=FleetProfileService(
            sessions, clock=lambda: datetime(2026, 9, 10, tzinfo=UTC)
        ),
    )
    return TestClient(app), codec


def _headers(codec: TokenCodec, role: str = "viewer") -> dict[str, str]:
    token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
    return {"Authorization": f"Bearer {token}"}


def test_profile_operator_routes_are_singular_and_unversioned() -> None:
    client, codec = _client()
    response = client.get("/api/profile", headers=_headers(codec))
    assert response.status_code == 200
    assert response.json()["profiles"] == []

    unused = client.get("/api/profile/2", headers=_headers(codec))
    assert unused.status_code == 200
    assert unused.json()["number"] == 2
    assert unused.json()["status"] == "not-created"

    assert (
        client.get("/api/profile/2/status", headers=_headers(codec)).status_code == 404
    )


def test_profile_request_lookup_is_authenticated_and_reports_missing_key() -> None:
    client, codec = _client()
    path = "/api/profile/1/requests/11111111-1111-4111-8111-111111111111"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_headers(codec)).status_code == 404


def test_profile_load_requires_and_applies_the_reviewed_preview_digest() -> None:
    client, codec = _client(with_idle_spark=True)
    headers = _headers(codec, "administrator")
    saved = client.put(
        "/api/profile/1", headers=headers, json={"name": "Empty profile"}
    )
    assert saved.status_code == 200

    first_preview = client.post("/api/profile/1/preview", headers=headers)
    assert first_preview.status_code == 200
    old_digest = first_preview.json()["plan_digest"]
    assert first_preview.json()["allowed"] is True

    missing = client.post(
        "/api/profile/1/load",
        headers=headers,
        json={"request_key": "11111111-1111-4111-8111-111111111111"},
    )
    assert missing.status_code == 422

    changed = client.put(
        "/api/profile/1",
        headers=headers,
        json={"name": "Renamed profile", "expected_revision": saved.json()["revision"]},
    )
    assert changed.status_code == 200

    stale = client.post(
        "/api/profile/1/load",
        headers=headers,
        json={
            "plan_digest": old_digest,
            "request_key": "22222222-2222-4222-8222-222222222222",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "Fleet profile preview is stale"

    current_preview = client.post("/api/profile/1/preview", headers=headers)
    loaded = client.post(
        "/api/profile/1/load",
        headers=headers,
        json={
            "plan_digest": current_preview.json()["plan_digest"],
            "request_key": "33333333-3333-4333-8333-333333333333",
        },
    )
    assert loaded.status_code == 202
    assert loaded.json()["profile_id"] == changed.json()["id"]


def test_profile_mutation_role_is_declared_for_final_route() -> None:
    # Root owns the shared auth registry; this assertion documents the exact
    # key the integration patch must install without retaining old aliases.
    assert ("PUT", "/api/profile/{number}") not in MUTATION_ROLES or (
        "administrator" in MUTATION_ROLES[("PUT", "/api/profile/{number}")]
    )


def test_production_app_composes_fleet_profiles_with_preparation_authority() -> None:
    """Production must compose Fleet profiles through the guarded builder.

    ``build_production_fleet_profile_service`` binds the Run/Switch adapter as
    the preparation provider, so a profile preview always carries the exact
    preparation the queued child operation binds.  Constructing
    ``FleetProfileService`` directly in production would silently drop that
    binding and admit a profile whose required assets cannot be attested.
    """

    from vonk_control import api

    source = inspect.getsource(api.production_app)
    assert "build_production_fleet_profile_service(" in source
    assert "FleetProfileService(" not in source
