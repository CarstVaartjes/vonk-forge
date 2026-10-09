"""Run switch contract: vocabulary."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BeforeValidator,
    Field,
    StringConstraints,
)
from vonk_agent_protocol import (
    LifecycleState,
    LifecycleSubject,
    state_adopter,
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

Digest = Annotated[str, StringConstraints(pattern=_DIGEST_PATTERN)]

PortNumber = Annotated[int, Field(ge=1, le=65535)]

Alias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
    ),
]

# Each closed value set is named once here and used by the contract's own field
# annotations and by the Run/Switch operation helpers that build those fields.
# A shared alias is what keeps a helper signature from drifting away from the
# set the model will accept, so the two cannot disagree without a type error.
RunSwitchPlacementAction = Literal["install", "run", "switch"]

RunSwitchAction = Literal[RunSwitchPlacementAction, "stop", "cleanup"]

RunSwitchRetention = Literal["retain-cached", "reclaim-unreferenced"]

RunSwitchReasonSeverity = Literal["blocker", "warning", "info"]

RunSwitchReasonScope = Literal[
    "model",
    "recipe",
    "mapping",
    "group",
    "node",
    "artifact",
    "freshness",
    "conflict",
    "operation",
]

RunSwitchChangeEffect = Literal["none", "restart", "reprepare", "rebuild", "reinstall"]

RunSwitchCoverage = Literal["complete", "partial", "unknown"]

RunSwitchBuildEvidenceState = Literal[
    "available",
    "planned",
    "building",
    "failed",
    "missing",
    "incompatible",
    "unknown",
]

RunSwitchContainerBuildState = Literal["planned", "building", "succeeded", "failed"]

RunSwitchPhaseKind = Literal[
    "transfer",
    "verify",
    "prepare",
    "cleanup",
    "stop",
    "start",
    "uninstall",
    "final_verify",
]

RunSwitchSubphase = Literal[
    "container-build",
    "model-download",
    "runtime-image",
    "runtime-plan",
    "target-copy",
    "runtime-install",
]

RunSwitchMemberState = Literal["pending", "running", "succeeded", "failed", "unknown"]

RunSwitchProgressState = Annotated[
    Literal[
        LifecycleState.QUEUED,
        LifecycleState.RUNNING,
        LifecycleState.BACKOFF,
        LifecycleState.OBSERVING,
        LifecycleState.NEEDS_OPERATOR,
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
        "unknown",
    ],
    # A document written before the rename may still say ``waiting`` or
    # ``waiting-for-operator``.
    BeforeValidator(state_adopter(LifecycleSubject.JOB)),
]

RunSwitchOperationKind = Literal[
    "recipe.run-switch.v2",
    "recipe.stop.v2",
    "recipe.cleanup.v2",
]
