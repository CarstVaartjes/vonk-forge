"""Shared strict JSON boundary helpers for Pydantic wire models."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from copy import deepcopy
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema


def _check_literal_type(value: object, expected: tuple[Any, ...]) -> object:
    if any(type(item) in (bool, int, float) for item in expected) and not any(
        type(value) is type(item) and value == item for item in expected
    ):
        raise ValueError("literal must use its exact JSON scalar type")
    return value


def _wrap_numeric_literals(schema: CoreSchema) -> CoreSchema:
    if not isinstance(schema, dict):
        return schema
    if schema.get("type") == "literal":
        expected = tuple(schema.get("expected", ()))
        if any(type(item) in (bool, int, float) for item in expected):
            return core_schema.no_info_before_validator_function(
                lambda value: _check_literal_type(value, expected), schema
            )
        return schema
    for key, nested in tuple(schema.items()):
        if isinstance(nested, dict):
            schema[key] = _wrap_numeric_literals(nested)
        elif isinstance(nested, list):
            schema[key] = [
                _wrap_numeric_literals(item) if isinstance(item, dict) else item
                for item in nested
            ]
    return schema


class StrictJSONModel(BaseModel):
    """Reject Python equality based coercion for numeric and boolean Literals.

    Pydantic's strict mode still accepts ``True`` and ``1.0`` for
    ``Literal[1]`` because they compare equal to ``1`` in Python.  JSON has
    distinct scalar types, so wire models must require an exact type/value
    match.  String enum and literal handling remains Pydantic's responsibility.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return _wrap_numeric_literals(deepcopy(handler(source_type)))


class WireModel(StrictJSONModel, Mapping[str, Any]):
    """Immutable JSON message with exact structure and scalar types."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    def __getitem__(self, key: str) -> Any:
        if key in self.__class__.model_fields:
            return getattr(self, key)
        try:
            return self.model_extra[key]
        except (KeyError, TypeError):
            raise KeyError(key) from None

    def __iter__(self) -> Iterator[str]:
        # Mapping iteration exposes explicitly supplied fields. Canonical wire
        # serialization separately applies the model-aware default/null policy.
        declared = (
            name
            for name in self.__class__.model_fields
            if name in self.model_fields_set
        )
        extras = iter(self.model_extra or {})
        return iter((*declared, *extras))

    def __len__(self) -> int:
        return len(self.model_fields_set) + len(self.model_extra or {})


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class OperationCheckpoint(WireModel):
    """Restart-safe cursor identifying a durable operation unit."""

    key: str = Field(min_length=1, max_length=128)
    sequence: int = Field(strict=True, ge=0)
    cursor: str | None = Field(default=None, max_length=512)
    digest: Digest | None = None


class OperationMemberProgress(WireModel):
    """Progress for one node, rank, shard, or other operation member."""

    member_id: str = Field(min_length=1, max_length=128)
    phase: str = Field(min_length=1, max_length=80)
    kind: str | None = Field(default=None, min_length=1, max_length=80)
    object_sha256: Digest | None = None
    completed_bytes: int = Field(default=0, strict=True, ge=0)
    total_bytes: int | None = Field(default=None, strict=True, ge=0)
    bytes_per_second: float | None = Field(default=None, strict=True, ge=0, le=10**15)
    eta_seconds: float | None = Field(default=None, strict=True, ge=0, le=10**9)
    smoothed_bytes_per_second: float | None = Field(default=None, strict=True, ge=0, le=10**15)
    completed_items: int | None = Field(default=None, strict=True, ge=0)
    total_items: int | None = Field(default=None, strict=True, ge=0)
    elapsed_seconds: float | None = Field(default=None, strict=True, ge=0)
    observed_at: str | None = Field(default=None, max_length=64)
    last_progress_at: str | None = Field(default=None, max_length=64)
    activity: Literal["active", "waiting", "possibly_stalled"] | None = None
    state: str = Field(default="running", min_length=1, max_length=32)

    @field_validator("observed_at", "last_progress_at")
    @classmethod
    def timestamp_has_timezone(cls, value: str | None) -> str | None:
        if value is not None and datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("progress timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def totals_are_consistent(self) -> OperationMemberProgress:
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("completed bytes cannot exceed total bytes")
        if self.total_items is not None and (self.completed_items or 0) > self.total_items:
            raise ValueError("completed items cannot exceed total items")
        return self


class OperationProgress(WireModel):
    """Canonical durable progress payload shared by Controller and agents."""

    phase: str = Field(min_length=1, max_length=80)
    kind: str | None = Field(default=None, min_length=1, max_length=80)
    object_sha256: Digest | None = None
    completed_bytes: int = Field(default=0, strict=True, ge=0)
    total_bytes: int | None = Field(default=None, strict=True, ge=0)
    total_bytes_known: bool = False
    bytes_per_second: float | None = Field(default=None, strict=True, ge=0, le=10**15)
    eta_seconds: float | None = Field(default=None, strict=True, ge=0, le=10**9)
    smoothed_bytes_per_second: float | None = Field(default=None, strict=True, ge=0, le=10**15)
    completed_items: int | None = Field(default=None, strict=True, ge=0)
    total_items: int | None = Field(default=None, strict=True, ge=0)
    elapsed_seconds: float | None = Field(default=None, strict=True, ge=0)
    observed_at: str | None = Field(default=None, max_length=64)
    last_progress_at: str | None = Field(default=None, max_length=64)
    activity: Literal["active", "waiting", "possibly_stalled"] | None = None
    checkpoint: OperationCheckpoint | None = None
    members: list[OperationMemberProgress] = Field(default_factory=list, max_length=1024)

    @field_validator("observed_at", "last_progress_at")
    @classmethod
    def timestamp_has_timezone(cls, value: str | None) -> str | None:
        if value is not None and datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("progress timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def totals_are_explicit_and_consistent(self) -> OperationProgress:
        if self.total_bytes_known != (self.total_bytes is not None):
            raise ValueError(
                "total_bytes_known must be false when total_bytes is unknown and true when present"
            )
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("completed bytes cannot exceed total bytes")
        if self.total_items is not None and (self.completed_items or 0) > self.total_items:
            raise ValueError("completed items cannot exceed total items")
        member_ids = [member.member_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("operation progress members must be unique")
        return self


def normalize_operation_progress(value: Mapping[str, object]) -> dict[str, object]:
    """Validate progress and retain every meaningful declared default."""

    from .contracts import canonical_message

    return json.loads(canonical_message(OperationProgress.model_validate(value)))


__all__ = [
    "OperationCheckpoint",
    "OperationMemberProgress",
    "OperationProgress",
    "StrictJSONModel",
    "WireModel",
    "normalize_operation_progress",
]
