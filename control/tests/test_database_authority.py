from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vonk_control.database_authority import (
    AuthorityChange,
    DatabaseAuthorityService,
    DatabaseProposalService,
    StaleAuthorityRevision,
)
from vonk_control.models import (
    Base,
    ControlAuthorityHead,
    ControlAuthorityProposal,
    ControlAuthorityRevision,
)


@pytest.fixture
def authority():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            ControlAuthorityRevision.__table__,
            ControlAuthorityHead.__table__,
            ControlAuthorityProposal.__table__,
        ],
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = DatabaseAuthorityService(
        sessions, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    service.ensure_initialized()
    return service, DatabaseProposalService(service), sessions


def test_initial_authority_is_persisted(authority):
    service, _, sessions = authority
    revision = service.head()
    assert len(revision) == 64
    assert service.read_document(revision, "inventory/topology.json").parsed == {
        "schema_version": 1,
        "nodes": [],
        "links": [],
    }
    with sessions() as session:
        assert session.get(ControlAuthorityHead, 1).revision_id == revision


def test_preview_survives_service_restart_and_apply_is_compare_and_swap(authority):
    service, proposals, sessions = authority
    base = service.head()
    preview = proposals.preview(
        "admin",
        base,
        [AuthorityChange("inventory/topology.json", {"schema_version": 1, "nodes": ["first"], "links": []})],
    )
    restarted = DatabaseProposalService(service)
    persisted = restarted.apply(preview.digest)
    assert persisted.digest == preview.digest
    changed = service.apply(persisted)
    assert changed != base
    assert service.head() == changed
    with sessions() as session:
        assert session.get(ControlAuthorityProposal, preview.digest).applied_revision == changed


def test_stale_proposal_is_rejected(authority):
    service, proposals, _ = authority
    base = service.head()
    first = proposals.preview(
        "admin",
        base,
        [AuthorityChange("inventory/topology.json", {"schema_version": 1, "nodes": ["first"], "links": []})],
    )
    second = proposals.preview(
        "admin",
        base,
        [AuthorityChange("inventory/topology.json", {"schema_version": 1, "nodes": ["a"], "links": []})],
    )
    service.apply(first)
    with pytest.raises(StaleAuthorityRevision):
        service.apply(second)
