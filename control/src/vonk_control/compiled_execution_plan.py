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
    # These identities are intentionally distinct.  A multi-platform
    # registry manifest, the selected linux-arm64 child manifest, the
    # imported OCI config and the Controller archive are different objects.
    registry_manifest_digest: ImageDigest | None = None
    platform_manifest_digest: ImageDigest
    local_image_config_id: ImageDigest
    local_image_reference: str | None = Field(default=None, min_length=1, max_length=512)
    runtime_interface_label: str = Field(min_length=1, max_length=128)

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
        if self.source == "published" and self.registry_manifest_digest is None:
            raise ValueError("published image receipts require a registry manifest")
        if self.source == "controller-build" and self.build_id is None:
            raise ValueError("Controller-built image receipts require a build id")
        if self.source == "controller-build" and self.registry_manifest_digest is not None:
            raise ValueError("Controller-built image receipts cannot claim a registry manifest")
        if self.platform_manifest_digest != self.image_digest:
            raise ValueError(
                "runtime image digest must identify the selected platform manifest"
            )
        parent = self.platform_manifest_digest
        expected_reference = (
            f"localhost/vonk/compiled-runtime-{self.oci_layout_sha256}@{parent}"
        )
        if self.local_image_reference not in (None, expected_reference):
            raise ValueError("runtime local image reference is not bound to its receipt")
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
                "registry_manifest_digest": image.registry_manifest_digest,
                "platform_manifest_digest": image.platform_manifest_digest,
                "local_image_config_id": image.local_image_config_id,
                "local_image_reference": image.local_image_reference,
                "runtime_interface_label": image.runtime_interface_label,
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
                "registry_manifest_digest": self.runtime_image.registry_manifest_digest,
                "platform_manifest_digest": self.runtime_image.platform_manifest_digest,
                "local_image_config_id": self.runtime_image.local_image_config_id,
                "local_image_reference": self.runtime_image.local_image_reference,
                "runtime_interface_label": self.runtime_image.runtime_interface_label,
                "distribution_object": self.runtime_image.distribution_object.model_dump(
                    mode="json"
                ),
            },
        }

    def to_compiled_launch_payload(
        self,
        runtime_spec: Mapping[str, object],
        *,
        placement: Mapping[str, object],
    ) -> dict[str, object]:
        """Project this receipt-bound plan into the agent launch DTO.

        ``CompiledExecutionPlan`` is deliberately the small receipt model used
        by the Controller's cache and build boundaries.  Agents need that
        evidence together with the final, already compiled launch facts.  This
        method is the only production projection into that wire shape: it
        carries no repository, source revision, credential, or retired cache
        authority and keeps selection-scoped model paths intact.
        """

        spec = _mapping(runtime_spec, "runtime spec")
        identity = _mapping(spec.get("identity"), "runtime identity")
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
        if not isinstance(raw_entrypoint, Sequence) or isinstance(
            raw_entrypoint, (str, bytes)
        ) or not raw_entrypoint or any(type(item) is not str for item in raw_entrypoint):
            raise CompiledExecutionPlanError("compiled runtime executable and argv are invalid")
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
                raise CompiledExecutionPlanError("compiled runtime environment is invalid")
            # Secret values are resolved by the Controller-owned secret
            # projection.  A recipe authoring value may never cross this
            # boundary as an opaque upstream handle.
            if value.get("secret") not in (None, ""):
                raise CompiledExecutionPlanError("compiled runtime secret projection is unavailable")
            environment.append({"name": name, "value": rendered})

        raw_mounts = security.get("mounts", ())
        if not isinstance(raw_mounts, Sequence) or isinstance(raw_mounts, (str, bytes)):
            raise CompiledExecutionPlanError("compiled security mounts are invalid")
        mounts: list[dict[str, object]] = []
        for raw in raw_mounts:
            mount = _mapping(raw, "compiled security mount")
            source = mount.get("source")
            target = mount.get("target")
            read_only = mount.get("read_only")
            if type(source) is not str or type(target) is not str or type(read_only) is not bool:
                raise CompiledExecutionPlanError("compiled security mounts are invalid")
            if source == "/run/vonk/models" or source.startswith("/run/vonk/models/"):
                source = "model"
            elif source == "/run/vonk/inputs":
                source = "inputs"
            elif source == "/run/vonk/outputs":
                source = "outputs"
            else:
                raise CompiledExecutionPlanError("compiled security mount is not Controller-owned")
            mounts.append({"source": source, "target": target, "read_only": read_only})

        def _required_int(value: object, label: str, *, minimum: int = 0) -> int:
            if type(value) is not int or value < minimum:
                raise CompiledExecutionPlanError(f"{label} is invalid")
            return value

        placement_doc = {
            "endpoint_address": placement.get("endpoint_address"),
            "rank": _required_int(placement.get("rank", topology.get("rank")), "runtime rank"),
            "role": placement.get("role", topology.get("role")),
            "world_size": _required_int(
                placement.get("world_size", topology.get("world_size")),
                "runtime world size",
                minimum=1,
            ),
            "local_address": placement.get("local_address"),
            "master_address": placement.get("master_address"),
            "master_port": placement.get("master_port"),
            "port": _required_int(
                placement.get(
                    "port",
                    _mapping(endpoint, "endpoint").get("port") if isinstance(endpoint, Mapping) else 1024,
                ),
                "runtime port",
                minimum=1,
            ),
            "reserved_memory_bytes": _required_int(
                placement.get("reserved_memory_bytes", 1),
                "runtime reserved memory",
                minimum=1,
            ),
        }
        if type(placement_doc["role"]) is not str or not placement_doc["role"]:
            raise CompiledExecutionPlanError("runtime role is invalid")

        declared_network_mode = security.get("network_mode")
        if declared_network_mode not in {"none", "bridge"} or security.get("host_network") is not False:
            raise CompiledExecutionPlanError(
                "compiled security has an unsupported network mode or host networking"
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
                "mount": {
                    "target": item.mount.target,
                    "read_only": item.mount.read_only,
                },
                "model": item.model.model_dump(mode="json"),
                "distribution_object": item.distribution_object.model_dump(mode="json"),
            }
            for item in self.artifacts
        ]
        runtime_image = self.runtime_image.model_dump(mode="json")
        parent = runtime_image["platform_manifest_digest"]
        runtime_image["local_image_reference"] = (
            f"localhost/vonk/compiled-runtime-{runtime_image['oci_layout_sha256']}@{parent}"
        )
        payload: dict[str, object] = {
            "schema_version": 2,
            "identity": {
                "recipe_revision_sha256": self.recipe_revision_sha256,
                "execution_sha256": self.execution_sha256,
                "harness_sha256": self.harness_sha256,
                "build_input_sha256": identity.get("build_input_sha256"),
                "model_artifact_set_sha256": self.model_artifact_set_sha256,
                "model_artifact_bytes": self.model_artifact_set_bytes,
            },
            "runtime": {
                "executable": executable,
                "argv": argv,
                "env": environment,
                "image_digest": self.runtime_image.image_digest,
                "placement": placement_doc,
            },
            "artifacts": artifacts,
            "runtime_image": runtime_image,
            "security": {
                "devices": list(security.get("devices", ())),
                "capabilities": list(security.get("capabilities", ())),
                "network_mode": network_mode,
                "host_network": security.get("host_network"),
                "privileged": security.get("privileged"),
                "user": security.get("user"),
                "mounts": mounts,
                "read_only_root": security.get("read_only_root"),
                "no_new_privileges": security.get("no_new_privileges"),
            },
            "topology": {
                "name": topology.get("name"),
                "mode": topology.get("mode"),
                "backend": topology.get("backend"),
                "node_count": topology.get("node_count"),
                "world_size": placement_doc["world_size"],
                "rank": placement_doc["rank"],
                "role": placement_doc["role"],
            },
            "lifecycle": {
                "pre_start": lifecycle.get("pre_start"),
                "post_stop": lifecycle.get("post_stop"),
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
        return payload


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
    runtime = _mapping(spec.get("runtime"), "runtime")
    runtime_image_reference = runtime.get("image")
    expected_runtime_digest = image.registry_manifest_digest or image.image_digest
    if not isinstance(runtime_image_reference, str) or not runtime_image_reference.endswith(
        f"@{expected_runtime_digest}"
    ):
        raise CompiledExecutionPlanError(
            "verified runtime image does not match the compiled runtime projection"
        )
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


def validate_compiled_launch_payload(value: object) -> dict[str, object]:
    """Validate the schema-2 launch projection before it reaches an agent.

    The shared agent protocol owns its wire DTO.  The Controller still
    validates persisted JSON at the HTTP boundary so a stale row or accidental
    legacy payload cannot be handed to a Spark process after a restart.
    """

    payload = _mapping(value, "compiled launch plan")
    expected = {
        "schema_version",
        "identity",
        "runtime",
        "artifacts",
        "runtime_image",
        "security",
        "topology",
        "lifecycle",
    }
    required = expected | {"endpoint", "job"}
    if set(payload) != required or payload.get("schema_version") != 2:
        raise CompiledExecutionPlanError("compiled launch plan schema is invalid")
    if (payload.get("endpoint") is None) == (payload.get("job") is None):
        raise CompiledExecutionPlanError("compiled launch plan interface is invalid")
    identity = _mapping(payload.get("identity"), "compiled launch identity")
    if set(identity) != {
        "recipe_revision_sha256",
        "execution_sha256",
        "harness_sha256",
        "build_input_sha256",
        "model_artifact_set_sha256",
        "model_artifact_bytes",
    }:
        raise CompiledExecutionPlanError("compiled launch identity fields are invalid")
    for key in (
        "recipe_revision_sha256",
        "execution_sha256",
        "harness_sha256",
        "model_artifact_set_sha256",
    ):
        _digest(identity.get(key), f"compiled launch identity {key}")
    build_input = identity.get("build_input_sha256")
    if build_input is not None:
        _digest(build_input, "compiled launch build input digest")
    artifact_bytes = identity.get("model_artifact_bytes")
    if type(artifact_bytes) is not int or artifact_bytes < 0:
        raise CompiledExecutionPlanError("compiled launch model artifact bytes are invalid")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)) or not artifacts:
        raise CompiledExecutionPlanError("compiled launch model artifacts are invalid")
    by_digest: dict[str, int] = {}
    selected: set[tuple[object, object]] = set()
    paths: set[tuple[object, object]] = set()
    for raw in artifacts:
        artifact = _mapping(raw, "compiled launch model artifact")
        if set(artifact) != {
            "selection_id",
            "file_id",
            "path",
            "sha256",
            "size_bytes",
            "roles",
            "mount",
            "model",
            "distribution_object",
        }:
            raise CompiledExecutionPlanError("compiled launch model artifact fields are invalid")
        selection_id = artifact.get("selection_id")
        file_id = artifact.get("file_id")
        path = artifact.get("path")
        if not all(type(item) is str and item for item in (selection_id, file_id, path)):
            raise CompiledExecutionPlanError("compiled launch model artifact identity is invalid")
        digest = _digest(artifact.get("sha256"), "compiled launch model artifact digest")
        size = artifact.get("size_bytes")
        if type(size) is not int or size < 0:
            raise CompiledExecutionPlanError("compiled launch model artifact size is invalid")
        distribution = DistributionObjectReceipt.model_validate(
            _mapping(artifact.get("distribution_object"), "compiled launch distribution object")
        )
        if distribution.kind != "model" or distribution.sha256 != digest or distribution.bytes != size:
            raise CompiledExecutionPlanError("compiled launch model receipt is inconsistent")
        mount = _mapping(artifact.get("mount"), "compiled launch model mount")
        if set(mount) != {"target", "read_only"} or type(mount.get("target")) is not str or mount.get("read_only") is not True:
            raise CompiledExecutionPlanError("compiled launch model mount is invalid")
        roles = artifact.get("roles")
        if (
            not isinstance(roles, list)
            or not roles
            or any(type(role) is not str or not role for role in roles)
            or roles != sorted(set(roles))
        ):
            raise CompiledExecutionPlanError("compiled launch model roles are invalid")
        try:
            _safe_path(path, absolute=False)
        except ValueError as error:
            raise CompiledExecutionPlanError("compiled launch model path is invalid") from error
        selected_key = (selection_id, file_id)
        path_key = (selection_id, path)
        if selected_key in selected or path_key in paths:
            raise CompiledExecutionPlanError("compiled launch model selection is duplicated")
        selected.add(selected_key)
        paths.add(path_key)
        previous = by_digest.setdefault(digest, size)
        if previous != size:
            raise CompiledExecutionPlanError("compiled launch model digest has conflicting sizes")
        model = _mapping(artifact.get("model"), "compiled launch model identity")
        if set(model) != {"publisher", "slug", "content_sha256"}:
            raise CompiledExecutionPlanError("compiled launch model identity is invalid")
        _digest(model.get("content_sha256"), "compiled launch model identity")
        if distribution.name != path:
            raise CompiledExecutionPlanError("compiled launch model receipt path is inconsistent")
    if sum(by_digest.values()) != artifact_bytes:
        raise CompiledExecutionPlanError("compiled launch model bytes do not match receipts")
    runtime = _mapping(payload.get("runtime"), "compiled launch runtime")
    if set(runtime) != {"executable", "argv", "env", "image_digest", "placement"}:
        raise CompiledExecutionPlanError("compiled launch runtime fields are invalid")
    if type(runtime.get("executable")) is not str or not runtime["executable"]:
        raise CompiledExecutionPlanError("compiled launch executable is invalid")
    if (
        not isinstance(runtime.get("argv"), list)
        or any(type(item) is not str for item in runtime["argv"])
    ):
        raise CompiledExecutionPlanError("compiled launch argv is invalid")
    environment = runtime.get("env")
    if (
        not isinstance(environment, list)
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"name", "value"}
            or type(item.get("name")) is not str
            or type(item.get("value")) is not str
            for item in environment
        )
    ):
        raise CompiledExecutionPlanError("compiled launch environment is invalid")
    runtime_image = CompiledRuntimeImage.model_validate(
        _mapping(payload.get("runtime_image"), "compiled launch runtime image")
    )
    if runtime.get("image_digest") != runtime_image.image_digest:
        raise CompiledExecutionPlanError("compiled launch image digest is inconsistent")
    placement = _mapping(runtime.get("placement"), "compiled launch placement")
    required_placement = {
        "endpoint_address", "rank", "role", "world_size", "local_address",
        "master_address", "master_port", "port", "reserved_memory_bytes",
    }
    if set(placement) != required_placement:
        raise CompiledExecutionPlanError("compiled launch placement fields are invalid")
    topology = _mapping(payload.get("topology"), "compiled launch topology")
    if (
        type(placement.get("rank")) is not int
        or placement["rank"] < 0
        or type(placement.get("world_size")) is not int
        or placement["world_size"] < 1
        or placement["rank"] >= placement["world_size"]
        or type(placement.get("port")) is not int
        or placement["port"] < 1
        or type(placement.get("reserved_memory_bytes")) is not int
        or placement["reserved_memory_bytes"] < 1
        or placement.get("rank") != topology.get("rank")
        or placement.get("role") != topology.get("role")
        or placement.get("world_size") != topology.get("world_size")
    ):
        raise CompiledExecutionPlanError("compiled launch placement does not match topology")
    topology_required = {
        "name",
        "mode",
        "backend",
        "node_count",
        "world_size",
        "rank",
        "role",
    }
    if set(topology) != topology_required:
        raise CompiledExecutionPlanError("compiled launch topology fields are invalid")
    security = _mapping(payload.get("security"), "compiled launch security")
    security_required = {
        "devices",
        "capabilities",
        "network_mode",
        "host_network",
        "privileged",
        "user",
        "mounts",
        "read_only_root",
        "no_new_privileges",
    }
    if set(security) != security_required:
        raise CompiledExecutionPlanError("compiled launch security fields are invalid")
    expected_network_mode = (
        "bridge"
        if placement.get("endpoint_address") is not None
        or placement.get("master_port") is not None
        else "none"
    )
    if (
        security.get("network_mode") != expected_network_mode
        or security.get("host_network") is not False
        or not isinstance(security.get("devices"), list)
        or len(security["devices"]) > 1
        or any(device != "nvidia.com/gpu=all" for device in security["devices"])
        or security.get("capabilities") != []
        or security.get("privileged") is not False
        or security.get("read_only_root") is not True
        or security.get("no_new_privileges") is not True
    ):
        raise CompiledExecutionPlanError(
            "compiled launch security must match signed ports with isolated network and host networking disabled"
        )
    mounts = security.get("mounts")
    if not isinstance(mounts, list):
        raise CompiledExecutionPlanError("compiled launch security mounts are invalid")
    for raw_mount in mounts:
        mount = _mapping(raw_mount, "compiled launch security mount")
        if (
            set(mount) != {"source", "target", "read_only"}
            or mount.get("source") not in {"model", "inputs", "outputs"}
            or type(mount.get("target")) is not str
            or type(mount.get("read_only")) is not bool
        ):
            raise CompiledExecutionPlanError("compiled launch security mount is invalid")
    lifecycle = _mapping(payload.get("lifecycle"), "compiled launch lifecycle")
    if set(lifecycle) != {"pre_start", "post_stop", "stop_timeout_seconds"}:
        raise CompiledExecutionPlanError("compiled launch lifecycle fields are invalid")
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if any(f'"{name}"' in serialized for name in ("model_version_sha256", "runtime_distribution_sha256", "patch_bundle_sha256")):
        raise CompiledExecutionPlanError("compiled launch plan contains retired authority")
    return dict(payload)


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
    "validate_compiled_launch_payload",
]
