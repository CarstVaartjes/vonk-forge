"""Run switch contract: progress."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol import (
    OperationProgress,
)

from ..run_switch_identity_contract import NodeId
from ..strict_json import StrictModel
from .vocabulary import (
    RunSwitchMemberState,
    RunSwitchPhaseKind,
    RunSwitchProgressState,
    RunSwitchSubphase,
)


class RunSwitchMemberProgress(StrictModel):
    node_id: NodeId
    phase: RunSwitchPhaseKind | None = None
    state: RunSwitchMemberState
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    error: Annotated[str, StringConstraints(max_length=256)] | None = None


class RunSwitchProgress(StrictModel):
    operation: OperationProgress | None = None
    startup_budget_seconds: int | None = Field(default=None, ge=1)
    start_deadline: datetime | None = None
    phase_index: int = Field(ge=0, le=31)
    phase_count: int = Field(ge=1, le=32)
    phase: RunSwitchPhaseKind | None
    state: RunSwitchProgressState
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    total_bytes_known: bool
    subphase: RunSwitchSubphase | None = None
    members: list[RunSwitchMemberProgress] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def total_bytes_state_is_consistent(self) -> RunSwitchProgress:
        if self.total_bytes_known != (self.total_bytes is not None):
            raise ValueError("progress total byte knowledge is inconsistent")
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("progress completed bytes exceed total bytes")
        return self


_FailureText = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9._-]*$")
]


class ArtifactVerificationEvidence(StrictModel):
    """One node's immutable artifact handoff evidence."""

    node_id: NodeId
    downloaded_bytes: int | None = Field(default=None, ge=0)
    copied_bytes: int | None = Field(default=None, ge=0)
    error: Annotated[str, StringConstraints(max_length=512)] | None = None
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    uncertain: bool = False
    # The agent's typed failure: kind decides retry, code and diagnostic say
    # which request or check refused (e.g. ``http_status=404``).
    failure_kind: _FailureText | None = None
    error_code: _FailureText | None = None
    diagnostic: Annotated[str, StringConstraints(max_length=512)] | None = None


class RunSwitchMemberReceipt(StrictModel):
    """Durable member projection emitted by a child distribution operation."""

    node_id: NodeId
    phase: RunSwitchPhaseKind | None = None
    state: RunSwitchMemberState
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    error: Annotated[str, StringConstraints(max_length=512)] | None = None
    cached: bool = False
    failure_kind: _FailureText | None = None
    error_code: _FailureText | None = None
    diagnostic: Annotated[str, StringConstraints(max_length=512)] | None = None


class RunSwitchRankReceipt(StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    state: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    fresh: bool | None = None


class RunSwitchChildProgress(StrictModel):
    """Progress nested in a durable child receipt."""

    operation: OperationProgress | None = None

    phase: (
        Literal[
            "transfer",
            "verify",
            "prepare",
            "cleanup",
            "stop",
            "start",
            "final_verify",
            "container-build",
            "model-download",
            "runtime-image",
            "runtime-plan",
            "target-copy",
            "runtime-install",
        ]
        | None
    ) = None
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    total_bytes_known: bool = False
    members: list[RunSwitchMemberReceipt] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def total_bytes_state_is_consistent(self) -> RunSwitchChildProgress:
        if self.total_bytes_known != (self.total_bytes is not None):
            raise ValueError("child progress total byte knowledge is inconsistent")
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("child progress completed bytes exceed total bytes")
        return self
