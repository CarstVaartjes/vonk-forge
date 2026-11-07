"""The route name of a live run: the one owner of changing it.

A run's execution identity is its installation, its Sparks and its generation.
The name clients route to (``RecipeRun.alias``) is routing metadata beside that
identity: it is set when the run is created and afterwards changes only here,
when a profile admission keeps an identical run under the name the newest
request chose.  The route publisher reads the column on every pass and
publishes one bundle that maps the run to its new name, so nothing is stopped,
restarted or re-admitted.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from vonk_forge_contracts import EndpointAlias

    from .models import RecipeRun

_LOGGER = logging.getLogger(__name__)


def owner_order(run: RecipeRun) -> tuple[datetime, str]:
    """The order in which runs own a name: the newest accepted intent (creation
    or rename) wins, the id breaks a tie. The route publisher applies the same
    order in ``recipe_routes.candidate``."""
    return (run.renamed_at or run.created_at, run.id)


def alias_owner(runs: Iterable[RecipeRun]) -> RecipeRun | None:
    """The run that owns the name among runs that carry it, or ``None``."""
    return max(runs, key=owner_order, default=None)


def runs_named(session: Session, alias: str) -> tuple[RecipeRun, ...]:
    """Every run, live or ended, whose route name is ``alias``."""
    from sqlalchemy import select

    from .models import RecipeRun

    return tuple(session.scalars(select(RecipeRun).where(RecipeRun.alias == alias)))


def carries_alias(run: RecipeRun, alias: str | None) -> bool:
    """Whether the run is currently routed under this name."""
    return alias is not None and run.alias == alias


def rename_run(
    session: Session, run: RecipeRun, alias: EndpointAlias, *, now: datetime
) -> bool:
    """Route the run under ``alias``; ``False`` when it already carries it.

    The newest accepted request for a name owns it, so the rename is stamped
    with its own time (``renamed_at``) beside the creation time.  The route
    state is untouched: the next publication pass finds the candidate digest
    changed and moves the name in one atomic bundle.
    """
    if run.alias == alias:
        return False
    previous = run.alias
    run.alias = alias
    run.renamed_at = now
    run.updated_at = now
    session.flush()
    _LOGGER.info(
        "recipe.route.renamed",
        extra={"run_id": run.id, "from_alias": previous, "to_alias": alias},
    )
    return True
