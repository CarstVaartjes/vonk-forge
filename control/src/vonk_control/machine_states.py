"""The stored states of installations, runs, routes and the other records, via the contract.

``vonk_agent_protocol.state_machines`` is the one definition of these words.  A
model that carries one validates through the ``*Field`` types below, so a row
written before a rename is adopted as the word it means now (the contract's
:data:`MACHINE_ALIASES` is the one table for that) and nothing here spells a word.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator
from vonk_agent_protocol import (
    CatalogSyncState,
    CertificateState,
    DistributionAssignmentState,
    EnrollmentGrantState,
    InstallationNodeState,
    InstallationState,
    ModelFileState,
    ReservationState,
    RoutePublicationState,
    RouteState,
    RunState,
    adopt_machine_state,
    machine_adopter,
)
from vonk_agent_protocol.wire_model import WireEnum

InstallationStateField = Annotated[
    InstallationState, BeforeValidator(machine_adopter(InstallationState))
]
InstallationNodeStateField = Annotated[
    InstallationNodeState, BeforeValidator(machine_adopter(InstallationNodeState))
]
DistributionAssignmentStateField = Annotated[
    DistributionAssignmentState,
    BeforeValidator(machine_adopter(DistributionAssignmentState)),
]
RunStateField = Annotated[RunState, BeforeValidator(machine_adopter(RunState))]
RouteStateField = Annotated[RouteState, BeforeValidator(machine_adopter(RouteState))]
RoutePublicationStateField = Annotated[
    RoutePublicationState, BeforeValidator(machine_adopter(RoutePublicationState))
]
CertificateStateField = Annotated[
    CertificateState, BeforeValidator(machine_adopter(CertificateState))
]
EnrollmentGrantStateField = Annotated[
    EnrollmentGrantState, BeforeValidator(machine_adopter(EnrollmentGrantState))
]
ModelFileStateField = Annotated[
    ModelFileState, BeforeValidator(machine_adopter(ModelFileState))
]
CatalogSyncStateField = Annotated[
    CatalogSyncState, BeforeValidator(machine_adopter(CatalogSyncState))
]
ReservationStateField = Annotated[
    ReservationState, BeforeValidator(machine_adopter(ReservationState))
]

#: An installation that holds (or is about to hold) files on a Spark.
INSTALLATION_ACTIVE: tuple[InstallationState, ...] = (
    InstallationState.PLANNED,
    InstallationState.INSTALLING,
    InstallationState.INSTALLED,
    InstallationState.PARTIAL,
)
#: A run that owns capacity: it has not stopped, failed or been lost.
RUN_LIVE: tuple[RunState, ...] = (
    RunState.PLANNED,
    RunState.STARTING,
    RunState.RUNNING,
    RunState.STOPPING,
)
#: Distribution assignments whose objects are still protected from collection: a
#: lapsed grant is renewed by the next attempt, a revoked one is not.
DISTRIBUTION_HELD: tuple[DistributionAssignmentState, ...] = (
    DistributionAssignmentState.ACTIVE,
    DistributionAssignmentState.EXPIRED,
)


def read_state[E: WireEnum](machine: type[E], stored: str) -> E:
    """The member a stored word means, adopting an old spelling; a foreign word is refused."""

    adopted = adopt_machine_state(machine, stored)
    if adopted is None:
        raise ValueError(f"unknown {machine.__name__} word: {stored!r}")
    return adopted
