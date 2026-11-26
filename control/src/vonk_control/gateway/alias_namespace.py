"""One namespace for the model names clients call: local runs and external models.

A name decides where a client's data goes, so two owners must never serve it.
Nothing locks: an owner reads who serves the name (:func:`alias_owner`) and
writes; a race is settled by the unique alias of the external row and by the
renderer, which leaves out an external entry whose alias a local route serves
(the local route wins until the external owner is refused again).

Local owners replace each other by design (the newest accepted request for a
name owns it), so a local producer is refused only by an external owner
(:func:`require_alias_not_external`).  An external model is refused by either
(:func:`claim_external_alias`).  An external row keeps its alias reserved until
its withdrawal is confirmed and the row is deleted.
"""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_agent_protocol.outcome import InvalidRequestError
from vonk_agent_protocol.state_machines import DesiredAssignmentState
from vonk_control.fleet_profile_contract.applications import (
    FleetProfileApplicationProgress,
)
from vonk_control.gateway.external_contract import ExternalEntryCode
from vonk_control.models.execution import ACTIVE_RUN_STATES, RecipeRun
from vonk_control.models.fleet import FleetProfileApplication, FleetProfileSelection
from vonk_control.models.gateway import GatewayExternalDeployment
from vonk_control.stored.rows import read_row_column


class AliasOwner(StrEnum):
    """Who serves a model name: a recipe run on the fleet or an external model."""

    LOCAL = "local"
    EXTERNAL = "external"


class AliasTaken(InvalidRequestError):
    """The name is served by another owner; nothing was repointed."""

    code = ExternalEntryCode.ALIAS_TAKEN

    def __init__(self, alias: str, owner: AliasOwner) -> None:
        super().__init__(
            f"Model name {alias} is already served by an {owner.value} owner.",
            reason=InvalidRequestReason.CONFLICT,
        )
        self.name = alias
        self.owner = owner


def alias_owner(session: Session, alias: str) -> AliasOwner | None:
    """Who serves ``alias`` now: an external row, an active run or the selected profile."""

    if alias in external_aliases(session, (alias,)):
        return AliasOwner.EXTERNAL
    if alias in _local_aliases(session, (alias,)):
        return AliasOwner.LOCAL
    return None


def require_alias_not_external(session: Session, aliases: Collection[str]) -> None:
    """Refuse a local producer whose name an external model owns."""

    for alias in sorted(external_aliases(session, aliases)):
        raise AliasTaken(alias, AliasOwner.EXTERNAL)


def claim_external_alias(session: Session, alias: str) -> None:
    """Refuse an external model whose name another owner serves."""

    owner = alias_owner(session, alias)
    if owner is not None:
        raise AliasTaken(alias, owner)


def external_aliases(session: Session, aliases: Collection[str]) -> set[str]:
    return set(
        session.scalars(
            select(GatewayExternalDeployment.alias).where(
                GatewayExternalDeployment.alias.in_(aliases)
            )
        )
    )


def _local_aliases(session: Session, aliases: Collection[str]) -> set[str]:
    wanted = set(aliases)
    served = set(
        session.scalars(
            select(RecipeRun.alias).where(
                RecipeRun.alias.in_(wanted),
                RecipeRun.state.in_(state.value for state in ACTIVE_RUN_STATES),
            )
        )
    )
    return served | (wanted & _selected_profile_aliases(session))


def _selected_profile_aliases(session: Session) -> set[str]:
    """The names the selected profile's accepted assignments run under."""

    selection = session.get(FleetProfileSelection, 1)
    if selection is None or selection.application_id is None:
        return set()
    application = session.get(FleetProfileApplication, selection.application_id)
    if application is None:
        return set()
    progress = read_row_column(application, "progress")
    if (
        not isinstance(progress, FleetProfileApplicationProgress)
        or progress.intended_profile is None
    ):
        return set()  # a damaged receipt is retired by the profile worker
    intended = progress.intended_profile
    return {
        assignment.alias
        for assignment in intended.assignments
        if assignment.alias is not None
        and assignment.desired_state == DesiredAssignmentState.RUNNING
    }
