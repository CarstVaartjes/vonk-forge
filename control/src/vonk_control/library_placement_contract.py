"""Strict contracts for one-shot recipe-to-Spark placement."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .library_contract import Digest, NodeId, UuidId

Alias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
    ),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class LibraryPlacementPreviewRequest(_StrictModel):
    recipe_id: UuidId
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    desired_state: Literal["installed", "running"] = "installed"
    alias: Alias | None = None
    invocation: Literal["drag-drop", "keyboard", "button"] = "button"

    @model_validator(mode="after")
    def validate_intent(self) -> LibraryPlacementPreviewRequest:
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("placement Spark identities must be unique")
        if self.node_ids != sorted(self.node_ids):
            raise ValueError("placement Spark identities must be sorted")
        if self.desired_state == "running" and self.alias is None:
            raise ValueError("running placements require an endpoint alias")
        if self.desired_state == "installed" and self.alias is not None:
            raise ValueError("installed-only placements cannot declare an alias")
        return self


class LibraryPlacementApplyRequest(LibraryPlacementPreviewRequest):
    plan_digest: Digest
    request_key: UuidId


class LibraryPlacementReason(_StrictModel):
    code: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    severity: Literal["info", "warning", "error"]
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)


class LibraryPlacementNode(_StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    endpoint_owner: bool
    disk_free_bytes: int | None = Field(default=None, ge=0)
    disk_required_bytes: int | None = Field(default=None, ge=0)
    disk_free_after_bytes: int | None = None
    memory_available_bytes: int | None = Field(default=None, ge=0)
    memory_required_bytes: int | None = Field(default=None, ge=0)
    memory_free_after_bytes: int | None = None


class LibraryPlacementLocations(_StrictModel):
    installation_ids: list[UuidId] = Field(max_length=16)
    run_ids: list[UuidId] = Field(max_length=16)
    installed: bool
    running: bool


class LibraryPlacementStep(_StrictModel):
    index: int = Field(ge=0, le=1023)
    kind: Literal[
        "stop",
        "create-placement",
        "build",
        "distribute-image",
        "install",
        "start",
        "keep",
    ]
    label: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)


class LibraryPlacementPreview(_StrictModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    recipe_id: UuidId
    recipe_revision_id: UuidId
    recipe_title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    topology_name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    desired_state: Literal["installed", "running"]
    alias: Alias | None
    invocation: Literal["drag-drop", "keyboard", "button"]
    selected_node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    selected_nodes: list[LibraryPlacementNode] = Field(max_length=32)
    allowed: bool
    steps: list[LibraryPlacementStep] = Field(max_length=16)
    blockers: list[LibraryPlacementReason] = Field(max_length=64)
    warnings: list[LibraryPlacementReason] = Field(max_length=64)
    locations: LibraryPlacementLocations
    plan_digest: Digest


class LibraryPlacementApplication(_StrictModel):
    schema_version: Literal[1] = 1
    id: UuidId
    state: Literal[
        "queued", "running", "waiting-for-operator", "succeeded", "failed", "cancelled"
    ]
    recipe_id: UuidId
    recipe_revision_id: UuidId
    selected_node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    desired_state: Literal["installed", "running"]
    alias: Alias | None
    plan_digest: Digest
    current_step: int = Field(ge=0, le=1024)
    total_steps: int = Field(ge=0, le=1024)
    current_operation_id: UuidId | None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None
    progress: dict[str, object]
    locations: LibraryPlacementLocations
    created_at: datetime
    updated_at: datetime


__all__ = [
    "LibraryPlacementApplication",
    "LibraryPlacementApplyRequest",
    "LibraryPlacementLocations",
    "LibraryPlacementNode",
    "LibraryPlacementPreview",
    "LibraryPlacementPreviewRequest",
    "LibraryPlacementReason",
    "LibraryPlacementStep",
]
