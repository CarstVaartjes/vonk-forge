import json
import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from vonk_control.auth import Actor, AuthError, has_capability, require_capability
from vonk_control.enrollment_service import (
    EnrollmentGrantError,
    EnrollmentGrantService,
    GrantRequest,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def service():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE enrollment_intents (
                intent_id TEXT PRIMARY KEY, node_id TEXT NOT NULL,
                state TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL,
                token_verifier TEXT NOT NULL, consumed_at DATETIME,
                controller_endpoint TEXT NOT NULL, enrollment_endpoint TEXT NOT NULL,
                ca_fingerprint TEXT NOT NULL, metadata JSON NOT NULL
            )
            """
        )
    clock = Clock()
    sessions = sessionmaker(engine, expire_on_commit=False)
    svc = EnrollmentGrantService(sessions, clock=clock, token_factory=lambda: b"x" * 32)
    return svc, sessions, clock


def request(actor="admin", node="spk_" + "a" * 32) -> GrantRequest:
    return GrantRequest(
        node,
        Actor(actor, "administrator" if actor == "admin" else actor),
        "https://controller",
        "https://enroll",
        "sha256:ca",
        60,
        {"source": "test"},
    )


def test_creation_returns_deterministic_response_and_exposes_token_once(service):
    svc, sessions, _ = service
    result = svc.create(request())

    assert uuid.UUID(result.intent_id)
    assert result.token == base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
    assert result.expires_at == datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    assert result.node_id == request().node_id
    assert result.controller_endpoint == "https://controller"
    assert result.enrollment_endpoint == "https://enroll"
    assert result.ca_fingerprint == "sha256:ca"
    row = sessions().execute(
        text("SELECT token_verifier, consumed_at, metadata FROM enrollment_intents")
    ).one()
    assert row.token_verifier != result.token
    assert row.consumed_at is None
    assert json.loads(row.metadata)["source"] == "test"


def test_valid_verification_consumes_and_replay_fails(service):
    svc, _, _ = service
    grant = svc.create(request())
    verified = svc.verify(grant.token, node_id=grant.node_id)
    assert verified.intent_id == grant.intent_id
    assert verified.node_id == grant.node_id
    assert verified.metadata == {"source": "test"}
    with pytest.raises(EnrollmentGrantError, match="verification failed"):
        svc.verify(grant.token, node_id=grant.node_id)


def test_token_mismatch_fails_closed(service):
    svc, _, _ = service
    grant = svc.create(request())
    with pytest.raises(EnrollmentGrantError, match="verification failed"):
        svc.verify(grant.token[:-1] + "A", node_id=grant.node_id)


def test_expiry_fails_closed(service):
    svc, _, clock = service
    grant = svc.create(request())
    clock.value += timedelta(seconds=60)
    with pytest.raises(EnrollmentGrantError, match="verification failed"):
        svc.verify(grant.token, node_id=grant.node_id)


def test_node_mismatch_fails_closed(service):
    svc, _, _ = service
    grant = svc.create(request())
    with pytest.raises(EnrollmentGrantError, match="verification failed"):
        svc.verify(grant.token, node_id="spk_" + "b" * 32)


def test_creation_requires_fleet_enroll_capability(service):
    svc, _, _ = service
    with pytest.raises(AuthError):
        svc.create(request(actor="viewer"))


@pytest.mark.parametrize("capability", ("fleet:enroll", "fleet:review"))
def test_missing_fleet_capabilities_are_denied(capability):
    viewer = Actor("viewer", "viewer")
    assert not has_capability(viewer, capability)
    with pytest.raises(AuthError):
        require_capability(viewer, capability)
