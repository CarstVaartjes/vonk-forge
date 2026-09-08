"""Canonical signed Recipe run observation messages.

The agent sends this message after the host helper has inspected a running
container.  The receipt is deliberately part of the wire model: an unsigned
run/readiness boolean is not a valid observation.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import AgentProtocolError, canonical_message
from .host_helper import (
    ExecuteContainerRuntimeRequestOperation,
    RecipeRunInspectionBinding,
    SignedHostHelperGrant,
    SignedRecipeRunObservationReceipt,
)
from .wire_model import WireModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_NODE = r"^spk_[0-9a-f]{32}$"
RECIPE_RUN_OBSERVATION_SCHEMA_VERSION = 2


def _strict_datetime(value: object, message: str) -> object:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(message) from error
    raise ValueError(message)


class RecipeRunObservationIdentity(RecipeRunInspectionBinding):
    """Canonical Controller identity projection for one exact observation."""

    schema_version: Literal[1]
    node_id: str = Field(pattern=_NODE)


class RecipeRunObservationWire(RecipeRunObservationIdentity):
    """One current, exact observation produced by the Rust agent."""

    observed_at: datetime
    endpoint_ready: bool | None = Field(strict=True)
    observation_identity_sha256: Digest
    grant: SignedHostHelperGrant
    helper_receipt: SignedRecipeRunObservationReceipt
    observation_receipt_public_key: Digest

    @field_validator("observed_at", mode="before")
    @classmethod
    def parse_observed_at(cls, value: object) -> object:
        return _strict_datetime(value, "recipe run observation time is invalid")

    @field_validator("observed_at")
    @classmethod
    def aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recipe run observation time must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("role", "model_identity")
    @classmethod
    def safe_text(cls, value: str) -> str:
        if any(char in value for char in "\x00\r\n"):
            raise ValueError("recipe run observation text is invalid")
        return value

    @field_validator("local_address", "master_address")
    @classmethod
    def valid_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        import ipaddress

        try:
            ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError("recipe run observation address is invalid") from error
        return value

    @model_validator(mode="after")
    def exact_receipt_binding(self) -> RecipeRunObservationWire:
        if self.rank >= self.world_size:
            raise ValueError("recipe run observation rank is invalid")
        singleton = self.world_size == 1
        rendezvous = (self.local_address, self.master_address, self.master_port)
        if (singleton and any(value is not None for value in rendezvous)) or (
            not singleton and any(value is None for value in rendezvous)
        ):
            raise ValueError("recipe run observation rendezvous is invalid")
        if (self.local_address == self.master_address) != (
            self.endpoint_ready is not None
        ):
            raise ValueError("recipe run observation endpoint readiness is invalid")
        if self.helper_receipt.claims.node_id != self.node_id:
            raise ValueError("recipe run observation receipt node is invalid")
        if self.grant.claims.node_id != self.node_id:
            raise ValueError("recipe run observation grant node is invalid")
        if self.grant.claims.request_id != self.helper_receipt.claims.request_id:
            raise ValueError("recipe run observation grant request is invalid")
        operation = self.grant.claims.operation
        if not isinstance(operation, ExecuteContainerRuntimeRequestOperation):
            raise ValueError(  # noqa: TRY004
                "recipe run observation grant operation is invalid"
            )
        if (
            operation.action != "run-inspect"
            or operation.job_id != self.run_id
            or operation.attempt != self.run_generation
            or operation.request_sha256 != self.helper_receipt.claims.request_sha256
            or operation.observation_identity_sha256 != self.observation_identity_sha256
        ):
            raise ValueError("recipe run observation grant binding is invalid")
        if (
            self.helper_receipt.claims.observation_identity_sha256
            != self.observation_identity_sha256
        ):
            raise ValueError("recipe run observation receipt identity is invalid")
        if self.observed_at.timestamp() != self.helper_receipt.claims.observed_at:
            raise ValueError("recipe run observation receipt time is invalid")
        expected_key_id = hashlib.sha256(
            bytes.fromhex(self.observation_receipt_public_key)
        ).hexdigest()
        if self.helper_receipt.signature.key_id != expected_key_id:
            raise ValueError("recipe run observation receipt key is invalid")
        if (
            self.observation_identity_sha256
            != hashlib.sha256(
                canonical_message(self.observation_identity())
            ).hexdigest()
        ):
            raise ValueError("recipe run observation identity is invalid")
        return self

    def observation_identity(self) -> dict[str, object]:
        return self.model_dump(
            mode="json",
            exclude={
                "observed_at",
                "endpoint_ready",
                "observation_identity_sha256",
                "grant",
                "helper_receipt",
                "observation_receipt_public_key",
            },
        )

    @classmethod
    def parse(cls, value: Any) -> RecipeRunObservationWire:
        try:
            return cls.model_validate_json(canonical_message(value))
        except Exception as error:
            raise AgentProtocolError("recipe run observation is invalid") from error


class RecipeRunObservationsWire(WireModel):
    """The sole current observation snapshot envelope."""

    schema_version: Literal[RECIPE_RUN_OBSERVATION_SCHEMA_VERSION]
    observed_at: datetime
    runs: list[RecipeRunObservationWire] = Field(max_length=64)

    @field_validator("observed_at", mode="before")
    @classmethod
    def parse_observed_at(cls, value: object) -> object:
        return _strict_datetime(value, "recipe run snapshot time is invalid")

    @field_validator("observed_at")
    @classmethod
    def aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recipe run snapshot time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def unique_runs(self) -> RecipeRunObservationsWire:
        ids = [run.run_id for run in self.runs]
        if len(ids) != len(set(ids)):
            raise ValueError("recipe run observation is duplicated")
        return self

    @classmethod
    def parse(cls, value: Any) -> RecipeRunObservationsWire:
        try:
            return cls.model_validate_json(canonical_message(value))
        except Exception as error:
            raise AgentProtocolError(
                "recipe run observation snapshot is invalid"
            ) from error


class RecipeRunObservationGrantRequest(RecipeRunObservationIdentity):
    """Exact Controller request that authorizes one runtime inspection."""

    job_id: str = Field(pattern=_UUID4)
    operation_id: str = Field(pattern=_UUID4)
    attempt: int = Field(ge=1, le=2**31 - 1, strict=True)
    fence: str = Field(pattern=_UUID4)
    request_sha256: Digest
    expires_in_seconds: Literal[10]

    @model_validator(mode="after")
    def exact_rendezvous(self) -> RecipeRunObservationGrantRequest:
        singleton = self.world_size == 1
        rendezvous = (self.local_address, self.master_address, self.master_port)
        if (singleton and any(value is not None for value in rendezvous)) or (
            not singleton and any(value is None for value in rendezvous)
        ):
            raise ValueError("recipe run observation rendezvous is invalid")
        return self

    def observation_identity(self) -> dict[str, object]:
        return RecipeRunObservationIdentity.model_validate({
            name: getattr(self, name)
            for name in RecipeRunObservationIdentity.model_fields
        }).model_dump(mode="json")


class RecipeRunObservationGrantWire(WireModel):
    """Canonical grant response used to authorize one exact observation."""

    schema_version: Literal[1]
    observation_identity_sha256: Digest
    grant: SignedHostHelperGrant


__all__ = [
    "RECIPE_RUN_OBSERVATION_SCHEMA_VERSION",
    "RecipeRunObservationGrantRequest",
    "RecipeRunObservationGrantWire",
    "RecipeRunObservationWire",
    "RecipeRunObservationsWire",
]
