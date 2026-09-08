"""Canonical schema-2 activation receipt shared with the LiteLLM supervisor."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


# One route transition includes old-child shutdown, all new-child startup work,
# and a bounded margin for polling, health requests and acknowledgement I/O.
ROUTE_SHUTDOWN_SECONDS = 30
ROUTE_KILL_REAP_SECONDS = 5
ROUTE_STARTUP_SECONDS = 120
ROUTE_ACTIVATION_MARGIN_SECONDS = 15
ROUTE_ACK_TIMEOUT_SECONDS = (
    ROUTE_SHUTDOWN_SECONDS + ROUTE_STARTUP_SECONDS + ROUTE_ACTIVATION_MARGIN_SECONDS
)
ROUTE_LEASE_SECONDS = ROUTE_ACK_TIMEOUT_SECONDS + ROUTE_ACTIVATION_MARGIN_SECONDS
ROUTE_MAXIMUM_LEASE_SECONDS = 300
ROUTE_EVIDENCE_MAX_AGE_SECONDS = 120


def recipe_route_lease_expiry(
    now: datetime, oldest_observation: datetime | None = None
) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("route lease clock must be timezone-aware")
    now = now.astimezone(UTC)
    expires = now + timedelta(seconds=ROUTE_LEASE_SECONDS)
    if oldest_observation is None:
        return expires
    if oldest_observation.tzinfo is None or oldest_observation.utcoffset() is None:
        raise ValueError("route observation must be timezone-aware")
    oldest = oldest_observation.astimezone(UTC)
    age = (now - oldest).total_seconds()
    if not 0 <= age <= ROUTE_EVIDENCE_MAX_AGE_SECONDS:
        raise ValueError("route observation is outside its admission freshness bound")
    return min(expires, oldest + timedelta(seconds=ROUTE_MAXIMUM_LEASE_SECONDS))


class SupervisorAcknowledgement(BaseModel):
    """One live, ready child acknowledging an exact finite route activation."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    acknowledged_at: str
    activation_sha256: Digest
    child_pid: Annotated[int, Field(gt=0)]
    expires_at: str
    generation: Annotated[int, Field(gt=0)]
    litellm_sha256: Digest
    state: Literal["maintenance", "published"]

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("acknowledgement schema must be integer 1")
        return value

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


class ActivationMarker(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[2]
    generation: Annotated[int, Field(gt=0)]
    state: Literal["maintenance", "published"]
    authority_id: str
    plan_digest: Digest
    evidence_set_digest: Digest
    routes_sha256: Digest
    litellm_sha256: Digest
    issued_at: str
    expires_at: str
    directory: Annotated[str, Field(pattern=r"^[0-9]{8}-[0-9a-f]{64}$")]
    manifest_sha256: Digest

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("activation schema must be integer 2")
        return value

    @field_validator("authority_id")
    @classmethod
    def canonical_authority(cls, value: str) -> str:
        if str(uuid.UUID(value)) != value:
            raise ValueError("activation authority must be a canonical UUID")
        return value

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def manifest_document(self) -> dict[str, object]:
        return self.model_dump(exclude={"directory", "manifest_sha256"})
