"""The closed state vocabularies of the stored records that are not lifecycle subjects.

A lifecycle subject (a job, an operation, an artifact job, a fleet profile
application) speaks :class:`LifecycleState`.  The records below are not work in
flight but the *condition of a thing*: an installation on a Spark, an artifact
distribution assignment, a run and the route that publishes it, a certificate, an
enrollment grant, a node's model file, a catalog sync, a resource reservation.
Each has its own closed set of words.  This module is the one definition of them;
``scripts/export-agent-wire-schema`` publishes them through
:class:`~vonk_agent_protocol.lifecycle_vocabulary.LifecycleVocabulary` into
``wire.json``, and the generators turn that into Rust, OpenAPI and TypeScript.
The Controller's CHECK constraints, its models and its readers use the enums, and
the vocabulary ratchet allows no other spelling.

A reader of an old stored row goes through :func:`adopt_machine_state`: a word is
matched on its normalized spelling (case, surrounding space, ``_`` for ``-``), and
:data:`MACHINE_ALIASES` is the one table where a retired word of a machine would be
mapped to its current member.  A word no member and no alias explains is ``None``
and is judged by its caller.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .wire_model import WireEnum


class InstallationState(WireEnum):
    """The condition of a recipe installation across its Sparks."""

    PLANNED = "planned"
    INSTALLING = "installing"
    INSTALLED = "installed"
    PARTIAL = "partial"
    FAILED = "failed"
    UNINSTALLED = "uninstalled"


class InstallationNodeState(WireEnum):
    """The condition of one rank of an installation on its Spark."""

    PLANNED = "planned"
    INSTALLED = "installed"
    FAILED = "failed"
    UNINSTALLED = "uninstalled"


class DistributionAssignmentState(WireEnum):
    """Whether a node may still fetch the artifacts of a distribution assignment."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class RunState(WireEnum):
    """The condition of a recipe run (and of each of its ranks)."""

    PLANNED = "planned"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    LOST = "lost"


class RouteState(WireEnum):
    """Whether a run's inference route is published to the gateway."""

    WITHDRAWN = "withdrawn"
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class RoutePublicationState(WireEnum):
    """The phases of one atomic route publication."""

    WITHDRAWAL_PENDING = "withdrawal-pending"
    ROUTES_WITHDRAWN = "routes-withdrawn"
    PUBLICATION_PENDING = "publication-pending"
    COMPLETED = "completed"
    FAILED = "failed"


class CertificateState(WireEnum):
    """The standing of a node's client certificate, as the fleet projection shows it."""

    VALID = "valid"
    MISSING = "missing"
    NOT_YET_VALID = "not-yet-valid"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INACTIVE = "inactive"


class EnrollmentGrantState(WireEnum):
    """The standing of an enrollment grant."""

    PENDING = "pending"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    REVOKED = "revoked"


class ModelFileState(WireEnum):
    """The condition of one model file a node holds."""

    PARTIAL = "partial"
    VERIFIED = "verified"
    MISSING = "missing"
    CORRUPT = "corrupt"


class CatalogSyncState(WireEnum):
    """The outcome of a catalog synchronization, as the catalog shows it."""

    SYNCING = "syncing"
    CURRENT = "current"
    PARTIAL = "partial"
    FAILED = "failed"


class ReservationState(WireEnum):
    """The standing of a resource reservation."""

    ACTIVE = "active"
    PROMISED = "promised"
    RELEASED = "released"
    EXPIRED = "expired"


class GatewayRouteState(WireEnum):
    """What the inference gateway currently serves: published routes, or maintenance.

    ``unavailable`` is the Controller's own word for a gateway whose marker it
    cannot read; the gateway never writes it.
    """

    PUBLISHED = "published"
    MAINTENANCE = "maintenance"
    UNAVAILABLE = "unavailable"


class DesiredAssignmentState(WireEnum):
    """What a fleet-profile assignment is asked to become on its Sparks."""

    INSTALLED = "installed"
    RUNNING = "running"


class EndpointState(WireEnum):
    """Whether the endpoint of a fleet-profile assignment can be reached."""

    INSTALLED_ONLY = "installed-only"
    NOT_PUBLISHED_YET = "not-published-yet"
    PUBLISHED = "published"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"
    UNAVAILABLE = "unavailable"


class ObservedAssignmentState(WireEnum):
    """Where a fleet-profile assignment stands on the Sparks, as observed."""

    NOT_PLACED = "not-placed"
    PLACED = "placed"
    INSTALLING = "installing"
    INSTALLED = "installed"
    RUNNING = "running"
    DEGRADED = "degraded"


class AssetAvailability(WireEnum):
    """What is known about a model asset on a Spark's disk."""

    VERIFIED = "verified"
    PARTIAL = "partial"
    MISSING = "missing"
    UNKNOWN = "unknown"


class PlacementInstallState(WireEnum):
    """How much of a placement's installation is already on its Sparks."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    NOT_PRESENT = "not_present"
    UNKNOWN = "unknown"


class PlacementLoadState(WireEnum):
    """Whether a placement's recipe is loaded on its Sparks."""

    LOADED = "loaded"
    NOT_LOADED = "not_loaded"
    UNKNOWN = "unknown"


class ModelCacheOperatorStatus(WireEnum):
    """The operator-facing word of a model-cache operation that is not a stored state.

    ``accepted``: the request is recorded and not yet picked up.  Every other state
    an operator sees is a :class:`~vonk_agent_protocol.LifecycleState` (a cancel
    under way is ``observing`` with its cancellation intent).
    """

    ACCEPTED = "accepted"


#: Every state machine of this module, by name.
MACHINES: Mapping[str, type[WireEnum]] = {
    "installation": InstallationState,
    "installation-node": InstallationNodeState,
    "distribution-assignment": DistributionAssignmentState,
    "run": RunState,
    "route": RouteState,
    "route-publication": RoutePublicationState,
    "certificate": CertificateState,
    "enrollment-grant": EnrollmentGrantState,
    "model-file": ModelFileState,
    "catalog-sync": CatalogSyncState,
    "reservation": ReservationState,
    "gateway-route": GatewayRouteState,
    "desired-assignment": DesiredAssignmentState,
    "endpoint": EndpointState,
    "observed-assignment": ObservedAssignmentState,
    "asset-availability": AssetAvailability,
    "placement-install": PlacementInstallState,
    "placement-load": PlacementLoadState,
}

#: The retired spelling of a word, per machine.  Empty today: no machine has renamed
#: a word.  A rename adds one row here and nowhere else, and every reader adopts it.
MACHINE_ALIASES: Mapping[type[WireEnum], Mapping[str, WireEnum]] = {}


def _normalized(word: str) -> str:
    return word.strip().lower().replace("_", "-")


def adopt_machine_state[E: WireEnum](machine: type[E], stored: object) -> E | None:
    """The member a stored word means, or ``None`` for a word the machine never had."""

    if isinstance(stored, machine):
        return stored
    if not isinstance(stored, str):
        return None
    spelled = _normalized(stored)
    for member in machine:
        if _normalized(member.value) == spelled:
            return member
    aliases = MACHINE_ALIASES.get(machine, {})
    adopted = aliases.get(stored) or aliases.get(spelled)
    return adopted if isinstance(adopted, machine) else None


def machine_adopter(machine: type[WireEnum]) -> Callable[[Any], Any]:
    """A pydantic ``BeforeValidator`` that adopts an old spelling of ``machine``.

    A contract model that carries a stored state validates a row written before a
    rename as the member it means now; anything else passes through for the field
    to judge.
    """

    def adopt(value: Any) -> Any:
        adopted = adopt_machine_state(machine, value)
        return adopted if adopted is not None else value

    return adopt


def machine_words(
    machine: type[WireEnum], members: Iterable[WireEnum] | None = None
) -> tuple[str, ...]:
    """Every stored word that means one of ``members`` (all of them by default).

    The members' own words, then the retired spellings that adopt into them, in the
    order a CHECK constraint lists them.
    """

    wanted = frozenset(machine if members is None else members)
    words = [member.value for member in machine if member in wanted]
    words.extend(
        alias
        for alias, member in MACHINE_ALIASES.get(machine, {}).items()
        if member in wanted
    )
    return tuple(words)


def machine_check(
    machine: type[WireEnum],
    column: str = "state",
    members: Iterable[WireEnum] | None = None,
) -> str:
    """The CHECK expression of a machine's column, generated from its words."""

    words = ",".join(f"'{word}'" for word in machine_words(machine, members))
    return f"{column} IN ({words})"


__all__ = [
    "MACHINES",
    "MACHINE_ALIASES",
    "AssetAvailability",
    "CatalogSyncState",
    "CertificateState",
    "DistributionAssignmentState",
    "EndpointState",
    "EnrollmentGrantState",
    "InstallationNodeState",
    "InstallationState",
    "ModelCacheOperatorStatus",
    "ModelFileState",
    "ObservedAssignmentState",
    "PlacementInstallState",
    "PlacementLoadState",
    "ReservationState",
    "RoutePublicationState",
    "RouteState",
    "RunState",
    "adopt_machine_state",
    "machine_adopter",
    "machine_check",
    "machine_words",
]
