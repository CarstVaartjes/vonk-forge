from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_control.audit import AuditRecord, SqlAuditStore
from vonk_control.models import (
    AuditEvent,
    Base,
)
from vonk_control.operation_api import _stored_activation_marker

NOW = datetime(2026, 8, 7, tzinfo=UTC)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_sql_audit_store_rejects_malformed_persisted_targets(sessions) -> None:
    store = SqlAuditStore(sessions, clock=lambda: NOW)
    store.append(AuditRecord("req", "actor", "action", None, ("node-a",), NOW))
    with sessions.begin() as session:
        session.get(AuditEvent, session.query(AuditEvent).one().id).targets = "node-a"

    with pytest.raises(ValueError, match="audit targets are invalid"):
        store.list()


def test_route_publication_reader_rejects_malformed_activation_marker() -> None:
    marker = ActivationMarker(
        schema_version=2,
        generation=1,
        state="published",
        authority_id="12345678-1234-5678-1234-567812345678",
        plan_digest="a" * 64,
        evidence_set_digest="b" * 64,
        routes_sha256="c" * 64,
        litellm_sha256="d" * 64,
        issued_at="2026-08-07T00:00:00+00:00",
        expires_at="2026-08-07T00:05:00+00:00",
        directory="00000001-" + "e" * 64,
        manifest_sha256="f" * 64,
    )
    assert _stored_activation_marker(marker.model_dump()) == marker
    with pytest.raises(RuntimeError, match="durable activation marker is invalid"):
        _stored_activation_marker(123)
