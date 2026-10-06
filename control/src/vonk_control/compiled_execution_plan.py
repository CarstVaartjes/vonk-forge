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
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from vonk_agent_protocol import (
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES,
    canonical_message,
)

from .content_identity import same_model_object
from .strict_json import StrictModel

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


class ExecutionMount(StrictModel):
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


class ModelCatalogIdentity(StrictModel):
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


class DistributionObjectReceipt(StrictModel):
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


class VerifiedModelObject(StrictModel):
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


class CompiledModelArtifact(StrictModel):
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


class VerifiedRuntimeImage(StrictModel):
    """The exact Controller-built linux/arm64 OCI archive given to each Spark."""

    image_digest: ImageDigest
    oci_layout_sha256: Digest
    image_bytes: int = Field(ge=1, le=16 * 1024**4)
    build_id: str = Field(min_length=1, max_length=128)
    # The imported OCI config differs from the image (manifest) digest.
    local_image_config_id: ImageDigest
    runtime_interface_label: str = Field(min_length=1, max_length=128)


class VerifiedExecutionPlan(StrictModel):
    """Internal verified execution plan consumed by distribution/install."""

    recipe_revision_sha256: Digest
    harness_sha256: Digest
    execution_sha256: Digest
    model_artifact_set_sha256: Digest
    model_artifact_set_bytes: int = Field(ge=0, le=16 * 1024**4)
    artifacts: list[CompiledModelArtifact] = Field(min_length=1, max_length=4096)
    runtime_image: VerifiedRuntimeImage

    @model_validator(mode="after")
    def artifact_set_bytes_are_exact(self) -> VerifiedExecutionPlan:
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
        runtime_spec: Mapping[str, object],
        *,
        placement: Mapping[str, object],
    ) -> dict[str, object]:
        """Project this receipt-bound plan into the agent launch DTO.

        ``VerifiedExecutionPlan`` is deliberately the small receipt model used
        by the Controller's cache and build boundaries.  Agents need that
        evidence together with the final, already compiled launch facts.  This
        method is the only production projection into that wire shape: it
        carries no repository, source revision, credential, or retired cache
        authority and keeps selection-scoped model paths intact.
        """

        spec = _mapping(runtime_spec, "runtime spec")
        runtime = _mapping(spec.get("runtime"), "runtime")
        security = _mapping(spec.get("security"), "security")
        lifecycle = _mapping(spec.get("lifecycle"), "lifecycle")
        topology = _mapping(spec.get("topology"), "topology")
        endpoint = spec.get("endpoint")
        job = spec.get("job")
        if (endpoint is None) == (job is None):
            raise CompiledExecutionPlanError(
                "compiled launch plan must contain exactly one endpoint or job"
            )

        raw_entrypoint = runtime.get("entrypoint")
        if (
            not isinstance(raw_entrypoint, Sequence)
            or isinstance(raw_entrypoint, (str, bytes))
            or not raw_entrypoint
            or any(type(item) is not str for item in raw_entrypoint)
        ):
            raise CompiledExecutionPlanError(
                "compiled runtime executable and argv are invalid"
            )
        executable = str(raw_entrypoint[0])
        argv = [str(item) for item in raw_entrypoint[1:]]
        raw_environment = runtime.get("environment", ())
        if not isinstance(raw_environment, Sequence) or isinstance(
            raw_environment, (str, bytes)
        ):
            raise CompiledExecutionPlanError("compiled runtime environment is invalid")
        environment: list[dict[str, str]] = []
        for raw in raw_environment:
            value = _mapping(raw, "compiled runtime environment entry")
            name = value.get("name")
            rendered = value.get("value")
            if type(name) is not str or type(rendered) is not str:
                raise CompiledExecutionPlanError(
                    "compiled runtime environment is invalid"
                )
            environment.append({"name": name, "value": rendered})

        raw_mounts = security.get("mounts", ())
        if not isinstance(raw_mounts, Sequence) or isinstance(raw_mounts, (str, bytes)):
            raise CompiledExecutionPlanError("compiled security mounts are invalid")
        mounts: list[dict[str, object]] = []
        for raw in raw_mounts:
            mount = _mapping(raw, "compiled security mount")
            source = mount.get("source")
            target = mount.get("target")
            if type(source) is not str or type(target) is not str:
                raise CompiledExecutionPlanError("compiled security mounts are invalid")
            if source == "/run/vonk/models" or source.startswith("/run/vonk/models/"):
                source = "model"
            elif source == "/run/vonk/inputs":
                source = "inputs"
            elif source == "/run/vonk/outputs":
                source = "outputs"
            else:
                raise CompiledExecutionPlanError(
                    "compiled security mount is not Controller-owned"
                )
            mounts.append({"source": source, "target": target})

        def _required_int(value: object, label: str, *, minimum: int = 0) -> int:
            if type(value) is not int or value < minimum:
                raise CompiledExecutionPlanError(f"{label} is invalid")
            return value

        if "port" not in placement:
            raise CompiledExecutionPlanError("runtime port is missing")
        raw_port = placement["port"]
        placement_doc = {
            "endpoint_address": placement.get("endpoint_address"),
            "rank": _required_int(placement.get("rank"), "runtime rank"),
            "role": placement.get("role"),
            "world_size": _required_int(
                placement.get("world_size"),
                "runtime world size",
                minimum=1,
            ),
            "local_address": placement.get("local_address"),
            "master_address": placement.get("master_address"),
            "master_port": placement.get("master_port"),
            "port": (
                None
                if raw_port is None
                else _required_int(raw_port, "runtime port", minimum=1)
            ),
            "reserved_memory_bytes": _required_int(
                placement.get("reserved_memory_bytes"),
                "runtime reserved memory",
                minimum=1,
            ),
            "memory_floor_bytes": _required_int(
                placement.get("memory_floor_bytes"),
                "runtime memory floor",
            ),
        }
        if type(placement_doc["role"]) is not str or not placement_doc["role"]:
            raise CompiledExecutionPlanError("runtime role is invalid")

        if security.get("network_mode") not in {"none", "bridge"}:
            raise CompiledExecutionPlanError(
                "compiled security has an unsupported network mode"
            )
        network_mode = (
            "bridge"
            if placement_doc["endpoint_address"] is not None
            or placement_doc["master_port"] is not None
            else "none"
        )

        artifacts = [
            {
                "selection_id": item.selection_id,
                "file_id": item.file_id,
                "path": item.path,
                "sha256": item.sha256,
                "size_bytes": item.bytes,
                "roles": list(item.roles),
                "mount": {"target": item.mount.target},
                "model": item.model.model_dump(mode="json"),
            }
            for item in self.artifacts
        ]
        payload: dict[str, object] = {
            "identity": {
                "recipe_revision_sha256": self.recipe_revision_sha256,
                "model_artifact_set_sha256": self.model_artifact_set_sha256,
            },
            "runtime": {
                "executable": executable,
                "argv": argv,
                "env": environment,
                "placement": placement_doc,
            },
            "artifacts": artifacts,
            "runtime_image": self.runtime_image.model_dump(mode="json"),
            "security": {
                "gpu": security.get("gpu"),
                "network_mode": network_mode,
                "user": security.get("user"),
                "mounts": mounts,
            },
            "topology": {
                "name": topology.get("name"),
                "node_count": topology.get("node_count"),
            },
            "lifecycle": {
                "stop_timeout_seconds": lifecycle.get("stop_timeout_seconds"),
            },
        }
        # Keep both mutually exclusive interface keys on the wire.  Rust and
        # the privileged helper validate the schema by shape, so omitting the
        # inactive branch would make a semantically valid endpoint payload
        # ambiguous after a round trip through persisted JSON.
        payload["endpoint"] = (
            dict(_mapping(endpoint, "endpoint")) if endpoint is not None else None
        )
        payload["job"] = dict(_mapping(job, "job")) if job is not None else None
        # The projected document is persisted and later served by the agent
        # route.  Validate it at this producer boundary so the stored payload
        # is the same canonical schema consumed by agents.
        return validate_compiled_launch_payload(payload)


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

    The recipe revision remains provenance on ``VerifiedExecutionPlan``.  This
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
    runtime_image: VerifiedRuntimeImage | Mapping[str, object],
) -> VerifiedExecutionPlan:
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
    selected_physical: dict[tuple[str, str], tuple[object, ...]] = {}
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
            not isinstance(path, str)
            or path != source.path
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
        physical = (
            model_identity,
            file_id,
            path,
            source.sha256,
            source.bytes,
            model.get("publisher"),
            model.get("slug"),
        )
        physical_key = (selection_id, path)
        previous_physical = selected_physical.get(physical_key)
        if previous_physical is not None and previous_physical != physical:
            raise CompiledExecutionPlanError(
                "runtime model artifact physical identity conflicts"
            )
        selected_physical[physical_key] = physical
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
            if isinstance(runtime_image, VerifiedRuntimeImage)
            else VerifiedRuntimeImage.model_validate(runtime_image)
        )
    except Exception as error:
        raise CompiledExecutionPlanError(
            "verified runtime image cannot be bound to the execution plan"
        ) from error
    runtime = _mapping(spec.get("runtime"), "runtime")
    runtime_image_reference = runtime.get("image")
    if not isinstance(
        runtime_image_reference, str
    ) or not runtime_image_reference.endswith(f"@{image.image_digest}"):
        raise CompiledExecutionPlanError(
            "verified runtime image does not match the compiled runtime projection"
        )
    try:
        return VerifiedExecutionPlan.model_validate(
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


def validate_compiled_launch_payload(value: object) -> dict[str, object]:
    """Enforce canonical schema/security validation and the transport ceiling."""
    from vonk_agent_protocol import validate_compiled_execution_plan

    payload = _mapping(value, "compiled launch plan")
    try:
        encoded = canonical_message(payload)
    except ValueError as error:
        raise CompiledExecutionPlanError("compiled launch plan is not JSON") from error
    if len(encoded) > MAX_COMPILED_EXECUTION_PLAN_BYTES:
        raise CompiledExecutionPlanError("compiled launch plan is too large")
    try:
        # This calls VerifiedExecutionPlan.model_validate, including all nested
        # schema, identity, path, mount, runtime and security validators.
        return validate_compiled_execution_plan(payload)
    except ValueError as error:
        raise CompiledExecutionPlanError(str(error)) from error


__all__ = [
    "EMPTY_SHA256",
    "CompiledExecutionPlanError",
    "CompiledModelArtifact",
    "DistributionObjectReceipt",
    "ExecutionMount",
    "ModelCatalogIdentity",
    "VerifiedExecutionPlan",
    "VerifiedModelObject",
    "VerifiedRuntimeImage",
    "compile_verified_execution_plan",
    "execution_identity_document",
    "execution_identity_sha256",
    "materialized_model_path",
    "validate_compiled_launch_payload",
]
