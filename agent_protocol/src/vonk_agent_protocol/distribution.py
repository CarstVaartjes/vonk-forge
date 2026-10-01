"""Pydantic contracts for authenticated Controller-to-Spark distribution."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .contracts import AgentProtocolError, canonical_message
from .wire_model import WireModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class DistributionObject(WireModel):
    """One model file referenced by an assignment."""

    name: str = Field(min_length=1, max_length=512)
    sha256: Digest
    bytes: int = Field(ge=0, le=16 * 1024**4)
    kind: Literal["model"]

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
    """What one node fetches for a distribution plan.

    Model files are downloaded as objects. The runtime image is not an
    object: the node pulls it by manifest digest from the Controller's
    layered image store, and only the layers it lacks travel.
    """

    objects: tuple[DistributionObject, ...] = Field(min_length=1, max_length=4096)
    oci_image_digest: ImageDigest
    oci_image_config_digest: ImageDigest

    @model_validator(mode="after")
    def object_set_is_complete(self) -> DistributionAssignment:
        if len({item.sha256 for item in self.objects}) != len(self.objects):
            raise ValueError("distribution assignment objects are duplicated")
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
