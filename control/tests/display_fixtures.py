"""Display of a fixture document, as the signed index would publish it."""

from __future__ import annotations

import json
import zlib
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


def _shared_display(
    name: str, document: Mapping[str, object], text: Mapping[str, object]
) -> dict[str, object]:
    """The shared example display, naming this document, with its text overridden."""
    shared = json.loads((_EXAMPLES / name).read_text(encoding="utf-8"))
    return {**shared, "identity": _identity(document), **text}


def display_of_recipe(document: Mapping[str, object], **text: object) -> RecipeDisplay:
    return read_recipe_display(
        _shared_display("recipe-display-source-build.json", document, text)
    )


def display_of_model(document: Mapping[str, object], **text: object) -> ModelDisplay:
    return read_model_display(_shared_display("model-display.json", document, text))


def model_display_denying(
    document: Mapping[str, object], denied_jurisdictions: tuple[str, ...], notice: str
) -> dict[str, object]:
    """The stored display of a Model whose license names denied jurisdictions."""
    shared = display_of_model(document)
    license_document = shared.license.model_dump(mode="json", exclude_none=True)
    license_document["territorial_restrictions"] = {
        "denied_jurisdictions": list(denied_jurisdictions),
        "notice": notice,
    }
    return published_json(display_of_model(document, license=license_document))


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


def _next_patch(version: str, text: str) -> str:
    """A version that differs per ``text``, so two rewordings stay two documents."""
    return f"{version[:100]} r{zlib.crc32(text.encode()) % 1_000_000}"


def reword(document: dict, text: str) -> None:
    """Change a stored document's bytes without changing what it is.

    Display (and a Model's license) is not part of a document, so a test that
    needs "the same recipe, edited" edits the attribution, which stays in a
    recipe. A Model document holds no free text: its version moves
    instead, whatever ``text`` says (its files, the execution data, stay put).
    """
    if document["kind"] == "model":
        document["identity"]["version"] = _next_patch(
            document["identity"]["version"], text
        )
        return
    document["provenance"]["attribution"] = [text]


def reworded(definition, text: str):
    """The same, for a parsed Recipe or Model definition."""
    if definition.kind == "model":
        return definition.model_copy(
            update={
                "identity": definition.identity.model_copy(
                    update={"version": _next_patch(definition.identity.version, text)}
                )
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
