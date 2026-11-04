"""The two stored reference facts a removal review asks by target.

* Which saved profiles name a recipe: ``fleet_profile_recipes``, derived from a
  profile's assignments in the flush that saves them.
* Which installations use a model set: ``recipe_installations.
  model_artifact_set_sha256``, written with the plan at install admission.

Rows written before these facts existed are filled from their own stored
documents at Controller startup (``fill_reference_facts``). Readers ask the
facts only; they never read assignments or plans to answer these questions.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from pydantic import TypeAdapter
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from vonk_agent_protocol import InstallationState
from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName

from .content_identity import catalog_selector_membership
from .lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from .models import (
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileRecipe,
    RecipeInstallation,
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


def recipes_without_revision(session: Session) -> frozenset[tuple[str, str]]:
    """The saved-profile recipes the catalog has no revision of at all.

    A profile naming one cannot be resolved, so it protects what it might use:
    the reader treats these as bound to every model.
    """
    return frozenset(
        (publisher, slug)
        for publisher, slug in session.execute(
            select(FleetProfileRecipe.publisher, FleetProfileRecipe.slug)
            .where(
                ~select(CatalogDocumentRevision.id)
                .where(
                    CatalogDocumentRevision.kind == RunSwitchReasonScopeName.RECIPE,
                    CatalogDocumentRevision.publisher == FleetProfileRecipe.publisher,
                    CatalogDocumentRevision.slug == FleetProfileRecipe.slug,
                )
                .exists()
            )
            .distinct()
        )
    )


def fill_reference_facts(connection: Connection) -> int:
    """Fill both facts for rows written before they existed; the rows filled.

    Idempotent, and run once at startup. An installation that is not
    uninstalled is filled from its own stored plan; a plan that does not name
    one set is reported once and left for its owner to replace. A profile with
    readable assignments has its projection rows made to match them (rows
    missing are added, rows it no longer names are removed); a profile whose
    assignments are damaged is reported and left as it is.
    """
    filled, _unreadable = fill_installation_model_sets(connection)
    return filled + _fill_profile_recipes(connection)


def plan_model_set(plan: object) -> str | None:
    """The one model set a stored installation plan runs; ``None`` when unreadable."""
    from .recipe_execution_contract import (
        RecipeExecutionContractError,
        parse_stored_installation_plan,
    )

    try:
        return parse_stored_installation_plan(
            plan, for_uninstall=True
        ).model_artifact_set_sha256
    except RecipeExecutionContractError:
        return None


def fill_installation_model_sets(
    connection: Connection,
) -> tuple[int, tuple[Residue, ...]]:
    """Record the model set of every installation that has none recorded yet.

    Returns how many were filled and the installations whose plan names no
    single set (reported once per process).
    """
    filled = 0
    unreadable: list[Residue] = []
    for installation_id, plan in connection.execute(
        select(RecipeInstallation.id, RecipeInstallation.plan).where(
            RecipeInstallation.model_artifact_set_sha256.is_(None),
            # An uninstalled installation holds no model: nothing reads it.
            RecipeInstallation.state != InstallationState.UNINSTALLED,
        )
    ).all():
        model_set = plan_model_set(plan)
        if model_set is None:
            unreadable.append(
                retire_as_unknown(
                    "recipe.installation-model-set",
                    installation_id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "the stored plan names no single model set",
                )
            )
            continue
        connection.execute(
            update(RecipeInstallation)
            .where(
                RecipeInstallation.id == installation_id,
                RecipeInstallation.model_artifact_set_sha256.is_(None),
            )
            .values(model_artifact_set_sha256=model_set)
        )
        filled += 1
    return filled, tuple(unreadable)


def _fill_profile_recipes(connection: Connection) -> int:
    present = {
        (profile_id, publisher, slug)
        for profile_id, publisher, slug in connection.execute(
            select(
                FleetProfileRecipe.profile_id,
                FleetProfileRecipe.publisher,
                FleetProfileRecipe.slug,
            )
        )
    }
    wanted: set[tuple[str, str, str]] = set()
    readable: set[str] = set()
    for profile_id, assignments in connection.execute(
        select(FleetProfile.id, FleetProfile.assignments)
    ):
        recipes = _readable_recipes(profile_id, assignments)
        if isinstance(recipes, Residue):
            continue
        readable.add(profile_id)
        wanted.update((profile_id, publisher, slug) for publisher, slug in recipes)
    missing = [
        {"profile_id": profile_id, "publisher": publisher, "slug": slug}
        for profile_id, publisher, slug in sorted(wanted - present)
    ]
    stale = sorted(row for row in present - wanted if row[0] in readable)
    for profile_id, publisher, slug in stale:
        connection.execute(
            delete(FleetProfileRecipe).where(
                FleetProfileRecipe.profile_id == profile_id,
                FleetProfileRecipe.publisher == publisher,
                FleetProfileRecipe.slug == slug,
            )
        )
    if missing:
        dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
        connection.execute(
            dialect.insert(FleetProfileRecipe).on_conflict_do_nothing(), missing
        )
    return len(missing) + len(stale)


def _readable_recipes(
    profile_id: str, assignments: object
) -> frozenset[tuple[str, str]] | Residue:
    """The recipes a stored profile names; damage is reported, never guessed."""
    try:
        return profile_recipes(assignments)
    except (TypeError, ValueError):
        return retire_as_unknown(
            "fleet-profile.recipes",
            profile_id,
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            "the saved assignments name no readable recipes",
        )
