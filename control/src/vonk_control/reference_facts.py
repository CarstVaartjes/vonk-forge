"""The stored reference facts a removal review (and the presence check) asks by target.

* Which saved profiles name a recipe: ``fleet_profile_recipes``, derived from a
  profile's assignments in the flush that saves them.
* Which installations use a model set: ``recipe_installations.
  model_artifact_set_sha256``, written with the plan at install admission.
* How many payload bytes a rank must have materialized:
  ``installation_nodes.required_payload_bytes``, written with the rank.
* Which source bundle a recipe revision was imported with:
  ``catalog_document_revisions.source_bundle_sha256``, written by the import.

Readers ask the facts only; they never read assignments or plans to answer these questions.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from pydantic import TypeAdapter
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from .content_identity import catalog_selector_membership
from .models import (
    FleetProfile,
    FleetProfileRecipe,
)


def profile_recipes(assignments: object) -> frozenset[tuple[str, str]]:
    """The (publisher, slug) of every recipe these saved assignments name.

    Assignments are validated where a profile is saved; a value that is not a
    list of assignments with a valid recipe selector is refused here too.
    """
    from .fleet_profile_contract.vocabulary import RecipeSelector

    if not isinstance(assignments, list):
        raise TypeError("saved profile assignments must be a list")
    selector = TypeAdapter(RecipeSelector)
    found: set[tuple[str, str]] = set()
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            raise TypeError("saved profile assignment must be an object")
        publisher, _, slug = selector.validate_python(
            assignment.get("recipe_selector"), strict=True
        ).partition("/")
        found.add((publisher, slug))
    return frozenset(found)


def write_profile_recipes(
    connection: Connection, profile_id: str, assignments: object
) -> None:
    """Replace one profile's projection with what its assignments name."""
    recipes = profile_recipes(assignments)
    connection.execute(
        delete(FleetProfileRecipe).where(FleetProfileRecipe.profile_id == profile_id)
    )
    if recipes:
        connection.execute(
            insert(FleetProfileRecipe),
            [
                {"profile_id": profile_id, "publisher": publisher, "slug": slug}
                for publisher, slug in sorted(recipes)
            ],
        )


def profiles_naming(
    session: Session,
    recipes: Collection[tuple[str, str]] | None = None,
    *,
    profile_id: str | None = None,
) -> dict[str, frozenset[tuple[str, str]]]:
    """Saved profile id -> the recipes it names, among ``recipes`` when given.

    ``profile_id`` restricts the answer to one profile.
    """
    if recipes is not None and not recipes:
        return {}
    statement = select(
        FleetProfileRecipe.profile_id,
        FleetProfileRecipe.publisher,
        FleetProfileRecipe.slug,
    ).join(FleetProfile, FleetProfile.id == FleetProfileRecipe.profile_id)
    if recipes is not None:
        statement = statement.where(
            catalog_selector_membership(
                FleetProfileRecipe.publisher,
                FleetProfileRecipe.slug,
                sorted(set(recipes)),
            )
        )
    if profile_id is not None:
        statement = statement.where(FleetProfileRecipe.profile_id == profile_id)
    found: dict[str, set[tuple[str, str]]] = {}
    for owner, publisher, slug in session.execute(statement):
        found.setdefault(owner, set()).add((publisher, slug))
    return {owner: frozenset(names) for owner, names in found.items()}
