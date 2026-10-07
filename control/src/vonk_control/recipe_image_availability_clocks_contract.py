"""Tolerant lifecycle observations independent of job-document imports.

These projections retain readable clocks when the owning document is damaged.
They neither authorize effects nor replace the full job payload contract.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)
from vonk_agent_protocol import canonical_message


def readable_or_none[T](adapter: TypeAdapter[T]) -> BeforeValidator:
    def read(value: object) -> object:
        try:
            return adapter.validate_python(value)
        except (TypeError, ValueError):
            try:
                return adapter.validate_json(canonical_message(value))
            except (TypeError, ValueError):
                return None

    return BeforeValidator(read)


_TIME = readable_or_none(TypeAdapter(AwareDatetime))
_TEXT = readable_or_none(TypeAdapter(Annotated[str, Field(strict=True, min_length=1)]))
_COUNT = readable_or_none(TypeAdapter(Annotated[int, Field(strict=True, ge=0)]))


class StoredAvailabilityRetryClock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    automatic_attempts: Annotated[int | None, _COUNT] = None


class StoredAvailabilityCancelClock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cancel_requested_at: Annotated[AwareDatetime | None, _TIME] = None
    cancel_request_id: Annotated[str | None, _TEXT] = None


class StoredAvailabilityFailureClock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    retry_time: Annotated[AwareDatetime | None, _TIME] = None


class StoredAvailabilityCheckpointClock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    retry_attempts: Annotated[int | None, _COUNT] = None
    failure: Annotated[
        StoredAvailabilityFailureClock | None,
        readable_or_none(TypeAdapter(StoredAvailabilityFailureClock)),
    ] = None


class StoredAvailabilityClocks(BaseModel):
    """Only the clocks and effect presence needed to adopt a stored job."""

    model_config = ConfigDict(extra="ignore")
    claim_owner: Annotated[str | None, _TEXT] = None
    claim_until: Annotated[AwareDatetime | None, _TIME] = None
    retry_after_at: Annotated[AwareDatetime | None, _TIME] = None
    retry: Annotated[
        StoredAvailabilityRetryClock | None,
        readable_or_none(TypeAdapter(StoredAvailabilityRetryClock)),
    ] = None
    cancellation: Annotated[
        StoredAvailabilityCancelClock | None,
        readable_or_none(TypeAdapter(StoredAvailabilityCancelClock)),
    ] = None
    checkpoint: Annotated[
        StoredAvailabilityCheckpointClock | None,
        readable_or_none(TypeAdapter(StoredAvailabilityCheckpointClock)),
    ] = None
    image_reference_intent: Annotated[bool, BeforeValidator(bool)] = False
    model_child: Annotated[bool, BeforeValidator(bool)] = False
    build_dependency: Annotated[bool, BeforeValidator(bool)] = False

    @classmethod
    def read(cls, document: object) -> StoredAvailabilityClocks:
        try:
            return cls.model_validate(document)
        except ValidationError:
            return cls()
