"""The compiled runtime images a Spark's container runtime holds now."""

from __future__ import annotations

from pydantic import Field, field_validator

from .agent_state import OciDigest
from .wire_model import WireEnum, WireModel

#: The most images one observation names; a Spark keeps a handful.
MAX_REPORTED_RUNTIME_IMAGES = 1024


class RuntimeImageInspectionKind(WireEnum):
    RUNTIME_IMAGES = "runtime-images"


class RuntimeImageInspectionRequest(WireModel):
    """Read-only helper request; contains no paths or mutation authority."""

    kind: RuntimeImageInspectionKind


class RuntimeImageObservation(WireModel):
    """The root helper's Docker listing, reported unchanged in every inventory.

    Each entry is the platform manifest digest of a compiled runtime image
    Docker holds under its local name. The Controller charges an install for
    any image not named here; a missing observation is unknown and charges too.
    """

    images: list[OciDigest] = Field(max_length=MAX_REPORTED_RUNTIME_IMAGES)

    @field_validator("images")
    @classmethod
    def unique_images(cls, images: list[str]) -> list[str]:
        if len(images) != len(set(images)):
            raise ValueError("runtime image observation repeats an image")
        return images
