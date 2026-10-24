"""A Library page observes only the entries it can return.

Production regression (2026-10-24): ``GET /api/recipe/library --limit 512``
read local state, stored-image presence and assessment for all 354 matching
recipes, then the 1 MiB byte budget returned about 105 of them. The discarded
work pushed the page past the client's 15 second timeout. The per-entry
observations must scale with the returned page, not with the requested limit,
and the page itself must stay the largest prefix of observed entries that fits.

The byte budget is lowered to a fraction of the full listing so the page is
byte-capped well below the requested limit; the assertions count calls, never
wall time.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest
from vonk_control.auth import TokenCodec
from vonk_control.library_contract import (
    LibraryModelProjection,
    LibraryRecipeProjection,
    ModelLibraryResponse,
    RecipeLibraryResponse,
)
from vonk_control.library_projection import LibraryProjection, common
from vonk_control.strict_json import serialize_json_value

from .test_library_assessment import (  # noqa: F401 - the shared fixture
    assessed_library,
)
from .test_library_canonical_projection import _insert_canonical_rows
from .test_library_listing_budget import _seed_recipes_with_images
from .test_recipe_operations import NOW

_ENTRIES = 40


def _response_bytes(response: ModelLibraryResponse | RecipeLibraryResponse) -> int:
    return len(common._wire_json_bytes(serialize_json_value(response)))


class _Counted:
    """Record how many entries each per-entry observation was asked about."""

    def __init__(self) -> None:
        self.local_state: list[int] = []
        self.images: list[tuple[str, int]] = []
        self.assessed: list[int] = []

    def clear(self) -> None:
        self.local_state.clear()
        self.images.clear()
        self.assessed.clear()


def _counting_projection(fixture, counted: _Counted) -> LibraryProjection:
    original, sessions, *_, storage = fixture
    assessment = original._assessment
    assert assessment is not None

    def probe(archive: str, size: int) -> bool:
        counted.images.append((archive, size))
        return storage.build_archive_available(archive, size)

    def assess(recipes: Sequence[LibraryRecipeProjection], *, budget_seconds: float):
        counted.assessed.append(len(recipes))
        return assessment(recipes, budget_seconds=budget_seconds)

    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
        clock=lambda: NOW,
        runtime_archive_available=probe,
        assessment=assess,
    )
    database_local_state = projection._database_local_state

    def local_state(digests: Sequence[str]):
        counted.local_state.append(len(digests))
        return database_local_state(digests)

    projection._database_local_state = local_state  # type: ignore[method-assign]
    return projection


def _check_bounded_prefix[T: LibraryModelProjection | LibraryRecipeProjection](
    page: Sequence[T],
    full: Sequence[T],
    *,
    with_entries: Callable[[Sequence[T]], ModelLibraryResponse | RecipeLibraryResponse],
    budget: int,
) -> None:
    """The page is the largest prefix of the observed listing that fits."""

    assert 0 < len(page) < len(full)
    assert list(page) == list(full[: len(page)])
    # Every cursor in this listing has the same length, so one more entry
    # would have overflowed the budget whatever its boundary.
    assert _response_bytes(with_entries(full[: len(page) + 1])) > budget


@pytest.mark.parametrize("assess", [False, True])
def test_recipe_page_observes_only_the_entries_it_returns(
    assessed_library,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    assess: bool,
):
    _seed_recipes_with_images(assessed_library, recipes=_ENTRIES, history=0)
    counted = _Counted()
    projection = _counting_projection(assessed_library, counted)

    full = projection.recipe_library(limit=512, assess=assess)
    assert len(full.recipes) > _ENTRIES and full.next_cursor is None
    budget = _response_bytes(projection.recipe_library(limit=512, assess=True)) // 4
    monkeypatch.setattr(common, "MAX_CONTROL_DOCUMENT_BYTES", budget)
    plain = len(projection.recipe_library(limit=512).recipes)
    counted.clear()

    page = projection.recipe_library(limit=512, assess=assess)

    returned = len(page.recipes)
    _check_bounded_prefix(
        page.recipes,
        full.recipes,
        with_entries=lambda entries: page.model_copy(update={"recipes": list(entries)}),
        budget=budget,
    )
    assert page.next_cursor is not None
    if assess:
        # An assessment grows each entry, so the page shrinks after it; the
        # observations stay bounded by what the page could return before.
        (observed,) = counted.local_state
        assert counted.assessed == [observed]
        assert returned < observed <= plain + 2 < len(full.recipes)
    else:
        # One batch, sized by the page plus the one entry that settles the
        # exact boundary, not by the requested limit or the whole catalog.
        assert counted.local_state == [returned + 1]
        assert counted.assessed == []
    assert 0 < len(counted.images) <= counted.local_state[0]

    # The cursor walks the rest of the listing with nothing lost or repeated.
    walked = list(page.recipes)
    cursor = page.next_cursor
    while cursor is not None:
        following = projection.recipe_library(limit=512, assess=assess, cursor=cursor)
        walked.extend(following.recipes)
        cursor = following.next_cursor
    assert walked == full.recipes


def test_model_page_observes_only_the_entries_it_returns(
    assessed_library,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    original, sessions, *_ = assessed_library
    selector = original.models().models[0].selector
    _insert_canonical_rows(
        sessions,
        kind="model",
        template=original.model_detail(selector).document.model_dump(mode="json"),
        count=_ENTRIES,
    )
    counted = _Counted()
    projection = _counting_projection(assessed_library, counted)

    full = projection.models(limit=512)
    assert len(full.models) > _ENTRIES and full.next_cursor is None
    budget = _response_bytes(full) // 4
    monkeypatch.setattr(common, "MAX_CONTROL_DOCUMENT_BYTES", budget)
    counted.clear()

    page = projection.models(limit=512)

    returned = len(page.models)
    _check_bounded_prefix(
        page.models,
        full.models,
        with_entries=lambda entries: page.model_copy(update={"models": list(entries)}),
        budget=budget,
    )
    assert counted.local_state == [returned + 1]

    walked = list(page.models)
    cursor = page.next_cursor
    while cursor is not None:
        following = projection.models(limit=512, cursor=cursor)
        walked.extend(following.models)
        cursor = following.next_cursor
    assert walked == full.models
