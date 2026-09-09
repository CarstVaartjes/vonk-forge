from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_control.audit import AuditRecord, SqlAuditStore
from vonk_control.database_authority import (
    AuthorityPolicyError,
    DatabaseAuthorityService,
    DatabaseProposalService,
    ProposalChangeRequest,
)
from vonk_control.models import (
    AuditEvent,
    Base,
    ControlAuthorityProposal,
    ControlAuthorityRevision,
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


def test_authority_proposal_reader_rejects_malformed_json_fields(sessions) -> None:
    authority = DatabaseAuthorityService(sessions, clock=lambda: NOW)
    proposals = DatabaseProposalService(authority)
    base = authority.ensure_initialized(acquire_advisory_lock=False)
    preview = proposals.preview(
        "admin",
        base,
        [
            ProposalChangeRequest(
                path="docs/audits/authority.json", document={"status": "draft"}
            )
        ],
    )

    with sessions.begin() as session:
        row = session.get(ControlAuthorityProposal, preview.digest)
        row.affected_documents = "docs/audits/authority.json"
    with pytest.raises(AuthorityPolicyError, match="proposal affected documents"):
        proposals.apply(preview.digest)

    with sessions.begin() as session:
        session.get(ControlAuthorityProposal, preview.digest).affected_documents = [
            "docs/audits/authority.json"
        ]
        session.get(ControlAuthorityProposal, preview.digest).changes = [
            {"path": "docs/audits/authority.json"}
        ]
    with pytest.raises(AuthorityPolicyError, match="proposal changes"):
        proposals.apply(preview.digest)


def test_authority_reader_rejects_non_object_documents(sessions) -> None:
    authority = DatabaseAuthorityService(sessions, clock=lambda: NOW)
    base = authority.ensure_initialized(acquire_advisory_lock=False)
    with sessions.begin() as session:
        session.get(ControlAuthorityRevision, base).documents = {
            "docs/audits/authority.json": "scalar"
        }

    with pytest.raises(AuthorityPolicyError, match="authority documents are invalid"):
        authority.inspect(base)


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
