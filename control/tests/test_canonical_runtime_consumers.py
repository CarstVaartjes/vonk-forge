"""Focused fail-closed checks for canonical runtime consumers."""

from __future__ import annotations

import json
from importlib.resources import files
from types import SimpleNamespace

import pytest

contracts = pytest.importorskip("vonk_forge_contracts")
from vonk_control.artifact_jobs import _active_recipe_revision
from vonk_control.fleet_projection import _canonical_recipe
from vonk_forge_contracts import content_sha256


def _document() -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text(encoding="utf-8")
    )


class _Session:
    def __init__(self, revision: object | None) -> None:
        self.revision = revision

    def get(self, _model: object, _revision_id: str) -> object | None:
        return self.revision


def _revision(*, state: str = "active", digest: str | None = None) -> object:
    document = _document()
    recipe = contracts.RecipeDefinition.model_validate(document)
    return SimpleNamespace(
        id="revision",
        kind="recipe",
        schema_version=2,
        state=state,
        document=document,
        content_digest=digest or content_sha256(recipe),
    )


def test_active_canonical_revision_is_consumed() -> None:
    revision = _revision()
    resolved = _active_recipe_revision(_Session(revision), "revision")
    assert resolved is not None
    assert resolved[0] is revision
    assert resolved[1].identity.slug == contracts.RecipeDefinition.model_validate(
        _document()
    ).identity.slug
    assert _canonical_recipe(revision) is not None


@pytest.mark.parametrize(
    "revision",
    [
        None,
        _revision(state="candidate"),
        _revision(digest="0" * 64),
    ],
)
def test_missing_or_stale_revision_fails_closed(revision: object | None) -> None:
    assert _active_recipe_revision(_Session(revision), "revision") is None
    assert revision is None or _canonical_recipe(revision) is None
