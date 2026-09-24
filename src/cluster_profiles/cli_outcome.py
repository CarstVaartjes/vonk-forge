"""Local process outcomes, separate from authoritative Controller documents."""

from __future__ import annotations

import argparse
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast

OutcomeContext = Literal["read", "preview", "mutation", "await"]


@dataclass
class EnrollmentDeliveryError(Exception):
    document: Mapping[str, object]
    interrupted: bool = False


@dataclass
class Submission:
    """What this process knows about acceptance, not remote execution state."""

    request_key: str
    path: str
    lookup_path: str
    timeout_seconds: float
    action: str = "download"
    acceptance: Literal["not_submitted", "unknown", "accepted", "refused"] = (
        "not_submitted"
    )
    operation_id: str | None = None
    failures: list[dict[str, object]] = field(default_factory=list)

    def document(self) -> dict[str, object]:
        return {
            "request_key": self.request_key,
            "path": self.path,
            "lookup_path": self.lookup_path,
            "timeout_seconds": self.timeout_seconds,
            "action": self.action,
            "acceptance": self.acceptance,
            **({"operation_id": self.operation_id} if self.operation_id else {}),
            "failures": self.failures,
        }


def operation_state(result: Mapping[str, object]) -> str:
    for key in ("state", "status", "outcome"):
        state = result.get(key)
        if isinstance(state, str):
            return state.casefold()
    operation = result.get("operation")
    return operation_state(operation) if isinstance(operation, Mapping) else ""


@dataclass
class Observation:
    path: str
    reconnect_command: str
    result: Mapping[str, object]
    timeout_seconds: float
    started: float
    confirmed: float
    observed_at: datetime
    status: Literal["following", "complete", "timed_out", "interrupted"] = "following"
    error: str | None = None

    def update(self, result: Mapping[str, object]) -> None:
        self.result = result
        self.confirmed = time.monotonic()
        self.observed_at = datetime.now(UTC)
        self.error = None

    def document(self) -> dict[str, object]:
        return {
            "result": dict(self.result),
            "observation": {
                "status": self.status,
                "path": self.path,
                "reconnect_command": self.reconnect_command,
                "timeout_seconds": self.timeout_seconds,
                "observed_at": self.observed_at.isoformat(),
                "age_seconds": max(0, time.monotonic() - self.confirmed),
                "reconnecting": self.error is not None,
                **({"error": self.error} if self.error is not None else {}),
            },
        }


@dataclass(frozen=True)
class CommandOutcome:
    document: Mapping[str, object]
    context: OutcomeContext
    observation: Observation | None = None

    @classmethod
    def from_command(
        cls, args: argparse.Namespace, result: Mapping[str, object]
    ) -> CommandOutcome:
        context = cast(OutcomeContext, getattr(args, "outcome_context", "read"))
        if getattr(args, "dry_run", False):
            context = "preview"
        elif getattr(args, "follow", False):
            context = "await"
        elif getattr(args, "model_action", None) == "cancel" and operation_state(
            result
        ) in {"cancelling", "cancelled"}:
            context = "read"
        observation = getattr(args, "observation", None)
        return cls(
            result,
            context,
            observation if isinstance(observation, Observation) else None,
        )

    @property
    def output(self) -> Mapping[str, object]:
        if self.observation and self.observation.status in {"timed_out", "interrupted"}:
            return self.observation.document()
        return self.document

    @property
    def exit_code(self) -> int:
        if self.observation and self.observation.status == "interrupted":
            return 130
        if self.observation and self.observation.status == "timed_out":
            return 2
        if self.context == "preview":
            return 0 if self.document.get("allowed") is True else 2
        if self.context == "read":
            return 0
        state = operation_state(self.document)
        if state == "partial":
            return 1
        if state in {
            "failed",
            "blocked",
            "cancelled",
            "rejected",
            "waiting-for-operator",
        }:
            return 2
        return 0
