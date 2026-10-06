"""Verified Controller receipts for the compiled Spark execution plan.

The canonical runtime compiler describes *how* a workload is launched.  This
module binds that description to the immutable objects that the Controller
actually delivers.  The resulting document is an internal boundary between
the catalog/compiler and the agent protocol:

* every selected model file has an exact digest, byte count, mount and role;
* every file points at a Controller distribution object;
* the runtime image has an exact image digest, OCI-layout digest and archive
  size; and
* the agent payload contains no upstream repository, revision or credential
  authority.

The cache/build services remain authorities for their respective identities.
This module never derives an artifact-set digest from a partial list and never
turns an upstream source reference into an agent download instruction.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from vonk_agent_protocol import (
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES,
    canonical_message,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledArtifact,
    CompiledArtifactMount,
    CompiledEndpoint,
    CompiledEnvironmentEntry,
    CompiledIdentity,
    CompiledJob,
    CompiledLifecycle,
    CompiledModelIdentity,
    CompiledPlacement,
    CompiledRuntime,
    CompiledSecurity,
    CompiledSecurityMount,
    CompiledTopology,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledRuntimeImage as WireCompiledRuntimeImage,
)

from .content_identity import same_model_object
from .runtime_spec_contract import RuntimeSpec
from .strict_json import StrictJSONModel

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
]
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
MAX_COMPILED_EXECUTION_PLAN_BYTES = MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
_WEIGHT_ROLES = frozenset({"model", "weight", "weights"})


class CompiledExecutionPlanError(ValueError):
    """The canonical runtime and verified delivery receipts cannot be bound."""


class _StrictModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _safe_path(value: str, *, absolute: bool, max_length: int = 512) -> str:
    parts = (
        value[1:].split("/") if absolute and value.startswith("/") else value.split("/")
    )
    if (
        not value
        or len(value) > max_length
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in parts)
        or (absolute and not value.startswith("/"))
        or (not absolute and value.startswith("/"))
    ):
        raise ValueError("path is not a safe Controller runtime path")
    return value


class ExecutionMount(_StrictModel):
    """The platform-owned read-only mount used by one selected model file."""

    source: str = Field(min_length=1, max_length=512)
    target: str = Field(min_length=1, max_length=512)

    @field_validator("source")
    @classmethod
    def source_is_safe(cls, value: str) -> str:
        value = _safe_path(value, absolute=True)
        if not value.startswith("/run/vonk/models/"):
            raise ValueError("model mount source must be Controller-owned")
        return value

    @field_validator("target")
    @classmethod
    def target_is_safe(cls, value: str) -> str:
        return _safe_path(value, absolute=True)


class ModelCatalogIdentity(_StrictModel):
    """Safe model identity used for display and execution evidence.

    Upstream repository and revision fields deliberately do not exist here.
    They remain Controller/cache inputs and are never sent to a Spark.
    """

    publisher: str = Field(min_length=1, max_length=128)
    slug: Identifier
    content_sha256: Digest

    @field_validator("publisher")
    @classmethod
    def publisher_is_safe_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("model publisher is invalid")
        return value


class DistributionObjectReceipt(_StrictModel):
    """A verified immutable object served by the Controller."""

    name: str = Field(min_length=1, max_length=512)
    sha256: Digest
    bytes: int = Field(ge=0, le=16 * 1024**4)
    kind: Literal["model", "oci-archive"]

    @field_validator("name")
    @classmethod
    def name_is_safe(cls, value: str) -> str:
        return _safe_path(value, absolute=False)

    @model_validator(mode="after")
    def byte_count_matches_digest_kind(self) -> DistributionObjectReceipt:
        if self.bytes == 0 and (self.kind != "model" or self.sha256 != EMPTY_SHA256):
            raise ValueError("only an empty model support file may have zero bytes")
        return self


class VerifiedModelObject(_StrictModel):
    """One cache-authorized model file before recipe mount selection.

    The model content identity and file ID are part of the lookup key.  A
    path alone is insufficient because different model definitions legitimately
    contain files with the same name, such as ``config.json``.
    """

    model_content_sha256: Digest
    file_id: Identifier
    path: str = Field(min_length=1, max_length=512)
    sha256: Digest
    bytes: int = Field(ge=0, le=16 * 1024**4)
    roles: list[Identifier] = Field(min_length=1, max_length=32)
    distribution_object: DistributionObjectReceipt

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _safe_path(value, absolute=False)

    @field_validator("roles")
    @classmethod
    def roles_are_canonical(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("verified model object roles must be unique")
        return sorted(value)

    @model_validator(mode="after")
    def exact_distribution_object_is_bound(self) -> VerifiedModelObject:
        if self.distribution_object.kind != "model":
            raise ValueError("verified model object kind is invalid")
        if self.distribution_object.name != self.path:
            raise ValueError("verified model object name does not match its path")
        if not same_model_object(self.distribution_object, self):
            raise ValueError(
                "verified model object digest or bytes do not match its receipt"
            )
        if self.bytes == 0 and any(
            role.casefold() in _WEIGHT_ROLES for role in self.roles
        ):
            raise ValueError("only non-weight support artifacts may be empty")
        return self


class CompiledModelArtifact(_StrictModel):
    """One exact model file selected by the canonical runtime compiler."""

    id: Identifier
    selection_id: Identifier
    file_id: Identifier
    path: str = Field(min_length=1, max_length=512)
    sha256: Digest
    bytes: int = Field(ge=0, le=16 * 1024**4)
    roles: list[Identifier] = Field(min_length=1, max_length=32)
    mount: ExecutionMount
    # This is a generated absolute path containing the selection prefix and
    # the complete canonical file path, so it is bounded separately from the
    # public ModelFile.path ceiling.
    materialized_path: str = Field(min_length=1, max_length=1024)
    model: ModelCatalogIdentity

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _safe_path(value, absolute=False)

    @field_validator("materialized_path")
    @classmethod
    def materialized_path_is_safe(cls, value: str) -> str:
        value = _safe_path(value, absolute=True, max_length=1024)
        if not value.startswith("/run/vonk/models/"):
            raise ValueError("materialized model path must be Controller-owned")
        return value

    @field_validator("roles")
    @classmethod
    def roles_are_canonical(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("model artifact roles must be unique")
        return sorted(value)

    @model_validator(mode="after")
    def mount_is_bound(self) -> CompiledModelArtifact:
        if self.bytes == 0 and any(
            role.casefold() in _WEIGHT_ROLES for role in self.roles
        ):
            raise ValueError("only non-weight support artifacts may be empty")
        expected_source = f"/run/vonk/models/{self.selection_id}"
        if self.mount.source != expected_source:
            raise ValueError("model mount source must be the selection root")
        expected_materialized = f"{expected_source}/{self.path}"
        if self.materialized_path != expected_materialized:
            raise ValueError(
                "materialized path must preserve the original model file path"
            )
        return self


class CompiledRuntimeImage(_StrictModel):
    """The exact Controller-built linux/arm64 OCI archive given to each Spark."""

    image_digest: ImageDigest
    oci_layout_sha256: Digest
    image_bytes: int = Field(ge=1, le=16 * 1024**4)
    build_id: str = Field(min_length=1, max_length=128)
    # The imported OCI config differs from the image (manifest) digest.
    local_image_config_id: ImageDigest
    runtime_interface_label: str = Field(min_length=1, max_length=128)


class CompiledExecutionPlan(_StrictModel):
    """Internal verified execution plan consumed by distribution/install."""

    recipe_revision_sha256: Digest
    harness_sha256: Digest
    execution_sha256: Digest
    model_artifact_set_sha256: Digest
    model_artifact_set_bytes: int = Field(ge=0, le=16 * 1024**4)
    artifacts: list[CompiledModelArtifact] = Field(min_length=1, max_length=4096)
    runtime_image: CompiledRuntimeImage

    @model_validator(mode="after")
    def artifact_set_bytes_are_exact(self) -> CompiledExecutionPlan:
        by_digest: dict[str, int] = {}
        by_physical: dict[tuple[str, str], tuple[object, ...]] = {}
        selected: dict[tuple[str, str], str] = {}
        projection_ids: set[str] = set()
        mount_paths: set[tuple[str, str]] = set()
        for artifact in self.artifacts:
            if artifact.id in projection_ids:
                raise ValueError("compiled model artifact projections must be unique")
            projection_ids.add(artifact.id)
            physical = (
                artifact.file_id,
                artifact.sha256,
                artifact.bytes,
                artifact.model.publisher,
                artifact.model.slug,
                artifact.model.content_sha256,
            )
            physical_key = (artifact.selection_id, artifact.path)
            previous = by_physical.get(physical_key)
            if previous is not None and previous != physical:
                raise ValueError("compiled model artifact physical identity conflicts")
            by_physical[physical_key] = physical
            selected_key = (artifact.selection_id, artifact.file_id)
            previous_path = selected.get(selected_key)
            if previous_path is not None and previous_path != artifact.path:
                raise ValueError("compiled model artifact file identity conflicts")
            selected[selected_key] = artifact.path
            previous = by_digest.setdefault(artifact.sha256, artifact.bytes)
            if previous != artifact.bytes:
                raise ValueError("one model digest cannot have multiple byte counts")
            mount_path = (artifact.mount.target, artifact.path)
            if mount_path in mount_paths:
                raise ValueError("compiled model artifacts repeat a mount target")
            mount_paths.add(mount_path)
        if sum(by_digest.values()) != self.model_artifact_set_bytes:
            raise ValueError("model artifact-set bytes do not match selected receipts")
        return self

    def to_compiled_launch_payload(
        self,
        runtime_spec: RuntimeSpec,
        *,
        placement: CompiledPlacement,
    ) -> WireCompiledExecutionPlan:
        """Project this receipt-bound plan into the agent launch plan.

        ``CompiledExecutionPlan`` is deliberately the small receipt model used
        by the Controller's cache and build boundaries.  Agents need that
        evidence together with the final, already compiled launch facts.  This
        method is the only production projection into that wire shape: it
        carries no repository, source revision, credential, or retired cache
        authority and keeps selection-scoped model paths intact.
        """

        spec = runtime_spec
        if (spec.endpoint is None) == (spec.job is None):
            raise CompiledExecutionPlanError(
                "compiled launch plan must contain exactly one endpoint or job"
            )
        if not spec.runtime.entrypoint:
            raise CompiledExecutionPlanError(
                "compiled runtime executable and argv are invalid"
            )
        mounts: list[CompiledSecurityMount] = []
        for mount in spec.security.mounts:
            source = mount.source
            if source == "/run/vonk/models" or source.startswith("/run/vonk/models/"):
                kind: Literal["model", "inputs", "outputs"] = "model"
            elif source == "/run/vonk/inputs":
                kind = "inputs"
            elif source == "/run/vonk/outputs":
                kind = "outputs"
            else:
                raise CompiledExecutionPlanError(
                    "compiled security mount is not Controller-owned"
                )
            try:
                mounts.append(CompiledSecurityMount(source=kind, target=mount.target))
            except ValueError as error:
                raise CompiledExecutionPlanError(str(error)) from error
        if spec.security.network_mode not in {"none", "bridge"}:
            raise CompiledExecutionPlanError(
                "compiled security has an unsupported network mode"
            )
        network_mode: Literal["none", "bridge"] = (
            "bridge"
            if placement.endpoint_address is not None
            or placement.master_port is not None
            else "none"
        )
        try:
            return WireCompiledExecutionPlan(
                identity=CompiledIdentity(
                    recipe_revision_sha256=self.recipe_revision_sha256,
                    model_artifact_set_sha256=self.model_artifact_set_sha256,
                ),
                runtime=CompiledRuntime(
                    executable=spec.runtime.entrypoint[0],
                    argv=list(spec.runtime.entrypoint[1:]),
                    env=[
                        CompiledEnvironmentEntry(name=item.name, value=item.value)
                        for item in spec.runtime.environment
                    ],
                    placement=placement,
                ),
                artifacts=[
                    CompiledArtifact(
                        selection_id=item.selection_id,
                        file_id=item.file_id,
                        path=item.path,
                        sha256=item.sha256,
                        size_bytes=item.bytes,
                        roles=list(item.roles),
                        mount=CompiledArtifactMount(target=item.mount.target),
                        model=CompiledModelIdentity(
                            publisher=item.model.publisher,
                            slug=item.model.slug,
                            content_sha256=item.model.content_sha256,
                        ),
                    )
                    for item in self.artifacts
                ],
                runtime_image=WireCompiledRuntimeImage.model_validate(
                    self.runtime_image.model_dump(mode="json")
                ),
                security=CompiledSecurity(
                    gpu=spec.security.gpu,
                    network_mode=network_mode,
                    user=spec.security.user,
                    mounts=mounts,
                ),
                topology=CompiledTopology(
                    name=spec.topology.name, node_count=spec.topology.node_count
                ),
                lifecycle=CompiledLifecycle(
                    stop_timeout_seconds=spec.lifecycle.stop_timeout_seconds
                ),
                endpoint=(
                    None
                    if spec.endpoint is None
                    else CompiledEndpoint.model_validate(
                        spec.endpoint.model_dump(mode="json")
                    )
                ),
                job=(
                    None
                    if spec.job is None
                    else CompiledJob.model_validate(spec.job.model_dump(mode="json"))
                ),
            )
        except ValueError as error:
            raise CompiledExecutionPlanError(str(error)) from error


def _digest(value: object, label: str, *, image: bool = False) -> str:
    if not isinstance(value, str):
        raise CompiledExecutionPlanError(f"{label} is missing")
    expected = r"^sha256:[0-9a-f]{64}$" if image else r"^[0-9a-f]{64}$"
    if re.fullmatch(expected, value) is None:
        raise CompiledExecutionPlanError(f"{label} is invalid")
    return value


def _verified_model_object(value: object) -> VerifiedModelObject:
    if isinstance(value, VerifiedModelObject):
        return value
    try:
        return VerifiedModelObject.model_validate(value)
    except Exception as error:
        raise CompiledExecutionPlanError(
            "verified model object receipt is invalid"
        ) from error


def materialized_model_path(
    models_root: Path | str, artifact: CompiledModelArtifact
) -> Path:
    """Resolve the original model file path below a selected models root."""

    root = Path(models_root)
    if not root.is_absolute():
        raise ValueError("model materialization root must be absolute")
    result = root / artifact.selection_id / artifact.path
    try:
        result.relative_to(root)
    except ValueError as error:
        raise ValueError("materialized model path escapes the selected root") from error
    return result


def compile_verified_execution_plan(
    runtime_spec: RuntimeSpec,
    *,
    model_artifact_set_sha256: str,
    model_objects: Sequence[VerifiedModelObject],
    runtime_image: CompiledRuntimeImage,
) -> CompiledExecutionPlan:
    """Bind canonical compiler output to verified cache/build receipts.

    ``model_objects`` must be the complete selected model object sequence for
    this compiled execution scope, as returned by the model-cache/distribution
    authority.  The function accepts no upstream source handle: repository,
    revision and credential fields cannot enter the resulting payload.  The
    artifact-set digest is supplied separately by that authority; it is never
    recomputed from this input list.
    """

    spec = runtime_spec
    artifact_set_sha256 = _digest(
        model_artifact_set_sha256, "model artifact-set digest"
    )
    recipe_revision_sha256 = _digest(
        spec.identity.recipe_revision_sha256, "recipe revision digest"
    )
    harness_sha256 = _digest(spec.identity.harness_sha256, "harness digest")
    execution_sha256 = spec.launch_identity_sha256()
    declared_execution_sha256 = spec.identity.execution_sha256
    if (
        declared_execution_sha256 is not None
        and declared_execution_sha256 != execution_sha256
    ):
        raise CompiledExecutionPlanError(
            "runtime execution identity does not cover the compiled launch facts"
        )
    if not spec.artifacts:
        raise CompiledExecutionPlanError("runtime spec has no selected model artifacts")
    if not model_objects:
        raise CompiledExecutionPlanError("verified model object sequence is empty")
    by_identity: dict[tuple[str, str], VerifiedModelObject] = {}
    for item in model_objects:
        key = (item.model_content_sha256, item.file_id)
        if key in by_identity:
            raise CompiledExecutionPlanError(
                "verified model objects repeat a model file identity"
            )
        by_identity[key] = item

    artifacts: list[CompiledModelArtifact] = []
    selected_keys: set[tuple[str, str]] = set()
    selected_physical: dict[tuple[str, str], tuple[object, ...]] = {}
    for item in spec.artifacts:
        model = item.model
        source = by_identity.get((model.content_sha256, item.file_id))
        if source is None:
            raise CompiledExecutionPlanError(
                "runtime model artifact is not covered by the verified cache objects"
            )
        if (
            item.path != source.path
            or item.sha256 != source.sha256
            or item.bytes != source.bytes
        ):
            raise CompiledExecutionPlanError(
                "runtime model file path, digest or size does not match the verified cache object"
            )
        physical = (
            model.content_sha256,
            item.file_id,
            item.path,
            source.sha256,
            source.bytes,
            model.publisher,
            model.slug,
        )
        physical_key = (item.selection_id, item.path)
        previous_physical = selected_physical.get(physical_key)
        if previous_physical is not None and previous_physical != physical:
            raise CompiledExecutionPlanError(
                "runtime model artifact physical identity conflicts"
            )
        selected_physical[physical_key] = physical
        selected_keys.add((model.content_sha256, item.file_id))
        try:
            artifacts.append(
                CompiledModelArtifact(
                    id=item.id,
                    selection_id=item.selection_id,
                    file_id=item.file_id,
                    path=item.path,
                    sha256=source.sha256,
                    bytes=source.bytes,
                    roles=list(item.roles),
                    mount=ExecutionMount(
                        source=item.mount.source, target=item.mount.target
                    ),
                    materialized_path=f"/run/vonk/models/{item.selection_id}/{item.path}",
                    model=ModelCatalogIdentity(
                        publisher=model.publisher,
                        slug=model.slug,
                        content_sha256=model.content_sha256,
                    ),
                )
            )
        except ValueError as error:
            raise CompiledExecutionPlanError(
                "canonical runtime artifact cannot bind the verified model object"
            ) from error
    if selected_keys != set(by_identity):
        raise CompiledExecutionPlanError(
            "verified cache objects do not exactly cover the selected model files"
        )

    if not spec.runtime.image.endswith(f"@{runtime_image.image_digest}"):
        raise CompiledExecutionPlanError(
            "verified runtime image does not match the compiled runtime projection"
        )
    try:
        return CompiledExecutionPlan(
            recipe_revision_sha256=recipe_revision_sha256,
            harness_sha256=harness_sha256,
            execution_sha256=execution_sha256,
            model_artifact_set_sha256=artifact_set_sha256,
            model_artifact_set_bytes=sum(
                {item.sha256: item.bytes for item in model_objects}.values()
            ),
            artifacts=artifacts,
            runtime_image=runtime_image,
        )
    except ValueError as error:
        raise CompiledExecutionPlanError(
            "verified execution plan receipts are inconsistent"
        ) from error


def validate_compiled_launch_payload(value: object) -> WireCompiledExecutionPlan:
    """Enforce canonical schema/security validation and the transport ceiling."""
    from vonk_agent_protocol import validate_compiled_execution_plan

    try:
        encoded = canonical_message(value)
    except ValueError as error:
        raise CompiledExecutionPlanError("compiled launch plan is not JSON") from error
    if len(encoded) > MAX_COMPILED_EXECUTION_PLAN_BYTES:
        raise CompiledExecutionPlanError("compiled launch plan is too large")
    try:
        # This calls CompiledExecutionPlan.model_validate, including all nested
        # schema, identity, path, mount, runtime and security validators.
        return validate_compiled_execution_plan(value)
    except ValueError as error:
        raise CompiledExecutionPlanError(str(error)) from error


__all__ = [
    "EMPTY_SHA256",
    "CompiledExecutionPlan",
    "CompiledExecutionPlanError",
    "CompiledModelArtifact",
    "CompiledRuntimeImage",
    "DistributionObjectReceipt",
    "ExecutionMount",
    "ModelCatalogIdentity",
    "VerifiedModelObject",
    "compile_verified_execution_plan",
    "materialized_model_path",
    "validate_compiled_launch_payload",
]
