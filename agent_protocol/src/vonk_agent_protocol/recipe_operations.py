"""Closed declarative protocol for digest-bound recipe lifecycle work."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from .compiled_execution_plan import CompiledExecutionPlan
from .contracts import (
    AgentOperation,
    AgentProtocolError,
    canonical_message,
)
from .wire_model import WireModel

RECIPE_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_INSTALL,
        AgentOperation.RECIPE_START,
        AgentOperation.RECIPE_STOP,
        AgentOperation.RECIPE_UNINSTALL,
        AgentOperation.RECIPE_MODEL_UNINSTALL,
    }
)

_UUID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OciDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
CanonicalUuid = Annotated[str, Field(pattern=f"^{_UUID_PATTERN}$")]
Role = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Alias = Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?$")]
ByteCount = Annotated[int, Field(ge=0, le=16 * 1024**4)]
PositiveByteCount = Annotated[int, Field(ge=1, le=16 * 1024**4)]
Port = Annotated[int, Field(ge=1024, le=65535)]
PositiveInt = Annotated[int, Field(ge=1)]


class _StrictPayload(WireModel):
    """Base for immutable, exact recipe lifecycle payloads."""

class RecipeInstallPayload(_StrictPayload):
    schema_version: Literal[2]
    installation_id: CanonicalUuid
    plan_digest: Digest
    rank: Annotated[int, Field(ge=0)]
    role: Role
    expected_bytes: ByteCount
    compiled_execution_plan: CompiledExecutionPlan

    @model_validator(mode="after")
    def identity_matches(self) -> RecipeInstallPayload:
        placement = self.compiled_execution_plan.runtime.placement
        if (self.rank, self.role) != (placement.rank, placement.role):
            raise ValueError("install placement does not match compiled plan")
        return self


class RecipeStartPayload(_StrictPayload):
    schema_version: Literal[2]
    run_id: CanonicalUuid
    installation_id: CanonicalUuid
    recipe_revision_id: CanonicalUuid
    recipe_content_sha256: Digest
    mapping_id: CanonicalUuid
    mapping_generation: PositiveInt
    image_digest: OciDigest
    plan_digest: Digest
    alias: Alias
    rank: Annotated[int, Field(ge=0)]
    role: Role
    port: Port
    reserved_memory_bytes: PositiveByteCount
    endpoint_address: str
    world_size: PositiveInt
    compiled_execution_plan: CompiledExecutionPlan
    local_address: str | None
    master_address: str | None
    master_port: Port | None
    phase: Literal["rank-launch", "collective-readiness"] | None = None
    start_deadline: str | None = None
    run_generation: PositiveInt | None = None

    @model_validator(mode="after")
    def placement_matches(self) -> RecipeStartPayload:
        if self.rank >= self.world_size:
            raise ValueError("start placement is invalid")
        placement = self.compiled_execution_plan.runtime.placement
        if (self.rank, self.role, self.world_size) != (placement.rank, placement.role, placement.world_size):
            raise ValueError("start placement does not match compiled plan")
        if self.image_digest != self.compiled_execution_plan.runtime.image_digest:
            raise ValueError("start image does not match compiled plan")
        if self.recipe_content_sha256 != self.compiled_execution_plan.identity.recipe_revision_sha256:
            raise ValueError("start recipe digest does not match compiled plan")
        placement = self.compiled_execution_plan.runtime.placement
        endpoint_matches = self.endpoint_address == placement.endpoint_address
        if placement.endpoint_address is None and self.world_size > 1:
            endpoint_matches = self.endpoint_address == self.local_address
        if not endpoint_matches or self.port != placement.port or self.reserved_memory_bytes != placement.reserved_memory_bytes or self.local_address != placement.local_address or self.master_address != placement.master_address or self.master_port != placement.master_port:
            raise ValueError("start placement does not match compiled plan")
        for name, address in (
            ("endpoint_address", self.endpoint_address),
            ("local_address", self.local_address),
            ("master_address", self.master_address),
        ):
            if address is None:
                continue
            parsed_address = ipaddress.ip_address(address)
            if parsed_address.is_loopback or parsed_address.is_link_local or parsed_address.is_multicast or parsed_address.is_unspecified or str(parsed_address) != address:
                raise ValueError(f"{name} is invalid")
        if self.world_size == 1:
            if self.rank != 0 or self.local_address is not None or self.master_address is not None or self.master_port is not None:
                raise ValueError("single-node rendezvous is invalid")
        elif self.local_address is None or self.master_address is None or self.master_port is None or self.master_port < 1024:
            raise ValueError("distributed rendezvous is invalid")
        if self.world_size == 1 and (
            self.phase is not None
            or self.start_deadline is not None
            or self.run_generation is not None
        ):
            raise ValueError("single-node start phases are invalid")
        if self.phase is not None:
            try:
                deadline = datetime.fromisoformat(self.start_deadline or "")
            except ValueError as error:
                raise ValueError("start deadline is invalid") from error
            if deadline.tzinfo is None or deadline.utcoffset() != UTC.utcoffset(deadline):
                raise ValueError("start deadline must be UTC")
        if self.phase is None and (self.start_deadline is not None or self.run_generation is not None):
            raise ValueError("start phase binding is invalid")
        if self.phase is not None and (self.start_deadline is None or self.run_generation is None or self.run_generation < 1):
            raise ValueError("start phase binding is invalid")
        return self


class RecipeStopPayload(_StrictPayload):
    schema_version: Literal[1]
    run_id: CanonicalUuid
    plan_digest: Digest


class RecipeUninstallPayload(_StrictPayload):
    schema_version: Literal[1]
    installation_id: CanonicalUuid
    plan_digest: Digest
    recipe_content_sha256: Digest
    # The key is required on the wire.  Null means this operation retains the
    # shared model cache and is deliberately different from an omitted key.
    cleanup_model_content_sha256: Digest | None


class RecipeModelCleanupInstallation(_StrictPayload):
    installation_id: CanonicalUuid
    recipe_content_sha256: Digest


class RecipeModelCleanupPayload(_StrictPayload):
    schema_version: Literal[1]
    model_content_sha256: Digest
    plan_digest: Digest
    installations: tuple[RecipeModelCleanupInstallation, ...] = Field(
        min_length=1, max_length=512
    )

    @model_validator(mode="after")
    def installations_are_unique(self) -> RecipeModelCleanupPayload:
        if len({item.installation_id for item in self.installations}) != len(
            self.installations
        ):
            raise ValueError("model cleanup installations are duplicated")
        return self


class RecipeStopResult(_StrictPayload):
    stopped: Literal[True]


class RecipeUninstallResult(_StrictPayload):
    uninstalled: Literal[True]
    removed_model_bytes: int = Field(ge=0, le=16 * 1024**4)


class RecipeModelCleanupResult(_StrictPayload):
    uninstalled_installations: int = Field(ge=1, le=512)
    removed_model_bytes: int = Field(ge=0, le=16 * 1024**4)


_REQUEST_MODELS = {
    AgentOperation.RECIPE_INSTALL: RecipeInstallPayload,
    AgentOperation.RECIPE_START: RecipeStartPayload,
    AgentOperation.RECIPE_STOP: RecipeStopPayload,
    AgentOperation.RECIPE_UNINSTALL: RecipeUninstallPayload,
    AgentOperation.RECIPE_MODEL_UNINSTALL: RecipeModelCleanupPayload,
}


class RecipeOperationRequest(_StrictPayload):
    """Typed lifecycle request envelope with an operation-specific payload."""

    operation: AgentOperation
    payload: (
        RecipeInstallPayload
        | RecipeStartPayload
        | RecipeStopPayload
        | RecipeUninstallPayload
        | RecipeModelCleanupPayload
    )

    @classmethod
    def parse(cls, operation: AgentOperation, payload: Any) -> RecipeOperationRequest:
        if operation not in RECIPE_OPERATIONS:
            raise AgentProtocolError("recipe operation is not supported")
        try:
            model = _REQUEST_MODELS[operation]
            typed = model.model_validate_json(canonical_message(payload))
            return cls(operation=operation, payload=typed)
        except (ValidationError, TypeError, ValueError) as error:
            raise AgentProtocolError(
                f"{operation.value.removeprefix('recipe ')} payload is invalid"
            ) from error

    @model_validator(mode="after")
    def operation_matches_payload(self) -> RecipeOperationRequest:
        try:
            model = _REQUEST_MODELS[self.operation]
        except KeyError:
            model = None
        if model is None or not isinstance(self.payload, model):
            raise ValueError("recipe operation payload type does not match operation")
        return self

    @property
    def schema_version(self) -> int:
        return self.payload.schema_version

    @property
    def plan_digest(self) -> str:
        return self.payload.plan_digest

    @property
    def installation_id(self) -> str | None:
        return getattr(self.payload, "installation_id", None)

    @property
    def recipe_revision_id(self) -> str | None:
        return getattr(self.payload, "recipe_revision_id", None)

    @property
    def recipe_content_sha256(self) -> str | None:
        return getattr(self.payload, "recipe_content_sha256", None)

    @property
    def mapping_id(self) -> str | None:
        return getattr(self.payload, "mapping_id", None)

    @property
    def mapping_generation(self) -> int | None:
        return getattr(self.payload, "mapping_generation", None)

    @property
    def image_digest(self) -> str | None:
        return getattr(self.payload, "image_digest", None)

    @property
    def alias(self) -> str | None:
        return getattr(self.payload, "alias", None)

    @property
    def port(self) -> int | None:
        return getattr(self.payload, "port", None)

    @property
    def reserved_memory_bytes(self) -> int | None:
        return getattr(self.payload, "reserved_memory_bytes", None)

    @property
    def endpoint_address(self) -> str | None:
        return getattr(self.payload, "endpoint_address", None)

    @property
    def world_size(self) -> int | None:
        return getattr(self.payload, "world_size", None)

    @property
    def local_address(self) -> str | None:
        return getattr(self.payload, "local_address", None)

    @property
    def master_address(self) -> str | None:
        return getattr(self.payload, "master_address", None)

    @property
    def master_port(self) -> int | None:
        return getattr(self.payload, "master_port", None)

    @property
    def phase(self) -> str | None:
        return getattr(self.payload, "phase", None)

    @property
    def start_deadline(self) -> str | None:
        return getattr(self.payload, "start_deadline", None)

    @property
    def run_generation(self) -> int | None:
        return getattr(self.payload, "run_generation", None)

    @property
    def expected_bytes(self) -> int | None:
        return getattr(self.payload, "expected_bytes", None)

    @property
    def run_id(self) -> str | None:
        return getattr(self.payload, "run_id", None)

    @property
    def rank(self) -> int | None:
        return getattr(self.payload, "rank", None)

    @property
    def role(self) -> str | None:
        return getattr(self.payload, "role", None)

    @property
    def compiled_execution_plan(self) -> Mapping[str, Any] | None:
        plan = getattr(self.payload, "compiled_execution_plan", None)
        return None if plan is None else plan.to_mapping()

    @property
    def cleanup_model_content_sha256(self) -> str | None:
        return getattr(self.payload, "cleanup_model_content_sha256", None)

    @property
    def model_content_sha256(self) -> str | None:
        return getattr(self.payload, "model_content_sha256", None)

    @property
    def installations(self) -> tuple[RecipeModelCleanupInstallation, ...]:
        value = getattr(self.payload, "installations", ())
        return tuple(value)


def parse_recipe_operation_result(
    operation: AgentOperation, result: Any
) -> RecipeStopResult | RecipeUninstallResult | RecipeModelCleanupResult:
    """Parse a successful stop, uninstall, or model-cleanup result exactly."""

    result_models = {
        AgentOperation.RECIPE_STOP: RecipeStopResult,
        AgentOperation.RECIPE_UNINSTALL: RecipeUninstallResult,
        AgentOperation.RECIPE_MODEL_UNINSTALL: RecipeModelCleanupResult,
    }
    try:
        model = result_models[operation]
        return model.model_validate_json(canonical_message(result))
    except (KeyError, ValidationError, TypeError, ValueError) as error:
        raise AgentProtocolError("recipe operation result is invalid") from error


__all__ = [
    "RECIPE_OPERATIONS",
    "RecipeInstallPayload",
    "RecipeModelCleanupInstallation",
    "RecipeModelCleanupPayload",
    "RecipeModelCleanupResult",
    "RecipeOperationRequest",
    "RecipeStartPayload",
    "RecipeStopPayload",
    "RecipeStopResult",
    "RecipeUninstallPayload",
    "RecipeUninstallResult",
    "parse_recipe_operation_result",
]
