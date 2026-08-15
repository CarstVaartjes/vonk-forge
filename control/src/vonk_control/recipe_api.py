"""Typed HTTP workflow for recipe admission and lifecycle operations."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from .audit import AuditRecord
from .auth import Actor
from .recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
    RecipeOperationView,
    RecipeRunStatus,
)

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_DIGEST = r"^[0-9a-f]{64}$"

RECIPE_OPERATION_IDS = {
    ("post", "/api/v1/recipes/mapping-plans/preview"): "previewRecipeMapping",
    ("post", "/api/v1/recipes/mappings"): "createRecipeMapping",
    ("post", "/api/v1/recipes/build-plans/preview"): "previewRecipeBuild",
    ("post", "/api/v1/recipes/source-checks"): "checkRecipeBuildSource",
    ("post", "/api/v1/recipes/builds"): "buildRecipe",
    ("post", "/api/v1/recipes/image-distributions"): "distributeRecipeImage",
    ("post", "/api/v1/recipes/install-plans/preview"): "previewRecipeInstall",
    ("post", "/api/v1/recipes/installations"): "installRecipe",
    ("post", "/api/v1/recipes/run-plans/preview"): "previewRecipeRun",
    ("post", "/api/v1/recipes/runs"): "startRecipeRun",
    ("post", "/api/v1/recipes/stop-plans/preview"): "previewRecipeStop",
    (
        "post",
        "/api/v1/recipes/uninstall-plans/preview",
    ): "previewRecipeUninstall",
    ("get", "/api/v1/recipes/runs/{run_id}"): "getRecipeRunStatus",
    ("get", "/api/v1/recipes/operations/{operation_id}"): "getRecipeOperation",
    ("post", "/api/v1/recipes/operations/{operation_id}/retry"): "retryRecipeOperation",
    ("post", "/api/v1/recipes/runs/{run_id}/stop"): "stopRecipeRun",
    (
        "post",
        "/api/v1/recipes/installations/{installation_id}/uninstall",
    ): "uninstallRecipe",
}


class AuditSink(Protocol):
    def append(self, record: AuditRecord) -> None: ...


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanReason(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=512)


class MappingNodePlanResponse(StrictModel):
    node_id: str
    rank: int
    role: str
    endpoint_owner: bool


class MappingPlanResponse(StrictModel):
    recipe_revision_id: str
    recipe_content_sha256: str
    topology_name: str
    generation: int
    parameters: dict[str, object]
    nodes: list[MappingNodePlanResponse]
    placement_digest: str


class MappingResponse(StrictModel):
    mapping_id: str
    generation: int
    placement_digest: str


class BuildPlanResponse(StrictModel):
    build_id: str
    recipe_revision_id: str
    recipe_content_sha256: str
    builder_node_id: str
    source_bundle_sha256: str
    build_input_sha256: str


class SourcePolicyFindingResponse(StrictModel):
    code: str
    path: str
    line: int | None
    detail: str


class SourcePolicyResponse(StrictModel):
    passed: bool
    source_bundle_sha256: str
    dockerfile: str
    findings: list[SourcePolicyFindingResponse]


class InstallNodePlanResponse(StrictModel):
    node_id: str
    rank: int
    role: str
    allowed: bool
    inventory_observed_at: datetime | None
    free_bytes: int | None
    active_reserved_bytes: int
    reused_bytes: int
    required_download_bytes: int
    required_bytes: int
    disk_floor_bytes: int
    free_after_bytes: int | None
    blockers: list[PlanReason]
    warnings: list[PlanReason]


class InstallPlanResponse(StrictModel):
    mapping_id: str
    mapping_generation: int
    recipe_build_id: str
    image_digest: str
    recipe_revision_id: str
    recipe_content_sha256: str
    allowed: bool
    nodes: list[InstallNodePlanResponse]
    plan_digest: str


class RunNodePlanResponse(StrictModel):
    node_id: str
    rank: int
    role: str
    endpoint_owner: bool
    port: int
    allowed: bool
    inventory_observed_at: datetime | None
    memory_kind: str
    required_memory_bytes: int
    available_memory_bytes: int | None
    active_reserved_bytes: int
    free_after_bytes: int | None
    memory_floor_bytes: int
    fabric_address: str | None
    fabric_bandwidth_mbps: int | None
    rendezvous_port: int | None
    blockers: list[PlanReason]
    warnings: list[PlanReason]


class RunPlanResponse(StrictModel):
    installation_id: str
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    mapping_id: str
    mapping_generation: int
    recipe_revision_id: str
    allowed: bool
    nodes: list[RunNodePlanResponse]
    plan_digest: str


class StopNodeImpactResponse(StrictModel):
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    rank: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=1, max_length=24)
    reserved_memory_bytes: int = Field(ge=0)
    active_memory_reservation_bytes: int = Field(ge=0)


class StopPlanResponse(StrictModel):
    run_id: str = Field(pattern=_UUID)
    installation_id: str = Field(pattern=_UUID)
    recipe_revision_id: str = Field(pattern=_UUID)
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    run_state: str = Field(min_length=1, max_length=24)
    route_state: str = Field(pattern=r"^(withdrawn|pending|published|failed)$")
    route_generation: int | None = Field(default=None, ge=1)
    route_digest: str | None = Field(default=None, pattern=_DIGEST)
    authority_digest: str = Field(pattern=_DIGEST)
    allowed: bool
    route_withdrawal: bool
    nodes: list[StopNodeImpactResponse] = Field(max_length=1024)
    total_active_memory_reservation_bytes: int = Field(ge=0)
    blockers: list[PlanReason] = Field(max_length=32)
    warnings: list[PlanReason] = Field(max_length=32)
    plan_digest: str = Field(pattern=_DIGEST)


class UninstallNodeImpactResponse(StrictModel):
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    rank: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=1, max_length=24)
    installed_bytes: int | None = Field(default=None, ge=0)


class UninstallActiveRunResponse(StrictModel):
    run_id: str = Field(pattern=_UUID)
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    state: str = Field(min_length=1, max_length=24)
    route_state: str = Field(pattern=r"^(withdrawn|pending|published|failed)$")


class UninstallConsequencesResponse(StrictModel):
    catalog_retained: bool
    automatic_stop: bool
    reinstall_required: bool


class UninstallPlanResponse(StrictModel):
    installation_id: str = Field(pattern=_UUID)
    recipe_id: str = Field(pattern=_UUID)
    recipe_revision_id: str = Field(pattern=_UUID)
    recipe_content_sha256: str = Field(pattern=_DIGEST)
    recipe_content: dict[str, object]
    installation_authority_digest: str = Field(pattern=_DIGEST)
    original_plan_digest: str = Field(pattern=_DIGEST)
    installation_state: str = Field(min_length=1, max_length=24)
    allowed: bool
    nodes: list[UninstallNodeImpactResponse] = Field(max_length=1024)
    bytes_removed: int | None = Field(default=None, ge=0)
    active_runs: list[UninstallActiveRunResponse] = Field(max_length=128)
    active_run_count: int = Field(ge=0)
    active_runs_truncated: bool
    blockers: list[PlanReason] = Field(max_length=32)
    warnings: list[PlanReason] = Field(max_length=32)
    consequences: UninstallConsequencesResponse
    plan_digest: str = Field(pattern=_DIGEST)


class OperationResponse(StrictModel):
    id: str
    kind: str
    owner_id: str
    state: str
    plan_digest: str
    nodes: list[str]
    result: dict[str, object] | None


class RunRankStatusResponse(StrictModel):
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    rank: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=1, max_length=24)
    observed_at: datetime
    age_seconds: float = Field(ge=0)
    fresh: bool


class RunStatusResponse(StrictModel):
    id: str = Field(pattern=_UUID)
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    state: str = Field(min_length=1, max_length=24)
    route_state: str = Field(pattern=r"^(withdrawn|pending|published|failed)$")
    healthy: bool
    ranks: list[RunRankStatusResponse]


class InstallPreviewRequest(StrictModel):
    mapping_id: str = Field(pattern=_UUID)
    recipe_build_id: str = Field(pattern=_UUID)


class MappingPreviewRequest(StrictModel):
    recipe_revision_id: str = Field(pattern=_UUID)
    node_ids: list[str] = Field(min_length=1, max_length=1024)
    parameters: dict[str, object] = Field(default_factory=dict, max_length=128)


class MappingRequest(MappingPreviewRequest):
    placement_digest: str = Field(pattern=_DIGEST)
    request_key: str = Field(pattern=_UUID)


class BuildPreviewRequest(StrictModel):
    recipe_revision_id: str = Field(pattern=_UUID)
    builder_node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")


class SourceCheckRequest(StrictModel):
    recipe_revision_id: str = Field(pattern=_UUID)


class BuildRequest(BuildPreviewRequest):
    build_input_sha256: str = Field(pattern=_DIGEST)
    request_key: str = Field(pattern=_UUID)


class ImageDistributionRequest(StrictModel):
    recipe_build_id: str = Field(pattern=_UUID)
    mapping_id: str = Field(pattern=_UUID)
    mapping_generation: int = Field(ge=1)
    request_key: str = Field(pattern=_UUID)


class InstallRequest(InstallPreviewRequest):
    plan_digest: str = Field(pattern=_DIGEST)
    request_key: str = Field(pattern=_UUID)


class RunPreviewRequest(StrictModel):
    installation_id: str = Field(pattern=_UUID)
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")


class RunRequest(RunPreviewRequest):
    plan_digest: str = Field(pattern=_DIGEST)
    request_key: str = Field(pattern=_UUID)


class StopPreviewRequest(StrictModel):
    run_id: str = Field(pattern=_UUID)


class StopRequest(StrictModel):
    plan_digest: str = Field(pattern=_DIGEST)
    request_key: str = Field(pattern=_UUID)


class UninstallPreviewRequest(StrictModel):
    installation_id: str = Field(pattern=_UUID)


class UninstallRequest(StrictModel):
    plan_digest: str = Field(pattern=_DIGEST)
    request_key: str = Field(pattern=_UUID)


class RequestKey(StrictModel):
    request_key: str = Field(pattern=_UUID)


def install_recipe_operation_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    audits: AuditSink,
    service: RecipeOperationService | None,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(RECIPE_OPERATION_IDS)
    authenticated = actor_dependency

    def recipes() -> RecipeOperationService:
        if service is None:
            raise HTTPException(status_code=503, detail="recipe operations unavailable")
        return service

    def administrator(actor: Actor) -> None:
        if actor.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")

    def operation(value: RecipeOperationView) -> dict[str, object]:
        return {
            "id": value.id,
            "kind": value.kind,
            "owner_id": value.owner_id,
            "state": value.state,
            "plan_digest": value.plan_digest,
            "nodes": list(value.nodes),
            "result": value.result,
        }

    def conflict(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "code": "recipe.operation_conflict",
                "detail": str(error)[:256],
                "request_id": request.state.request_id,
            },
        )

    @app.post(
        "/api/v1/recipes/mapping-plans/preview",
        response_model=MappingPlanResponse,
        operation_id="previewRecipeMapping",
    )
    def preview_mapping(body: MappingPreviewRequest, actor: Actor = authenticated):
        administrator(actor)
        try:
            return asdict(
                recipes().preview_mapping(
                    body.recipe_revision_id,
                    tuple(body.node_ids),
                    parameters=body.parameters,
                    actor=actor.subject,
                )
            )
        except (KeyError, RecipeOperationConflict, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None

    @app.post(
        "/api/v1/recipes/mappings",
        response_model=MappingResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="createRecipeMapping",
    )
    def create_mapping(
        body: MappingRequest, request: Request, actor: Actor = authenticated
    ):
        administrator(actor)
        try:
            plan = recipes().preview_mapping(
                body.recipe_revision_id,
                tuple(body.node_ids),
                parameters=body.parameters,
                actor=actor.subject,
            )
            if plan.placement_digest != body.placement_digest:
                raise RecipeOperationConflict(
                    "submitted mapping does not match preview"
                )
            mapping_id = recipes().create_mapping(plan, actor=actor.subject)
        except (KeyError, RecipeOperationConflict, ValueError) as error:
            return conflict(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.mapping.create",
                None,
                (mapping_id, plan.placement_digest, *body.node_ids),
            )
        )
        return {
            "mapping_id": mapping_id,
            "generation": plan.generation,
            "placement_digest": plan.placement_digest,
        }

    @app.post(
        "/api/v1/recipes/source-checks",
        response_model=SourcePolicyResponse,
        operation_id="checkRecipeBuildSource",
    )
    def check_source(body: SourceCheckRequest, actor: Actor = authenticated):
        administrator(actor)
        try:
            return asdict(recipes().check_build_source(body.recipe_revision_id))
        except (KeyError, RecipeOperationConflict, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None

    @app.post(
        "/api/v1/recipes/build-plans/preview",
        response_model=BuildPlanResponse,
        operation_id="previewRecipeBuild",
    )
    def preview_build(body: BuildPreviewRequest, actor: Actor = authenticated):
        administrator(actor)
        try:
            plan = recipes().preview_build(
                body.recipe_revision_id, body.builder_node_id
            )
        except (KeyError, RecipeOperationConflict, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None
        return {
            key: value for key, value in asdict(plan).items() if key != "agent_payload"
        }

    @app.post(
        "/api/v1/recipes/builds",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="buildRecipe",
    )
    def build(body: BuildRequest, request: Request, actor: Actor = authenticated):
        administrator(actor)
        try:
            plan = recipes().preview_build(
                body.recipe_revision_id, body.builder_node_id
            )
            value = recipes().build(
                plan,
                build_input_sha256=body.build_input_sha256,
                actor=actor.subject,
                request_id=body.request_key,
            )
        except (KeyError, RecipeOperationConflict, ValueError) as error:
            return conflict(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.build",
                None,
                (value.owner_id, value.plan_digest, *value.nodes),
            )
        )
        return operation(value)

    @app.post(
        "/api/v1/recipes/image-distributions",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="distributeRecipeImage",
    )
    def distribute_image(
        body: ImageDistributionRequest,
        request: Request,
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            value = recipes().distribute_image(
                body.recipe_build_id,
                body.mapping_id,
                mapping_generation=body.mapping_generation,
                actor=actor.subject,
                request_id=body.request_key,
            )
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.image.distribute",
                None,
                (body.recipe_build_id, body.mapping_id, *value.nodes),
            )
        )
        return operation(value)

    @app.post(
        "/api/v1/recipes/install-plans/preview",
        response_model=InstallPlanResponse,
        operation_id="previewRecipeInstall",
    )
    def preview_install(body: InstallPreviewRequest, actor: Actor = authenticated):
        administrator(actor)
        return asdict(recipes().preview_install(body.mapping_id, body.recipe_build_id))

    @app.post(
        "/api/v1/recipes/installations",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="installRecipe",
    )
    def install(body: InstallRequest, request: Request, actor: Actor = authenticated):
        administrator(actor)
        try:
            plan = recipes().preview_install(body.mapping_id, body.recipe_build_id)
            value = recipes().install(
                plan,
                plan_digest=body.plan_digest,
                actor=actor.subject,
                request_id=body.request_key,
            )
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.install",
                None,
                (value.owner_id, value.plan_digest, *value.nodes),
            )
        )
        return operation(value)

    @app.post(
        "/api/v1/recipes/run-plans/preview",
        response_model=RunPlanResponse,
        operation_id="previewRecipeRun",
    )
    def preview_run(body: RunPreviewRequest, actor: Actor = authenticated):
        administrator(actor)
        return asdict(recipes().preview_run(body.installation_id, body.alias))

    @app.post(
        "/api/v1/recipes/stop-plans/preview",
        response_model=StopPlanResponse,
        operation_id="previewRecipeStop",
    )
    def preview_stop(body: StopPreviewRequest, actor: Actor = authenticated):
        administrator(actor)
        try:
            return asdict(recipes().preview_stop(body.run_id))
        except RecipeOperationConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None

    @app.post(
        "/api/v1/recipes/uninstall-plans/preview",
        response_model=UninstallPlanResponse,
        operation_id="previewRecipeUninstall",
    )
    def preview_uninstall(body: UninstallPreviewRequest, actor: Actor = authenticated):
        administrator(actor)
        try:
            return asdict(recipes().preview_uninstall(body.installation_id))
        except RecipeOperationConflict as error:
            raise HTTPException(status_code=409, detail=str(error)[:256]) from None

    @app.post(
        "/api/v1/recipes/runs",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="startRecipeRun",
    )
    def start(body: RunRequest, request: Request, actor: Actor = authenticated):
        administrator(actor)
        try:
            value = recipes().replay_start(
                body.installation_id,
                body.alias,
                plan_digest=body.plan_digest,
                request_id=body.request_key,
            )
            if value is None:
                plan = recipes().preview_run(body.installation_id, body.alias)
                value = recipes().start(
                    plan,
                    plan_digest=body.plan_digest,
                    actor=actor.subject,
                    request_id=body.request_key,
                )
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.start",
                None,
                (value.owner_id, value.plan_digest, *value.nodes),
            )
        )
        return operation(value)

    @app.get(
        "/api/v1/recipes/runs/{run_id}",
        response_model=RunStatusResponse,
        operation_id="getRecipeRunStatus",
    )
    def run_status(
        run_id: str = Path(pattern=_UUID), actor: Actor = authenticated
    ) -> dict[str, object]:
        administrator(actor)
        try:
            value: RecipeRunStatus = recipes().run_status(run_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="recipe run not found"
            ) from None
        return asdict(value)

    @app.get(
        "/api/v1/recipes/operations/{operation_id}",
        response_model=OperationResponse,
        operation_id="getRecipeOperation",
    )
    def get_operation(
        operation_id: str = Path(pattern=_UUID), _actor: Actor = authenticated
    ):
        try:
            return operation(recipes().get(operation_id))
        except KeyError:
            raise HTTPException(
                status_code=404, detail="recipe operation not found"
            ) from None

    @app.post(
        "/api/v1/recipes/operations/{operation_id}/retry",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="retryRecipeOperation",
    )
    def retry(
        body: RequestKey,
        request: Request,
        operation_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            value = recipes().retry(
                operation_id, actor=actor.subject, request_id=body.request_key
            )
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.retry",
                None,
                (operation_id, value.id),
            )
        )
        return operation(value)

    @app.post(
        "/api/v1/recipes/runs/{run_id}/stop",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="stopRecipeRun",
    )
    def stop(
        body: StopRequest,
        request: Request,
        run_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            value = recipes().stop(
                run_id,
                plan_digest=body.plan_digest,
                actor=actor.subject,
                request_id=body.request_key,
            )
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.stop",
                None,
                (run_id, value.plan_digest, *value.nodes),
            )
        )
        return operation(value)

    @app.post(
        "/api/v1/recipes/installations/{installation_id}/uninstall",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="uninstallRecipe",
    )
    def uninstall(
        body: UninstallRequest,
        request: Request,
        installation_id: str = Path(pattern=_UUID),
        actor: Actor = authenticated,
    ):
        administrator(actor)
        try:
            value = recipes().uninstall(
                installation_id,
                plan_digest=body.plan_digest,
                actor=actor.subject,
                request_id=body.request_key,
            )
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "recipe.uninstall",
                None,
                (installation_id, value.plan_digest, *value.nodes),
            )
        )
        return operation(value)


__all__ = ["RECIPE_OPERATION_IDS", "install_recipe_operation_routes"]
