"""Shared schema-2 truth for Controller-owned rollout preparation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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
    verified_sha256: Digest | None = None
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
            if self.verified_sha256 is None or self.verified_at is None:
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
    verified_sha256: Digest | None = None
    imported_image_digest: ImageDigest | None = None
    verified_at: datetime | None = None
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
        if self.state == "ready" and (
            self.expected_bytes is None
            or self.missing_bytes != 0
            or self.verified_sha256 is None
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
    artifact_set_bytes: int = Field(ge=1)
    dependency_model_version_sha256: list[Digest] = Field(
        default_factory=list, max_length=128
    )
    completeness: Literal["complete", "incomplete", "unknown"]
    controller: ControllerAssetState
    targets: list[TargetAssetState] = Field(max_length=64)

    @model_validator(mode="after")
    def exact_model_set_is_bound(self) -> ModelArtifactPreparation:
        if len(self.dependency_model_version_sha256) != len(
            set(self.dependency_model_version_sha256)
        ) or self.dependency_model_version_sha256 != sorted(
            self.dependency_model_version_sha256
        ):
            raise ValueError("dependency model identities must be sorted and unique")
        if self.model_version_sha256 in self.dependency_model_version_sha256:
            raise ValueError("primary model cannot also be a dependency")
        if self.controller.expected_bytes != self.artifact_set_bytes:
            raise ValueError("Controller model bytes do not match the exact artifact set")
        if (
            self.controller.state == "ready"
            and self.controller.verified_sha256 != self.artifact_set_sha256
        ):
            raise ValueError("Controller model digest does not match the exact artifact set")
        for target in self.targets:
            if target.expected_bytes != self.artifact_set_bytes:
                raise ValueError("target model bytes do not match the exact artifact set")
            if target.imported_image_digest is not None:
                raise ValueError("model targets cannot claim an imported image identity")
            if (
                target.state == "ready"
                and target.verified_sha256 != self.artifact_set_sha256
            ):
                raise ValueError("target model digest does not match the exact artifact set")
        if (
            self.completeness == "complete"
            and self.controller.state == "ready"
            and self.controller.verified_bytes != self.artifact_set_bytes
        ):
            raise ValueError("complete model set must verify every expected byte")
        return self


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
    def exact_oci_image_is_bound(self) -> RuntimeImagePreparation:
        if self.controller.expected_bytes != self.image_bytes:
            raise ValueError("Controller image bytes do not match the OCI layout")
        if (
            self.controller.state == "ready"
            and self.controller.verified_sha256 != self.oci_layout_sha256
        ):
            raise ValueError("Controller image digest does not match the OCI layout")
        for target in self.targets:
            if target.expected_bytes != self.image_bytes:
                raise ValueError("target image bytes do not match the OCI layout")
            if target.state == "ready" and (
                target.verified_sha256 != self.oci_layout_sha256
                or target.imported_image_digest != self.image_digest
            ):
                raise ValueError(
                    "ready runtime image must verify the exact OCI layout and imported image"
                )
        return self


class CompatibilityIdentity(_StrictModel):
    """Immutable inputs for an exceptional reusable preparation artifact."""

    recipe_revision_sha256: Digest
    model_version_sha256: Digest
    runtime_image_digest: ImageDigest
    parameters_sha256: Digest
    hardware_profile_sha256: Digest | None = None


class CompatibilityPreparation(_StrictModel):
    """Explicit reusable work that cannot be embedded in the base image."""

    kind: Literal["engine-generation", "jit", "tuning"]
    stage: Literal["controller-prepare", "target-prepare"]
    compatibility: CompatibilityIdentity
    compatibility_key_sha256: Digest
    state: PreparationState
    reusable: Literal[True]
    node_ids: list[NodeId] = Field(default_factory=list, max_length=64)
    artifact_sha256: Digest | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def exception_state_is_evidenced(self) -> CompatibilityPreparation:
        identity = self.compatibility.model_dump(mode="json")
        expected_key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.compatibility_key_sha256 != expected_key:
            raise ValueError("compatibility key does not match immutable preparation inputs")
        if len(self.node_ids) != len(set(self.node_ids)) or self.node_ids != sorted(
            self.node_ids
        ):
            raise ValueError("exception target nodes must be sorted and unique")
        if not self.node_ids:
            raise ValueError("exception preparation requires an explicit target scope")
        if self.state == "ready" and self.artifact_sha256 is None:
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
        if len(self.target_node_ids) != len(set(self.target_node_ids)) or (
            self.target_node_ids != sorted(self.target_node_ids)
        ):
            raise ValueError("preparation target nodes must be sorted and unique")
        expected_targets = set(self.target_node_ids)
        for asset in (self.model, self.runtime_image):
            observed = [target.node_id for target in asset.targets]
            if len(observed) != len(set(observed)) or set(observed) != expected_targets:
                raise ValueError("asset readiness must cover the complete target scope")
        for exception in self.exceptions:
            if not set(exception.node_ids) <= expected_targets:
                raise ValueError("exception preparation exceeds the rollout target scope")
            if self.model.recipe_revision_sha256 is None:
                raise ValueError("exception preparation requires an exact recipe revision")
            identity = exception.compatibility
            if (
                identity.recipe_revision_sha256
                != self.model.recipe_revision_sha256
                or identity.model_version_sha256 != self.model.model_version_sha256
                or identity.runtime_image_digest != self.runtime_image.image_digest
            ):
                raise ValueError("exception identity does not match the rollout authority")
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
    "CompatibilityIdentity",
    "CompatibilityPreparation",
    "ControllerAssetState",
    "ModelArtifactPreparation",
    "PreparationReason",
    "PreparationState",
    "RolloutPreparation",
    "RuntimeImagePreparation",
    "TargetAssetState",
]
