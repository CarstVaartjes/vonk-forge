"""A profile never takes a model name an external model serves."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy.orm import Session

from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_control.fleet_profiles.contracts import FleetProfileAliasTaken
from vonk_control.gateway.alias_namespace import external_aliases


def refuse_external_aliases(session: Session, aliases: Collection[str]) -> None:
    """Refuse a save or an apply naming a name an external model serves."""

    taken = sorted(external_aliases(session, aliases))
    if taken:
        raise FleetProfileAliasTaken(
            f"Model name {taken[0]} is served by an external model",
            reason=InvalidRequestReason.CONFLICT,
        )
