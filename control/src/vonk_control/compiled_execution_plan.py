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
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
]
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_WEIGHT_ROLES = frozenset({"model", "weight", "weights"})


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
    path alone is insufficient because different model versions legitimately
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
        if self.distribution_object.sha256 != self.sha256:
            raise ValueError("verified model object digest does not match its receipt")
        if self.distribution_object.bytes != self.bytes:
            raise ValueError("verified model object bytes do not match its receipt")
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
    materialized_path: str = Field(min_length=1, max_length=512)
    model: ModelCatalogIdentity
    distribution_object: DistributionObjectReceipt

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return _safe_path(value, absolute=False)

    @field_validator("materialized_path")
    @classmethod
    def materialized_path_is_safe(cls, value: str) -> str:
        value = _safe_path(value, absolute=True)
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
        if self.bytes == 0 and any(
            role.casefold() in _WEIGHT_ROLES for role in self.roles
        ):
            raise ValueError("only non-weight support artifacts may be empty")
        expected_source = f"/run/vonk/models/{self.selection_id}"
        if self.mount.source != expected_source:
            raise ValueError("model mount source must be the selection root")
        if not self.mount.read_only:
            raise ValueError("model mounts must be read-only")
        expected_materialized = f"{expected_source}/{self.path}"
        if self.materialized_path != expected_materialized:
            raise ValueError(
                "materialized path must preserve the original model file path"
            )
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
    model_artifact_set_bytes: int = Field(ge=0, le=16 * 1024**4)
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
        paths = [(item.selection_id, item.path) for item in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("compiled model artifacts repeat a materialized path")
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
                    "materialized_path": item.materialized_path,
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


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def execution_identity_document(
    runtime_spec: Mapping[str, object],
) -> dict[str, object]:
    """Project every compiled launch-affecting fact into one identity.

    The recipe revision remains provenance on ``CompiledExecutionPlan``.  This
    projection instead covers the final runtime command, bound settings,
    environment, security, mounts, lifecycle, interface, topology and exact
    selected model files.  Editorial fields outside this projection do not
    change the execution identity.
    """

    spec = _mapping(runtime_spec, "runtime spec")
    identity = _mapping(spec.get("identity"), "runtime identity")
    artifacts = spec.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        raise CompiledExecutionPlanError("runtime spec model artifacts are missing")
    selected: list[dict[str, object]] = []
    for raw in artifacts:
        item = _mapping(raw, "runtime model artifact")
        model = _mapping(item.get("model"), "runtime model identity")
        selected.append(
            {
                "selection_id": item.get("selection_id"),
                "file_id": item.get("file_id"),
                "path": item.get("path"),
                "sha256": item.get("sha256"),
                "bytes": item.get("bytes"),
                "roles": item.get("roles"),
                "mount": item.get("mount"),
                "model": {
                    "publisher": model.get("publisher"),
                    "slug": model.get("slug"),
                    "content_sha256": model.get("content_sha256"),
                },
            }
        )
    selected.sort(
        key=lambda item: (
            str(item["selection_id"]),
            str(item["file_id"]),
            str(item["path"]),
        )
    )
    raw_dependencies = spec.get("model_dependencies", ())
    if not isinstance(raw_dependencies, Sequence) or isinstance(
        raw_dependencies, (str, bytes)
    ):
        raise CompiledExecutionPlanError("runtime model dependencies are invalid")
    dependencies: list[dict[str, object]] = []
    for raw in raw_dependencies:
        dependency = _mapping(raw, "runtime model dependency")
        dependencies.append(
            {
                "selection_id": dependency.get("selection_id"),
                "publisher": dependency.get("publisher"),
                "slug": dependency.get("slug"),
                "content_sha256": dependency.get("content_sha256"),
            }
        )
    dependencies.sort(key=lambda item: str(item["selection_id"]))
    return {
        "schema_version": 2,
        "harness_sha256": identity.get("harness_sha256"),
        "runtime": spec.get("runtime"),
        "security": spec.get("security"),
        "lifecycle": spec.get("lifecycle"),
        "topology": spec.get("topology"),
        "endpoint": spec.get("endpoint"),
        "job": spec.get("job"),
        "model_dependencies": dependencies,
        "artifacts": selected,
    }


def execution_identity_sha256(runtime_spec: Mapping[str, object]) -> str:
    """Return the canonical full launch identity, excluding editorial notes."""

    return _canonical_digest(execution_identity_document(runtime_spec))


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
    execution_sha256 = execution_identity_sha256(spec)
    declared_execution_sha256 = identity.get("execution_sha256")
    if (
        declared_execution_sha256 is not None
        and declared_execution_sha256 != execution_sha256
    ):
        raise CompiledExecutionPlanError(
            "runtime execution identity does not cover the compiled launch facts"
        )
    raw_artifacts = spec.get("artifacts")
    if not isinstance(raw_artifacts, Sequence) or isinstance(
        raw_artifacts, (str, bytes)
    ):
        raise CompiledExecutionPlanError("runtime spec model artifacts are missing")
    if not raw_artifacts:
        raise CompiledExecutionPlanError("runtime spec has no selected model artifacts")

    verified_objects = tuple(_verified_model_object(value) for value in model_objects)
    if not verified_objects:
        raise CompiledExecutionPlanError("verified model object sequence is empty")
    by_identity: dict[tuple[str, str], VerifiedModelObject] = {}
    for item in verified_objects:
        key = (item.model_content_sha256, item.file_id)
        if key in by_identity:
            raise CompiledExecutionPlanError(
                "verified model objects repeat a model file identity"
            )
        by_identity[key] = item

    artifacts: list[CompiledModelArtifact] = []
    selected_keys: set[tuple[str, str]] = set()
    for raw in raw_artifacts:
        item = _mapping(raw, "runtime model artifact")
        allowed = {
            "id",
            "selection_id",
            "file_id",
            "path",
            "sha256",
            "bytes",
            "roles",
            "mount",
            "model",
        }
        if set(item) != allowed:
            raise CompiledExecutionPlanError(
                "runtime model artifact contains retired or unknown authority"
            )
        model = _mapping(item.get("model"), "runtime model identity")
        if set(model) != {"publisher", "slug", "content_sha256"}:
            raise CompiledExecutionPlanError(
                "runtime model identity contains upstream authority"
            )
        model_identity = model.get("content_sha256")
        file_id = item.get("file_id")
        if not isinstance(model_identity, str) or not isinstance(file_id, str):
            raise CompiledExecutionPlanError(
                "runtime model artifact identity is incomplete"
            )
        source = by_identity.get((model_identity, file_id))
        if source is None:
            raise CompiledExecutionPlanError(
                "runtime model artifact is not covered by the verified cache objects"
            )
        path = item.get("path")
        if (
            path != source.path
            or item.get("sha256") != source.sha256
            or item.get("bytes") != source.bytes
        ):
            raise CompiledExecutionPlanError(
                "runtime model file path, digest or size does not match the verified cache object"
            )
        selection_id = item.get("selection_id")
        if not isinstance(selection_id, str):
            raise CompiledExecutionPlanError(
                "runtime model selection identity is invalid"
            )
        selected_keys.add((model_identity, file_id))
        artifact_data = {
            "id": item.get("id"),
            "selection_id": selection_id,
            "file_id": file_id,
            "path": path,
            "sha256": source.sha256,
            "bytes": source.bytes,
            "roles": item.get("roles"),
            "mount": item.get("mount"),
            "materialized_path": f"/run/vonk/models/{selection_id}/{path}",
            "model": model,
            "distribution_object": source.distribution_object.model_dump(mode="json"),
        }
        try:
            artifacts.append(CompiledModelArtifact.model_validate(artifact_data))
        except Exception as error:
            raise CompiledExecutionPlanError(
                "canonical runtime artifact cannot bind the verified model object"
            ) from error
    if selected_keys != set(by_identity):
        raise CompiledExecutionPlanError(
            "verified cache objects do not exactly cover the selected model files"
        )

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
    "execution_identity_document",
    "execution_identity_sha256",
    "materialized_model_path",
]
