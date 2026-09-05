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

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from vonk_agent_protocol import DistributionObject

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
]


class CompiledExecutionPlanError(ValueError):
    """The canonical runtime and verified delivery receipts cannot be bound."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _safe_path(value: str, *, absolute: bool) -> str:
    parts = (
        value[1:].split("/") if absolute and value.startswith("/") else value.split("/")
    )
    if (
        not value
        or len(value) > 512
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in parts)
        or (absolute and not value.startswith("/"))
        or (not absolute and value.startswith("/"))
    ):
        raise ValueError("path is not a safe Controller runtime path")
    return value


class ExecutionMount(_StrictModel):
    """The platform-owned mount used by one selected model file."""

    source: str = Field(min_length=1, max_length=512)
    target: str = Field(min_length=1, max_length=512)
    read_only: bool

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

    publisher: Identifier
    slug: Identifier
    content_sha256: Digest


class DistributionObjectReceipt(_StrictModel):
    """A verified immutable object served by the Controller."""

    name: str = Field(min_length=1, max_length=512)
    sha256: Digest
    bytes: int = Field(ge=1, le=16 * 1024**4)
    kind: Literal["model", "oci-archive"]

    @field_validator("name")
    @classmethod
    def name_is_safe(cls, value: str) -> str:
        return _safe_path(value, absolute=False)


class CompiledModelArtifact(_StrictModel):
    """One exact model file selected by the canonical runtime compiler."""

    id: Identifier
    selection_id: Identifier
    file_id: Identifier
    path: str = Field(min_length=1, max_length=512)
    sha256: Digest
    bytes: int = Field(ge=1, le=16 * 1024**4)
    roles: list[Identifier] = Field(min_length=1, max_length=32)
    mount: ExecutionMount
    model: ModelCatalogIdentity
    distribution_object: DistributionObjectReceipt

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _safe_path(value, absolute=False)

    @field_validator("roles")
    @classmethod
    def roles_are_canonical(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("model artifact roles must be unique")
        return sorted(value)

    @model_validator(mode="after")
    def exact_object_and_mount_are_bound(self) -> CompiledModelArtifact:
        if self.distribution_object.kind != "model":
            raise ValueError(
                "model artifact must reference a model distribution object"
            )
        if self.distribution_object.name != self.path:
            raise ValueError(
                "model distribution object name does not match selected path"
            )
        if self.distribution_object.sha256 != self.sha256:
            raise ValueError(
                "model distribution object digest does not match selected file"
            )
        if self.distribution_object.bytes != self.bytes:
            raise ValueError(
                "model distribution object bytes do not match selected file"
            )
        expected_source = f"/run/vonk/models/{self.selection_id}/{self.file_id}"
        if self.mount.source != expected_source:
            raise ValueError("model mount source does not match the selected file")
        if not self.mount.read_only:
            raise ValueError("model mounts must be read-only")
        return self


class CompiledRuntimeImage(_StrictModel):
    """The exact OCI archive that the Controller gives to each Spark."""

    image_digest: ImageDigest
    oci_layout_sha256: Digest
    image_bytes: int = Field(ge=1, le=16 * 1024**4)
    architecture: Literal["linux-arm64"]
    runtime_interface: str = Field(min_length=1, max_length=128)
    source: Literal["published", "controller-build"]
    build_id: str | None = Field(default=None, min_length=1, max_length=128)
    distribution_object: DistributionObjectReceipt

    @model_validator(mode="after")
    def exact_archive_is_bound(self) -> CompiledRuntimeImage:
        if self.distribution_object.kind != "oci-archive":
            raise ValueError("runtime image must reference an OCI archive object")
        if self.distribution_object.name != "image.oci.tar":
            raise ValueError("runtime image distribution object name is not canonical")
        if self.distribution_object.sha256 != self.oci_layout_sha256:
            raise ValueError("OCI archive digest does not match the layout digest")
        if self.distribution_object.bytes != self.image_bytes:
            raise ValueError("OCI archive bytes do not match the image receipt")
        if self.source == "published" and self.build_id is not None:
            raise ValueError("published image receipts cannot claim a Controller build")
        if self.source == "controller-build" and self.build_id is None:
            raise ValueError("Controller-built image receipts require a build id")
        return self


class CompiledExecutionPlan(_StrictModel):
    """Internal verified execution plan consumed by distribution/install."""

    schema_version: Literal[2] = 2
    recipe_revision_sha256: Digest
    harness_sha256: Digest
    execution_sha256: Digest
    model_artifact_set_sha256: Digest
    model_artifact_set_bytes: int = Field(ge=1, le=16 * 1024**4)
    artifacts: list[CompiledModelArtifact] = Field(min_length=1, max_length=4096)
    runtime_image: CompiledRuntimeImage

    @model_validator(mode="after")
    def artifact_set_bytes_are_exact(self) -> CompiledExecutionPlan:
        by_digest: dict[str, int] = {}
        for artifact in self.artifacts:
            previous = by_digest.setdefault(artifact.sha256, artifact.bytes)
            if previous != artifact.bytes:
                raise ValueError("one model digest cannot have multiple byte counts")
        if sum(by_digest.values()) != self.model_artifact_set_bytes:
            raise ValueError("model artifact-set bytes do not match selected receipts")
        keys = [(item.selection_id, item.file_id) for item in self.artifacts]
        if len(keys) != len(set(keys)):
            raise ValueError("compiled model artifacts repeat a selected file")
        return self

    def reusable_identity_document(self) -> dict[str, object]:
        """Return the execution/cache identity without catalog provenance.

        Requested recipe/model document digests and build source labels are
        retained on the plan for authorization and evidence.  They are not
        reusable byte identities: unchanged files, mounts, settings and image
        bytes must remain reusable when editorial catalog facts change.
        """

        artifacts = []
        for item in sorted(
            self.artifacts,
            key=lambda value: (value.selection_id, value.file_id, value.path),
        ):
            artifacts.append(
                {
                    "selection_id": item.selection_id,
                    "file_id": item.file_id,
                    "path": item.path,
                    "sha256": item.sha256,
                    "bytes": item.bytes,
                    "roles": list(item.roles),
                    "mount": item.mount.model_dump(mode="json"),
                    "distribution_object": item.distribution_object.model_dump(
                        mode="json"
                    ),
                }
            )
        image = self.runtime_image
        return {
            "schema_version": self.schema_version,
            "harness_sha256": self.harness_sha256,
            "execution_sha256": self.execution_sha256,
            "model_artifact_set_sha256": self.model_artifact_set_sha256,
            "model_artifact_set_bytes": self.model_artifact_set_bytes,
            "artifacts": artifacts,
            "runtime_image": {
                "image_digest": image.image_digest,
                "oci_layout_sha256": image.oci_layout_sha256,
                "image_bytes": image.image_bytes,
                "architecture": image.architecture,
                "runtime_interface": image.runtime_interface,
                "distribution_object": image.distribution_object.model_dump(
                    mode="json"
                ),
            },
        }

    @property
    def reusable_identity_sha256(self) -> str:
        payload = json.dumps(
            self.reusable_identity_document(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_agent_payload(self) -> dict[str, object]:
        """Project only verified delivery and launch facts to the agent.

        In particular, ``recipe_revision_sha256``, ``source`` and ``build_id``
        are Controller evidence; upstream repository/revision/credential data
        is absent entirely.
        """

        return {
            "schema_version": self.schema_version,
            "identity": {
                "harness_sha256": self.harness_sha256,
                "execution_sha256": self.execution_sha256,
                "model_artifact_set_sha256": self.model_artifact_set_sha256,
                "model_artifact_set_bytes": self.model_artifact_set_bytes,
            },
            "artifacts": [item.model_dump(mode="json") for item in self.artifacts],
            "runtime_image": {
                "image_digest": self.runtime_image.image_digest,
                "oci_layout_sha256": self.runtime_image.oci_layout_sha256,
                "image_bytes": self.runtime_image.image_bytes,
                "architecture": self.runtime_image.architecture,
                "runtime_interface": self.runtime_image.runtime_interface,
                "distribution_object": self.runtime_image.distribution_object.model_dump(
                    mode="json"
                ),
            },
        }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CompiledExecutionPlanError(f"{label} must be a mapping")
    return value


def _digest(value: object, label: str, *, image: bool = False) -> str:
    if not isinstance(value, str):
        raise CompiledExecutionPlanError(f"{label} is missing")
    expected = r"^sha256:[0-9a-f]{64}$" if image else r"^[0-9a-f]{64}$"
    if re.fullmatch(expected, value) is None:
        raise CompiledExecutionPlanError(f"{label} is invalid")
    return value


def _distribution_object(value: object) -> DistributionObject:
    if isinstance(value, DistributionObject):
        result = value
    else:
        try:
            result = DistributionObject.parse(value)
        except Exception as error:
            raise CompiledExecutionPlanError(
                "verified distribution object is invalid"
            ) from error
    if result.kind != "model":
        raise CompiledExecutionPlanError("model receipt includes a non-model object")
    return result


def compile_verified_execution_plan(
    runtime_spec: Mapping[str, object],
    *,
    model_artifact_set_sha256: str,
    model_objects: Sequence[object],
    runtime_image: CompiledRuntimeImage | Mapping[str, object],
) -> CompiledExecutionPlan:
    """Bind canonical compiler output to verified cache/build receipts.

    ``model_objects`` must be the complete selected model object sequence for
    this compiled execution scope, as returned by the model-cache/distribution
    authority.  The function accepts no upstream source handle: repository,
    revision and credential fields cannot enter the resulting payload.  The
    artifact-set digest is supplied separately by that authority; it is never
    recomputed from this input list.
    """

    spec = _mapping(runtime_spec, "runtime spec")
    artifact_set_sha256 = _digest(
        model_artifact_set_sha256, "model artifact-set digest"
    )
    if (
        "model_artifact_set_sha256" in spec
        and spec["model_artifact_set_sha256"] != artifact_set_sha256
    ):
        raise CompiledExecutionPlanError(
            "runtime model artifact-set digest does not match the cache authority"
        )
    identity = _mapping(spec.get("identity"), "runtime identity")
    if {
        "model_version_sha256",
        "runtime_distribution_sha256",
        "patch_bundle_sha256",
    } & set(identity):
        raise CompiledExecutionPlanError("runtime identity contains retired authority")
    recipe_revision_sha256 = _digest(
        identity.get("recipe_revision_sha256"), "recipe revision digest"
    )
    harness_sha256 = _digest(identity.get("harness_sha256"), "harness digest")
    execution_sha256 = _digest(identity.get("execution_sha256"), "execution digest")
    raw_artifacts = spec.get("artifacts")
    if not isinstance(raw_artifacts, Sequence) or isinstance(
        raw_artifacts, (str, bytes)
    ):
        raise CompiledExecutionPlanError("runtime spec model artifacts are missing")
    if not raw_artifacts:
        raise CompiledExecutionPlanError("runtime spec has no selected model artifacts")

    verified_objects = tuple(_distribution_object(value) for value in model_objects)
    if not verified_objects:
        raise CompiledExecutionPlanError("verified model object sequence is empty")
    by_name: dict[str, DistributionObject] = {}
    for item in verified_objects:
        if item.name in by_name:
            raise CompiledExecutionPlanError("verified model objects repeat a name")
        by_name[item.name] = item

    artifacts: list[CompiledModelArtifact] = []
    for raw in raw_artifacts:
        item = _mapping(raw, "runtime model artifact")
        allowed = {"id", "selection_id", "file_id", "path", "roles", "mount", "model"}
        if set(item) != allowed:
            raise CompiledExecutionPlanError(
                "runtime model artifact contains retired or unknown authority"
            )
        model = _mapping(item.get("model"), "runtime model identity")
        if set(model) != {"publisher", "slug", "content_sha256"}:
            raise CompiledExecutionPlanError(
                "runtime model identity contains upstream authority"
            )
        path = item.get("path")
        if not isinstance(path, str) or path not in by_name:
            raise CompiledExecutionPlanError(
                "runtime model artifact is not covered by the verified cache objects"
            )
        source = by_name[path]
        artifact_data = {
            "id": item.get("id"),
            "selection_id": item.get("selection_id"),
            "file_id": item.get("file_id"),
            "path": path,
            "sha256": source.sha256,
            "bytes": source.bytes,
            "roles": item.get("roles"),
            "mount": item.get("mount"),
            "model": model,
            "distribution_object": source.to_mapping(),
        }
        try:
            artifacts.append(CompiledModelArtifact.model_validate(artifact_data))
        except Exception as error:
            raise CompiledExecutionPlanError(
                "canonical runtime artifact cannot bind the verified model object"
            ) from error

    try:
        image = (
            runtime_image
            if isinstance(runtime_image, CompiledRuntimeImage)
            else CompiledRuntimeImage.model_validate(runtime_image)
        )
    except Exception as error:
        raise CompiledExecutionPlanError(
            "verified runtime image cannot be bound to the execution plan"
        ) from error
    try:
        return CompiledExecutionPlan.model_validate(
            {
                "recipe_revision_sha256": recipe_revision_sha256,
                "harness_sha256": harness_sha256,
                "execution_sha256": execution_sha256,
                "model_artifact_set_sha256": artifact_set_sha256,
                "model_artifact_set_bytes": sum(
                    {item.sha256: item.bytes for item in verified_objects}.values()
                ),
                "artifacts": artifacts,
                "runtime_image": image,
            }
        )
    except Exception as error:
        raise CompiledExecutionPlanError(
            "verified execution plan receipts are inconsistent"
        ) from error


__all__ = [
    "CompiledExecutionPlan",
    "CompiledExecutionPlanError",
    "CompiledModelArtifact",
    "CompiledRuntimeImage",
    "DistributionObjectReceipt",
    "ExecutionMount",
    "ModelCatalogIdentity",
    "compile_verified_execution_plan",
]
