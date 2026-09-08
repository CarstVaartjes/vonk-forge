"""Pydantic contracts for authenticated Controller-to-Spark distribution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from .contracts import AgentProtocolError, canonical_message
from .wire_model import WireModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class DistributionObject(WireModel):
    """One model file, OCI archive, or OCI layer referenced by an assignment."""

    name: str = Field(min_length=1, max_length=512)
    sha256: Digest
    bytes: int = Field(ge=0, le=16 * 1024**4)
    kind: Literal["model", "oci-archive", "oci-layer"]

    @field_validator("name")
    @classmethod
    def name_is_relative(cls, value: str) -> str:
        if (
            len(value) > 512
            or value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("distribution object name must be a safe relative path")
        return value

    @model_validator(mode="after")
    def empty_only_for_model(self) -> DistributionObject:
        if self.bytes == 0 and (self.kind != "model" or self.sha256 != _EMPTY_SHA256):
            raise ValueError("zero-sized distribution object is invalid")
        return self

    @classmethod
    def parse(cls, value: Any) -> DistributionObject:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError("distribution object is invalid") from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class DistributionAssignment(WireModel):
    """Controller authorization for one node, generation and object set."""

    schema_version: Literal[2]
    assignment_id: str = Field(json_schema_extra={"format": "uuid"})
    plan_digest: Digest
    generation: int = Field(ge=1)
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    expires_at: datetime
    model_artifact_set_sha256: Digest
    objects: tuple[DistributionObject, ...] = Field(min_length=1, max_length=4096)
    oci_image_digest: ImageDigest
    oci_archive_sha256: Digest

    @field_validator("assignment_id")
    @classmethod
    def random_assignment_id(cls, value: str) -> str:
        parsed = UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError("assignment_id must be a canonical random UUID")
        return value

    @field_validator("expires_at")
    @classmethod
    def expiration_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("distribution expiration must be UTC")
        return value

    @field_serializer("expires_at")
    def serialize_expiration(self, value: datetime) -> str:
        return value.isoformat()

    @model_validator(mode="after")
    def object_set_is_complete(self) -> DistributionAssignment:
        if len({item.sha256 for item in self.objects}) != len(self.objects):
            raise ValueError("distribution assignment objects are duplicated")
        if not any(item.kind == "model" for item in self.objects):
            raise ValueError("distribution assignment has no model objects")
        if not any(
            item.sha256 == self.oci_archive_sha256 and item.kind == "oci-archive"
            for item in self.objects
        ):
            raise ValueError("distribution assignment OCI archive is not declared")
        return self

    @classmethod
    def parse(cls, value: Any) -> DistributionAssignment:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError("distribution assignment is invalid") from error

    def to_mapping(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @property
    def object_digests(self) -> frozenset[str]:
        return frozenset(item.sha256 for item in self.objects)


__all__ = ["DistributionAssignment", "DistributionObject"]
