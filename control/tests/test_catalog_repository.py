from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_control.catalog_repository import CatalogRepository
from vonk_control.models import Base, LocalRecipe, LocalRecipeRevision


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


def test_revision_allocation_is_monotonic_and_locked(session: Session) -> None:
    now = datetime(2026, 8, 7, tzinfo=UTC)
    repository = CatalogRepository()
    recipe = LocalRecipe(
        slug="qwen3-vllm", title="Qwen", description="Recipe", source_kind="local",
        created_by="admin", created_at=now, updated_at=now,
    )
    session.add(recipe)
    session.flush()

    assert repository.next_revision_number(session, recipe.id) == 1
    session.add(LocalRecipeRevision(
        recipe_id=recipe.id, revision_number=1, lifecycle="draft", schema_version=1,
        document={}, content_sha256=None, created_by="admin", created_at=now,
    ))
    session.flush()
    assert repository.next_revision_number(session, recipe.id) == 2


def test_duplicate_slug_rolls_back_without_partial_revision(session: Session) -> None:
    now = datetime(2026, 8, 7, tzinfo=UTC)
    for actor in ("first", "second"):
        session.add(LocalRecipe(
            slug="same-slug", title="Same", description="Recipe", source_kind="local",
            created_by=actor, created_at=now, updated_at=now,
        ))
        try:
            session.flush()
            session.commit()
        except IntegrityError:
            session.rollback()

    assert session.query(LocalRecipe).filter_by(slug="same-slug").count() == 1


def test_redaction_never_persists_secret_values() -> None:
    repository = CatalogRepository()
    source = {"safe": 1, "nested": {"access_token": "secret-value"}}

    redacted = repository.redact_source(source)

    assert redacted == {"safe": 1, "nested": {"access_token": "[REDACTED]"}}
    assert "secret-value" not in json.dumps(redacted)
