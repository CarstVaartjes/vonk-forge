"""Production composition of canonical runtime facts and verified receipts.

The recipe compiler owns launch behavior and the model/build services own
immutable bytes.  This module is the narrow Controller seam that binds the
two authorities into the schema-2 launch document persisted on an installation
and returned to an agent.  It never accepts an upstream repository or source
path as an agent instruction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from sqlalchemy.orm import Session
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .compiled_execution_plan import (
    CompiledExecutionPlanError,
    CompiledRuntimeImage,
    compile_verified_execution_plan,
    execution_identity_sha256,
)
from .distribution import ModelCacheVerifiedObjectSource
from .models import CatalogDocumentRevision, ClusterMappingNode, RecipeBuild
from .recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    compile_runtime_spec,
    resolve_recipe_entities,
)
from .runtime_image_preparation import RuntimeImageReceipt


class ExecutionPlanCompilationError(ValueError):
    """Canonical launch facts and verified Controller receipts cannot agree."""


RuntimeImageResolver = Callable[
    [Mapping[str, object], str, Mapping[str, object]],
    RuntimeImageReceipt,
]
RuntimeImagePreparer = Callable[
    [Mapping[str, object], Mapping[str, object], RecipeBuild | None], RuntimeImageReceipt
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
        build: RecipeBuild | None,
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
            recipe = _canonical_recipe(revision.document, resolved_entities)
        except (TypeError, ValueError) as error:
            raise ExecutionPlanCompilationError(
                "recipe does not satisfy the canonical contract"
            ) from error
        if content_sha256(recipe) != revision.content_digest:
            raise ExecutionPlanCompilationError(
                "recipe revision digest does not match the canonical document"
            )
        try:
            resolved = (
                dict(resolved_entities)
                if resolved_entities is not None
                else resolve_recipe_entities(session, revision.document)
            )
            models = _canonical_models(resolved["models"])
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
        world_size = _world_size(recipe)
        result: dict[str, dict[str, object]] = {}
        for node in sorted(mapping_nodes, key=lambda item: (item.rank, item.node_id)):
            package = _build_package(build) if _is_source_build(recipe) else None
            try:
                runtime_spec = compile_runtime_spec(
                    recipe,
                    resolved_entities={"recipe": recipe, "models": models},
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
                placement = _placement(recipe, runtime_spec, node, world_size)
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
        build: RecipeBuild | None,
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

    if not isinstance(receipt, RuntimeImageReceipt):
        raise ExecutionPlanCompilationError("runtime image receipt is invalid")
    return {
        "image_digest": receipt.image_digest,
        "oci_layout_sha256": receipt.oci_archive_sha256,
        "image_bytes": receipt.image_bytes,
        "architecture": receipt.architecture,
        "runtime_interface": receipt.runtime_interface,
        "runtime_interface_label": receipt.runtime_interface_label,
        "source": receipt.source,
        "build_id": receipt.build_id,
        "registry_manifest_digest": receipt.registry_manifest_digest,
        "platform_manifest_digest": receipt.platform_manifest_digest,
        "local_image_config_id": receipt.local_image_config_id,
        "local_image_reference": receipt.local_image_reference,
        "distribution_object": {
            "name": "image.oci.tar",
            "sha256": receipt.oci_archive_sha256,
            "bytes": receipt.image_bytes,
            "kind": "oci-archive",
        },
    }


def _canonical_recipe(
    document: Mapping[str, object], resolved_entities: Mapping[str, object] | None
) -> RecipeDefinition:
    """Return the producer-resolved canonical recipe after validating its source."""

    parsed = RecipeDefinition.model_validate(document)
    resolved = resolved_entities.get("recipe") if resolved_entities is not None else None
    if resolved is None:
        return parsed
    if not isinstance(resolved, RecipeDefinition):
        raw = getattr(resolved, "document", resolved)
        resolved = RecipeDefinition.model_validate(raw)
    if content_sha256(resolved) != content_sha256(parsed):
        raise ValueError("resolved recipe projection does not match the revision")
    return resolved


def _canonical_models(value: object) -> tuple[ModelDefinition, ...]:
    """Validate resolved model revisions before selecting their exact files."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("canonical model projections are missing")
    result: list[ModelDefinition] = []
    for item in value:
        if isinstance(item, ModelDefinition):
            result.append(item)
            continue
        raw = getattr(item, "document", item)
        result.append(ModelDefinition.model_validate(raw))
    return tuple(result)


def _is_source_build(recipe: RecipeDefinition) -> bool:
    return recipe.execution.mode == "build"


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


def _world_size(recipe: RecipeDefinition) -> int:
    return recipe.topology.parallelism.world_size


def _placement(
    recipe: RecipeDefinition,
    runtime_spec: Mapping[str, object],
    node: ClusterMappingNode,
    world_size: int,
) -> dict[str, object]:
    endpoint = runtime_spec.get("endpoint")
    role = next((item for item in recipe.topology.roles if item.name == node.role), None)
    if role is None:
        raise ExecutionPlanCompilationError(
            f"mapped role {node.role!r} is absent from the canonical recipe topology"
        )
    reserved = role.resources.memory.startup_peak_bytes
    if type(reserved) is not int or reserved <= 0:
        raise ExecutionPlanCompilationError("canonical recipe role memory is invalid")
    if recipe.interfaces[0].adapter == "openai":
        if not isinstance(endpoint, Mapping):
            raise ExecutionPlanCompilationError("compiled runtime endpoint is unavailable")
        port = endpoint.get("port")
        if type(port) is not int or port <= 0 or port > 65535:
            raise ExecutionPlanCompilationError("compiled runtime endpoint port is invalid")
    else:
        if endpoint is not None:
            raise ExecutionPlanCompilationError("job recipe has an unexpected runtime endpoint")
        port = None
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
    try:
        canonical_models = _canonical_models(models)
    except (TypeError, ValueError) as error:
        raise ExecutionPlanCompilationError("canonical model projection is invalid") from error
    for model in canonical_models:
        identity = content_sha256(model)
        for file in model.files:
            by_identity[(identity, file.id)] = file.model_dump(mode="json")
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
