"""The Controller's record of one node's artifact distribution grant."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import Field, ValidationError, field_validator
from vonk_agent_protocol import (
    AgentProtocolError,
    DistributionAssignment,
    canonical_message,
)


class NodeDistributionAssignment(DistributionAssignment):
    """A distribution grant scoped to one node, plan and model set.

    Only the object set travels to the agent (``wire``); the scope fields stay
    with the Controller, which authorizes every download against them.
    """

    assignment_id: str = Field(json_schema_extra={"format": "uuid"})
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=1)
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    expires_at: datetime
    model_artifact_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The runtime image's address in the Controller's layered store (its
    # manifest digest hex); the reference lifecycle protects it while the
    # grant is open. The node pulls the image by ``oci_image_digest``.
    oci_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

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

    @classmethod
    def parse(cls, value: Any) -> NodeDistributionAssignment:
        try:
            return cls.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise AgentProtocolError("distribution assignment is invalid") from error

    def wire(self) -> DistributionAssignment:
        return DistributionAssignment(
            objects=self.objects,
            oci_image_digest=self.oci_image_digest,
            oci_image_config_digest=self.oci_image_config_digest,
        )


__all__ = ["NodeDistributionAssignment"]
