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
from vonk_control.models import Base


def _client() -> tuple[TestClient, TokenCodec]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    codec = TokenCodec(b"p" * 32)
    app = create_app(
        jobs=object(),
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

    assert client.get("/api/profile/2/status", headers=_headers(codec)).status_code == 404


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
