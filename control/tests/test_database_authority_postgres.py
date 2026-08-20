from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from vonk_control.database_authority import (
    AuthorityChange,
    DatabaseAuthorityService,
    DatabaseProposalService,
)
from vonk_control.models import (
    Base,
    ControlAuthorityHead,
    ControlAuthorityProposal,
    ControlAuthorityRevision,
)


def _postgres_unavailable(message: str) -> None:
    if os.getenv("CI"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        _postgres_unavailable("Docker is required for PostgreSQL authority tests")
    try:
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "-e",
                "POSTGRES_PASSWORD=postgres",
                "-p",
                "127.0.0.1::5432",
                "postgres:16",
            ],
            text=True,
            timeout=30,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        _postgres_unavailable(f"disposable PostgreSQL is unavailable: {error}")
    try:
        port = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                container,
            ],
            text=True,
            timeout=10,
        ).strip()
        engine = create_engine(
            f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres"
        )
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except (OSError, SQLAlchemyError):
                time.sleep(0.1)
        else:
            _postgres_unavailable("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(
            ["docker", "stop", container],
            check=False,
            capture_output=True,
            timeout=30,
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
        assert session.get(ControlAuthorityProposal, preview.proposal_id).applied_revision == revision
