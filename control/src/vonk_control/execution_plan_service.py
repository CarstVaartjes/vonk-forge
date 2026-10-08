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
from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_agent_protocol.compiled_execution_plan import CompiledPlacement
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, read_recipe
from vonk_forge_contracts.model import ModelFile
from vonk_forge_contracts.recipe import Scalar

from .compiled_execution_plan import (
    CompiledExecutionPlanError,
    DistributionObjectReceipt,
    VerifiedModelObject,
    VerifiedRuntimeImage,
    compile_verified_execution_plan,
)
from .distribution import ModelCacheObjectSource
from .models import CatalogDocumentRevision, ClusterMappingNode, RecipeBuild
from .recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    ResolvedRecipe,
    compile_runtime_spec,
    resolve_recipe_entities,
    split_option_choices,
)
from .resource_planning import PLATFORM_MEMORY_FLOOR_BYTES
from .runtime_image_preparation import RuntimeImageReceipt
from .runtime_spec_contract import RuntimeSpec, SpecArtifact, SpecModelMount


def compile_job_invocation(
    session: Session,
    *,
    revision: CatalogDocumentRevision,
    installed: WireCompiledExecutionPlan,
    build: RecipeBuild | None,
    parameters: Mapping[str, Scalar],
    timeout_seconds: int,
    memory_floor_bytes: int,
    reserved_memory_bytes: int,
    option_choices: Mapping[str, str] | None = None,
) -> WireCompiledExecutionPlan:
    """Compile invocation settings against the installation's exact receipts."""
    plan = installed
    recipe = read_recipe(revision.document)
    if plan.job is None or not 1 <= timeout_seconds <= plan.job.timeout_seconds:
        raise ExecutionPlanCompilationError(
            "job timeout exceeds the installed workload"
        )
    if revision.content_digest != plan.identity.recipe_revision_sha256:
        raise ExecutionPlanCompilationError(
            "job recipe differs from the installed workload"
        )
    role = next(
        (
            item
            for item in recipe.topology.roles
            if item.name == plan.runtime.placement.role
        ),
        None,
    )
    if role is None:
        raise ExecutionPlanCompilationError(
            "job role differs from the accepted canonical workload"
        )
    # The floor is the platform's, applied at review. Installed plans made before
    # it replaced the recipe reserve may record a larger one, which must not strand
    # their jobs, so a job may carry a smaller floor but never zero.
    if (
        type(memory_floor_bytes) is not int
        or memory_floor_bytes < 0
        or (memory_floor_bytes == 0 and plan.runtime.placement.memory_floor_bytes > 0)
    ):
        raise ExecutionPlanCompilationError("job memory floor is invalid")
    if type(reserved_memory_bytes) is not int or reserved_memory_bytes <= 0:
        raise ExecutionPlanCompilationError("job memory reservation is invalid")
    resolved = resolve_recipe_entities(session, revision.document)
    models = resolved.models
    if build is None:
        raise ExecutionPlanEvidenceUnknown("job build receipt is unavailable")
    runtime_spec = compile_runtime_spec(
        recipe,
        recipe_digest=revision.content_digest,
        models=models,
        package_handle=_build_package(build),
        parameters=parameters,
        # The job runs with the recipe options its installation was made with.
        option_choices=option_choices,
        role=plan.runtime.placement.role,
        rank=plan.runtime.placement.rank,
    )
    runtime_spec = _bind_runtime_artifacts(runtime_spec, models)
    compiled_image_digest = _image_digest(runtime_spec.runtime.image)
    # The installed plan binds the exact reviewed runtime image, and a
    # repaired build row can acquire a different image digest after
    # installation.  The stored plan carries no execution digest of its own
    # (per-job parameters and timeout intentionally recompile it), so this
    # exact image digest is the installed-identity comparison available on
    # the apply path.
    if compiled_image_digest != plan.runtime_image.image_digest:
        raise ExecutionPlanCompilationError(
            "job build differs from the installed workload"
        )
    if runtime_spec.job is None:
        raise ExecutionPlanCompilationError(
            "compiled runtime job settings are unavailable"
        )
    runtime_spec.job.timeout_seconds = timeout_seconds
    runtime_spec.identity.execution_sha256 = runtime_spec.launch_identity_sha256()
    objects = tuple(
        VerifiedModelObject(
            model_content_sha256=artifact.model.content_sha256,
            file_id=artifact.file_id,
            path=artifact.path,
            sha256=artifact.sha256,
            bytes=artifact.size_bytes,
            roles=list(artifact.roles),
            distribution_object=DistributionObjectReceipt(
                name=artifact.path,
                sha256=artifact.sha256,
                bytes=artifact.size_bytes,
                kind="model",
            ),
        )
        for artifact in plan.artifacts
    )
    compiled = compile_verified_execution_plan(
        runtime_spec,
        model_artifact_set_sha256=plan.identity.model_artifact_set_sha256,
        model_objects=objects,
        runtime_image=VerifiedRuntimeImage.model_validate(
            plan.runtime_image.model_dump(mode="json")
        ),
    )
    # Installation carries the recipe's estimated envelope. The running
    # assignment owns the accepted capacity promise used by every job.
    placement = CompiledPlacement.model_validate(
        {
            **plan.runtime.placement.model_dump(mode="json"),
            "memory_floor_bytes": memory_floor_bytes,
            "reserved_memory_bytes": reserved_memory_bytes,
        }
    )
    return compiled.to_compiled_launch_payload(runtime_spec, placement=placement)


class ExecutionPlanCompilationError(InvalidRequestError, ValueError):
    """Caller-supplied compilation inputs fail validation before effects."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, reason=InvalidRequestReason.INCOMPLETE)


class ExecutionPlanEvidenceUnknown(UnknownOutcomeError, ValueError):
    """The preparation owner must re-observe unavailable receipt evidence."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, reason=WaitReason.RECEIPT_MISSING)


RuntimeImageResolver = Callable[
    [Mapping[str, object], str, RuntimeSpec],
    RuntimeImageReceipt,
]
RuntimeImagePreparer = Callable[
    [Mapping[str, object], RuntimeSpec, RecipeBuild | None],
    RuntimeImageReceipt,
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
        resolved_entities: ResolvedRecipe | None = None,
    ) -> dict[str, WireCompiledExecutionPlan]:
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
            recipe = (
                resolved_entities.recipe
                if resolved_entities is not None
                else read_recipe(revision.document)
            )
        except (TypeError, ValueError) as error:
            raise ExecutionPlanCompilationError(
                "recipe does not satisfy the canonical contract"
            ) from error
        try:
            resolved = (
                resolved_entities
                if resolved_entities is not None
                else resolve_recipe_entities(session, revision.document)
            )
            models = resolved.models
            resolver = getattr(self._model_cache, "resolve_artifact_set", None)
            if not isinstance(resolver, Callable):
                raise TypeError("NAS cache artifact-set resolver is unavailable")
            manifest = resolver(recipe_revision_sha256=revision.content_digest)
            declared_digest = getattr(manifest, "digest", None)
            if not isinstance(declared_digest, str):
                raise TypeError("NAS cache artifact-set digest is unavailable")
            artifact_set_sha256 = declared_digest
            direct_receipts = getattr(
                self._model_cache, "verified_model_objects_for_set", None
            )
            model_objects: Sequence[VerifiedModelObject]
            if callable(direct_receipts):
                # The production ModelCacheService exposes the persisted
                # manifest through ModelCacheObjectSource.  A bound
                # canonical cache adapter may expose the already validated
                # receipt sequence directly; it is still required to carry
                # selection/file identity and exact distribution objects.
                direct_objects = direct_receipts(artifact_set_sha256)
                if not isinstance(direct_objects, Sequence) or isinstance(
                    direct_objects, (str, bytes)
                ):
                    raise TypeError("verified model object receipts are unavailable")
                model_objects = tuple(
                    VerifiedModelObject.model_validate(item) for item in direct_objects
                )
            else:
                model_source = ModelCacheObjectSource.from_service(self._model_cache)
                # Describe the shared verified bytes with this revision's
                # model identities, whichever revision cached them first.
                model_objects = model_source.verified_model_objects_for_set(
                    artifact_set_sha256, manifest
                )
        except (UnknownOutcomeError, SecurityRefusalError):
            # Unconfirmed storage or bookkeeping keeps its type: the admitting
            # owner observes it again instead of reading a verdict on the plan.
            raise
        except Exception as error:
            raise ExecutionPlanEvidenceUnknown(
                "verified model artifact-set receipt is unavailable"
            ) from error

        option_choices, settings = split_option_choices(parameters)
        document = revision.document
        world_size = _world_size(recipe)
        result: dict[str, WireCompiledExecutionPlan] = {}
        if build is None:
            # No selected build is an incomplete compilation request. Storage
            # uncertainty for a selected receipt retains its unknown type.
            raise ExecutionPlanCompilationError("recipe build receipt is unavailable")
        package = _build_package(build)
        for node in sorted(mapping_nodes, key=lambda item: (item.rank, item.node_id)):
            try:
                runtime_spec = compile_runtime_spec(
                    recipe,
                    recipe_digest=revision.content_digest,
                    models=models,
                    package_handle=package,
                    parameters=settings,
                    option_choices=option_choices,
                    role=node.role,
                    rank=node.rank,
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
            except (UnknownOutcomeError, SecurityRefusalError):
                raise
            except (
                CompiledExecutionPlanError,
                RecipeRuntimeSpecError,
                TypeError,
                ValueError,
            ) as error:
                raise ExecutionPlanCompilationError(
                    f"compiled execution plan for {node.node_id} is unavailable: {error}"
                ) from error
        if not result:
            raise ExecutionPlanCompilationError(
                "compiled execution plan has no mapped targets"
            )
        return result

    def _runtime_image(
        self,
        document: Mapping[str, object],
        build: RecipeBuild | None,
        runtime_spec: RuntimeSpec,
    ) -> VerifiedRuntimeImage:
        image_digest = _image_digest(runtime_spec.runtime.image)
        if self._runtime_image_preparer is not None:
            receipt = self._runtime_image_preparer(document, runtime_spec, build)
            value = _runtime_image_receipt(receipt)
        elif self._runtime_image_resolver is not None:
            receipt = self._runtime_image_resolver(document, image_digest, runtime_spec)
            value = _runtime_image_receipt(receipt)
        else:
            raise ExecutionPlanEvidenceUnknown(
                "verified OCI archive receipt is unavailable for the selected runtime image"
            )
        image = value
        if image.image_digest != image_digest:
            raise ExecutionPlanEvidenceUnknown(
                "runtime image receipt does not match the compiled runtime image"
            )
        return image


def _runtime_image_receipt(receipt: RuntimeImageReceipt) -> VerifiedRuntimeImage:
    """Project a preparation receipt into the strict launch-image DTO.

    Runtime-image preparation persists provenance and storage fields alongside
    the portable launch identity.  The agent plan carries only the verified
    schema-2 image receipt, so the projection is explicit and rejects
    accidental leakage of the storage envelope.
    """

    if not isinstance(receipt, RuntimeImageReceipt):
        raise ExecutionPlanEvidenceUnknown("runtime image receipt is invalid")
    try:
        return VerifiedRuntimeImage(
            image_digest=receipt.image_digest,
            oci_layout_sha256=receipt.oci_archive_sha256,
            image_bytes=receipt.image_bytes,
            build_id=receipt.build_id,
            local_image_config_id=receipt.local_image_config_id,
            runtime_interface_label=receipt.runtime_interface_label,
        )
    except ValueError as error:
        raise ExecutionPlanEvidenceUnknown(
            "verified runtime image receipt is invalid"
        ) from error


def _build_package(build: RecipeBuild) -> dict[str, object]:
    if (
        build.state != "succeeded"
        or not isinstance(build.image_digest, str)
        or not isinstance(build.build_input_sha256, str)
    ):
        raise ExecutionPlanEvidenceUnknown(
            "successful Controller build receipt is unavailable"
        )
    return {
        "image_digest": build.image_digest,
        "image_reference": f"localhost/vonk/recipe-build@{build.image_digest}",
        "build_input_sha256": build.build_input_sha256,
    }


def _image_digest(value: str) -> str:
    if "@sha256:" not in value:
        raise ExecutionPlanCompilationError(
            "compiled runtime image digest is unavailable"
        )
    digest = value.rsplit("@", 1)[-1]
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ExecutionPlanCompilationError("compiled runtime image digest is invalid")
    return digest


def _world_size(recipe: RecipeDefinition) -> int:
    return recipe.topology.world_size


@dataclass(frozen=True, slots=True)
class _PlacementTarget:
    """A mapped rank/role pair that is not a persisted mapping row."""

    rank: int
    role: str


def _placement(
    recipe: RecipeDefinition,
    runtime_spec: RuntimeSpec,
    node: ClusterMappingNode | _PlacementTarget,
    world_size: int,
) -> CompiledPlacement:
    endpoint = runtime_spec.endpoint
    role = next(
        (item for item in recipe.topology.roles if item.name == node.role), None
    )
    if role is None:
        raise ExecutionPlanCompilationError(
            f"mapped role {node.role!r} is absent from the canonical recipe topology"
        )
    reserved = role.resources.memory.peak_bytes
    memory_floor = PLATFORM_MEMORY_FLOOR_BYTES
    if recipe.interfaces[0].adapter == "openai":
        if endpoint is None:
            raise ExecutionPlanCompilationError(
                "compiled runtime endpoint is unavailable"
            )
        port: int | None = endpoint.port
        if port <= 0 or port > 65535:
            raise ExecutionPlanCompilationError(
                "compiled runtime endpoint port is invalid"
            )
    else:
        if endpoint is not None:
            raise ExecutionPlanCompilationError(
                "job recipe has an unexpected runtime endpoint"
            )
        port = None
    return CompiledPlacement(
        endpoint_address=None,
        rank=node.rank,
        role=node.role,
        world_size=world_size,
        local_address=None,
        master_address=None,
        # Installation has no rendezvous authority. Bind the complete fabric
        # placement together in the signed start request.
        master_port=None,
        port=port,
        reserved_memory_bytes=reserved,
        memory_floor_bytes=memory_floor,
    )


def _bind_runtime_artifacts(
    runtime_spec: RuntimeSpec, models: Mapping[str, ModelDefinition]
) -> RuntimeSpec:
    """Add exact file bytes from canonical model revisions to harness output."""

    by_identity: dict[tuple[str, str], ModelFile] = {}
    for identity, model in models.items():
        for file in model.files:
            by_identity[(identity, file.id)] = file
    bound: list[SpecArtifact] = []
    for artifact in runtime_spec.artifacts:
        file = by_identity.get((artifact.model.content_sha256, artifact.file_id))
        if file is None:
            raise ExecutionPlanCompilationError(
                "selected model file is absent from the canonical model manifest"
            )
        if artifact.path != file.path:
            raise ExecutionPlanCompilationError(
                "selected model file path does not match the canonical manifest"
            )
        bound.append(
            artifact.model_copy(
                update={
                    "sha256": file.sha256,
                    "bytes": file.size_bytes,
                    "mount": SpecModelMount(
                        source=f"/run/vonk/models/{artifact.selection_id}",
                        target=artifact.mount.target,
                    ),
                }
            )
        )
    result = runtime_spec.model_copy(update={"artifacts": bound})
    result.identity = result.identity.model_copy(
        update={"execution_sha256": result.launch_identity_sha256()}
    )
    return result


__all__ = [
    "ControllerExecutionPlanService",
    "ExecutionPlanCompilationError",
    "RuntimeImageReceipt",
]
