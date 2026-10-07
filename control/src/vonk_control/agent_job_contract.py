"""Typed facts the agent job queue renders into an operator-facing note."""

from __future__ import annotations

from pydantic import Field

from .strict_json import StrictModel


class ClaimFacts(StrictModel):
    """The bounded control-plane facts that explain one refused claim.

    Only the named check, the operation kind and state, attempt and ordinal
    integers and small state labels belong here (never a payload, a digest, a
    certificate or an agent-supplied string).  The declaration order is the
    order the facts are rendered in.
    """

    kind: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=64)
    operation_intent: int | None = Field(default=None, ge=0)
    node_intent: int | None = Field(default=None, ge=0)
    attempt: int | None = Field(default=None, ge=0)
    attempt_state: str | None = Field(default=None, max_length=64)
    lease_deadline: str | None = Field(default=None, max_length=64)
    retry_due_at: str | None = Field(default=None, max_length=64)

    def rendered(self) -> dict[str, int | str]:
        """The facts that are present, in declaration order, as note keywords."""

        return self.model_dump(exclude_none=True)
