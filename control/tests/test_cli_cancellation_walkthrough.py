"""Disposable installed-CLI U6 setup across cache and Profile owners.

The smoke drives only real Controller services and authenticated CLI routes.
Its deterministic executor reports exact AgentJobService receipts; no physical
Spark is contacted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentClaim,
    canonical_message,
    format_model_identity,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.execution_plan_service import (
    ControllerExecutionPlanService,
    _build_package,
)
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileInput,
    FleetProfileView,
)
from vonk_control.fleet_profiles import (
    FleetProfileService,
    build_production_fleet_profile_service,
)
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.jobs import JobService
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_api import register_model_cache_operation_provider
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentPresence,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    RecipeInstallation,
    User,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_cli_operator_walkthrough import (
    _AuthorizationHeaders,
    _build_installed_vonkctl,
    _session_environment,
)
from .test_fleet_profile_cancel import (
    _agent_result,
    _profile_worker_process_dies_with_pending_cancel,
    _resume_profile_cancel_process,
)
from .test_profile_load_installed_cli import (
    _https_api_peer,
)
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    mark_current_exact_observations,
    setup_services,
)
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


@dataclass(slots=True)
class _WalkthroughState:
    sessions: sessionmaker[Session]
    run_switch: RunSwitchOperationService
    profiles: FleetProfileService
    profile: FleetProfileView
    application: FleetProfileApplicationView
    start_agents: AgentJobService
    start_claim: AgentClaim
    stop_child: str
    run_child: str
    cache: ModelCacheService
    cache_http_client: httpx.Client
    library: LibraryProjection
    recipe_image_availability: RecipeImageAvailabilityService
    recipe_selector: str
    profile_model_digest: str
    profile_file_sha: str
    profile_expected_bytes: int


pytest_plugins = ("tests.test_profile_load_installed_cli",)
pytestmark = [
    pytest.mark.lane,
    pytest.mark.skipif(
        "VONK_CANCELLATION_WALKTHROUGH_MODE" not in os.environ,
        reason="set VONK_CANCELLATION_WALKTHROUGH_MODE=smoke or interactive to opt in",
    ),
]

_PROFILE_MODEL_BYTES = b"verified reusable U6 model fixture bytes"
_PROFILE_MODEL_REVISION = "a" * 40
_TOKEN_KEY = b"u6-cancellation-walkthrough-token-key"


def _profile_model_documents() -> tuple[dict[str, object], str]:
    from importlib import resources

    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    source = document["source"]
    files = document["files"]
    assert isinstance(source, dict) and isinstance(files, list) and files
    source.update(
        repository="https://huggingface.co/fixture/u6-profile-model",
        revision=_PROFILE_MODEL_REVISION,
    )
    first_file = files[0]
    assert isinstance(first_file, dict)
    first_file.update(
        sha256=hashlib.sha256(_PROFILE_MODEL_BYTES).hexdigest(),
        size_bytes=len(_PROFILE_MODEL_BYTES),
    )
    model = ModelDefinition.model_validate(document)
    return model.model_dump(mode="json"), content_sha256(model)


def _add_second_single_node_fixture(
    sessions: sessionmaker[Session], *, node_id: str
) -> None:
    capabilities = ("runtime.vonk.v1", "recipe.operations.v1")
    serial = "u6-serial-1"
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=list(capabilities),
            )
        )
        session.flush()
        session.add(
            AgentCertificate(
                serial=serial,
                node_id=node_id,
                fingerprint="u6-fixture-fingerprint",
                not_before=NOW,
                not_after=datetime(2027, 8, 7, 12, tzinfo=UTC),
            )
        )
        session.add(
            AgentPresence(
                node_id=node_id,
                certificate_serial=serial,
                certificate_fingerprint="u6-fixture-fingerprint",
                management_address="192.168.1.212",
                observed_at=NOW,
            )
        )
    InventoryRepository(sessions, clock=lambda: NOW).record(
        InventorySnapshotInput(
            node_id,
            NOW,
            10_000,
            8_000,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            capabilities,
            memory_pool="shared",
        )
    )


def _ensure_catalog_heads(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as session:
        for revision in session.scalars(select(CatalogDocumentRevision)):
            head = session.scalar(
                select(CatalogDocumentHead).where(
                    CatalogDocumentHead.kind == revision.kind,
                    CatalogDocumentHead.publisher == revision.publisher,
                    CatalogDocumentHead.slug == revision.slug,
                )
            )
            if head is None:
                session.add(
                    CatalogDocumentHead(
                        kind=revision.kind,
                        publisher=revision.publisher,
                        slug=revision.slug,
                        active_revision_id=revision.id,
                    )
                )


def _seed_reusable_model(
    cache: ModelCacheService,
    *,
    model_digest: str,
) -> tuple[str, str, int]:
    """Publish actual fixture-source bytes through the cache worker owner."""

    manifest = cache.resolve_artifact_set(model_content_sha256=model_digest)
    assert len(manifest.artifacts) == 1
    artifact = manifest.artifacts[0]
    preview = cache.download_preview(
        model_content_sha256=model_digest,
    )
    operation = cache.start_download(
        actor="admin",
        request_key=str(uuid4()),
        selector="vonk-forge/synthetic-tiny-fp16",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model_digest,
    )
    assert cache.run_pending(limit=1) == 1
    assert cache.get_operation(operation.id).state == "succeeded"
    assert cache._object_is_available(artifact.sha256, artifact.expected_bytes)
    return artifact.sha256, manifest.digest, artifact.expected_bytes


def _prepare_profile_cancellation(
    sessions,
    lifecycle,
    run_switch,
    nodes,
    build_id,
    existing_installation,
    cache_set_digest,
):
    from vonk_control.models import CatalogDocumentRevision

    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        installation = session.get(RecipeInstallation, existing_installation.owner_id)
    assert revision is not None
    assert isinstance(build_id, str)
    assert installation is not None
    assert installation.recipe_build_id == build_id
    assert installation.state == "installed"
    model = revision.document["models"][0]["model"]
    model_identity = format_model_identity(
        model["publisher"], model["slug"], model["content_sha256"]
    )
    old_run = _start_existing_workload(
        sessions,
        lifecycle,
        existing_installation.owner_id,
        nodes,
        model_identity=model_identity,
        artifact_set_digest=cache_set_digest,
    )
    del old_run

    service = build_production_fleet_profile_service(
        sessions, clock=lambda: NOW, run_switch_operations=run_switch
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "U6 cancellation fixture",
                "assignments": [
                    {
                        "recipe_selector": f"{revision.publisher}/{revision.slug}",
                        "spark_ids": [nodes[0]],
                        "desired_state": "running",
                        "assignment_name": "u6-first-effect",
                    },
                    {
                        "recipe_selector": f"{revision.publisher}/{revision.slug}",
                        "spark_ids": [nodes[1]],
                        "desired_state": "running",
                        "assignment_name": "u6-second-effect",
                    },
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed, preview.reasons
    application = service.load(
        profile.number,
        request_key=str(uuid4()),
        actor="admin",
        expected_plan_digest=preview.plan_digest,
    )
    return service, profile, application


def _start_existing_workload(
    sessions,
    lifecycle,
    installation_id: str,
    nodes: tuple[str, ...],
    *,
    model_identity: str,
    artifact_set_digest: str,
):
    from vonk_control.agent_jobs import AgentJobService

    plan = lifecycle.preview_run(installation_id, "u6-existing-workload")
    operation = lifecycle.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == operation.id)
                .order_by(AgentOperation.node_id)
            )
        )
    assert len(children) == 1
    child = children[0]
    jobs = AgentJobService(
        sessions, clock=lambda: NOW, result_consumer=lifecycle.consume_agent_result
    )
    node_index = nodes.index(child.node_id)
    with sessions.begin() as session:
        node = session.get(AgentNode, child.node_id)
        assert node is not None
        node.capabilities = sorted(set(node.capabilities or ()) | {"recipe.start"})
        runtime_identity = {
            **PACKAGED_RUNTIME_IDENTITY,
            "architecture": node.architecture,
        }
        if node.observation_receipt_public_key is not None:
            runtime_identity["observation_receipt_public_key"] = (
                node.observation_receipt_public_key
            )
    claim = claim_agent(
        jobs,
        child.node_id,
        f"serial-{node_index}",
        300,
        capabilities=[
            "agent.runtime.rust.v1",
            "recipe.operations.v1",
            "recipe.start",
        ],
        runtime_identity=runtime_identity,
    )
    assert claim is not None
    assert claim.job_id == operation.id and claim.operation_id == child.id
    payload = claim.payload
    identity = {
        "recipe_revision_id": payload["recipe_revision_id"],
        "recipe_content_sha256": payload["recipe_content_sha256"],
        "image_digest": payload["image_digest"],
        "artifact_set_digest": artifact_set_digest,
        "model_identity": model_identity,
        "rank": payload["rank"],
        "world_size": payload["world_size"],
        "endpoint": f"http://{payload['endpoint_address']}:{payload['port']}",
        "memory_reservation_bytes": payload["reserved_memory_bytes"],
        "ready": True,
        "run_generation": payload["run_generation"],
        "runtime_arguments_sha256": "c" * 64,
        "local_address": payload["local_address"],
        "master_address": payload["master_address"],
        "master_port": payload["master_port"],
    }
    evidence = {
        **identity,
        "evidence_digest": hashlib.sha256(canonical_message(identity)).hexdigest(),
    }
    jobs.record_result(
        _agent_result(
            claim,
            state="succeeded",
            result={
                "evidence": evidence,
                "evidence_digest": evidence["evidence_digest"],
            },
        )
    )
    assert lifecycle.get(operation.id).state == "succeeded"
    mark_current_exact_observations(sessions, operation.owner_id, NOW)
    return operation


def _agent_service_and_claim(
    sessions,
    lifecycle,
    node_id: str,
    nodes: tuple[str, ...],
    job_id: str,
    *,
    capability: str,
) -> tuple[AgentJobService, AgentClaim]:
    from vonk_control.agent_jobs import AgentJobService

    capabilities = ["agent.runtime.rust.v1", "recipe.operations.v1", capability]
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.capabilities = sorted(set(node.capabilities or ()) | set(capabilities))
        identity = {**PACKAGED_RUNTIME_IDENTITY, "architecture": node.architecture}
        if node.observation_receipt_public_key is not None:
            identity["observation_receipt_public_key"] = (
                node.observation_receipt_public_key
            )
    jobs = AgentJobService(
        sessions, clock=lambda: NOW, result_consumer=lifecycle.consume_agent_result
    )
    index = nodes.index(node_id)
    claim = claim_agent(
        jobs,
        node_id,
        f"serial-{index}",
        300,
        capabilities=capabilities,
        runtime_identity=identity,
    )
    assert claim is not None and claim.job_id == job_id
    return jobs, claim


def _complete_pending_runtime_preflight(
    sessions: sessionmaker[Session],
    nodes: tuple[str, ...],
    job_id: str,
    *,
    observed_at: datetime,
) -> None:
    """Return deterministic evidence through the exact AgentJobService fence."""
    from vonk_agent_protocol.runtime_preflight import (
        RuntimePreflightRequest,
    )
    from vonk_control.agent_jobs import _NEXT_CAPABILITIES, AgentJobService
    from vonk_control.runtime_preflight import (
        mandatory_capabilities,
        node_fingerprint,
        request_digest,
    )

    with sessions() as session:
        parent = session.get(Job, job_id)
        assert parent is not None and parent.kind == "runtime.preflight.v1"
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job_id)
        )
        assert operation is not None and operation.kind == "runtime.preflight.v1"
        node = session.get(AgentNode, operation.node_id)
        assert node is not None
        request = RuntimePreflightRequest.model_validate(operation.payload)
        fingerprint = node_fingerprint(node.capabilities or [])
        assert fingerprint is not None
        runtime_identity = {
            **PACKAGED_RUNTIME_IDENTITY,
            "architecture": node.architecture,
        }
        if node.observation_receipt_public_key is not None:
            runtime_identity["observation_receipt_public_key"] = (
                node.observation_receipt_public_key
            )

    index = nodes.index(operation.node_id)
    jobs = AgentJobService(sessions, clock=lambda: observed_at)
    claim = claim_agent(
        jobs,
        operation.node_id,
        f"serial-{index}",
        300,
        capabilities=sorted(_NEXT_CAPABILITIES | {"runtime.preflight.v1"}),
        runtime_identity=runtime_identity,
    )
    assert claim is not None
    assert claim.job_id == parent.id and claim.operation_id == operation.id
    jobs.record_result(
        _agent_result(
            claim,
            state="succeeded",
            result={
                "schema_version": 1,
                "fingerprint": fingerprint,
                "request_sha256": request_digest(request),
                "observed_at": int(observed_at.timestamp()),
                "duration_ms": 1,
                "cached": False,
                "findings": [
                    {
                        "capability": capability,
                        "status": "passed",
                        "code": "available",
                    }
                    for capability in mandatory_capabilities(request)
                ],
            },
        )
    )


def _issue_first_stop_and_second_start(
    sessions,
    lifecycle,
    run_switch,
    service,
    application,
    nodes: tuple[str, ...],
):
    """Complete one exact stop, then issue one start while the next stays queued."""

    active_before = service.application(application.id).progress.switch_adapter
    assert active_before is None
    assert service.tick()
    stop_child_id = None
    stop_job_id = None
    for _ in range(8):
        run_switch.tick()
        current = service.application(application.id).progress.switch_adapter
        assert current is not None
        if current.active_operation_id is not None:
            stop_child_id = current.active_operation_id
        with sessions() as session:
            stop_job = session.scalar(select(Job).where(Job.kind == "recipe.stop"))
        if stop_job is not None:
            stop_job_id = stop_job.id
            break
        service.tick()
    assert stop_child_id is not None and stop_job_id is not None
    stop_operation = run_switch.get(stop_child_id)
    stop_job = (
        stop_operation.result.child_operation_id if stop_operation.result else None
    )
    assert stop_job == stop_job_id

    stop_agents, stop_claim = _agent_service_and_claim(
        sessions,
        lifecycle,
        nodes[0],
        nodes,
        stop_job_id,
        capability="recipe.stop",
    )
    stop_agents.record_result(
        _agent_result(stop_claim, state="succeeded", result={"stopped": True})
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.capabilities = sorted(
            set(node.capabilities or ())
            | {
                "runtime.preflight.v1",
                "runtime.preflight.fingerprint." + "a" * 64,
            }
        )
    for _ in range(8):
        run_switch.tick()
        service.tick()
        current = service.application(application.id).progress.switch_adapter
        assert current is not None
        if current.active_operation_id != stop_child_id:
            break
    current = service.application(application.id).progress.switch_adapter
    assert current is not None and current.active_operation_id is not None
    run_child_id = current.active_operation_id
    assert run_child_id != stop_child_id

    lifecycle_job_id = None
    start_operation = None
    # The start owner has a five-second fresh-inventory retry deadline. Move
    # the deterministic Controller clock past that boundary without sleeping.
    run_switch._clock = lambda: NOW + timedelta(seconds=6)
    completed_preflights: set[str] = set()
    for _ in range(24):
        run_switch.tick()
        service.tick()
        start_operation = run_switch.get(run_child_id)
        result = start_operation.result
        preflight = result.preflight if result is not None else None
        if (
            preflight is not None
            and preflight.pending_job_id is not None
            and preflight.pending_job_id not in completed_preflights
        ):
            _complete_pending_runtime_preflight(
                sessions,
                nodes,
                preflight.pending_job_id,
                observed_at=NOW + timedelta(seconds=6),
            )
            completed_preflights.add(preflight.pending_job_id)
        candidate_id = result.child_operation_id if result is not None else None
        if candidate_id is not None:
            with sessions() as session:
                candidate = session.get(Job, candidate_id)
                if candidate is not None and candidate.kind == "recipe.start":
                    start_job = session.scalar(
                        select(AgentOperation).where(
                            AgentOperation.parent_job_id == candidate_id,
                            AgentOperation.state.in_(("queued", "running")),
                        )
                    )
                    if start_job is not None:
                        lifecycle_job_id = candidate_id
                        break
        if start_operation.state in {"failed", "cancelled"}:
            break
    assert lifecycle_job_id is not None and start_operation is not None, (
        "profile run effect did not issue a recipe.start job: "
        f"run_switch={start_operation!r}; "
        f"profile={service.application(application.id).progress.switch_adapter!r}"
    )
    with sessions() as session:
        start_job = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == lifecycle_job_id,
                AgentOperation.state.in_(("queued", "running")),
            )
        )
        parent = session.get(Job, lifecycle_job_id)
    assert start_job is not None and parent is not None
    assert parent.kind == "recipe.start"
    start_agents, start_claim = _agent_service_and_claim(
        sessions,
        lifecycle,
        start_job.node_id,
        nodes,
        lifecycle_job_id,
        capability="recipe.start",
    )
    assert start_claim.operation_id == start_job.id
    return start_agents, stop_child_id, run_child_id, start_claim


def _api(
    sessions: sessionmaker[Session],
    *,
    route_root: Path,
    profiles,
    run_switch: RunSwitchOperationService,
    cache: ModelCacheService,
    library: LibraryProjection,
    recipe_image_availability,
    codec: TokenCodec,
):
    route_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    operations = durable_operation_services(
        sessions,
        route_root,
        clock=lambda: NOW,
        cursors=codec.cursor_codec(),
        operation_providers=(
            profiles.operation_provider(),
            run_switch.activity_provider(),
        ),
    )
    operations = register_model_cache_operation_provider(operations, cache)
    return create_app(
        jobs=JobService(sessions, clock=lambda: NOW),
        tokens=codec,
        audits=MemoryAuditStore(),
        now=lambda: int(NOW.timestamp()),
        library_projection=library,
        operations=operations,
        fleet_profiles=profiles,
        run_switch_operations=run_switch,
        model_cache=cache,
        recipe_image_availability=recipe_image_availability,
    )


def _run_cli(
    executable: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_cancellation_effects(value: dict[str, object], *, terminal: bool) -> None:
    cancellation = value.get("cancellation")
    assert isinstance(cancellation, dict)
    completed = cancellation["completed_effects"]
    pending = cancellation["pending_effects"]
    cancelled = cancellation["cancelled_effects"]
    assert isinstance(completed, list) and isinstance(pending, list)
    assert isinstance(cancelled, list)
    assert any(
        effect["kind"] == "stop" and effect["outcome"] == "succeeded"
        for effect in completed
    )
    assert any(
        effect["kind"] == "run"
        and effect["outcome"] == ("cancelled" if terminal else "pending")
        for effect in (cancelled if terminal else pending)
    )
    assert any(effect["outcome"] == "not-issued" for effect in cancelled)
    assert (not pending) if terminal else bool(pending)


def _prepare(
    workspace: Path,
    postgres_engine,
    *,
    codec: TokenCodec,
) -> _WalkthroughState:
    """Create real SQL/storage owners and one issued profile start."""

    model_document, model_digest = _profile_model_documents()

    def replace_model(document: dict[str, object]) -> None:
        document.update(model_document)

    def bind_model(document: dict[str, object]) -> None:
        selections = document["models"]
        assert isinstance(selections, list) and selections
        selected_model = selections[0]["model"]
        assert isinstance(selected_model, dict)
        selected_model["content_sha256"] = model_digest

    sessions, lifecycle, _queue, mapping_id, build_id, first_node = setup_services(
        workspace,
        engine=postgres_engine,
        model_transform=replace_model,
        recipe_transform=bind_model,
    )
    second_node = "spk_" + f"{2:032x}"
    _add_second_single_node_fixture(sessions, node_id=second_node)
    nodes = (first_node[0], second_node)
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    runtime_storage = FilesystemRuntimeImageStorage(workspace / "runtime-images")
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=_PROFILE_MODEL_BYTES, request=request
            )
        )
    )
    try:
        cache = ModelCacheService(
            sessions,
            workspace / "managed-model-cache",
            reserve_bytes=0,
            clock=lambda: NOW,
            fixture_sources=True,
            http_client=http_client,
            runtime_archive_available=runtime_storage.build_archive_available,
        )
    except BaseException:
        http_client.close()
        raise
    try:
        profile_file_sha, cache_set_digest, expected_bytes = _seed_reusable_model(
            cache, model_digest=model_digest
        )
        compiler = getattr(
            lifecycle._install_admission._compiled_plan_provider, "__self__", None
        )
        assert isinstance(compiler, ControllerExecutionPlanService)
        compiler._model_cache = cache
        _ensure_catalog_heads(sessions)

        existing_installation = installed_recipe(
            lifecycle,
            mapping_id,
            build_id,
            (nodes[0],),
            request_id=str(uuid4()),
        )
        run_switch = RunSwitchOperationService(
            sessions,
            lifecycle=lifecycle,
            clock=lambda: NOW,
            model_cache=cache,
            build_archive_available=runtime_storage.build_archive_available,
            artifacts=CompleteArtifactInspector(),
            artifact_phase_executor=RecordingArtifactExecutor(),
            memory_floor_bytes=50,
        )
        from vonk_control.library_assessment import LibraryAssessment
        from vonk_control.recipe_image_availability import (
            RecipeImageAvailabilityService,
        )
        from vonk_control.recipe_runtime_specs import (
            compile_runtime_spec,
            resolve_recipe_entities,
        )

        def resolve_recipe(recipe_revision_id: str, *, force: bool = False):
            del force
            with sessions() as session:
                revision = session.get(CatalogDocumentRevision, recipe_revision_id)
                if (
                    revision is None
                    or revision.kind != "recipe"
                    or revision.state != "active"
                ):
                    raise ValueError("selected recipe revision is not active")
                recipe = RecipeDefinition.model_validate_json(
                    json.dumps(revision.document)
                )
                entities = resolve_recipe_entities(
                    session, recipe.model_dump(mode="json")
                )
            with sessions() as session:
                build = session.get(RecipeBuild, build_id)
                if build is None or build.recipe_revision_id != recipe_revision_id:
                    raise ValueError("selected recipe has no exact build receipt")
                package = _build_package(build)
            role = recipe.topology.roles[0]
            compiled = compile_runtime_spec(
                recipe,
                resolved_entities=entities,
                role=role.name,
                rank=0,
                package_handle=package,
            )
            runtime = dict(compiled) | {"recipe_revision_id": revision.id}
            runtime_build_input = package.get("build_input_sha256")
            if isinstance(runtime_build_input, str):
                runtime["build_input_sha256"] = runtime_build_input
            return recipe, runtime

        recipe_image_availability = RecipeImageAvailabilityService(
            sessions,
            storage=runtime_storage,
            authority=resolve_recipe,
            clock=lambda: NOW,
            model_cache=cache,
        )
        assessment = LibraryAssessment(
            sessions,
            run_switch=run_switch,
            model_cache=cache,
            clock=lambda: NOW,
        )
        with sessions() as session:
            recipe_revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
        assert recipe_revision is not None
        recipe_selector = f"{recipe_revision.publisher}/{recipe_revision.slug}"
        resolved_recipe, resolved_runtime = resolve_recipe(recipe_revision.id)
        assert content_sha256(resolved_recipe) == recipe_revision.content_digest
        assert resolved_runtime
        profiles, profile, application = _prepare_profile_cancellation(
            sessions,
            lifecycle,
            run_switch,
            nodes,
            build_id,
            existing_installation,
            cache_set_digest,
        )
        start_agents, stop_child, run_child, start_claim = (
            _issue_first_stop_and_second_start(
                sessions, lifecycle, run_switch, profiles, application, nodes
            )
        )
        library = LibraryProjection(
            sessions,
            cursors=codec.cursor_codec(),
            clock=lambda: NOW,
            runtime_archive_available=runtime_storage.build_archive_available,
            assessment=assessment,
        )
        return _WalkthroughState(
            sessions=sessions,
            run_switch=run_switch,
            profiles=profiles,
            profile=profile,
            application=application,
            start_agents=start_agents,
            start_claim=start_claim,
            stop_child=stop_child,
            run_child=run_child,
            cache=cache,
            cache_http_client=http_client,
            library=library,
            recipe_image_availability=recipe_image_availability,
            recipe_selector=recipe_selector,
            profile_model_digest=model_digest,
            profile_file_sha=profile_file_sha,
            profile_expected_bytes=expected_bytes,
        )
    except BaseException:
        cache.close()
        http_client.close()
        raise


def _settle_after_restart(state: _WalkthroughState, postgres_engine) -> None:
    application = state.application
    profiles = state.profiles
    start_agents = state.start_agents
    start_claim = state.start_claim
    dsn = postgres_engine.url.render_as_string(hide_password=False)

    context = __import__("multiprocessing").get_context("spawn")
    recovery_at = NOW + timedelta(seconds=6)
    profiles._clock = lambda: recovery_at
    state.run_switch._clock = lambda: recovery_at
    start_agents._clock = lambda: recovery_at
    dead_worker = context.Process(
        target=_profile_worker_process_dies_with_pending_cancel,
        args=(dsn, recovery_at.isoformat()),
    )
    dead_worker.start()
    dead_worker.join(timeout=25)
    if dead_worker.is_alive():
        dead_worker.terminate()
        dead_worker.join(timeout=5)
    assert dead_worker.exitcode == 23
    pending = profiles.application(application.id)
    assert pending.cancellation is not None
    assert pending.cancellation.state == "cancelling"
    run_child_id = state.run_child
    sessions = state.sessions
    assert pending.cancellation.dependency == run_child_id
    assert any(
        effect.operation_id == run_child_id
        for effect in pending.cancellation.pending_effects
    )
    with sessions() as session:
        issued_start = session.get(AgentOperation, start_claim.operation_id)
        assert issued_start is not None
        assert issued_start.parent_job_id == start_claim.job_id
        assert issued_start.state == "running"

    directive = start_agents.heartbeat(start_claim, None, 30)
    assert directive.cancel_requested is True
    start_agents.record_result(
        _agent_result(
            start_claim,
            state="cancelled",
            result={
                "error_code": "operation_cancelled",
                "reason": "U6 deterministic executor returned the exact cancellation receipt",
            },
        )
    )
    resumed_worker = context.Process(
        target=_resume_profile_cancel_process,
        args=(
            dsn,
            (NOW + timedelta(seconds=61)).isoformat(),
            application.id,
        ),
    )
    resumed_worker.start()
    resumed_worker.join(timeout=30)
    if resumed_worker.is_alive():
        resumed_worker.terminate()
        resumed_worker.join(timeout=5)
    assert resumed_worker.exitcode == 0


def _run_walkthrough(postgres_engine, mode: str) -> None:
    with tempfile.TemporaryDirectory(prefix="vonk-cli-cancellation-") as temporary:
        workspace = Path(temporary)
        workspace.chmod(0o700)
        executable = _build_installed_vonkctl(workspace / "installed-cli")
        codec = TokenCodec(_TOKEN_KEY)
        now_seconds = int(NOW.timestamp())
        administrator = Actor("administrator", "administrator")
        headers = _AuthorizationHeaders(
            Authorization=f"Bearer {codec.issue(administrator, ttl_seconds=3_600, now=now_seconds)}"
        )
        state = _prepare(workspace, postgres_engine, codec=codec)
        try:
            cache = state.cache
            sessions = state.sessions
            profile = state.profile
            application = state.application
            with sessions.begin() as session:
                if session.get(User, "administrator") is None:
                    session.add(User(subject="administrator", role="administrator"))

            operator_cwd = workspace / "operator-cwd"
            operator_cwd.mkdir(mode=0o700)
            api = _api(
                sessions,
                route_root=workspace / "routes",
                profiles=state.profiles,
                run_switch=state.run_switch,
                cache=cache,
                library=state.library,
                recipe_image_availability=state.recipe_image_availability,
                codec=codec,
            )
            with (
                TestClient(api) as api_client,
                _https_api_peer(workspace, api_client, headers) as (
                    url,
                    _certificate,
                    peer,
                ),
            ):
                environment = _session_environment(
                    installed_vonkctl=executable,
                    workspace=workspace,
                    url=url,
                    certificate=_certificate,
                    headers=headers,
                )
                token_file = Path(environment["VONK_CONTROL_TOKEN_FILE"])
                assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
                if mode == "smoke":
                    _smoke(
                        executable=executable,
                        workspace=workspace,
                        cwd=operator_cwd,
                        environment=environment,
                        peer=peer,
                        state=state,
                        postgres_engine=postgres_engine,
                    )
                else:
                    print(f"Disposable Controller: {url}", flush=True)
                    print(f"Installed CLI: {executable}", flush=True)
                    print(f"Profile number: {profile.number}", flush=True)
                    print(f"Application ID: {application.id}", flush=True)
                    print(
                        f"Blocked recipe selector: {state.recipe_selector}",
                        flush=True,
                    )
                    print(
                        "This is a disposable connected fixture with no physical "
                        "Spark. The selected Profile is ready for the walkthrough; "
                        "recipe detail shows its current cache blocker and advertised "
                        "preparation action, which has not been requested.",
                        flush=True,
                    )
                    print(
                        "Use only the U6 outcome card and shipped vonkctl runbook. "
                        "After you request cancellation, keep this shell open while "
                        "the fixture reconciles issued effects. Keep the local URL "
                        "and private token file. Type `exit` "
                        "or press Ctrl-D to close this disposable shell.",
                        flush=True,
                    )
                    shell = shutil.which("bash") or "/bin/bash"
                    process = subprocess.Popen(
                        [shell, "--noprofile", "--norc", "-i"],
                        env=environment,
                        cwd=operator_cwd,
                    )
                    settled = False
                    try:
                        while process.poll() is None:
                            if not settled:
                                cancellation = state.profiles.application(
                                    application.id
                                ).cancellation
                                if (
                                    cancellation is not None
                                    and cancellation.state == "cancelling"
                                ):
                                    _settle_after_restart(state, postgres_engine)
                                    print(
                                        "\nCancellation recovery has settled after "
                                        "the worker restart. The disposable shell is "
                                        "still open so you can inspect terminal progress.",
                                        flush=True,
                                    )
                                    settled = True
                            time.sleep(0.1)
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)
                    if not settled:
                        cancellation = state.profiles.application(
                            application.id
                        ).cancellation
                        if cancellation is not None:
                            print(
                                "The shell exited before live cancellation settlement; "
                                "the disposable fixture will reconcile during cleanup.",
                                flush=True,
                            )
                            _settle_after_restart(state, postgres_engine)
                        else:
                            print(
                                "No Profile cancellation was accepted; no worker or "
                                "agent result was started during cleanup.",
                                flush=True,
                            )
                    print(
                        f"Operator shell exited with status {process.returncode}.",
                        flush=True,
                    )

        finally:
            state.cache.close()
            state.cache_http_client.close()
    assert not workspace.exists()
    print(
        "U6 temporary wheel, local HTTPS peer, token, TLS key, and managed "
        "storage were removed. PostgreSQL fixture teardown drops only this run's database.",
        flush=True,
    )


def _smoke(
    *,
    executable: Path,
    workspace: Path,
    cwd: Path,
    environment: dict[str, str],
    peer,
    state: _WalkthroughState,
    postgres_engine,
) -> None:
    recipe_selector = state.recipe_selector
    application = state.application
    profile = state.profile

    detail = _run_cli(
        executable,
        ("recipe", "detail", recipe_selector),
        environment,
        cwd,
    )
    assert detail.returncode == 0, detail.stdout + detail.stderr
    assert f"vonkctl recipe download {recipe_selector}" in detail.stdout
    assert "Readiness: blocked" in detail.stdout

    preparation_key = str(uuid4())
    preparation = _run_cli(
        executable,
        (
            "recipe",
            "download",
            recipe_selector,
            "--detach",
            "--request-key",
            preparation_key,
            "--json",
        ),
        environment,
        cwd,
    )
    assert preparation.returncode == 0, preparation.stdout + preparation.stderr
    preparation_receipt = json.loads(preparation.stdout)
    assert preparation_receipt["request_id"] == preparation_key
    assert preparation_receipt["kind"] == "recipe.image.availability.v2"
    assert preparation_receipt["state"] == "queued"
    assert preparation_receipt["request"] == {
        "kind": "selector",
        "selector": recipe_selector,
        "force": False,
    }

    cancel_key = str(uuid4())
    cancel = _run_cli(
        executable,
        (
            "--profile",
            str(profile.number),
            "profile",
            "cancel",
            application.id,
            "--yes",
            "--request-key",
            cancel_key,
            "--detach",
            "--json",
        ),
        environment,
        cwd,
    )
    assert cancel.returncode == 0, cancel.stdout + cancel.stderr
    receipt = json.loads(cancel.stdout)
    assert receipt["cancellation"]["request_key"] == cancel_key
    assert receipt["cancellation"]["state"] == "cancelling"
    _assert_cancellation_effects(receipt, terminal=False)

    activity = _run_cli(
        executable,
        (
            "fleet",
            "activity",
            "--state",
            "cancelling",
            "--request-id",
            application.request_key,
        ),
        environment,
        cwd,
    )
    assert activity.returncode == 0, activity.stdout + activity.stderr
    assert application.id in activity.stdout
    assert "Completed effect" in activity.stdout
    assert "Pending effect" in activity.stdout
    assert "Not issued" in activity.stdout

    cancellation = state.profiles.application(application.id).cancellation
    assert cancellation is not None and cancellation.state == "cancelling"
    _settle_after_restart(state, postgres_engine)

    # The fresh service/API is the same durable owner after a process death.

    restarted = build_production_fleet_profile_service(
        state.sessions,
        clock=lambda: NOW + timedelta(seconds=61),
        run_switch_operations=state.run_switch,
    )
    terminal = restarted.application(application.id)
    assert terminal.state == "cancelled"
    assert terminal.cancellation is not None
    _assert_cancellation_effects(terminal.model_dump(mode="json"), terminal=True)
    assert any(
        effect.operation_id == state.stop_child and effect.outcome == "succeeded"
        for effect in terminal.cancellation.completed_effects
    )
    assert any(
        effect.operation_id == state.run_child and effect.outcome == "cancelled"
        for effect in terminal.cancellation.cancelled_effects
    )
    assert any(
        effect.outcome == "not-issued"
        for effect in terminal.cancellation.cancelled_effects
    )

    terminal_progress = _run_cli(
        executable,
        (
            "--profile",
            str(profile.number),
            "profile",
            "progress",
            "--application",
            application.id,
            "--json",
        ),
        environment,
        cwd,
    )
    assert terminal_progress.returncode == 0, (
        terminal_progress.stdout + terminal_progress.stderr
    )
    operator_terminal = json.loads(terminal_progress.stdout)
    assert operator_terminal["id"] == application.id
    assert operator_terminal["state"] == "cancelled"
    assert operator_terminal["cancellation"]["request_key"] == cancel_key
    assert operator_terminal["cancellation"]["state"] == "cancelled"
    _assert_cancellation_effects(operator_terminal, terminal=True)
    assert any(
        effect["operation_id"] == state.stop_child and effect["outcome"] == "succeeded"
        for effect in operator_terminal["cancellation"]["completed_effects"]
    )
    assert any(
        effect["operation_id"] == state.run_child and effect["outcome"] == "cancelled"
        for effect in operator_terminal["cancellation"]["cancelled_effects"]
    )

    sha = state.profile_file_sha
    size = state.profile_expected_bytes
    cache = state.cache
    assert cache._object_is_available(sha, size)
    preview = cache.download_preview(model_content_sha256=state.profile_model_digest)
    assert preview["already_cached_bytes"] == size
    assert not preview["blockers"]

    profile_model = _run_cli(
        executable,
        ("model", "detail", "vonk-forge/synthetic-tiny-fp16"),
        environment,
        cwd,
    )
    assert profile_model.returncode == 0, profile_model.stdout + profile_model.stderr
    assert "cached" in profile_model.stdout.lower()
    assert peer.calls
    print(
        "U6 smoke passed: the installed CLI used the advertised recipe preparation "
        "action, cancellation preserved a completed stop receipt and exact "
        "pending/unissued effects across worker death, and the verified model "
        "remained available in managed storage.",
        flush=True,
    )


@pytest.mark.skipif(
    os.environ.get("VONK_CANCELLATION_WALKTHROUGH_MODE") != "smoke",
    reason="set VONK_CANCELLATION_WALKTHROUGH_MODE=smoke to run U6 smoke",
)
def test_disposable_cli_cancellation_walkthrough_smoke(
    postgres_engine,
) -> None:
    _run_walkthrough(postgres_engine, "smoke")


@pytest.mark.skipif(
    os.environ.get("VONK_CANCELLATION_WALKTHROUGH_MODE") != "interactive",
    reason="set VONK_CANCELLATION_WALKTHROUGH_MODE=interactive to start U6 shell",
)
def test_disposable_cli_cancellation_walkthrough_interactive(
    postgres_engine,
) -> None:
    _run_walkthrough(postgres_engine, "interactive")
