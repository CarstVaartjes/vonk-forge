from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from vonk_control.catalog_seeds import seed_builtin_harnesses
from vonk_control.harnesses import BUILTIN_HARNESS_SLUGS
from vonk_control.models import Base, CatalogEntity, CatalogEntityRevision


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


def test_builtin_harness_seed_creates_one_resolved_entity_per_builtin(session) -> None:
    first = seed_builtin_harnesses(session, datetime(2026, 8, 15, tzinfo=UTC))
    session.commit()

    assert first.created == len(BUILTIN_HARNESS_SLUGS)
    assert set(first.identifiers) == set(BUILTIN_HARNESS_SLUGS)
    assert set(
        session.scalars(
            select(CatalogEntity.slug).where(CatalogEntity.kind == "execution-harness")
        )
    ) == set(BUILTIN_HARNESS_SLUGS)
    assert (
        session.scalar(
            select(CatalogEntityRevision).where(
                CatalogEntityRevision.lifecycle == "resolved"
            )
        )
        is not None
    )


def test_builtin_harness_seed_is_idempotent(session) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    seed_builtin_harnesses(session, now)
    session.commit()

    second = seed_builtin_harnesses(session, now)
    session.commit()

    assert second.created == 0
    assert second.identifiers == ()
