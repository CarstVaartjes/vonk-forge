"""Recipe run observation report sent by the mTLS-authenticated agent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from .contracts import AgentProtocolError, canonical_message
from .host_helper import Uuid4Text
from .wire_model import MAX_RUN_GENERATION, WireModel


def _strict_datetime(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("recipe run observation time is invalid") from error
    raise ValueError("recipe run observation time is invalid")


class RecipeRunObservationWire(WireModel):
    """What the agent saw of one local run generation."""

    run_id: Uuid4Text
    run_generation: int = Field(ge=1, le=MAX_RUN_GENERATION, strict=True)
    process_running: bool = Field(strict=True)
    # Only the endpoint owner probes readiness; every other rank sends null.
    endpoint_ready: bool | None = Field(strict=True)


class RecipeRunObservationsWire(WireModel):
    """The agent's current observation of every locally managed run.

    ``observed_at`` is taken before the agent lists its local runs, so a run
    the Controller changed after that instant is never overwritten by it.
    """

    observed_at: datetime
    runs: list[RecipeRunObservationWire] = Field(max_length=64)

    @field_validator("observed_at", mode="before")
    @classmethod
    def parse_observed_at(cls, value: object) -> object:
        return _strict_datetime(value)

    @field_validator("observed_at")
    @classmethod
    def aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recipe run observation time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def unique_runs(self) -> RecipeRunObservationsWire:
        ids = [run.run_id for run in self.runs]
        if len(ids) != len(set(ids)):
            raise ValueError("recipe run observation is duplicated")
        return self

    @classmethod
    def parse(cls, value: Any) -> RecipeRunObservationsWire:
        try:
            return cls.model_validate_json(canonical_message(value))
        except Exception as error:
            raise AgentProtocolError("recipe run observations are invalid") from error


__all__ = [
    "RecipeRunObservationWire",
    "RecipeRunObservationsWire",
]
