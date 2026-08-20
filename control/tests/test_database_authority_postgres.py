from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.database_authority import (
    AuthorityChange,
    DatabaseAuthorityService,
    DatabaseProposalService,
    StaleAuthorityRevision,
)
from vonk_control.db import initialize_database
from vonk_control.models import (
    Base,
    ControlAuthorityHead,
    ControlAuthorityProposal,
    ControlAuthorityRevision,
)


@pytest.fixture
def authority(postgres_engine: Engine):
    tables = [
        ControlAuthorityProposal.__table__,
        ControlAuthorityHead.__table__,
        ControlAuthorityRevision.__table__,
    ]
    Base.metadata.drop_all(postgres_engine, tables=tables)
    Base.metadata.create_all(postgres_engine, tables=list(reversed(tables)))
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    service = DatabaseAuthorityService(
        sessions, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    return service, DatabaseProposalService(service), sessions


def test_postgres_initialization_persists_revision_before_head(authority):
    service, _, sessions = authority

    revision = service.ensure_initialized()

    with sessions() as session:
        assert session.get(ControlAuthorityRevision, revision) is not None
        assert session.get(ControlAuthorityHead, 1).revision_id == revision


def test_postgres_initial_authority_document_is_readable(authority):
    service, _, _ = authority

    revision = service.ensure_initialized()

    assert service.read_document(revision, "inventory/topology.json").parsed == {
        "schema_version": 1,
        "nodes": [],
        "links": [],
    }


def test_postgres_apply_persists_revision_before_moving_head(authority):
    service, proposals, sessions = authority
    base = service.ensure_initialized()
    preview = proposals.preview(
        "admin",
        base,
        [
            AuthorityChange(
                "inventory/topology.json",
                {"schema_version": 1, "nodes": ["first"], "links": []},
            )
        ],
    )

    revision = service.apply(preview)

    assert revision != base
    with sessions() as session:
        assert session.get(ControlAuthorityRevision, revision) is not None
        assert session.get(ControlAuthorityHead, 1).revision_id == revision
        assert session.get(ControlAuthorityProposal, preview.digest).applied_revision == revision


def test_postgres_preview_survives_service_restart_and_apply_is_idempotent(authority):
    service, proposals, sessions = authority
    base = service.ensure_initialized()
    preview = proposals.preview(
        "admin",
        base,
        [
            AuthorityChange(
                "inventory/topology.json",
                {"schema_version": 1, "nodes": ["first"], "links": []},
            )
        ],
    )

    restarted = DatabaseProposalService(service)
    persisted = restarted.apply(preview.digest)
    changed = service.apply(persisted)

    assert service.apply(persisted) == changed
    assert service.head() == changed
    with sessions() as session:
        assert session.get(ControlAuthorityProposal, preview.digest).applied_revision == changed


def test_postgres_compare_and_swap_rejects_stale_proposal(authority):
    service, proposals, _ = authority
    base = service.ensure_initialized()
    first = proposals.preview(
        "admin",
        base,
        [
            AuthorityChange(
                "inventory/topology.json",
                {"schema_version": 1, "nodes": ["first"], "links": []},
            )
        ],
    )
    second = proposals.preview(
        "admin",
        base,
        [
            AuthorityChange(
                "inventory/topology.json",
                {"schema_version": 1, "nodes": ["second"], "links": []},
            )
        ],
    )

    service.apply(first)

    with pytest.raises(StaleAuthorityRevision):
        service.apply(second)


def test_concurrent_fresh_startup_migrates_once_and_creates_one_authority_head(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"

    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = list(
            pool.map(
                lambda _: initialize_database(
                    database_url,
                    config_path=config_path,
                ),
                range(2),
            )
        )

    assert revisions[0] == revisions[1]
    with postgres_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == "0001_fleet_library_baseline"
        assert connection.exec_driver_sql(
            "SELECT count(*) FROM control_authority_revisions"
        ).scalar_one() == 1
        assert connection.exec_driver_sql(
            "SELECT count(*) FROM control_authority_heads"
        ).scalar_one() == 1
        assert connection.exec_driver_sql(
            """
            SELECT count(*)
            FROM control_authority_heads AS head
            JOIN control_authority_revisions AS revision
              ON revision.revision_id = head.revision_id
            """
        ).scalar_one() == 1
