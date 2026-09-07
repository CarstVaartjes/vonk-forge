"""Canonical schema-2 activation receipt shared with the LiteLLM supervisor."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


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
