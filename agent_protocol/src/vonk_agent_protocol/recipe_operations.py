"""Closed declarative protocol for digest-bound recipe lifecycle work."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .compiled_execution_plan import CompiledExecutionPlan
from .contracts import (
    AgentOperation,
    AgentProtocolError,
    _fields,
    _mapping,
    _uuid,
    _version,
    canonical_message,
)

RECIPE_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_INSTALL,
        AgentOperation.RECIPE_START,
        AgentOperation.RECIPE_STOP,
        AgentOperation.RECIPE_UNINSTALL,
        AgentOperation.RECIPE_MODEL_UNINSTALL,
    }
)

class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

class RecipeInstallPayload(_StrictPayload):
    schema_version: Literal[2]
    installation_id: str
    plan_digest: str
    rank: int
    role: str
    expected_bytes: int
    compiled_execution_plan: CompiledExecutionPlan

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_strict(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("schema version is invalid")
        return value

    @model_validator(mode="after")
    def identity_matches(self) -> RecipeInstallPayload:
        _uuid(self.installation_id, name="installation_id")
        _digest(self.plan_digest, "plan_digest")
        if self.rank < 0 or _ROLE.fullmatch(self.role) is None or not 0 <= self.expected_bytes <= 16 * 1024**4:
            raise ValueError("install identity is invalid")
        placement = self.compiled_execution_plan.runtime.placement
        if (self.rank, self.role) != (placement.rank, placement.role):
            raise ValueError("install placement does not match compiled plan")
        return self

class RecipeStartPayload(_StrictPayload):
    schema_version: Literal[2]
    run_id: str
    installation_id: str
    recipe_revision_id: str
    recipe_content_sha256: str
    mapping_id: str
    mapping_generation: int
    image_digest: str
    plan_digest: str
    alias: str
    rank: int
    role: str
    port: int
    reserved_memory_bytes: int
    endpoint_address: str
    world_size: int
    compiled_execution_plan: CompiledExecutionPlan
    local_address: str | None
    master_address: str | None
    master_port: int | None
    phase: Literal["rank-launch", "collective-readiness"] | None = None
    start_deadline: str | None = None
    run_generation: int | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_strict(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("schema version is invalid")
        return value

    @model_validator(mode="after")
    def placement_matches(self) -> RecipeStartPayload:
        for key in ("run_id", "installation_id", "recipe_revision_id", "mapping_id"):
            _uuid(getattr(self, key), name=key)
        for key in ("plan_digest", "recipe_content_sha256"):
            _digest(getattr(self, key), key)
        if not _OCI_DIGEST.fullmatch(self.image_digest) or _ALIAS.fullmatch(self.alias) is None or _ROLE.fullmatch(self.role) is None:
            raise ValueError("start identity is invalid")
        if self.mapping_generation < 1 or self.rank < 0 or not 1024 <= self.port <= 65535 or not 1 <= self.reserved_memory_bytes <= 16 * 1024**4 or self.world_size < 1 or self.rank >= self.world_size:
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
        ):
            raise ValueError("single-node start phases are invalid")
        if self.world_size == 1 and self.run_generation is not None and self.run_generation < 1:
            raise ValueError("single-node run generation is invalid")
        if self.phase is not None:
            try:
                deadline = datetime.fromisoformat(self.start_deadline or "")
            except ValueError as error:
                raise ValueError("start deadline is invalid") from error
            if deadline.tzinfo is None or deadline.utcoffset() != UTC.utcoffset(deadline):
                raise ValueError("start deadline must be UTC")
        if self.phase is None and self.start_deadline is not None:
            raise ValueError("start phase binding is invalid")
        if self.world_size > 1 and self.phase is None and self.run_generation is not None:
            raise ValueError("distributed run generation requires a start phase")
        if self.phase is not None and (self.start_deadline is None or self.run_generation is None or self.run_generation < 1):
            raise ValueError("start phase binding is invalid")
        return self

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ALIAS = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?\Z")
_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")

def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} must be a lowercase SHA-256")
    return value


def _bytes(value: object, name: str, *, positive: bool = False) -> int:
    floor = 1 if positive else 0
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not floor <= value <= 16 * 1024**4
    ):
        raise AgentProtocolError(f"{name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class RecipeOperationRequest:
    operation: AgentOperation
    schema_version: int
    plan_digest: str
    installation_id: str | None = None
    recipe_revision_id: str | None = None
    recipe_content_sha256: str | None = None
    mapping_id: str | None = None
    mapping_generation: int | None = None
    recipe_build_id: str | None = None
    image_digest: str | None = None
    expected_bytes: int | None = None
    run_id: str | None = None
    alias: str | None = None
    rank: int | None = None
    role: str | None = None
    port: int | None = None
    reserved_memory_bytes: int | None = None
    endpoint_address: str | None = None
    world_size: int | None = None
    local_address: str | None = None
    master_address: str | None = None
    master_port: int | None = None
    phase: str | None = None
    start_deadline: str | None = None
    run_generation: int | None = None
    cleanup_model_content_sha256: str | None = None
    model_content_sha256: str | None = None
    installations: tuple[tuple[str, str], ...] = ()
    compiled_execution_plan: Mapping[str, Any] | None = None

    @classmethod
    def parse(cls, operation: AgentOperation, payload: Any) -> RecipeOperationRequest:
        if operation not in RECIPE_OPERATIONS:
            raise AgentProtocolError("recipe operation is not supported")
        value = json.loads(canonical_message(_mapping(payload)))
        if operation is AgentOperation.RECIPE_INSTALL:
            try:
                parsed = RecipeInstallPayload.model_validate(value)
            except Exception as error:
                raise AgentProtocolError("recipe install payload is invalid") from error
            return cls(
                operation=operation,
                schema_version=2,
                plan_digest=parsed.plan_digest,
                installation_id=parsed.installation_id,
                rank=parsed.rank,
                role=parsed.role,
                expected_bytes=parsed.expected_bytes,
                compiled_execution_plan=parsed.compiled_execution_plan.to_mapping(),
            )
        if operation is AgentOperation.RECIPE_START:
            try:
                parsed = RecipeStartPayload.model_validate(value)
            except Exception as error:
                raise AgentProtocolError("recipe start payload is invalid") from error
            return cls(
                operation=operation,
                schema_version=2,
                plan_digest=parsed.plan_digest,
                installation_id=parsed.installation_id,
                recipe_revision_id=parsed.recipe_revision_id,
                recipe_content_sha256=parsed.recipe_content_sha256,
                mapping_id=parsed.mapping_id,
                mapping_generation=parsed.mapping_generation,
                image_digest=parsed.image_digest,
                run_id=parsed.run_id,
                alias=parsed.alias,
                rank=parsed.rank,
                role=parsed.role,
                port=parsed.port,
                reserved_memory_bytes=parsed.reserved_memory_bytes,
                endpoint_address=parsed.endpoint_address,
                world_size=parsed.world_size,
                local_address=parsed.local_address,
                master_address=parsed.master_address,
                master_port=parsed.master_port,
                phase=parsed.phase,
                start_deadline=parsed.start_deadline,
                run_generation=parsed.run_generation,
                compiled_execution_plan=parsed.compiled_execution_plan.to_mapping(),
            )
        common = {"schema_version", "plan_digest"}
        if operation is AgentOperation.RECIPE_STOP:
            required = common | {"run_id"}
        elif operation is AgentOperation.RECIPE_MODEL_UNINSTALL:
            required = common | {"model_content_sha256", "installations"}
        else:
            required = common | {"installation_id", "recipe_content_sha256"}
            if "cleanup_model_content_sha256" in value:
                required.add("cleanup_model_content_sha256")
        _fields(value, required=required)
        schema_version = _version(value["schema_version"])
        plan_digest = _digest(value["plan_digest"], "plan_digest")
        installation_id = (
            _uuid(value["installation_id"], name="installation_id")
            if "installation_id" in value
            else None
        )
        recipe_digest = (
            _digest(value["recipe_content_sha256"], "recipe_content_sha256")
            if "recipe_content_sha256" in value
            else None
        )
        cleanup_model_digest = (
            _digest(
                value["cleanup_model_content_sha256"],
                "cleanup_model_content_sha256",
            )
            if value.get("cleanup_model_content_sha256") is not None
            else None
        )
        model_digest = (
            _digest(value["model_content_sha256"], "model_content_sha256")
            if "model_content_sha256" in value
            else None
        )
        installations: tuple[tuple[str, str], ...] = ()
        if operation is AgentOperation.RECIPE_MODEL_UNINSTALL:
            raw_installations = value["installations"]
            if (
                not isinstance(raw_installations, list)
                or not 1 <= len(raw_installations) <= 512
            ):
                raise AgentProtocolError("model uninstall installations are invalid")
            parsed_installations: list[tuple[str, str]] = []
            for raw_installation in raw_installations:
                item = _mapping(raw_installation)
                _fields(
                    item,
                    required={"installation_id", "recipe_content_sha256"},
                )
                parsed_installations.append(
                    (
                        _uuid(item["installation_id"], name="installation_id"),
                        _digest(
                            item["recipe_content_sha256"],
                            "recipe_content_sha256",
                        ),
                    )
                )
            installations = tuple(parsed_installations)
            if len({item[0] for item in installations}) != len(installations):
                raise AgentProtocolError("model uninstall installations are duplicated")
        expected_bytes = (
            _bytes(value["expected_bytes"], "expected_bytes")
            if "expected_bytes" in value
            else None
        )
        run_id = _uuid(value["run_id"], name="run_id") if "run_id" in value else None
        return cls(
            operation=operation,
            schema_version=schema_version,
            plan_digest=plan_digest,
            installation_id=installation_id,
            recipe_content_sha256=recipe_digest,
            expected_bytes=expected_bytes,
            run_id=run_id,
            cleanup_model_content_sha256=cleanup_model_digest,
            model_content_sha256=model_digest,
            installations=installations,
        )


__all__ = ["RECIPE_OPERATIONS", "RecipeOperationRequest"]
