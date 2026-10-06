"""Focused fail-closed checks for canonical runtime consumers."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from sqlalchemy.orm import Session

contracts = pytest.importorskip("vonk_forge_contracts")
from vonk_control.artifact_jobs import _active_recipe_revision
from vonk_control.fleet_projection import _canonical_recipe
from vonk_control.models import CatalogDocumentRevision
from vonk_forge_contracts import document_sha256


def _document() -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )


class _Session(Session):
    """A one-revision Session stand-in that never opens a database."""

    def __init__(self, revision: CatalogDocumentRevision | None) -> None:
        self.revision = revision

    def get(self, *args: object, **kwargs: object) -> object | None:
        return self.revision


def _revision(
    *, state: str = "active", digest: str | None = None
) -> CatalogDocumentRevision:
    document = _document()
    recipe = contracts.RecipeDefinition.model_validate(document)
    return CatalogDocumentRevision(
        id="revision",
        kind="recipe",
        schema_version=2,
        state=state,
        document=recipe.model_dump(mode="json"),
        content_digest=digest or document_sha256(recipe.model_dump(mode="json")),
    )


def test_active_canonical_revision_is_consumed() -> None:
    revision = _revision()
    resolved = _active_recipe_revision(_Session(revision), "revision")
    assert resolved is not None
    assert resolved[0] is revision
    assert (
        resolved[1].identity.slug
        == contracts.RecipeDefinition.model_validate(_document()).identity.slug
    )
    assert _canonical_recipe(revision) is not None


@pytest.mark.parametrize(
    "revision",
    [
        None,
        _revision(state="candidate"),
    ],
)
def test_missing_or_stale_revision_fails_closed(
    revision: CatalogDocumentRevision | None,
) -> None:
    assert _active_recipe_revision(_Session(revision), "revision") is None
    assert revision is None or _canonical_recipe(revision) is None
