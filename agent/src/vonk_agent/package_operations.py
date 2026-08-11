"""Stable agent-side boundary for generic workload package engines."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from vonk_agent_protocol import AgentClaim, PackageOperationRequest

from .deadlines import MonotonicDeadline


@dataclass(frozen=True)
class OperationBinding:
    """Fencing identity supplied to every package-engine side effect."""

    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str

    @classmethod
    def from_claim(cls, claim: AgentClaim) -> OperationBinding:
        if type(claim) is not AgentClaim:
            raise TypeError("package operation binding requires an agent claim")
        return cls(
            job_id=claim.job_id,
            operation_id=claim.operation_id,
            attempt=claim.attempt,
            fence=claim.fence,
            node_id=claim.node_id,
        )


class PackageDisposition(StrEnum):
    READY = "ready"
    SAFE_TO_RETRY = "safe-to-retry"
    COMPLETED = "completed"
    COMPENSATE = "compensate"
    OPERATOR_INTERVENTION = "operator-intervention"


@dataclass(frozen=True)
class PackageInspection:
    disposition: PackageDisposition
    evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, PackageDisposition):
            raise TypeError("package inspection disposition is invalid")
        if self.evidence is not None and not isinstance(self.evidence, Mapping):
            raise ValueError("package inspection evidence is invalid")


class PackageOperationsBoundary(Protocol):
    """Versioned unprivileged package engine consumed by the fixed registry."""

    def execute(
        self,
        request: PackageOperationRequest,
        binding: OperationBinding,
        deadline: MonotonicDeadline,
    ) -> Mapping[str, object]: ...

    def inspect(
        self,
        request: PackageOperationRequest,
        binding: OperationBinding,
        deadline: MonotonicDeadline,
    ) -> PackageInspection: ...
