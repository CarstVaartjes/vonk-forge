"""Shared schema-2 truth for Controller-owned rollout preparation."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import StringConstraints


Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[
    str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
]
NodeId = Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]

PreparationState = Literal[
    "unknown", "missing", "preparing", "verifying", "ready", "failed", "unsupported"
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class PreparationReason(_StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    detail: str = Field(min_length=1, max_length=512)
    severity: Literal["blocker", "warning", "info"]
    node_ids: list[NodeId] = Field(default_factory=list, max_length=64)


class ControllerAssetState(_StrictModel):
    """Availability of one immutable asset in Controller/NAS storage."""

    state: PreparationState
    expected_bytes: int | None = Field(default=None, ge=0)
    verified_bytes: int = Field(default=0, ge=0)
    missing_bytes: int | None = Field(default=None, ge=0)
    verified_at: datetime | None = None
    source: Literal["published", "controller-build", "nas-cache", "unknown"]
    reason: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def byte_state_is_consistent(self) -> ControllerAssetState:
        if self.expected_bytes is not None:
            if self.verified_bytes > self.expected_bytes:
                raise ValueError("verified asset bytes exceed expected bytes")
            expected_missing = self.expected_bytes - self.verified_bytes
            if self.missing_bytes != expected_missing:
                raise ValueError("asset missing bytes do not match expected coverage")
        elif self.missing_bytes is not None:
            raise ValueError("asset missing bytes require a known expected size")
        if self.state == "ready":
            if self.expected_bytes is None or self.missing_bytes != 0:
                raise ValueError("ready Controller asset requires complete byte coverage")
            if self.verified_at is None:
                raise ValueError("ready Controller asset requires verification evidence")
        if self.state in {"failed", "unsupported"} and self.reason is None:
            raise ValueError("failed or unsupported Controller asset requires a reason")
        return self


class TargetAssetState(_StrictModel):
    """Staging and verification state for one immutable asset on one Spark."""

    node_id: NodeId
    state: PreparationState
    expected_bytes: int | None = Field(default=None, ge=0)
    present_bytes: int = Field(default=0, ge=0)
    missing_bytes: int | None = Field(default=None, ge=0)
    verified_identity: str | None = Field(default=None, min_length=1, max_length=128)
    verified_at: datetime | None = None
    imported: bool | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def target_state_is_consistent(self) -> TargetAssetState:
        if self.expected_bytes is not None:
            if self.present_bytes > self.expected_bytes:
                raise ValueError("target asset bytes exceed expected bytes")
            if self.missing_bytes != self.expected_bytes - self.present_bytes:
                raise ValueError("target missing bytes do not match expected coverage")
        elif self.missing_bytes is not None:
            raise ValueError("target missing bytes require a known expected size")
        if self.state == "ready":
            if (
                self.expected_bytes is None
                or self.missing_bytes != 0
                or self.verified_identity is None
                or self.verified_at is None
            ):
                raise ValueError("ready target asset requires complete verification evidence")
        if self.state in {"failed", "unsupported"} and self.reason is None:
            raise ValueError("failed or unsupported target asset requires a reason")
        return self


class ModelArtifactPreparation(_StrictModel):
    """Complete exact model set, including auxiliary and dependency files."""

    artifact_set_sha256: Digest
    model_version_sha256: Digest
    recipe_revision_sha256: Digest | None = None
    artifact_count: int = Field(ge=1, le=1024)
    dependency_model_version_sha256: list[Digest] = Field(
        default_factory=list, max_length=128
    )
    completeness: Literal["complete", "incomplete", "unknown"]
    controller: ControllerAssetState
    targets: list[TargetAssetState] = Field(max_length=64)


class RuntimeImagePreparation(_StrictModel):
    """Exact executable OCI image kept separate from model payloads."""

    image_digest: ImageDigest
    oci_layout_sha256: Digest
    image_bytes: int = Field(ge=0)
    architecture: Literal["linux-arm64"]
    runtime_interface: str = Field(min_length=1, max_length=64)
    build_id: str | None = Field(default=None, min_length=1, max_length=128)
    controller: ControllerAssetState
    targets: list[TargetAssetState] = Field(max_length=64)

    @model_validator(mode="after")
    def ready_target_images_are_imported(self) -> RuntimeImagePreparation:
        if any(target.state == "ready" and target.imported is not True for target in self.targets):
            raise ValueError("ready runtime image must be imported on its target")
        return self


class CompatibilityPreparation(_StrictModel):
    """Explicit reusable work that cannot be embedded in the base image."""

    kind: Literal["engine-generation", "jit", "tuning"]
    stage: Literal["controller-prepare", "target-prepare"]
    compatibility_key: str = Field(min_length=1, max_length=256)
    state: PreparationState
    reusable: bool
    node_ids: list[NodeId] = Field(default_factory=list, max_length=64)
    artifact_sha256: Digest | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def exception_state_is_evidenced(self) -> CompatibilityPreparation:
        if self.state == "ready" and self.reusable and self.artifact_sha256 is None:
            raise ValueError("reusable prepared exception requires an artifact digest")
        if self.state in {"failed", "unsupported"} and self.reason is None:
            raise ValueError("failed or unsupported preparation requires a reason")
        return self


class RolloutPreparation(_StrictModel):
    """Normalized preparation identity shared by profiles, Run, web and CLI."""

    schema_version: Literal[2] = 2
    model: ModelArtifactPreparation
    runtime_image: RuntimeImagePreparation
    exceptions: list[CompatibilityPreparation] = Field(default_factory=list, max_length=64)
    target_node_ids: list[NodeId] = Field(min_length=1, max_length=64)
    controller_ready: bool
    targets_ready: bool
    ready: bool
    reasons: list[PreparationReason] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def readiness_and_scope_are_consistent(self) -> RolloutPreparation:
        if len(self.target_node_ids) != len(set(self.target_node_ids)):
            raise ValueError("preparation target nodes must be unique")
        expected_targets = set(self.target_node_ids)
        for asset in (self.model, self.runtime_image):
            observed = [target.node_id for target in asset.targets]
            if len(observed) != len(set(observed)) or set(observed) != expected_targets:
                raise ValueError("asset readiness must cover the complete target scope")
        computed_controller = (
            self.model.completeness == "complete"
            and self.model.controller.state == "ready"
            and self.runtime_image.controller.state == "ready"
        )
        computed_targets = all(
            target.state == "ready"
            for asset in (self.model, self.runtime_image)
            for target in asset.targets
        )
        exceptions_ready = all(item.state == "ready" for item in self.exceptions)
        blockers_absent = not any(reason.severity == "blocker" for reason in self.reasons)
        if self.controller_ready != computed_controller:
            raise ValueError("controller readiness does not match asset evidence")
        if self.targets_ready != computed_targets:
            raise ValueError("target readiness does not match asset evidence")
        if self.ready != (
            computed_controller and computed_targets and exceptions_ready and blockers_absent
        ):
            raise ValueError("rollout readiness does not match preparation evidence")
        return self


__all__ = [
    "CompatibilityPreparation",
    "ControllerAssetState",
    "ModelArtifactPreparation",
    "PreparationReason",
    "PreparationState",
    "RolloutPreparation",
    "RuntimeImagePreparation",
    "TargetAssetState",
]
