"""Display of a fixture document, as the signed index would publish it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from typing import cast

from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_revision_contract import PublishedDocument
from vonk_control.models import CatalogDocumentRevision
from vonk_control.recipe_library_types import CatalogModelEntry
from vonk_forge_contracts import (
    ModelDisplay,
    RecipeDisplay,
    published_json,
    read_model_display,
    read_recipe_display,
)

_EXAMPLES = resources.files("vonk_forge_contracts") / "examples"


def _identity(document: Mapping[str, object]) -> Mapping[str, str]:
    identity = document["identity"]
    assert isinstance(identity, Mapping)
    return {"publisher": identity["publisher"], "slug": identity["slug"]}


def _shared_display(name: str, document: Mapping[str, object]) -> dict[str, object]:
    """The shared example display, naming this document."""
    shared = json.loads((_EXAMPLES / name).read_text(encoding="utf-8"))
    return {**shared, "identity": _identity(document)}


def display_of_recipe(document: Mapping[str, object]) -> RecipeDisplay:
    return read_recipe_display(
        _shared_display("recipe-display-source-build.json", document)
    )


def display_of_model(document: Mapping[str, object]) -> ModelDisplay:
    return read_model_display(_shared_display("model-display.json", document))


def display_of(document: Mapping[str, object]) -> RecipeDisplay | ModelDisplay:
    return (
        display_of_model(document)
        if document.get("kind") == "model"
        else display_of_recipe(document)
    )


def model_entry(document: Mapping[str, object]) -> CatalogModelEntry:
    return CatalogModelEntry(dict(document), display_of_model(document))


def publish_displays(index: dict) -> dict:
    """Give each entry of a fixture index the display a release carries."""
    for entry in index["catalog_entities"]:
        entry["display"] = published_json(display_of_model(entry["document"]))
    for entry in index["recipes"]:
        entry["display"] = published_json(display_of_recipe(entry["document"]))
    return index


def reword(document: dict, text: str) -> None:
    """Change a stored document's bytes through its free-text attribution.

    Display is not part of a document, so a test that needs "the same recipe,
    edited" edits the attribution, which stays in the document.
    """
    section = document["license" if document["kind"] == "model" else "provenance"]
    section["attribution"] = [text]


def reworded(definition, text: str):
    """The same, for a parsed Recipe or Model definition."""
    if definition.kind == "model":
        return definition.model_copy(
            update={
                "license": definition.license.model_copy(update={"attribution": [text]})
            }
        )
    return definition.model_copy(
        update={
            "provenance": definition.provenance.model_copy(
                update={"attribution": [text]}
            )
        }
    )


def create_draft(
    entities: CatalogEntityService, document: Mapping[str, object], *, actor: str
) -> CatalogDocumentRevision:
    """Draft a fixture document with the display its index entry would carry."""
    return entities.create_draft(
        cast(PublishedDocument, document), display_of(document), actor=actor
    )
