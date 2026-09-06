"""Production composition of canonical runtime facts and verified receipts.

The recipe compiler owns launch behavior and the model/build services own
immutable bytes.  This module is the narrow Controller seam that binds the
two authorities into the schema-2 launch document persisted on an installation
and returned to an agent.  It never accepts an upstream repository or source
path as an agent instruction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .compiled_execution_plan import (
    CompiledExecutionPlanError,
    CompiledRuntimeImage,
    compile_verified_execution_plan,
    execution_identity_sha256,
)
from .distribution import ModelCacheVerifiedObjectSource
from .models import ClusterMappingNode, CatalogDocumentRevision, RecipeBuild
from .recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    compile_runtime_spec,
    resolve_recipe_entities,
)


class ExecutionPlanCompilationError(ValueError):
    """Canonical launch facts and verified Controller receipts cannot agree."""


@dataclass(frozen=True, slots=True)
class RuntimeImageReceipt:
    """The exact Controller-distributed OCI archive for one runtime image."""

    image_digest: str
    oci_layout_sha256: str
    image_bytes: int
    source: str
    build_id: str | None
    registry_manifest_digest: str | None
    platform_manifest_digest: str
    local_image_config_id: str
    architecture: str
    runtime_interface: str
    runtime_interface_label: str
    local_image_reference: str | None

    def as_mapping(self) -> dict[str, object]:
        return {
            "image_digest": self.image_digest,
            "oci_layout_sha256": self.oci_layout_sha256,
            "image_bytes": self.image_bytes,
            "architecture": self.architecture,
            "runtime_interface": self.runtime_interface,
            "runtime_interface_label": self.runtime_interface_label,
            "source": self.source,
            "build_id": self.build_id,
            "registry_manifest_digest": self.registry_manifest_digest,
            "platform_manifest_digest": self.platform_manifest_digest,
            "local_image_config_id": self.local_image_config_id,
            "local_image_reference": self.local_image_reference,
            "distribution_object": {
                "name": "image.oci.tar",
                "sha256": self.oci_layout_sha256,
                "bytes": self.image_bytes,
                "kind": "oci-archive",
            },
        }


RuntimeImageResolver = Callable[
    [Mapping[str, object], str, Mapping[str, object]],
    Mapping[str, object] | RuntimeImageReceipt,
]
RuntimeImagePreparer = Callable[
    [Mapping[str, object], Mapping[str, object], RecipeBuild], object
]


class ControllerExecutionPlanService:
    """Bind canonical recipe compilation to model-cache and OCI receipts."""

    def __init__(
        self,
        model_cache: object,
        *,
        runtime_image_resolver: RuntimeImageResolver | None = None,
        runtime_image_preparer: RuntimeImagePreparer | None = None,
    ) -> None:
        self._model_cache = model_cache
        self._runtime_image_resolver = runtime_image_resolver
        self._runtime_image_preparer = runtime_image_preparer

    def compile_installation(
        self,
        session: Session,
        *,
        revision: CatalogDocumentRevision,
        build: RecipeBuild,
        mapping_nodes: Sequence[ClusterMappingNode],
        parameters: Mapping[str, object] | None,
        mapping: object | None = None,
        resolved_entities: Mapping[str, object] | None = None,
    ) -> dict[str, dict[str, object]]:
        """Compile one launch document per mapped Spark rank.

        The model cache is asked for the exact recipe selection manifest.  A
        partial or path-only manifest is rejected by the receipt compiler;
        callers therefore cannot accidentally install a subset of a model or
        bind two colliding ``config.json`` files to the wrong selection.
        """

        if (
            revision.kind != "recipe"
            or revision.state != "active"
            or revision.content_digest is None
        ):
            raise ExecutionPlanCompilationError("recipe revision digest is unavailable")
        try:
            resolved = (
                dict(resolved_entities)
                if resolved_entities is not None
                else resolve_recipe_entities(session, revision.document)
            )
            models = tuple(resolved["models"])
            manifest = self._model_cache.resolve_artifact_set(
                recipe_revision_sha256=revision.content_digest,
            )
            artifact_set_sha256 = manifest.digest
            direct_receipts = getattr(
                self._model_cache, "verified_model_objects_for_set", None
            )
            if callable(direct_receipts):
                # The production ModelCacheService exposes the persisted
                # manifest through ModelCacheVerifiedObjectSource.  A bound
                # canonical cache adapter may expose the already validated
                # receipt sequence directly; it is still required to carry
                # selection/file identity and exact distribution objects.
                model_objects = direct_receipts(artifact_set_sha256)
            else:
                model_source = ModelCacheVerifiedObjectSource.from_service(
                    self._model_cache
                )
                model_objects = model_source.verified_model_objects_for_set(
                    artifact_set_sha256
                )
        except Exception as error:
            raise ExecutionPlanCompilationError(
                "verified model artifact-set receipt is unavailable"
            ) from error

        document = revision.document
        world_size = _world_size(document, len(mapping_nodes))
        result: dict[str, dict[str, object]] = {}
        for node in sorted(mapping_nodes, key=lambda item: (item.rank, item.node_id)):
            package = _build_package(build) if _is_source_build(document) else None
            try:
                runtime_spec = compile_runtime_spec(
                    document,
                    resolved_entities={"models": models},
                    parameters=parameters,
                    role=node.role,
                    rank=node.rank,
                    package_handle=package,
                )
                runtime_spec = _bind_runtime_artifacts(runtime_spec, models)
                receipt = self._runtime_image(document, build, runtime_spec)
                compiled = compile_verified_execution_plan(
                    runtime_spec,
                    model_artifact_set_sha256=artifact_set_sha256,
                    model_objects=model_objects,
                    runtime_image=receipt,
                )
                placement = _placement(document, runtime_spec, node, world_size)
                result[node.node_id] = compiled.to_compiled_launch_payload(
                    runtime_spec,
                    placement=placement,
                )
            except (CompiledExecutionPlanError, RecipeRuntimeSpecError, TypeError, ValueError) as error:
                raise ExecutionPlanCompilationError(
                    f"compiled execution plan for {node.node_id} is unavailable: {error}"
                ) from error
        if not result:
            raise ExecutionPlanCompilationError("compiled execution plan has no mapped targets")
        return result

    def _runtime_image(
        self,
        document: Mapping[str, object],
        build: RecipeBuild,
        runtime_spec: Mapping[str, object],
    ) -> CompiledRuntimeImage:
        runtime = runtime_spec.get("runtime")
        runtime_image = runtime.get("image") if isinstance(runtime, Mapping) else None
        image_digest = _image_digest(runtime_image)
        if self._runtime_image_preparer is not None:
            receipt = self._runtime_image_preparer(document, runtime_spec, build)
            value = _runtime_receipt_mapping(receipt)
        elif self._runtime_image_resolver is not None:
            receipt = self._runtime_image_resolver(document, image_digest, runtime_spec)
            value = _runtime_receipt_mapping(receipt)
        else:
            raise ExecutionPlanCompilationError(
                "verified OCI archive receipt is unavailable for the selected runtime image"
            )
        try:
            image = CompiledRuntimeImage.model_validate(value)
        except Exception as error:
            raise ExecutionPlanCompilationError("verified runtime image receipt is invalid") from error
        expected_digest = image.registry_manifest_digest or image.image_digest
        if expected_digest != image_digest:
            raise ExecutionPlanCompilationError(
                "runtime image receipt does not match the compiled runtime image"
            )
        return image


def _runtime_receipt_mapping(receipt: object) -> dict[str, object]:
    """Project a preparation receipt into the strict launch-image DTO.

    Runtime-image preparation persists provenance and storage fields alongside
    the portable launch identity.  The agent plan carries only the verified
    schema-2 image receipt, so the projection is explicit and rejects
    accidental leakage of the storage envelope.
    """

    if isinstance(receipt, RuntimeImageReceipt):
        return receipt.as_mapping()
    to_mapping = getattr(receipt, "to_mapping", None)
    raw = to_mapping() if callable(to_mapping) else receipt
    if not isinstance(raw, Mapping):
        raise ExecutionPlanCompilationError("runtime image receipt is invalid")
    value = dict(raw)
    archive_sha256 = value.get("oci_archive_sha256")
    if "oci_layout_sha256" not in value and isinstance(archive_sha256, str):
        image_bytes = value.get("image_bytes")
        value = {
            "image_digest": value.get("image_digest"),
            "oci_layout_sha256": archive_sha256,
            "image_bytes": image_bytes,
            "architecture": value.get("architecture"),
            "runtime_interface": value.get("runtime_interface"),
            "runtime_interface_label": value.get("runtime_interface_label"),
            "source": value.get("source"),
            "build_id": value.get("build_id"),
            "registry_manifest_digest": value.get("registry_manifest_digest"),
            "platform_manifest_digest": value.get("platform_manifest_digest"),
            "local_image_config_id": value.get("local_image_config_id"),
            "local_image_reference": value.get("local_image_reference"),
            "distribution_object": {
                "name": "image.oci.tar",
                "sha256": archive_sha256,
                "bytes": image_bytes,
                "kind": "oci-archive",
            },
        }
    return value


def _is_source_build(document: Mapping[str, object]) -> bool:
    execution = document.get("execution")
    return isinstance(execution, Mapping) and execution.get("mode") == "build"


def _build_package(build: RecipeBuild) -> dict[str, object]:
    if (
        build.state != "succeeded"
        or not isinstance(build.image_digest, str)
        or not isinstance(build.build_input_sha256, str)
    ):
        raise ExecutionPlanCompilationError("successful Controller build receipt is unavailable")
    return {
        "image_digest": build.image_digest,
        "image_reference": f"localhost/vonk/recipe-build@{build.image_digest}",
        "build_input_sha256": build.build_input_sha256,
        "platform": "linux/arm64",
    }


def _image_digest(value: object) -> str:
    if not isinstance(value, str) or "@sha256:" not in value:
        raise ExecutionPlanCompilationError("compiled runtime image digest is unavailable")
    digest = value.rsplit("@", 1)[-1]
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ExecutionPlanCompilationError("compiled runtime image digest is invalid")
    return digest


def _world_size(document: Mapping[str, object], fallback: int) -> int:
    topology = document.get("topology")
    parallelism = topology.get("parallelism") if isinstance(topology, Mapping) else None
    value = parallelism.get("world_size") if isinstance(parallelism, Mapping) else None
    return value if type(value) is int and value > 0 else max(1, fallback)


def _placement(
    document: Mapping[str, object],
    runtime_spec: Mapping[str, object],
    node: ClusterMappingNode,
    world_size: int,
) -> dict[str, object]:
    endpoint = runtime_spec.get("endpoint")
    role_resources: Mapping[str, object] | None = None
    raw_topology = document.get("topology")
    roles = raw_topology.get("roles") if isinstance(raw_topology, Mapping) else None
    if isinstance(roles, Sequence) and not isinstance(roles, (str, bytes)):
        for raw_role in roles:
            if isinstance(raw_role, Mapping) and raw_role.get("name") == node.role:
                candidate = raw_role.get("resources")
                if isinstance(candidate, Mapping):
                    role_resources = candidate
                break
    memory = role_resources.get("memory") if role_resources else None
    reserved = memory.get("startup_peak_bytes") if isinstance(memory, Mapping) else None
    if type(reserved) is not int or reserved <= 0:
        reserved = 1
    port = endpoint.get("port") if isinstance(endpoint, Mapping) else 1024
    if type(port) is not int or port <= 0:
        port = 1024
    return {
        "endpoint_address": None,
        "rank": node.rank,
        "role": node.role,
        "world_size": world_size,
        "local_address": None,
        "master_address": None,
        "master_port": 29500 if world_size > 1 else None,
        "port": port,
        "reserved_memory_bytes": reserved,
    }


def _bind_runtime_artifacts(
    runtime_spec: Mapping[str, object], models: Sequence[object]
) -> dict[str, object]:
    """Add exact file bytes from canonical model revisions to harness output."""

    result = dict(runtime_spec)
    raw_artifacts = runtime_spec.get("artifacts")
    if not isinstance(raw_artifacts, Sequence) or isinstance(raw_artifacts, (str, bytes)):
        raise ExecutionPlanCompilationError("canonical runtime model artifacts are unavailable")
    by_identity: dict[tuple[str, str], Mapping[str, object]] = {}
    for model in models:
        document = getattr(model, "document", None)
        identity = getattr(model, "content_digest", None)
        if not isinstance(document, Mapping) or not isinstance(identity, str):
            continue
        files = document.get("files")
        if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
            continue
        for raw in files:
            if isinstance(raw, Mapping) and isinstance(raw.get("id"), str):
                by_identity[(identity, str(raw["id"]))] = raw
    bound: list[dict[str, object]] = []
    for raw in raw_artifacts:
        if not isinstance(raw, Mapping):
            raise ExecutionPlanCompilationError("canonical runtime model artifact is invalid")
        model = raw.get("model")
        file_id = raw.get("file_id")
        model_digest = model.get("content_sha256") if isinstance(model, Mapping) else None
        file = by_identity.get((model_digest, file_id))
        if file is None:
            raise ExecutionPlanCompilationError("selected model file is absent from the canonical model manifest")
        digest = file.get("sha256")
        size = file.get("size_bytes")
        path = file.get("path")
        if not isinstance(digest, str) or type(size) is not int or size < 0 or not isinstance(path, str):
            raise ExecutionPlanCompilationError("selected model file integrity metadata is invalid")
        item = dict(raw)
        if item.get("path") != path:
            raise ExecutionPlanCompilationError("selected model file path does not match the canonical manifest")
        item["sha256"] = digest
        item["bytes"] = size
        mount = item.get("mount")
        if not isinstance(mount, Mapping):
            raise ExecutionPlanCompilationError("selected model file mount is invalid")
        item["mount"] = {
            "source": f"/run/vonk/models/{item.get('selection_id')}",
            "target": mount.get("target"),
            "read_only": mount.get("read_only"),
        }
        bound.append(item)
    result["artifacts"] = bound
    identity = result.get("identity")
    if isinstance(identity, Mapping):
        identity = dict(identity)
        identity["execution_sha256"] = execution_identity_sha256(result)
        result["identity"] = identity
    return result


__all__ = [
    "ControllerExecutionPlanService",
    "ExecutionPlanCompilationError",
    "RuntimeImageReceipt",
]
