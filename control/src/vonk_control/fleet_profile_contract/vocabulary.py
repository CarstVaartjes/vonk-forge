"""Fleet profile contract: vocabulary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import (
    BeforeValidator,
    Field,
    StringConstraints,
)
from vonk_agent_protocol import (
    DesiredAssignmentState,
    EndpointState,
    LifecycleState,
    LifecycleSubject,
    ObservedAssignmentState,
    SupersedeCode,
    machine_adopter,
    state_adopter,
)
from vonk_agent_protocol.agent_words import (
    ProfileAction,
    ProfileChildPhase,
    ProfileInstallationPolicy,
    ProfileOperationKind,
    ProfileReportedPhase,
)


def profile_switch_child_request_key(
    application_id: str, position: int, kind: str, owner_id: str
) -> str:
    """One child identity for dispatch and recovery before its checkpoint exists."""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"vonk-forge:profile-run-switch:{application_id}:{position}:{kind}:{owner_id}",
        )
    )


MAX_PROFILE_WARNINGS = 128

_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

UuidId = Annotated[str, StringConstraints(pattern=_UUID_PATTERN)]

NodeId = Annotated[str, StringConstraints(pattern=_NODE_PATTERN)]

Digest = Annotated[str, StringConstraints(pattern=_DIGEST_PATTERN)]

Name = Annotated[
    str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)
]

Description = Annotated[str, StringConstraints(max_length=1000, strip_whitespace=True)]

LabelName = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9_.-]{0,61}[a-z0-9])?$"
    ),
]

LabelValue = Annotated[str, StringConstraints(min_length=1, max_length=63)]

Alias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$",
    ),
]

OptionSlug = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
]

OptionChoices = Annotated[dict[OptionSlug, OptionSlug], Field(max_length=16)]

RecipeSelector = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9-]{1,62}/[a-z0-9][a-z0-9-]{1,62}$",
    ),
]

if TYPE_CHECKING:
    FleetProfileInstallationPolicy = Literal["keep-cached", "exact"]
else:
    FleetProfileInstallationPolicy = Literal[
        tuple(member.value for member in ProfileInstallationPolicy)
    ]

if TYPE_CHECKING:
    FleetProfileOperationState = LifecycleState
    FleetProfileCancellationState = LifecycleState
else:
    FleetProfileOperationState = Annotated[
        Literal[
            LifecycleState.QUEUED,
            LifecycleState.RUNNING,
            LifecycleState.NEEDS_OPERATOR,
            LifecycleState.SUCCEEDED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
            LifecycleState.SUPERSEDED,
        ],
        BeforeValidator(state_adopter(LifecycleSubject.FLEET_PROFILE_APPLICATION)),
    ]
    FleetProfileCancellationState = Annotated[
        Literal[LifecycleState.OBSERVING, LifecycleState.CANCELLED],
        BeforeValidator(state_adopter(LifecycleSubject.FLEET_PROFILE_APPLICATION)),
    ]

FleetProfileSupersedeCode = SupersedeCode

FLEET_PROFILE_ENDED_STATES = frozenset(
    {
        LifecycleState.SUCCEEDED.value,
        LifecycleState.FAILED.value,
        LifecycleState.CANCELLED.value,
        "superseded",
    }
)

if TYPE_CHECKING:
    FleetProfileChildPhase = Literal[
        "model-download",
        "container-download",
        "container-build",
        "target-copy",
        "runtime-install",
        "start",
        "final-verify",
        "transfer",
        "verify",
        "prepare",
        "cleanup",
        "stop",
        "uninstall",
        "final_verify",
    ]
else:
    FleetProfileChildPhase = Literal[
        tuple(member.value for member in (*ProfileChildPhase, *ProfileReportedPhase))
    ]

DesiredAssignmentStateField = Annotated[
    DesiredAssignmentState, BeforeValidator(machine_adopter(DesiredAssignmentState))
]

FleetProfileAssignmentState = Annotated[
    ObservedAssignmentState, BeforeValidator(machine_adopter(ObservedAssignmentState))
]

if TYPE_CHECKING:
    FleetProfileAction = Literal["switch", "keep", "adopt"]
else:
    FleetProfileAction = Literal[tuple(member.value for member in ProfileAction)]

FleetProfilePlanStepKind = Literal["switch", "prepare"]

if TYPE_CHECKING:
    FleetProfileOperationKind = Literal["fleet-profile.apply"]
else:
    FleetProfileOperationKind = Literal[
        tuple(member.value for member in ProfileOperationKind)
    ]

FleetProfileEndpointState = Annotated[
    EndpointState, BeforeValidator(machine_adopter(EndpointState))
]
