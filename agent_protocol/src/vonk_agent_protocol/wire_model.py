"""Shared strict JSON boundary helpers for Pydantic wire models."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    model_serializer,
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

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__class__.model_fields:
            raise AttributeError(f"{self.__class__.__name__} is immutable")
        super().__setattr__(name, value)

    def __getitem__(self, key: str) -> Any:
        if key in self.__class__.model_fields:
            return getattr(self, key)
        try:
            return self.model_extra[key]
        except (KeyError, TypeError):
            raise KeyError(key) from None

    def __iter__(self) -> Iterator[str]:
        # Mapping consumers in the Controller use this as the wire view.  Keep
        # omitted defaults omitted, matching ``model_dump(exclude_unset=True)``
        # used by canonical_message.
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
    state: str = Field(default="running", min_length=1, max_length=32)

    @model_validator(mode="after")
    def totals_are_consistent(self) -> OperationMemberProgress:
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("completed bytes cannot exceed total bytes")
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
    checkpoint: OperationCheckpoint | None = None
    members: list[OperationMemberProgress] = Field(default_factory=list, max_length=1024)

    @model_serializer(mode="wrap")
    def serialize_compact(self, handler: Any) -> dict[str, Any]:
        document = handler(self)
        for key in ("kind", "object_sha256", "total_bytes", "bytes_per_second", "eta_seconds", "checkpoint"):
            if document.get(key) is None:
                document.pop(key, None)
        if "completed_bytes" not in self.model_fields_set:
            document.pop("completed_bytes", None)
        if "total_bytes_known" not in self.model_fields_set:
            document.pop("total_bytes_known", None)
        if "members" not in self.model_fields_set or not self.members:
            document.pop("members", None)
        return document

    @model_validator(mode="after")
    def totals_are_explicit_and_consistent(self) -> OperationProgress:
        if self.total_bytes_known != (self.total_bytes is not None):
            raise ValueError(
                "total_bytes_known must be false when total_bytes is unknown and true when present"
            )
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("completed bytes cannot exceed total bytes")
        member_ids = [member.member_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("operation progress members must be unique")
        return self


def normalize_operation_progress(value: Mapping[str, object]) -> dict[str, object]:
    """Validate and canonicalize progress while preserving phase-only wire data."""

    parsed = OperationProgress.model_validate(value)
    document = parsed.model_dump(mode="json", exclude_none=True)
    if not document.get("members"):
        document.pop("members", None)
    if parsed.checkpoint is None:
        document.pop("checkpoint", None)
    if parsed.completed_bytes == 0 and "completed_bytes" not in value:
        document.pop("completed_bytes", None)
    extended = bool(
        set(value)
        & {
            "completed_bytes",
            "total_bytes",
            "bytes_per_second",
            "eta_seconds",
            "checkpoint",
            "members",
        }
    )
    if parsed.total_bytes_known is False and "total_bytes_known" not in value:
        if extended:
            document["total_bytes_known"] = False
        else:
            document.pop("total_bytes_known", None)
    return document


__all__ = [
    "OperationCheckpoint",
    "OperationMemberProgress",
    "OperationProgress",
    "StrictJSONModel",
    "WireModel",
    "normalize_operation_progress",
]
