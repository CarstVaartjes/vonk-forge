"""Server-side evidence for the canonical Library ``sort`` and ``updated_since``.

The browser-side tests prove the web library *sends* these controls. These
tests drive ``LibraryProjection`` itself, so an implementation that accepts the
parameters and ignores them cannot pass.

The corpus is synthetic and small: three Model documents and three Recipe
documents whose projected ``updated_at`` is the active revision's ``created_at``,
advanced by one minute per insert. The name order (alpha, bravo, charlie) and
the recency order (bravo, alpha, charlie) therefore differ, which is exactly
what a constant or missing sort key would collapse.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import Base, CatalogDocument, CatalogDocumentRevision
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from tests.recipe_library_source import recipe_library_root

ROOT = recipe_library_root()

# Inserted oldest first, so the two orderings deliberately disagree.
_EPOCH = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_SLUGS_OLDEST_FIRST = ("charlie", "alpha", "bravo")
_NAME_ORDER = ("alpha", "bravo", "charlie")
_RECENT_FIRST = ("bravo", "alpha", "charlie")
# Strictly between the oldest and middle insertion, and exactly on the middle.
_AFTER_OLDEST = _EPOCH + timedelta(seconds=30)
_AT_MIDDLE = _EPOCH + timedelta(minutes=1)


def _selectors(slugs: tuple[str, ...]) -> list[str]:
    return [f"test/{slug}" for slug in slugs]


def _revision_times() -> dict[str, datetime]:
    return {
        slug: _EPOCH + timedelta(minutes=index)
        for index, slug in enumerate(_SLUGS_OLDEST_FIRST)
    }


def _insert_synthetic_corpus(
    sessions: sessionmaker,
    *,
    kind: str,
    template: dict[str, object],
) -> None:
    """Insert one active revision per slug, each one minute newer than the last."""

    times = _revision_times()
    rows: list[CatalogDocument | CatalogDocumentRevision] = []
    for slug in _SLUGS_OLDEST_FIRST:
        document = copy.deepcopy(template)
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity["publisher"] = "test"
        identity["slug"] = slug
        if kind == "model":
            family = identity["family"]
            assert isinstance(family, dict)
            family["publisher"] = "test"
            family["slug"] = "family"
            model = identity["model"]
            assert isinstance(model, dict)
            model["publisher"] = "test"
            model["slug"] = slug
        canonical = (
            ModelDefinition.model_validate(document)
            if kind == "model"
            else RecipeDefinition.model_validate(document)
        )
        title = (
            canonical.identity.model.title
            if isinstance(canonical, ModelDefinition)
            else canonical.metadata.title
        )
        clean = canonical.model_dump(mode="json")
        document_id = str(uuid.uuid4())
        stamp = times[slug]
        rows.append(
            CatalogDocument(
                id=document_id,
                kind=kind,
                publisher=canonical.identity.publisher,
                slug=canonical.identity.slug,
                title=title,
                created_by="test",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        rows.append(
            CatalogDocumentRevision(
                id=str(uuid.uuid4()),
                document_id=document_id,
                kind=kind,
                publisher=canonical.identity.publisher,
                slug=canonical.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=clean,
                content_digest=content_sha256(canonical),
                projected={},
                created_by="test",
                created_at=stamp,
            )
        )
    with sessions.begin() as session:
        session.add_all(rows)


@pytest.fixture(scope="module")
def projection(tmp_path_factory: pytest.TempPathFactory) -> LibraryProjection:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    engine = create_engine(
        f"sqlite:///{tmp_path_factory.mktemp('sort-recency') / 'library.sqlite'}"
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    _insert_synthetic_corpus(
        sessions, kind="model", template=index["catalog_entities"][0]["document"]
    )
    _insert_synthetic_corpus(
        sessions, kind="recipe", template=index["recipes"][0]["document"]
    )
    return LibraryProjection(
        sessions,
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        clock=lambda: _EPOCH + timedelta(days=1),
    )


def test_models_sort_name_and_updated_are_distinct_and_complete(
    projection: LibraryProjection,
) -> None:
    by_name = projection.models(limit=100, sort="name")
    by_updated = projection.models(limit=100, sort="updated")
    name_order = [item.selector for item in by_name.models]
    updated_order = [item.selector for item in by_updated.models]

    # A projection that ignores ``sort`` returns the same sequence twice, so
    # this difference is the assertion that pins the control. It is checked
    # before the exact orders so a constant sort key fails here first.
    assert name_order != updated_order
    assert name_order == _selectors(_NAME_ORDER)
    assert updated_order == _selectors(_RECENT_FIRST)
    # Sorting must not drop, duplicate or invent identities.
    assert set(name_order) == set(updated_order)
    assert len(name_order) == len(set(name_order)) == len(_SLUGS_OLDEST_FIRST)
    assert by_name.filters.sort == "name"
    assert by_updated.filters.sort == "updated"


def test_models_updated_since_excludes_older_rows_only(
    projection: LibraryProjection,
) -> None:
    everything = projection.models(limit=100, sort="updated")
    recent = projection.models(limit=100, sort="updated", updated_since=_AFTER_OLDEST)
    recent_selectors = [item.selector for item in recent.models]

    assert recent_selectors == _selectors(("bravo", "alpha"))
    assert "test/charlie" not in set(recent_selectors)
    # The boundary is inclusive: a revision exactly at the cutoff is kept.
    at_middle = projection.models(limit=100, sort="updated", updated_since=_AT_MIDDLE)
    assert [item.selector for item in at_middle.models] == _selectors(("bravo", "alpha"))
    # The surviving rows keep their relative order under either sort.
    recent_by_name = projection.models(limit=100, sort="name", updated_since=_AFTER_OLDEST)
    assert [item.selector for item in recent_by_name.models] == _selectors(("alpha", "bravo"))
    # Filtering a page must not change the facets, which describe the catalog.
    assert recent.facets == everything.facets
    assert recent.filters.updated_since == _AFTER_OLDEST.isoformat()


def test_models_cursor_is_bound_to_its_ordering(projection: LibraryProjection) -> None:
    updated_page = projection.models(limit=2, sort="updated")
    assert updated_page.next_cursor is not None
    # The issuing ordering still accepts the cursor.
    assert projection.models(
        limit=2, sort="updated", cursor=updated_page.next_cursor
    ).models

    name_page = projection.models(limit=2, sort="name")
    assert name_page.next_cursor is not None

    with pytest.raises(ValueError, match="cursor is invalid"):
        projection.models(limit=2, sort="name", cursor=updated_page.next_cursor)
    with pytest.raises(ValueError, match="cursor is invalid"):
        projection.models(limit=2, sort="updated", cursor=name_page.next_cursor)


def test_recipe_library_sort_recency_and_cursor(
    projection: LibraryProjection,
) -> None:
    by_name = projection.recipe_library(limit=100, all_models=True, sort="name")
    by_updated = projection.recipe_library(limit=100, all_models=True, sort="updated")
    name_order = [item.selector for item in by_name.recipes]
    updated_order = [item.selector for item in by_updated.recipes]

    assert name_order != updated_order
    assert name_order == _selectors(_NAME_ORDER)
    assert updated_order == _selectors(_RECENT_FIRST)
    assert set(name_order) == set(updated_order)

    recent = projection.recipe_library(
        limit=100, all_models=True, sort="updated", updated_since=_AFTER_OLDEST
    )
    assert [item.selector for item in recent.recipes] == _selectors(("bravo", "alpha"))
    assert recent.facets == by_updated.facets
    assert recent.filters.sort == "updated"

    page = projection.recipe_library(limit=2, all_models=True, sort="updated")
    assert page.next_cursor is not None
    with pytest.raises(ValueError, match="cursor is invalid"):
        projection.recipe_library(
            limit=2, all_models=True, sort="name", cursor=page.next_cursor
        )


def _reject_unknown_sort(method: Callable[..., object], message: str) -> None:
    """Call a projection entry point with a value outside its ``Literal`` type.

    ``sort`` is typed ``Literal["updated", "name"]``, so an unknown value can
    only arrive from a caller outside the type system. Widening the callable
    here exercises the projection's own runtime guard instead of the type hint.
    """

    with pytest.raises(ValueError, match=message):
        method(sort="sideways")


def test_unknown_sort_is_rejected_by_the_projection(
    projection: LibraryProjection,
) -> None:
    _reject_unknown_sort(projection.models, "model library sort is invalid")
    _reject_unknown_sort(projection.recipe_library, "recipe library sort is invalid")
