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
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import CursorError, TokenCodec
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

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
    heads: list[CatalogDocumentHead] = []
    for slug in _SLUGS_OLDEST_FIRST:
        document = copy.deepcopy(template)
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity["publisher"] = "test"
        identity["slug"] = slug
        if kind == "model":
            model = identity["model"]
            assert isinstance(model, dict)
            model["publisher"] = "test"
            model["slug"] = slug
        canonical = (
            ModelDefinition.model_validate(document)
            if kind == "model"
            else RecipeDefinition.model_validate(document)
        )
        title = canonical.identity.slug
        clean = canonical.model_dump(mode="json")
        document_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
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
                id=revision_id,
                document_id=document_id,
                kind=kind,
                publisher=canonical.identity.publisher,
                slug=canonical.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=clean,
                content_digest=document_sha256(canonical.model_dump(mode="json")),
                projected={},
                created_by="test",
                created_at=stamp,
            )
        )
        heads.append(
            CatalogDocumentHead(
                kind=kind,
                publisher=canonical.identity.publisher,
                slug=canonical.identity.slug,
                active_revision_id=revision_id,
            )
        )
    with sessions.begin() as session:
        session.add_all(rows)
        session.flush()
        session.add_all(heads)


def _build(path: Path) -> tuple[LibraryProjection, sessionmaker]:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    engine = create_engine(f"sqlite:///{path / 'library.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    _insert_synthetic_corpus(
        sessions, kind="model", template=index["catalog_entities"][0]["document"]
    )
    _insert_synthetic_corpus(
        sessions, kind="recipe", template=index["recipes"][0]["document"]
    )
    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        clock=lambda: _EPOCH + timedelta(days=1),
    )
    return projection, sessions


@pytest.fixture(scope="module")
def projection(tmp_path_factory: pytest.TempPathFactory) -> LibraryProjection:
    return _build(tmp_path_factory.mktemp("sort-recency"))[0]


@pytest.mark.parametrize("kind", ["model", "recipe"])
@pytest.mark.parametrize(
    ("sort", "order"), [("name", _NAME_ORDER), ("updated", _RECENT_FIRST)]
)
def test_cursor_continues_after_a_boundary_that_left_the_catalog(
    tmp_path: Path, kind: str, sort: Literal["name", "updated"], order: tuple[str, ...]
) -> None:
    def page(projection: LibraryProjection, limit: int, cursor: str | None = None):
        if kind == "model":
            result = projection.models(limit=limit, sort=sort, cursor=cursor)
            return [item.selector for item in result.models], result.next_cursor
        result = projection.recipe_library(limit=limit, sort=sort, cursor=cursor)
        return [item.selector for item in result.recipes], result.next_cursor

    projection, sessions = _build(tmp_path)
    selectors, cursor = page(projection, 2)
    assert selectors == _selectors(order[:2]) and cursor
    with sessions.begin() as session:
        session.query(CatalogDocumentHead).filter_by(kind=kind, slug=order[1]).delete()
    # A fresh projection: the first one may hold the earlier catalog snapshot.
    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        clock=lambda: _EPOCH + timedelta(days=1),
    )
    assert page(projection, 100)[0] == _selectors((order[0], order[2]))
    # The boundary is gone, yet the listing continues after it, not at page 1.
    assert page(projection, 2, cursor)[0] == _selectors(order[2:])


@pytest.mark.parametrize("kind", ["model", "recipe"])
@pytest.mark.parametrize("sort", ["name", "updated"])
@pytest.mark.parametrize("boundary", [["old-shape"], "bare-string", [1, 2]])
def test_a_cursor_with_an_old_shape_is_an_invalid_cursor(
    tmp_path: Path, kind: str, sort: Literal["name", "updated"], boundary: object
) -> None:
    """A signed cursor whose boundary is not the current sort key is rejected.

    It used to restart the listing at page 1, which an older client read as the
    next page of the same listing.
    """

    class _OldShape(type(TokenCodec(b"s" * 32).cursor_codec())):
        def decode(self, token, *, resource, order, context):
            return boundary

    _projection, sessions = _build(tmp_path)
    projection = LibraryProjection(
        sessions,
        cursors=_OldShape(b"k" * 32),
        clock=lambda: _EPOCH + timedelta(days=1),
    )
    with pytest.raises(CursorError):
        if kind == "model":
            projection.models(limit=2, sort=sort, cursor="any")
        else:
            projection.recipe_library(limit=2, sort=sort, cursor="any")


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
    assert [item.selector for item in at_middle.models] == _selectors(
        ("bravo", "alpha")
    )
    # The surviving rows keep their relative order under either sort.
    recent_by_name = projection.models(
        limit=100, sort="name", updated_since=_AFTER_OLDEST
    )
    assert [item.selector for item in recent_by_name.models] == _selectors(
        ("alpha", "bravo")
    )
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

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        projection.models(limit=2, sort="name", cursor=updated_page.next_cursor)
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        projection.models(limit=2, sort="updated", cursor=name_page.next_cursor)


def test_recipe_library_sort_recency_and_cursor(
    projection: LibraryProjection,
) -> None:
    by_name = projection.recipe_library(limit=100, sort="name")
    by_updated = projection.recipe_library(limit=100, sort="updated")
    name_order = [item.selector for item in by_name.recipes]
    updated_order = [item.selector for item in by_updated.recipes]

    assert name_order != updated_order
    assert name_order == _selectors(_NAME_ORDER)
    assert updated_order == _selectors(_RECENT_FIRST)
    assert set(name_order) == set(updated_order)

    recent = projection.recipe_library(
        limit=100, sort="updated", updated_since=_AFTER_OLDEST
    )
    assert [item.selector for item in recent.recipes] == _selectors(("bravo", "alpha"))
    assert recent.facets == by_updated.facets
    assert recent.filters.sort == "updated"

    page = projection.recipe_library(limit=2, sort="updated")
    assert page.next_cursor is not None
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        projection.recipe_library(limit=2, sort="name", cursor=page.next_cursor)


def _reject_unknown_sort(method: Callable[..., object], message: str) -> None:
    """Call a projection entry point with a value outside its ``Literal`` type.

    ``sort`` is typed ``Literal["updated", "name"]``, so an unknown value can
    only arrive from a caller outside the type system. Widening the callable
    here exercises the projection's own runtime guard instead of the type hint.
    """

    with pytest.raises(ValueError):
        method(sort="sideways")


def test_unknown_sort_is_rejected_by_the_projection(
    projection: LibraryProjection,
) -> None:
    _reject_unknown_sort(projection.models, "model library sort is invalid")
    _reject_unknown_sort(projection.recipe_library, "recipe library sort is invalid")


def test_fits_fleet_sort_key_puts_working_recipes_first_then_newest():
    from types import SimpleNamespace
    from typing import Any

    from vonk_control.library_projection import LibraryProjection

    def item(name: str, minute: int, status: str) -> Any:
        return SimpleNamespace(
            updated_at=_EPOCH + timedelta(minutes=minute),
            selector=name,
            identity=SimpleNamespace(content_sha256=name),
            qualification=SimpleNamespace(status=status),
        )

    rows = [
        item("new", 3, "Untested"),
        item("old", 1, "Working"),
        item("mid", 2, "Working"),
    ]
    key: Any = LibraryProjection._recipe_sort_key("updated", working_first=True)
    assert [r.selector for r in sorted(rows, key=key, reverse=True)] == [
        "mid",
        "old",
        "new",
    ]
    plain: Any = LibraryProjection._recipe_sort_key("updated")
    assert [r.selector for r in sorted(rows, key=plain, reverse=True)] == [
        "new",
        "mid",
        "old",
    ]
    assert len(key(rows[0])) == 3
