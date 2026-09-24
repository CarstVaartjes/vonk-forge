"""Nonsecret operator enrollment status and request identity."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints

from .strict_json import StrictJSONModel

ENROLLMENT_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
EnrollmentId = Annotated[str, StringConstraints(pattern=ENROLLMENT_ID_PATTERN)]


class EnrollmentGrantStatus(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    id: EnrollmentId
    state: Literal["pending", "expired", "consumed", "revoked"]
    purpose: Literal["new-node", "re-enroll"]
    node_id: str | None = Field(pattern=r"^spk_[0-9a-f]{32}$")
    display_name: str | None
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None
