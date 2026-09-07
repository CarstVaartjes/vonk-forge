import copy
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from vonk_control.auth import TokenCodec
from vonk_control.catalog_contract import catalog_content_sha256
from vonk_control.catalog_entities import CatalogEntityService
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


def test_builtin_harness_seed_revises_an_existing_identity_to_the_canonical_digest(
    session,
) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    seed_builtin_harnesses(session, now)
    session.commit()
    canonical = session.scalar(
        select(CatalogEntityRevision)
        .join(CatalogEntity)
        .where(
            CatalogEntity.kind == "execution-harness",
            CatalogEntity.publisher == "vonk-forge",
            CatalogEntity.slug == "vllm",
            CatalogEntityRevision.lifecycle == "resolved",
        )
    )
    assert canonical is not None
    canonical_document = copy.deepcopy(canonical.document)
    older_document = copy.deepcopy(canonical.document)
    older_document["metadata"]["title"] = "Older vLLM execution harness"
    service = CatalogEntityService(
        session,
        clock=lambda: now,
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
    )
    older_draft = service.revise(canonical.entity_id, older_document, actor="test")
    older_resolved = service.resolve(older_draft.id, actor="test")
    session.commit()

    updated = seed_builtin_harnesses(session, now)
    session.commit()

    canonical_revision = session.scalar(
        select(CatalogEntityRevision)
        .join(CatalogEntity)
        .where(
            CatalogEntity.kind == "execution-harness",
            CatalogEntity.publisher == "vonk-forge",
            CatalogEntity.slug == "vllm",
            CatalogEntityRevision.lifecycle == "resolved",
            CatalogEntityRevision.content_sha256
            == catalog_content_sha256(canonical_document),
        )
        .order_by(CatalogEntityRevision.revision_number.desc())
        .limit(1)
    )
    assert updated.created == 1
    assert updated.identifiers == ("vllm",)
    assert canonical_revision is not None
    assert canonical_revision.entity_id == canonical.entity_id
    assert canonical_revision.revision_number > older_resolved.revision_number
    assert session.scalar(
        select(CatalogEntity).where(
            CatalogEntity.kind == "execution-harness",
            CatalogEntity.publisher == "vonk-forge",
            CatalogEntity.slug == "vllm",
        )
    ).id == canonical.entity_id
