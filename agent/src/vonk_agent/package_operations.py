"""Stable package-helper/storage fencing primitives."""

from __future__ import annotations

from dataclasses import dataclass

from vonk_agent_protocol import AgentClaim


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
