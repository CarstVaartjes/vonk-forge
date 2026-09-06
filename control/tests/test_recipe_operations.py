from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    RecipeRunObservationReceiptClaims,
    SignedRecipeRunObservationReceipt,
    canonical_message,
    recipe_run_observation_receipt_signing_bytes,
)
from vonk_agent_protocol.host_helper import HostHelperSignature
from vonk_control.artifact_sizes import ArtifactSize, StaticArtifactSizeResolver
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.execution_plan_service import (
    ControllerExecutionPlanService,
)
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.litellm import LiteLlmGeneration
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentPresence,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    InstallationNode,
    Job,
    NodeArtifact,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RoutePublication,
    RoutePublicationOwner,
    RunNode,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
    RecipeRunObservation,
    prepare_exact_recipe_run_observation_nodes,
    record_recipe_run_observations,
)
from vonk_control.recipe_routes import (
    AtomicRecipeRoutePublisher,
    RecipeRouteError,
    RecipeRouteNotReady,
    RecipeRouteService,
)
from vonk_control.route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AtomicRouteBundlePublisher,
    FileSupervisorAcknowledger,
    RouteRuntimeError,
    verify_active_route_bundle,
)
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    prepare_runtime_image,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256


class RecordingQueue:
    def __init__(self) -> None:
        self.available = 0

    def enqueue_in_session(
        self,
        session,
        parent_job_id,
        node_id,
        operation,
        authority_revision,
        payload,
        *,
        operation_id,
    ):
        row = AgentOperation(
            id=operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=operation,
            payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
            payload=dict(payload),
            authority_revision=authority_revision,
            state="queued",
            current_attempt=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(row)
        return row

    def notify_available(self) -> None:
        self.available += 1


class FailingQueue(RecordingQueue):
    def enqueue_in_session(self, *args, **kwargs):
        raise RuntimeError("queue write failed")


class ConcurrentPublisher:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._generation = 0
        self.aliases: list[tuple[str, ...]] = []

    def publish(self, state, _policy):
        with self._guard:
            self._generation += 1
            self.aliases.append(tuple(sorted(state.aliases)))
            return LiteLlmGeneration(
                self._generation,
                state.digest,
                state.digest,
                "memory",
            )

    def publish_empty(self, route_digest):
        with self._guard:
            self._generation += 1
            self.aliases.append(())
            return LiteLlmGeneration(
                self._generation,
                route_digest,
                route_digest,
                "memory",
            )


NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)
RECEIPT_SIGNER = ed25519.Ed25519PrivateKey.from_private_bytes(b"r" * 32)


def _synthetic_model_content_sha256() -> str:
    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    return content_sha256(ModelDefinition.model_validate(document))


class _CanonicalModelCache:
    """Small exact cache authority used by the canonical operation fixture."""

    artifact_set_sha256 = "f" * 64
    file_sha256 = "c" * 64

    def resolve_artifact_set(self, **_kwargs):
        return type("Manifest", (), {"digest": self.artifact_set_sha256})()

    def verified_model_objects_for_set(self, artifact_set_sha256):
        if artifact_set_sha256 != self.artifact_set_sha256:
            raise ValueError("unknown artifact set")
        return (
            {
                "model_content_sha256": _synthetic_model_content_sha256(),
                "file_id": "weights",
                "path": "model.safetensors",
                "sha256": self.file_sha256,
                "bytes": 1024,
                "roles": ["weights"],
                "distribution_object": {
                    "name": "model.safetensors",
                    "sha256": self.file_sha256,
                    "bytes": 1024,
                    "kind": "model",
                },
            },
        )


def signed_observation_receipt(
    grant,
    observation_identity_sha256: str,
    *,
    node_id: str,
    observed_at: datetime,
    outcome: str = "running",
) -> dict[str, object]:
    claims = RecipeRunObservationReceiptClaims(
        schema_version=1,
        authority="vonk.recipe-run-observation-helper",
        node_id=node_id,
        request_id=grant.claims.request_id,
        request_sha256=str(grant.claims.operation.values["request_sha256"]),
        observation_identity_sha256=observation_identity_sha256,
        outcome=outcome,
        observed_at=int(observed_at.timestamp()),
    )
    public_key = RECEIPT_SIGNER.public_key().public_bytes_raw()
    return SignedRecipeRunObservationReceipt(
        schema_version=1,
        claims=claims,
        signature=HostHelperSignature(
            algorithm="ed25519",
            key_id=hashlib.sha256(public_key).hexdigest(),
            value=RECEIPT_SIGNER.sign(
                recipe_run_observation_receipt_signing_bytes(claims)
            ).hex(),
        ),
    ).to_mapping()


def start_evidence(payload: dict[str, object]) -> dict[str, object]:
    model_identity = (
        "vonk-forge/synthetic-tiny-fp16/" + _synthetic_model_content_sha256()
    )
    if payload.get("phase") == "rank-launch":
        identity = {
            "phase": "rank-launch",
            "run_id": payload["run_id"],
            "recipe_revision_id": payload["recipe_revision_id"],
            "recipe_content_sha256": payload["recipe_content_sha256"],
            "image_digest": str(payload["image_digest"]).removeprefix("sha256:"),
            "artifact_set_digest": "b" * 64,
            "model_identity": model_identity,
            "rank": payload["rank"],
            "role": payload["role"],
            "world_size": payload["world_size"],
            "local_address": str(payload["local_address"]),
            "master_address": str(payload["master_address"]),
            "master_port": payload["master_port"],
            "memory_reservation_bytes": payload["reserved_memory_bytes"],
            "process_running": True,
            "fabric_projection_bound": True,
            "launched": True,
        }
        if "run_generation" in payload:
            identity.update(
                {
                    "run_generation": payload["run_generation"],
                    "runtime_arguments_sha256": "c" * 64,
                }
            )
        return {
            **identity,
            "evidence_digest": hashlib.sha256(canonical_message(identity)).hexdigest(),
        }
    identity = {
        "recipe_revision_id": payload["recipe_revision_id"],
        "recipe_content_sha256": payload["recipe_content_sha256"],
        "image_digest": str(payload["image_digest"]).removeprefix("sha256:"),
        "artifact_set_digest": "b" * 64,
        "model_identity": model_identity,
        "rank": payload["rank"],
        "world_size": payload["world_size"],
        "endpoint": f"http://{payload['endpoint_address']}:{payload['port']}",
        "memory_reservation_bytes": payload["reserved_memory_bytes"],
        "ready": True,
    }
    if payload.get("phase") == "collective-readiness":
        identity.update(
            {
                "phase": "collective-readiness",
                "run_id": payload["run_id"],
                "role": payload["role"],
            }
        )
    if "run_generation" in payload:
        identity.update(
            {
                "run_generation": payload["run_generation"],
                "runtime_arguments_sha256": "c" * 64,
                "local_address": str(payload["local_address"]),
                "master_address": str(payload["master_address"]),
                "master_port": payload["master_port"],
            }
        )
    return {
        **identity,
        "evidence_digest": hashlib.sha256(canonical_message(identity)).hexdigest(),
    }


def setup_services(
    tmp_path: Path,
    *,
    nodes: int = 1,
    endpoint_owner_rank_one: bool = False,
    distributed_lifecycle: bool = False,
    start_order: tuple[str, ...] | None = None,
    recipe_transform: Callable[[dict[str, object]], None] | None = None,
    model_transform: Callable[[dict[str, object]], None] | None = None,
    engine=None,
    route_withdrawer=None,
):
    engine = engine or create_engine(
        f"sqlite:///{tmp_path / 'operations.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_ids = tuple("spk_" + f"{index + 1:032x}" for index in range(nodes))
    with sessions.begin() as session:
        for index, node_id in enumerate(node_ids):
            serial = f"serial-{index}"
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    architecture="linux-arm64",
                    observation_receipt_public_key=(
                        RECEIPT_SIGNER.public_key().public_bytes_raw().hex()
                        if nodes > 1
                        else None
                    ),
                    capabilities=["runtime.vonk.v1", "recipe.operations.v1"]
                    + (
                        [
                            "fabric.connected.mbps.1000",
                            "recipe.start.two-phase.v1",
                            "recipe.run.inspect.exact.v1",
                            "recipe.run.inspect.receipt.v1",
                        ]
                        if nodes > 1
                        else []
                    ),
                )
            )
            session.flush()
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    fingerprint=f"fingerprint-{index}",
                    not_before=NOW,
                    not_after=datetime(2027, 8, 7, 12, tzinfo=UTC),
                )
            )
            session.add(
                AgentPresence(
                    node_id=node_id,
                    certificate_serial=serial,
                    certificate_fingerprint=f"fingerprint-{index}",
                    management_address=f"192.168.1.{211 + index}",
                    observed_at=NOW,
                )
            )
    inventory = InventoryRepository(sessions, clock=lambda: NOW)
    capabilities = ("runtime.vonk.v1", "recipe.operations.v1") + (
        (
            "fabric.connected.mbps.1000",
            "recipe.start.two-phase.v1",
            "recipe.run.inspect.exact.v1",
            "recipe.run.inspect.receipt.v1",
        )
        if nodes > 1
        else ()
    )
    for index, node_id in enumerate(node_ids):
        inventory.record(
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
                fabric_address=(f"192.168.100.{index + 2}" if nodes > 1 else None),
                fabric_bandwidth_mbps=(1000 if nodes > 1 else None),
            )
        )
    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )
    model_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    document["identity"]["slug"] = "qwen3-vllm"
    role = document["topology"]["roles"][0]
    role["resources"] = {
        "disk": {
            "image_bytes": 30,
            "artifact_bytes": 1024,
            "staging_bytes": 20,
            "cache_bytes": 0,
            "rollback_bytes": 0,
            "safety_margin_bytes": 10,
        },
        "memory": {
            "kind": "unified",
            "startup_peak_bytes": 225,
            "steady_state_bytes": 200,
            "runtime_growth_bytes": 25,
            "system_reserve_bytes": 0,
        },
    }
    if nodes > 1:
        worker = json.loads(json.dumps(role))
        worker.update({"name": "worker", "count": nodes - 1, "endpoint_owner": False})
        roles = [role, worker]
        if endpoint_owner_rank_one:
            roles = [worker, role]
        document["topology"] = {
            **document["topology"],
            "name": f"nodes_{nodes}",
            "mode": "tensor_parallel",
            "node_count": nodes,
            "parallelism": {
                "world_size": nodes,
                "tensor": nodes,
                "pipeline": 1,
                "data": 1,
                "backend": "tcp",
            },
            "roles": roles,
            "fabric": {"connectivity": "connected", "minimum_bandwidth_mbps": 1},
            "start_order": list(start_order or ("worker", "entrypoint")),
            "stop_order": ["entrypoint", "worker"],
        }
        document["models"][0]["files"][0]["roles"] = ["entrypoint", "worker"]
        if distributed_lifecycle:
            document["topology"]["mode"] = "distributed"
            document["topology"]["parallelism"]["backend"] = "mp"
            document["runtime"]["lifecycle"] = {
                "failure": {
                    "rank_loss": "withdraw-endpoint",
                    "recovery": "restart-worker-then-entrypoint",
                },
                "pre_start": [],
                "post_stop": [],
                "stop_timeout_seconds": 30,
            }
    if recipe_transform is not None:
        recipe_transform(document)
    if model_transform is not None:
        model_transform(model_document)
    recipe_definition = RecipeDefinition.model_validate(document)
    model_definition = ModelDefinition.model_validate(model_document)
    recipe_digest = content_sha256(recipe_definition)
    model_digest = content_sha256(model_definition)
    canonical_recipe_document = recipe_definition.model_dump(mode="json")
    canonical_model_document = model_definition.model_dump(mode="json")
    recipe_revision_id = str(uuid.uuid4())
    with sessions.begin() as session:
        recipe_catalog = CatalogDocument(
            kind="recipe",
            publisher=document["identity"]["publisher"],
            slug=document["identity"]["slug"],
            title=document["metadata"]["title"],
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(recipe_catalog)
        session.flush()
        revision = CatalogDocumentRevision(
            id=recipe_revision_id,
            document_id=recipe_catalog.id,
            kind="recipe",
            publisher=recipe_catalog.publisher,
            slug=recipe_catalog.slug,
            revision_number=1,
            schema_version=2,
            state="active",
            document=canonical_recipe_document,
            content_digest=recipe_digest,
            projected={},
            created_by="admin",
            created_at=NOW,
        )
        session.add(revision)
        model_catalog = CatalogDocument(
            kind="model",
            publisher=model_document["identity"]["publisher"],
            slug=model_document["identity"]["slug"],
            title=model_document["identity"]["model"]["title"],
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(model_catalog)
        session.flush()
        session.add(
            CatalogDocumentRevision(
                document_id=model_catalog.id,
                kind="model",
                publisher=model_catalog.publisher,
                slug=model_catalog.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=canonical_model_document,
                content_digest=model_digest,
                projected={},
                created_by="admin",
                created_at=NOW,
            )
        )
        session.flush()
    mappings = ClusterMappingService(sessions)
    mapping_plan = mappings.preview(revision.id, node_ids, {}, "admin")
    mapping_id = mappings.materialize(mapping_plan, actor="admin", now=NOW)
    image_archive = b"canonical-runtime-image-archive"[:30]
    image_archive_sha256 = hashlib.sha256(image_archive).hexdigest()

    class _CanonicalImageTransport:
        def inspect_archive(
            self,
            archive: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            del archive
            return PulledImageEvidence(
                manifest_digest="sha256:" + "1" * 64,
                requested_manifest_digest=None,
                config_id="sha256:" + "4" * 64,
                local_reference="localhost/vonk/fixture@sha256:" + "4" * 64,
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    runtime_image_storage = FilesystemRuntimeImageStorage(
        tmp_path / "runtime-images"
    )
    runtime_image_archive = runtime_image_storage.root / image_archive_sha256
    runtime_image_archive.write_bytes(image_archive)
    with sessions.begin() as session:
        build = RecipeBuild(
            recipe_revision_id=revision.id,
            builder_node_id=node_ids[0],
            source_bundle_sha256="d" * 64,
            build_input_sha256="e" * 64,
            state="succeeded",
            policy_report={"passed": True},
            plan={},
            image_digest="sha256:" + "1" * 64,
            oci_layout_sha256=image_archive_sha256,
            image_bytes=30,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(build)
        session.flush()
        build_id = build.id
        session.add_all(
            NodeArtifact(
                node_id=node_id,
                kind="image",
                digest="1" * 64,
                source="docker-archive:" + image_archive_sha256,
                size_bytes=30,
                state="verified",
                ref_count=0,
                verified_at=NOW,
                updated_at=NOW,
            )
            for node_id in node_ids
        )
    sizes = StaticArtifactSizeResolver(
        (
            ArtifactSize(
                "vonk-forge/synthetic-tiny@0123456789abcdef0123456789abcdef01234567",
                "2" * 64,
                70,
            ),
        )
    )
    canonical_cache = _CanonicalModelCache()

    def prepare_canonical_runtime_image(document, runtime_spec, build):
        runtime = runtime_spec.get("runtime")
        if not isinstance(runtime, dict):
            raise TypeError("canonical runtime projection is unavailable")
        return prepare_runtime_image(
            document,
            runtime=runtime,
            storage=runtime_image_storage,
            transport=_CanonicalImageTransport(),
            build_receipt={
                "state": build.state,
                "build_id": build.id,
                "image_digest": build.image_digest,
                "oci_layout_sha256": build.oci_layout_sha256,
                "image_bytes": build.image_bytes,
            },
            now=NOW,
        )

    execution_plans = ControllerExecutionPlanService(
        canonical_cache,
        runtime_image_preparer=prepare_canonical_runtime_image,
    )
    install = InstallAdmissionService(
        sessions,
        sizes=sizes,
        inventory_max_age=300,
        disk_floor_bytes=10,
        compiled_plan_provider=execution_plans.compile_installation,
    )
    run = RunAdmissionService(sessions, inventory_max_age=300, memory_floor_bytes=50)
    queue = RecordingQueue()
    service = RecipeOperationService(
        sessions,
        install_admission=install,
        run_admission=run,
        agent_jobs=queue,
        clock=lambda: NOW,
        route_withdrawer=route_withdrawer,
    )
    return sessions, service, queue, mapping_id, build_id, node_ids


def record_exact_empty_snapshot(
    sessions: sessionmaker, node_id: str, observed_at: datetime
) -> None:
    with sessions.begin() as session:
        prepare_exact_recipe_run_observation_nodes(session, node_id, observed_at, set())


def mark_current_exact_observations(
    sessions: sessionmaker, run_id: str, observed_at: datetime
) -> None:
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        if run.plan.get("observation_schema_version") != 2:
            return
        for node in session.scalars(
            select(RunNode).where(RunNode.run_id == run_id).order_by(RunNode.rank)
        ):
            node.observed_run_generation = run.run_generation
            node.observation_receipt_sha256 = hashlib.sha256(
                f"{run_id}:{run.run_generation}:{node.node_id}".encode()
            ).hexdigest()
            node.observation_endpoint_ready = (
                True if node.role == "entrypoint" else None
            )
            node.updated_at = observed_at


def installed_recipe(
    service: RecipeOperationService,
    mapping_id: str,
    build_id: str,
    nodes: tuple[str, ...],
    *,
    request_id: str,
):
    plan = service.preview_install(mapping_id, build_id)
    operation = service.install(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    for node_id in nodes:
        service.record_node_result(
            operation.id,
            node_id,
            succeeded=True,
            evidence={"installed_bytes": 120},
        )
    return operation


def test_canonical_recipe_revision_drives_install_and_schema2_payload(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)

    plan = service.preview_install(mapping_id, build_id)
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, plan.recipe_revision_id)
        assert revision is not None
        assert revision.kind == "recipe"
        assert revision.state == "active"
        assert plan.recipe_content_sha256 == revision.content_digest

    operation = service.install(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="canonical-schema2-install",
    )
    with sessions() as session:
        installation = session.get(RecipeInstallation, operation.owner_id)
        assert installation is not None
        compiled = installation.plan.get("compiled_execution_plans")
        assert isinstance(compiled, dict)
        payload = compiled[nodes[0]]
        assert payload["schema_version"] == 2
        assert payload["identity"]["recipe_revision_sha256"] == revision.content_digest


def started_recipe(
    sessions,
    service: RecipeOperationService,
    installation_id: str,
    nodes: tuple[str, ...],
    *,
    request_id: str,
    alias: str = "qwen",
):
    plan = service.preview_run(installation_id, alias)
    operation = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    completed_operations: set[str] = set()
    while service.get(operation.id).state == "running":
        with sessions() as session:
            children = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == operation.id)
                    .order_by(AgentOperation.node_id)
                )
            )
        pending = tuple(
            child for child in children if child.id not in completed_operations
        )
        assert pending
        for child in pending:
            service.record_node_result(
                operation.id,
                child.node_id,
                succeeded=True,
                evidence=start_evidence(child.payload),
            )
            completed_operations.add(child.id)
    mark_current_exact_observations(sessions, operation.owner_id, NOW)
    return operation


def complete_collective_readiness(
    sessions,
    service: RecipeOperationService,
    operation_id: str,
    endpoint_owner_node_id: str,
) -> None:
    with sessions() as session:
        readiness = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == operation_id,
                AgentOperation.node_id == endpoint_owner_node_id,
                AgentOperation.state == "queued",
            )
        )
    assert readiness is not None
    assert readiness.payload.get("phase") == "collective-readiness"
    service.record_node_result(
        operation_id,
        endpoint_owner_node_id,
        succeeded=True,
        evidence=start_evidence(readiness.payload),
    )
    assert service.get(operation_id).state == "succeeded"
    with sessions() as session:
        operation = session.get(Job, operation_id)
        assert operation is not None
        run_id = operation.payload["owner_id"]
    mark_current_exact_observations(sessions, run_id, NOW)


def bind_route_publications(
    sessions,
    service: RecipeOperationService,
    publisher: ConcurrentPublisher,
) -> tuple[RecipeOperationService, RecipeRouteService]:
    routes = RecipeRouteService(
        sessions,
        publisher=publisher,
        management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
        clock=lambda: NOW,
        maximum_age_seconds=300,
    )
    bound = RecipeOperationService(
        sessions,
        install_admission=service._install_admission,
        run_admission=service._run_admission,
        agent_jobs=service._agent_jobs,
        clock=lambda: NOW,
        route_publications=routes,
    )
    return bound, routes


def clone_running_run(sessions, source_run_id: str, *, alias: str) -> str:
    run_id = str(uuid.uuid4())
    authority = hashlib.sha256(alias.encode()).hexdigest()
    with sessions.begin() as session:
        source = session.get(RecipeRun, source_run_id)
        source_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == source_run_id)
                .order_by(RunNode.rank)
            )
        )
        assert source is not None
        session.add(
            RecipeRun(
                id=run_id,
                installation_id=source.installation_id,
                mapping_id=source.mapping_id,
                mapping_generation=source.mapping_generation,
                alias=alias,
                plan_digest=authority,
                plan={**source.plan, "plan_digest": authority},
                state="running",
                route_state="pending",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        cloned_nodes = []
        for node in source_nodes:
            port = node.port + 10
            endpoint = dict(node.endpoint) if node.endpoint is not None else None
            if endpoint is not None and isinstance(endpoint.get("url"), str):
                parsed = urlsplit(endpoint["url"])
                host = parsed.hostname
                if host is None or parsed.port is None:
                    raise AssertionError(
                        "running test endpoint must include a host and port"
                    )
                netloc = f"[{host}]" if ":" in host else host
                endpoint["url"] = urlunsplit(parsed._replace(netloc=f"{netloc}:{port}"))
            cloned_nodes.append(
                RunNode(
                    run_id=run_id,
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state="running",
                    port=port,
                    reserved_memory_bytes=node.reserved_memory_bytes,
                    endpoint=endpoint,
                    evidence_digest=node.evidence_digest,
                    updated_at=NOW,
                )
            )
        session.add_all(cloned_nodes)
        session.add_all(
            ResourceReservation(
                node_id=node.node_id,
                kind="unified-memory",
                resource_key=authority,
                amount_bytes=node.reserved_memory_bytes,
                owner_kind="run",
                owner_id=run_id,
                state="active",
                plan_digest=authority,
                created_at=NOW,
            )
            for node in source_nodes
        )
    return run_id


def _postgres_backend_pid(connection) -> int:
    return int(connection.connection.driver_connection.info.backend_pid)


def _wait_for_postgres_block(engine, *, blocked_pid: int, blocker_pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT pg_blocking_pids(pid), wait_event_type
                    FROM pg_stat_activity
                    WHERE pid = :pid
                    """
                ),
                {"pid": blocked_pid},
            ).one()
        if blocker_pid in row[0] and row[1] == "Lock":
            return
        time.sleep(0.05)
    pytest.fail("recipe operation never became database-lock blocked")


def test_install_is_digest_bound_idempotent_and_gang_complete(tmp_path: Path) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    operation = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="1" * 36
    )
    repeated = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="1" * 36
    )

    assert repeated == operation
    assert operation.kind == "recipe.install"
    assert operation.state == "running"
    assert queue.available == 1
    with sessions() as session:
        jobs = list(session.scalars(select(Job)))
        child_operations = list(session.scalars(select(AgentOperation)))
        assert len(jobs) == 1
        assert {item.kind for item in child_operations} == {"recipe.install"}
        assert all(
            "shell" not in json.dumps(item.payload).lower() for item in child_operations
        )

    service.record_node_result(
        operation.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    assert service.get(operation.id).state == "running"
    service.record_node_result(
        operation.id, nodes[1], succeeded=True, evidence={"installed_bytes": 120}
    )
    completed = service.get(operation.id)
    assert completed.state == "succeeded"
    assert completed.result == {
        "successful_nodes": sorted(nodes),
        "failed_nodes": [],
        "node_evidence": {
            nodes[0]: {"installed_bytes": 120},
            nodes[1]: {"installed_bytes": 120},
        },
    }
    with sessions() as session:
        assert session.get(RecipeInstallation, operation.owner_id).state == "installed"


def test_multirank_role_phases_persist_recover_and_stop_in_reverse_order(
    tmp_path: Path,
) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=3
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="0" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "phased")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="1" * 36,
    )
    with sessions() as session:
        first = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        assert {item.payload["role"] for item in first} == {"worker"}
        assert len(first) == 2
        stored = session.get(Job, start.id)
        assert stored is not None and len(stored.payload["phases"]) == 2
    for operation in first:
        service.record_node_result(
            start.id,
            operation.node_id,
            succeeded=True,
            evidence=start_evidence(operation.payload),
        )
    recovered = RecipeOperationService(
        sessions,
        install_admission=service._install_admission,
        run_admission=service._run_admission,
        agent_jobs=queue,
        clock=lambda: NOW,
    )
    with sessions() as session:
        all_children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        second = tuple(
            item for item in all_children if item.payload["role"] == "entrypoint"
        )
        assert len(second) == 1
    recovered.record_node_result(
        start.id,
        second[0].node_id,
        succeeded=True,
        evidence=start_evidence(second[0].payload),
    )
    assert recovered.get(start.id).state == "succeeded"

    stop_plan = recovered.preview_stop(start.owner_id)
    stop = recovered.stop(
        start.owner_id,
        plan_digest=stop_plan.plan_digest,
        actor="admin",
        request_id="2" * 36,
    )
    with sessions() as session:
        first_stop = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == stop.id)
            )
        )
        assert [item.node_id for item in first_stop] == [nodes[0]]
        assert set(first_stop[0].payload) == {
            "schema_version",
            "run_id",
            "plan_digest",
        }
    recovered.record_node_result(
        stop.id, first_stop[0].node_id, succeeded=True, evidence={"stopped": True}
    )
    with sessions() as session:
        all_stop = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == stop.id)
            )
        )
        second_stop = tuple(item for item in all_stop if item.node_id != nodes[0])
        assert len(second_stop) == 2
    for operation in second_stop:
        recovered.record_node_result(
            stop.id, operation.node_id, succeeded=True, evidence={"stopped": True}
        )
    assert recovered.get(stop.id).state == "succeeded"


def test_failed_start_phase_never_enqueues_dependent_role(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="3" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "blocked")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="4" * 36,
    )
    with sessions() as session:
        worker = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        assert worker is not None and worker.payload["role"] == "worker"
    service.record_node_result(
        start.id, worker.node_id, succeeded=False, evidence={"reason": "nope"}
    )
    with sessions() as session:
        starts = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        assert {item.payload["role"] for item in starts} == {"worker"}
    assert service.get(start.id).state == "failed"


@pytest.mark.parametrize(
    "start_order",
    (("worker", "entrypoint"), ("entrypoint", "worker")),
)
def test_distributed_start_launches_all_ranks_then_checks_collective(
    tmp_path: Path, start_order: tuple[str, ...]
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        distributed_lifecycle=True,
        start_order=start_order,
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="p" * 36
    )
    plan = service.preview_run(installation.owner_id, "two-phase")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="q" * 36,
    )

    def children() -> tuple[AgentOperation, ...]:
        with sessions() as session:
            return tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == start.id)
                    .order_by(AgentOperation.created_at, AgentOperation.id)
                )
            )

    launches = children()
    assert len(launches) == 2
    assert all(item.payload["phase"] == "rank-launch" for item in launches)
    assert {item.payload["role"] for item in launches} == set(start_order)
    deadline = launches[0].payload["start_deadline"]
    assert all(item.payload["start_deadline"] == deadline for item in launches)
    with sessions() as session:
        job = session.get(Job, start.id)
        assert job.targets == sorted(nodes)
        phase_items = [item for phase in job.payload["phases"] for item in phase]
        assert len(phase_items) == 3
        assert len({item["operation_id"] for item in phase_items}) == 3
        assert [item["payload"]["role"] for item in job.payload["phases"][0]] == list(
            start_order
        )
        assert job.payload["start_deadline"] == deadline

    first_by_role = {item.payload["role"]: item for item in launches}
    for role in start_order:
        launch = first_by_role[role]
        service.record_node_result(
            start.id,
            launch.node_id,
            succeeded=True,
            evidence=start_evidence(launch.payload),
        )

    all_children = children()
    readiness = next(
        item
        for item in all_children
        if item.payload.get("phase") == "collective-readiness"
    )
    assert len(all_children) == 3
    assert readiness.payload["role"] == "entrypoint"
    assert readiness.payload["start_deadline"] == deadline
    assert sum(item.node_id == readiness.node_id for item in all_children) == 2
    with sessions() as session:
        run = session.get(RecipeRun, start.owner_id)
        run_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        )
        assert run.state == "starting"
        assert run.route_state == "withdrawn"
        assert [node.state for node in run_nodes] == ["starting", "starting"]
        assert all(node.endpoint is None for node in run_nodes)

    service.record_node_result(
        start.id,
        readiness.node_id,
        succeeded=True,
        evidence=start_evidence(readiness.payload),
    )
    assert service.get(start.id).state == "succeeded"
    with sessions() as session:
        run = session.get(RecipeRun, start.owner_id)
        run_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        )
        assert run.state == "running"
        assert run.route_state == "pending"
        assert [node.state for node in run_nodes] == ["running", "running"]
        assert [node.endpoint is not None for node in run_nodes] == [True, False]
        assert all(node.observed_run_generation is None for node in run_nodes)

    publisher = ConcurrentPublisher()
    _service, routes = bind_route_publications(sessions, service, publisher)
    with pytest.raises(RecipeRouteNotReady):
        routes.publish_run(start.owner_id)
    with sessions.begin() as session:
        owner = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id, RunNode.role == "entrypoint"
            )
        )
        owner.observed_run_generation = 1
        owner.observation_receipt_sha256 = "d" * 64
        owner.observation_endpoint_ready = True
        owner.updated_at = NOW
    with pytest.raises(RecipeRouteNotReady):
        routes.publish_run(start.owner_id)
    with sessions.begin() as session:
        worker = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id, RunNode.role == "worker"
            )
        )
        worker.observed_run_generation = 1
        worker.observation_receipt_sha256 = "e" * 64
        worker.observation_endpoint_ready = None
        worker.updated_at = NOW
    routes.publish_run(start.owner_id)
    assert publisher.aliases[-1] == ("two-phase",)
    with sessions() as session:
        assert session.get(RecipeRun, start.owner_id).observation_deadline_at is None


def test_distributed_start_rejects_changed_launch_evidence(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="s" * 36
    )
    plan = service.preview_run(installation.owner_id, "bad-launch")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="t" * 36,
    )
    with sessions() as session:
        launch = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
    evidence = start_evidence(launch.payload)
    evidence["role"] = "entrypoint"
    identity = {
        key: value for key, value in evidence.items() if key != "evidence_digest"
    }
    evidence["evidence_digest"] = hashlib.sha256(
        canonical_message(identity)
    ).hexdigest()

    with pytest.raises(RecipeOperationConflict, match="fenced request"):
        service.record_node_result(
            start.id,
            launch.node_id,
            succeeded=True,
            evidence=evidence,
        )


def test_worker_death_while_owner_is_healthy_never_publishes_route(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="d" * 36
    )
    start = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="e" * 36,
        alias="owner-healthy-worker-dead",
    )
    record_exact_empty_snapshot(sessions, nodes[1], NOW + timedelta(seconds=1))

    publisher = ConcurrentPublisher()
    _service, routes = bind_route_publications(sessions, service, publisher)
    with pytest.raises(RecipeRouteError, match="every .*rank"):
        routes.publish_run(start.owner_id)

    assert publisher.aliases == []
    with sessions() as session:
        owner = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id,
                RunNode.role == "entrypoint",
            )
        )
        worker = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id,
                RunNode.role == "worker",
            )
        )
        assert owner.state == "running"
        assert owner.observation_endpoint_ready is True
        assert worker.state == "failed"


def test_collective_readiness_starts_distinct_observation_grace(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="g" * 36
    )
    plan = service.preview_run(installation.owner_id, "observation-grace")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="h" * 36,
    )
    with sessions() as session:
        launches = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
    assert len(launches) == 2
    for launch in launches:
        service.record_node_result(
            start.id,
            launch.node_id,
            succeeded=True,
            evidence=start_evidence(launch.payload),
        )
    with sessions() as session:
        readiness = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == start.id,
                AgentOperation.payload["phase"].as_string() == "collective-readiness",
            )
        )
    assert readiness is not None

    collective_at = NOW + timedelta(seconds=59)
    service._clock = lambda: collective_at
    service.record_node_result(
        start.id,
        readiness.node_id,
        succeeded=True,
        evidence=start_evidence(readiness.payload),
    )
    with sessions() as session:
        run = session.get(RecipeRun, start.owner_id)
        assert run.observation_deadline_at.replace(
            tzinfo=UTC
        ) == collective_at + timedelta(seconds=120)

    _bound, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    before_expiry = RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: NOW + timedelta(seconds=60),
    )
    assert before_expiry.tick() is False
    with sessions() as session:
        run = session.get(RecipeRun, start.owner_id)
        assert run.route_state == "pending"
        assert all(
            node.state == "running"
            for node in session.scalars(
                select(RunNode).where(RunNode.run_id == start.owner_id)
            )
        )


def test_distributed_start_deadline_is_enforced_before_phase_advance(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="v" * 36
    )
    plan = service.preview_run(installation.owner_id, "expired-start")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="w" * 36,
    )
    with sessions() as session:
        launches = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        assert len(launches) == 2
        assert all(
            launch.payload["start_deadline"]
            == (NOW + timedelta(seconds=60)).isoformat()
            for launch in launches
        )
        launch = launches[0]

    service._clock = lambda: NOW + timedelta(seconds=60)
    service.record_node_result(
        start.id,
        launch.node_id,
        succeeded=True,
        evidence=start_evidence(launch.payload),
    )

    assert service.get(start.id).state == "failed"
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        assert len(children) == 2
        assert {child.state for child in children} == {"failed", "succeeded"}
        run = session.get(RecipeRun, start.owner_id)
        assert run.state == "stopping"
        assert run.route_state == "withdrawn"
        cleanup = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == start.owner_id,
            )
        )
        assert cleanup is not None


def test_distributed_start_capability_is_an_admission_blocker(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="u" * 36
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[1])
        node.capabilities = [
            capability
            for capability in node.capabilities
            if capability != "recipe.start.two-phase.v1"
        ]

    plan = service.preview_run(installation.owner_id, "unsupported")
    assert plan.allowed is False
    assert "run.distributed_start_capability_missing" in {
        blocker.code for blocker in plan.nodes[1].blockers
    }


def test_distributed_start_requires_enrollment_pinned_receipt_key(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="k" * 36
    )
    with sessions.begin() as session:
        session.get(AgentNode, nodes[1]).observation_receipt_public_key = None

    plan = service.preview_run(installation.owner_id, "unpinned-receipt-key")
    assert plan.allowed is False
    assert "run.distributed_observation_receipt_capability_missing" in {
        blocker.code for blocker in plan.nodes[1].blockers
    }


def test_nonzero_endpoint_owner_controls_rendezvous_for_every_rank(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        endpoint_owner_rank_one=True,
        distributed_lifecycle=True,
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="5" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "nonzero")
    assert next(node for node in run_plan.nodes if node.endpoint_owner).rank == 1
    start = started_recipe(
        sessions,
        service,
        install.owner_id,
        nodes,
        request_id="6" * 36,
        alias="nonzero",
    )
    with sessions() as session:
        job = session.get(Job, start.id)
        assert job is not None
        payloads = [
            entry["payload"] for phase in job.payload["phases"] for entry in phase
        ]
    assert {payload["master_address"] for payload in payloads} == {"192.168.100.3"}
    assert {payload["master_port"] for payload in payloads} == {29500}
    with sessions() as session:
        run_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        )
        assert [node.role for node in run_nodes] == ["worker", "entrypoint"]
        assert [node.endpoint is not None for node in run_nodes] == [False, True]
        assert [node.observation_endpoint_ready for node in run_nodes] == [None, True]

    publisher = ConcurrentPublisher()
    _service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(start.owner_id)
    assert publisher.aliases[-1] == ("nonzero",)


def test_install_admission_and_queue_creation_roll_back_together(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    service._agent_jobs = FailingQueue()
    plan = service.preview_install(mapping_id, build_id)

    with pytest.raises(RuntimeError, match="queue write failed"):
        service.install(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="0" * 36,
        )

    with sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
        assert list(session.scalars(select(ResourceReservation))) == []
        assert list(session.scalars(select(Job))) == []


def test_run_admission_and_start_queue_roll_back_together(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="b" * 36,
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    run_plan = service.preview_run(install.owner_id, "qwen")
    service._agent_jobs = FailingQueue()

    with pytest.raises(RuntimeError, match="queue write failed"):
        service.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            actor="admin",
            request_id="c" * 36,
        )

    with sessions() as session:
        assert list(session.scalars(select(RecipeRun))) == []
        assert (
            list(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "run"
                    )
                )
            )
            == []
        )
        assert session.scalar(select(Job).where(Job.request_id == "c" * 36)) is None


def test_start_rejects_alias_mismatched_digest_before_side_effects_and_replays_exactly(
    tmp_path: Path,
) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="0" * 35 + "1"
    )
    qwen = service.preview_run(installation.owner_id, "qwen")
    alternate = service.preview_run(installation.owner_id, "qwen-alt")

    assert qwen.plan_digest != alternate.plan_digest
    with pytest.raises(RecipeOperationConflict, match="does not match preview"):
        service.start(
            alternate,
            plan_digest=qwen.plan_digest,
            actor="admin",
            request_id="0" * 35 + "2",
        )

    with sessions() as session:
        assert tuple(session.scalars(select(RecipeRun))) == ()
        assert (
            tuple(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "run"
                    )
                )
            )
            == ()
        )
        assert (
            tuple(session.scalars(select(Job).where(Job.kind == "recipe.start"))) == ()
        )
        assert (
            tuple(
                session.scalars(
                    select(AgentOperation).where(AgentOperation.kind == "recipe.start")
                )
            )
            == ()
        )
    assert queue.available == 1

    started = service.start(
        qwen,
        plan_digest=qwen.plan_digest,
        actor="admin",
        request_id="0" * 35 + "3",
    )
    post_admission = service.preview_run(installation.owner_id, "qwen")
    assert post_admission.allowed is False
    assert post_admission.plan_digest != qwen.plan_digest

    replayed = service.replay_start(
        installation.owner_id,
        "qwen",
        plan_digest=qwen.plan_digest,
        request_id="0" * 35 + "3",
    )

    assert replayed == started
    assert queue.available == 2
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        job = session.get(Job, started.id)
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == started.id)
        )
        assert run is not None and job is not None and child is not None
        assert run.alias == qwen.alias == run.plan["alias"] == child.payload["alias"]
        assert job.payload["plan_digest"] == qwen.plan_digest == started.plan_digest

    assert (
        service.replay_start(
            installation.owner_id,
            "qwen-alt",
            plan_digest=qwen.plan_digest,
            request_id="0" * 35 + "3",
        )
        is None
    )
    mismatched = service.preview_run(installation.owner_id, "qwen-alt")
    with pytest.raises(RecipeOperationConflict, match="does not match preview"):
        service.start(
            mismatched,
            plan_digest=qwen.plan_digest,
            actor="admin",
            request_id="0" * 35 + "3",
        )

    with sessions() as session:
        runs = tuple(session.scalars(select(RecipeRun)))
        reservations = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run"
                )
            )
        )
        jobs = tuple(session.scalars(select(Job).where(Job.kind == "recipe.start")))
        operations = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "recipe.start")
            )
        )
    assert len(runs) == len(jobs) == len(operations) == 1
    assert len(reservations) == 2
    assert queue.available == 2


def test_stop_state_and_queue_creation_roll_back_together(tmp_path: Path) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="1" * 35 + "a",
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    run_plan = service.preview_run(install.owner_id, "qwen")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="1" * 35 + "b",
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        evidence = start_evidence(child.payload)
    service.record_node_result(start.id, nodes[0], succeeded=True, evidence=evidence)
    service._agent_jobs = FailingQueue()
    plan = service.preview_stop(start.owner_id)

    with pytest.raises(RuntimeError, match="queue write failed"):
        service.stop(
            start.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="1" * 35 + "c",
        )

    with sessions() as session:
        assert session.get(RecipeRun, start.owner_id).state == "running"
        assert (
            session.scalar(select(Job).where(Job.request_id == "1" * 35 + "c")) is None
        )
    assert withdrawn == []


def test_stop_withdrawal_failure_rolls_back_job_and_run_state(tmp_path: Path) -> None:
    withdrawn: list[str] = []

    def fail_withdrawal(run_id: str) -> None:
        withdrawn.append(run_id)
        raise RuntimeError("route withdrawal failed")

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=fail_withdrawal
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="1" * 35 + "d"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="1" * 35 + "e",
    )
    with sessions.begin() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        stored.route_state = "published"
    plan = service.preview_stop(run.owner_id)

    with pytest.raises(RuntimeError, match="route withdrawal failed"):
        service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="1" * 35 + "f",
        )

    assert withdrawn == [run.owner_id]
    with sessions() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        assert (stored.state, stored.route_state) == ("running", "published")
        assert (
            session.scalar(select(Job).where(Job.request_id == "1" * 35 + "f")) is None
        )


def test_stop_commit_failure_after_publication_is_safe_side(tmp_path: Path) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="2" * 35 + "c"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="2" * 35 + "d",
    )
    with sessions.begin() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        stored.route_state = "published"
        session.add(
            RoutePublicationOwner(
                singleton_id=1,
                reconciliation_id=None,
                owner_generation=0,
                updated_at=NOW,
            )
        )
    plan = service.preview_stop(run.owner_id)

    def fail_commit(_session) -> None:
        raise RuntimeError("database commit failed")

    event.listen(sessions.class_, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="database commit failed"):
            service.stop(
                run.owner_id,
                plan_digest=plan.plan_digest,
                actor="admin",
                request_id="2" * 35 + "e",
            )
    finally:
        event.remove(sessions.class_, "before_commit", fail_commit)

    assert withdrawn == [run.owner_id]
    with sessions() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        assert (stored.state, stored.route_state) == ("running", "published")
        assert (
            session.scalar(select(Job).where(Job.request_id == "2" * 35 + "e")) is None
        )


def test_stop_preview_blocks_nonexact_reservation_authority(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="2" * 35 + "f"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="2" * 35 + "0",
    )
    with sessions.begin() as session:
        reservation = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run.owner_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert reservation is not None
        reservation.plan_digest = "0" * 64

    plan = service.preview_stop(run.owner_id)

    assert plan.allowed is False
    assert [reason.code for reason in plan.blockers] == [
        "stop.reservation_membership_changed"
    ]


def test_stop_preview_is_stable_exact_and_defers_capacity_release(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="2" * 35 + "a"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="2" * 35 + "b",
    )

    first = service.preview_stop(run.owner_id)
    second = service.preview_stop(run.owner_id)

    assert second == first
    assert first.allowed is True
    assert first.run_id == run.owner_id
    assert first.installation_id == installation.owner_id
    assert first.authority_digest == run.plan_digest
    assert first.route_withdrawal is True
    assert [(node.node_id, node.rank, node.role) for node in first.nodes] == [
        (nodes[0], 0, "entrypoint"),
        (nodes[1], 1, "worker"),
    ]
    assert [node.active_memory_reservation_bytes for node in first.nodes] == [225, 225]
    assert first.total_active_memory_reservation_bytes == 450
    assert [warning.code for warning in first.warnings] == [
        "stop.capacity_release_deferred"
    ]
    assert len(first.plan_digest) == 64


@pytest.mark.parametrize("changed_fact", ("state", "rank", "route", "reservation"))
def test_stop_apply_rejects_stale_plan_before_route_or_queue_side_effects(
    tmp_path: Path, changed_fact: str
) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="3" * 35 + "a"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="3" * 35 + "b",
    )
    plan = service.preview_stop(run.owner_id)
    with sessions.begin() as session:
        stored_run = session.get(RecipeRun, run.owner_id)
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run.owner_id))
        reservation = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run.owner_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert stored_run is not None and rank is not None and reservation is not None
        if changed_fact == "state":
            stored_run.state = "lost"
        elif changed_fact == "rank":
            rank.role = "changed"
        elif changed_fact == "route":
            stored_run.route_state = "failed"
        else:
            reservation.amount_bytes += 1

    changed = service.preview_stop(run.owner_id)
    assert changed.plan_digest != plan.plan_digest
    with pytest.raises(RecipeOperationConflict, match="stale or blocked"):
        service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="3" * 35 + "c",
        )

    assert withdrawn == []
    with sessions() as session:
        assert (
            session.scalar(select(Job).where(Job.request_id == "3" * 35 + "c")) is None
        )


def test_stop_replay_is_bound_to_selected_run_kind_and_action_digest(
    tmp_path: Path,
) -> None:
    withdrawn: list[str] = []
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="4" * 35 + "a"
    )
    first_run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="4" * 35 + "b",
        alias="first",
    )
    second_run_id = str(uuid.uuid4())
    second_authority = "9" * 64
    with sessions.begin() as session:
        source = session.get(RecipeRun, first_run.owner_id)
        source_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == first_run.owner_id)
                .order_by(RunNode.rank)
            )
        )
        assert source is not None
        second_plan_document = {**source.plan, "plan_digest": second_authority}
        session.add(
            RecipeRun(
                id=second_run_id,
                installation_id=source.installation_id,
                mapping_id=source.mapping_id,
                mapping_generation=source.mapping_generation,
                alias="second",
                plan_digest=second_authority,
                plan=second_plan_document,
                state="running",
                route_state="published",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            RunNode(
                run_id=second_run_id,
                node_id=node.node_id,
                rank=node.rank,
                role=node.role,
                state=node.state,
                port=node.port + 10,
                reserved_memory_bytes=node.reserved_memory_bytes,
                updated_at=NOW,
            )
            for node in source_nodes
        )
    first_plan = service.preview_stop(first_run.owner_id)
    second_plan = service.preview_stop(second_run_id)
    request_key = "4" * 35 + "d"

    operation = service.stop(
        first_run.owner_id,
        plan_digest=first_plan.plan_digest,
        actor="admin",
        request_id=request_key,
    )
    replay = service.stop(
        first_run.owner_id,
        plan_digest=first_plan.plan_digest,
        actor="admin",
        request_id=request_key,
    )
    with pytest.raises(RecipeOperationConflict, match="request key"):
        service.stop(
            second_run_id,
            plan_digest=second_plan.plan_digest,
            actor="admin",
            request_id=request_key,
        )

    assert replay == operation
    assert operation.plan_digest == first_plan.plan_digest
    assert operation.nodes == tuple(sorted(nodes))
    assert withdrawn == [first_run.owner_id]
    assert queue.available == 4
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == operation.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert len(children) == 1
        assert children[0].node_id == nodes[0]
        assert set(children[0].payload) == {
            "schema_version",
            "run_id",
            "plan_digest",
        }
        assert {child.authority_revision for child in children} == {
            first_run.plan_digest
        }
        assert {child.payload["plan_digest"] for child in children} == {
            first_run.plan_digest
        }


def test_concurrent_duplicate_stop_maps_to_one_operation_on_sqlite(
    tmp_path: Path,
) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="4" * 35 + "e"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="4" * 35 + "f",
    )
    plan = service.preview_stop(run.owner_id)
    start = threading.Barrier(2)
    request_key = "4" * 35 + "0"

    def stop() -> str:
        start.wait()
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        operation_ids = list(pool.map(lambda _index: stop(), range(2)))

    assert len(set(operation_ids)) == 1
    assert withdrawn == [run.owner_id]
    with sessions() as session:
        assert (
            len(
                tuple(session.scalars(select(Job).where(Job.request_id == request_key)))
            )
            == 1
        )


def test_partial_multinode_stop_retains_every_active_capacity_reservation(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="5" * 35 + "a"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="5" * 35 + "b",
    )
    with sessions() as session:
        before = len(
            tuple(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "run",
                        ResourceReservation.owner_id == run.owner_id,
                        ResourceReservation.state == "active",
                    )
                )
            )
        )
    plan = service.preview_stop(run.owner_id)
    operation = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="5" * 35 + "c",
    )

    service.record_node_result(
        operation.id, nodes[0], succeeded=True, evidence={"stopped": True}
    )
    service.record_node_result(
        operation.id,
        nodes[1],
        succeeded=False,
        evidence={"code": "stop.failed"},
    )

    assert service.get(operation.id).state == "failed"
    with sessions() as session:
        assert session.get(RecipeRun, run.owner_id).state == "failed"
        active = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.state == "active",
                )
            )
        )
        assert len(active) == before


def test_partial_install_fails_as_a_group_and_can_retry(tmp_path: Path) -> None:
    _sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    first = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36
    )
    service.record_node_result(
        first.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    service.record_node_result(
        first.id, nodes[1], succeeded=False, evidence={"code": "pull.failed"}
    )

    assert service.get(first.id).state == "failed"
    assert service.get(first.id).result["successful_nodes"] == [nodes[0]]
    retry = service.retry(first.id, actor="admin", request_id="3" * 36)
    assert retry.id != first.id
    assert retry.owner_id == first.owner_id
    with pytest.raises(RecipeOperationConflict, match="not retryable"):
        service.retry(first.id, actor="admin", request_id="3" * 35 + "4")


@pytest.mark.parametrize("terminal_state", ("failed", "waiting-for-operator"))
def test_terminal_image_distribution_retry_requeues_exact_persisted_group(
    tmp_path: Path, terminal_state: str
) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    with sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        archive_sha256 = build.oci_layout_sha256
        assert archive_sha256 is not None
    plan_digest = "4" * 64
    payloads = tuple(
        (
            node_id,
            {
                "schema_version": 1,
                "kind": "recipe.image.import.v1",
                "build_id": build_id,
                "mapping_id": mapping_id,
                "mapping_generation": 1,
                "source_node_id": nodes[0],
                "image_digest": "sha256:" + "1" * 64,
                "oci_layout_sha256": archive_sha256,
                "image_bytes": 30,
            },
        )
        for node_id in nodes
    )
    first = service._queue(
        kind="recipe.image.import.v1",
        owner_kind="image-distribution",
        owner_id=build_id,
        plan_digest=plan_digest,
        actor="admin",
        request_id="4" * 36,
        node_payloads=payloads,
        authority_digest=plan_digest,
    )
    service.record_node_result(
        first.id,
        nodes[0],
        succeeded=True,
        evidence={
            "build_id": build_id,
            "image_bytes": 30,
            "image_digest": "sha256:" + "1" * 64,
            "oci_layout_sha256": archive_sha256,
        },
    )
    service.record_node_result(
        first.id,
        nodes[1],
        succeeded=False,
        evidence={"reason": "helper grant expired"},
    )
    if terminal_state == "waiting-for-operator":
        with sessions.begin() as session:
            parent = session.get(Job, first.id)
            held_child = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == first.id,
                    AgentOperation.node_id == nodes[1],
                )
            )
            assert parent is not None
            assert held_child is not None
            parent.state = terminal_state
            held_child.state = terminal_state

    retry = service.retry(first.id, actor="admin", request_id="5" * 36)
    replay = service.retry(first.id, actor="admin", request_id="5" * 36)

    assert retry.id != first.id
    assert replay == retry
    assert retry.kind == "recipe.image.import.v1"
    assert retry.owner_id == build_id
    assert retry.plan_digest == plan_digest
    assert retry.nodes == tuple(sorted(nodes))
    assert queue.available == 2
    with sessions() as session:
        retried_job = session.get(Job, retry.id)
        assert retried_job is not None
        assert retried_job.authority_revision == plan_digest
        retried_children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == retry.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert tuple(
            (child.node_id, child.payload) for child in retried_children
        ) == tuple(sorted(payloads))
    with pytest.raises(RecipeOperationConflict, match="active retry"):
        service.retry(first.id, actor="admin", request_id="6" * 36)


@pytest.mark.parametrize(
    "tamper",
    (
        "owner-kind",
        "targets",
        "authority",
        "child-kind",
        "child-state",
        "child-authority",
        "child-digest",
        "child-payload",
    ),
)
def test_image_distribution_retry_rejects_malformed_persisted_group(
    tmp_path: Path, tamper: str
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    plan_digest = "7" * 64
    first = service._queue(
        kind="recipe.image.import.v1",
        owner_kind="image-distribution",
        owner_id=build_id,
        plan_digest=plan_digest,
        actor="admin",
        request_id="7" * 36,
        node_payloads=(
            (
                nodes[0],
                {
                    "schema_version": 1,
                    "kind": "recipe.image.import.v1",
                    "build_id": build_id,
                    "mapping_id": mapping_id,
                    "mapping_generation": 1,
                    "source_node_id": nodes[0],
                    "image_digest": "sha256:" + "1" * 64,
                    "oci_layout_sha256": "3" * 64,
                    "image_bytes": 30,
                },
            ),
        ),
        authority_digest=plan_digest,
    )
    service.record_node_result(
        first.id,
        nodes[0],
        succeeded=False,
        evidence={"reason": "helper grant expired"},
    )
    with sessions.begin() as session:
        job = session.get(Job, first.id)
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert job is not None and child is not None
        if tamper == "owner-kind":
            job.payload = {**job.payload, "owner_kind": "recipe-build"}
        elif tamper == "targets":
            job.targets = []
        elif tamper == "authority":
            job.authority_revision = "0" * 64
        elif tamper == "child-kind":
            child.kind = "recipe.install"
        elif tamper == "child-state":
            child.state = "queued"
        elif tamper == "child-authority":
            child.authority_revision = "0" * 64
        elif tamper == "child-digest":
            child.payload_digest = "0" * 64
        else:
            payload = dict(child.payload)
            del payload["image_digest"]
            child.payload = payload

    with pytest.raises(RecipeOperationConflict, match="stored.*invalid"):
        service.retry(first.id, actor="admin", request_id="8" * 36)


def test_failed_install_retry_state_rolls_back_when_queue_write_fails(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    first = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 35 + "a"
    )
    service.record_node_result(
        first.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    service.record_node_result(
        first.id, nodes[1], succeeded=False, evidence={"code": "pull.failed"}
    )
    with sessions() as session:
        before = {
            node.node_id: node.state
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == first.owner_id
                )
            )
        }
    service._agent_jobs = FailingQueue()

    with pytest.raises(RuntimeError, match="queue write failed"):
        service.retry(first.id, actor="admin", request_id="2" * 35 + "b")

    with sessions() as session:
        assert session.get(RecipeInstallation, first.owner_id).state == "partial"
        after = {
            node.node_id: node.state
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == first.owner_id
                )
            )
        }
        assert after == before


def test_start_stop_and_uninstall_preserve_capacity_safely(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="4" * 36,
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )

    run_plan = service.preview_run(install.owner_id, "qwen")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="5" * 36,
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        assert child is not None
        assert child.payload["endpoint_address"] == "192.168.1.211"
        assert child.payload["world_size"] == 1
        assert child.payload["master_address"] is None
        evidence = start_evidence(child.payload)
    blocked_uninstall = service.preview_uninstall(install.owner_id)
    assert blocked_uninstall.allowed is False
    assert [reason.code for reason in blocked_uninstall.blockers] == [
        "uninstall.active_run"
    ]
    with pytest.raises(RecipeOperationConflict, match="stale or blocked"):
        service.uninstall(
            install.owner_id,
            plan_digest=blocked_uninstall.plan_digest,
            actor="admin",
            request_id="6" * 36,
        )

    service.record_node_result(
        start.id,
        nodes[0],
        succeeded=True,
        evidence=evidence,
    )
    assert service.get(start.id).state == "succeeded"
    stop_plan = service.preview_stop(start.owner_id)
    stop = service.stop(
        start.owner_id,
        plan_digest=stop_plan.plan_digest,
        actor="admin",
        request_id="7" * 36,
    )
    assert service.get(stop.id).state == "running"
    blocked_stop = service.preview_stop(start.owner_id)
    with pytest.raises(RecipeOperationConflict, match="stale or blocked"):
        service.stop(
            start.owner_id,
            plan_digest=blocked_stop.plan_digest,
            actor="admin",
            request_id="7" * 35 + "a",
        )
    service.record_node_result(
        stop.id, nodes[0], succeeded=True, evidence={"stopped": True}
    )
    with sessions() as session:
        run = session.get(RecipeRun, start.owner_id)
        reservations = list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == run.id,
                    ResourceReservation.state == "active",
                )
            )
        )
        assert run.state == "stopped"
        assert reservations == []

    uninstall_plan = service.preview_uninstall(install.owner_id)
    uninstall = service.uninstall(
        install.owner_id,
        plan_digest=uninstall_plan.plan_digest,
        actor="admin",
        request_id="8" * 36,
    )
    service.record_node_result(
        uninstall.id, nodes[0], succeeded=True, evidence={"removed": True}
    )
    with sessions() as session:
        installation = session.get(RecipeInstallation, install.owner_id)
        assert installation is not None
        assert installation.state == "uninstalled"
        revision = session.get(
            CatalogDocumentRevision, installation.recipe_revision_id
        )
        assert revision is not None
        assert revision.kind == "recipe"
        assert revision.state == "active"


def test_uninstall_preview_has_exact_bytes_content_and_fixed_consequences(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "a"
    )

    first = service.preview_uninstall(installation.owner_id)
    second = service.preview_uninstall(installation.owner_id)

    assert second == first
    assert first.allowed is True
    assert first.installation_id == installation.owner_id
    assert first.original_plan_digest == installation.plan_digest
    assert first.bytes_removed == 240
    assert [
        (node.node_id, node.rank, node.role, node.installed_bytes)
        for node in first.nodes
    ] == [
        (nodes[0], 0, "entrypoint", 120),
        (nodes[1], 1, "worker", 120),
    ]
    assert first.active_runs == ()
    assert first.consequences.catalog_retained is True
    assert first.consequences.automatic_stop is False
    assert first.consequences.reinstall_required is True
    assert first.model_impact.effect == "recipe-and-unused-model"
    assert first.model_impact.dependent_recipe_ids == ()
    assert first.model_impact.cleanup_node_ids == nodes
    assert first.model_impact.retained_node_ids == ()
    with sessions() as session:
        stored = session.get(RecipeInstallation, installation.owner_id)
        assert stored is not None
        revision = session.get(CatalogDocumentRevision, stored.recipe_revision_id)
        assert revision is not None
        assert first.installation_authority_digest == revision.content_digest
        assert first.recipe_content == revision.document


def test_uninstall_keeps_model_when_another_installed_recipe_uses_it(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    first = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "1"
    )
    second = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "2"
    )

    preview = service.preview_uninstall(first.owner_id)

    assert preview.model_impact.effect == "recipe-only"
    assert preview.model_impact.dependent_recipe_ids == (preview.recipe_id,)
    assert preview.model_impact.cleanup_node_ids == ()
    assert preview.model_impact.retained_node_ids == nodes
    operation = service.uninstall(
        first.owner_id,
        plan_digest=preview.plan_digest,
        actor="admin",
        request_id="8" * 35 + "3",
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == operation.id)
        )
        assert child is not None
        assert child.payload["cleanup_model_version_sha256"] is None

    service.record_node_result(
        operation.id,
        nodes[0],
        succeeded=True,
        evidence={"removed": True},
    )
    final_dependent = service.preview_uninstall(second.owner_id)
    assert final_dependent.model_impact.effect == "recipe-and-unused-model"
    assert final_dependent.model_impact.dependent_recipe_ids == ()
    assert final_dependent.model_impact.cleanup_node_ids == nodes
    assert final_dependent.model_impact.retained_node_ids == ()


def test_uninstall_cleans_model_per_spark_when_dependency_is_node_local(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    target = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "4"
    )
    retained = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "5"
    )
    with sessions.begin() as session:
        no_longer_installed = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == retained.owner_id,
                InstallationNode.node_id == nodes[1],
            )
        )
        assert no_longer_installed is not None
        no_longer_installed.state = "uninstalled"

    preview = service.preview_uninstall(target.owner_id)

    assert preview.model_impact.effect == "recipe-and-partial-model-cleanup"
    assert preview.model_impact.dependent_recipe_ids == (preview.recipe_id,)
    assert preview.model_impact.retained_node_ids == (nodes[0],)
    assert preview.model_impact.cleanup_node_ids == (nodes[1],)
    operation = service.uninstall(
        target.owner_id,
        plan_digest=preview.plan_digest,
        actor="admin",
        request_id="8" * 35 + "6",
    )
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == operation.id)
                .order_by(AgentOperation.node_id)
            )
        )
    assert [child.payload["cleanup_model_version_sha256"] for child in children] == [
        None,
        preview.model_impact.model_version_sha256,
    ]


def test_model_deletion_preview_and_apply_cascade_custom_recipe_installation(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="d" * 36
    )
    second_installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="d" * 35 + "1"
    )
    uninstall = service.preview_uninstall(installation.owner_id)
    model_digest = uninstall.model_impact.model_version_sha256

    preview = service.preview_model_deletion(model_digest)

    assert preview.allowed is True
    assert preview.model_version_sha256 == model_digest
    assert preview.shared_cache_policy == "remove-unreferenced-model-artifacts-only"
    assert preview.bytes_removed == 480
    assert [item.installation_id for item in preview.installations] == sorted(
        [installation.owner_id, second_installation.owner_id]
    )
    expected_installation_ids = tuple(
        sorted([installation.owner_id, second_installation.owner_id])
    )
    assert [(item.node_id, item.installation_ids) for item in preview.nodes] == [
        (nodes[0], expected_installation_ids),
        (nodes[1], expected_installation_ids),
    ]
    assert [warning.code for warning in preview.warnings] == [
        "model-delete.shared_cache_protected"
    ]

    operation = service.delete_model(
        model_digest,
        plan_digest=preview.plan_digest,
        actor="admin",
        request_id="e" * 36,
    )
    replay = service.delete_model(
        model_digest,
        plan_digest=preview.plan_digest,
        actor="admin",
        request_id="e" * 36,
    )
    assert replay == operation
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == operation.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert [child.node_id for child in children] == list(nodes)
        assert {child.kind for child in children} == {"recipe.model-uninstall.v1"}
        expected_content_digests = {
            item.installation_id: item.recipe_content_sha256
            for item in preview.installations
        }
        expected_payload = {
            "schema_version": 1,
            "model_version_sha256": model_digest,
            "plan_digest": preview.plan_digest,
            "installations": [
                {
                    "installation_id": installation_id,
                    "recipe_content_sha256": expected_content_digests[installation_id],
                }
                for installation_id in sorted(
                    [installation.owner_id, second_installation.owner_id]
                )
            ],
        }
        assert {child.payload_digest for child in children} == {
            hashlib.sha256(canonical_message(expected_payload)).hexdigest()
        }
        assert all(child.payload == expected_payload for child in children)
    progress = service.record_node_result(
        operation.id,
        nodes[0],
        succeeded=True,
        evidence={"uninstalled_installations": 2, "removed_model_bytes": 70},
    )
    assert progress.state == "running"
    assert progress.result is not None
    assert set(progress.result["node_evidence"]) == {nodes[0]}
    service.record_node_result(
        operation.id,
        nodes[1],
        succeeded=True,
        evidence={"uninstalled_installations": 2, "removed_model_bytes": 70},
    )
    with sessions() as session:
        stored = session.get(RecipeInstallation, installation.owner_id)
        second_stored = session.get(RecipeInstallation, second_installation.owner_id)
        assert stored is not None and stored.state == "uninstalled"
        assert second_stored is not None and second_stored.state == "uninstalled"
        assert not list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id.in_(
                        [installation.owner_id, second_installation.owner_id]
                    ),
                    ResourceReservation.state == "active",
                )
            )
        )


def test_model_deletion_requires_explicit_stop_for_every_active_run(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="f" * 36
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="1" * 35 + "f",
    )
    model_digest = service.preview_uninstall(
        installation.owner_id
    ).model_impact.model_version_sha256

    preview = service.preview_model_deletion(model_digest)

    assert preview.allowed is False
    assert [item.code for item in preview.blockers] == ["model-delete.active_run"]
    assert [item.run_id for item in preview.active_runs] == [run.owner_id]
    with pytest.raises(RecipeOperationConflict, match="stale or blocked"):
        service.delete_model(
            model_digest,
            plan_digest=preview.plan_digest,
            actor="admin",
            request_id="2" * 35 + "f",
        )


def test_uninstall_unknown_bytes_and_active_runs_block_without_implicit_stop(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "b"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="8" * 35 + "c",
    )
    active = service.preview_uninstall(installation.owner_id)
    assert active.allowed is False
    assert [item.run_id for item in active.active_runs] == [run.owner_id]
    with pytest.raises(RecipeOperationConflict, match="stale or blocked"):
        service.uninstall(
            installation.owner_id,
            plan_digest=active.plan_digest,
            actor="admin",
            request_id="8" * 35 + "d",
        )
    with sessions() as session:
        assert (
            session.scalar(select(Job).where(Job.request_id == "8" * 35 + "d")) is None
        )
        assert (
            session.scalar(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == run.owner_id,
                )
            )
            is None
        )

    with sessions.begin() as session:
        stored_run = session.get(RecipeRun, run.owner_id)
        stored_installation = session.get(RecipeInstallation, installation.owner_id)
        failed_node = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation.owner_id,
                InstallationNode.node_id == nodes[1],
            )
        )
        assert stored_run is not None and stored_installation is not None
        assert failed_node is not None
        stored_run.state = "stopped"
        stored_installation.state = "partial"
        failed_node.state = "failed"
    unknown = service.preview_uninstall(installation.owner_id)
    assert unknown.allowed is False
    assert unknown.bytes_removed is None
    assert [reason.code for reason in unknown.blockers] == ["uninstall.bytes_unknown"]
    assert unknown.nodes[1].installed_bytes is None


def test_uninstall_rejects_stale_bytes_before_transactional_full_group_queue(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "e"
    )
    stale = service.preview_uninstall(installation.owner_id)
    with sessions.begin() as session:
        node = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation.owner_id,
                InstallationNode.node_id == nodes[1],
            )
        )
        assert node is not None
        node.installed_bytes += 1
    assert (
        service.preview_uninstall(installation.owner_id).plan_digest
        != stale.plan_digest
    )

    with pytest.raises(RecipeOperationConflict, match="stale or blocked"):
        service.uninstall(
            installation.owner_id,
            plan_digest=stale.plan_digest,
            actor="admin",
            request_id="8" * 35 + "f",
        )
    with sessions() as session:
        assert (
            session.scalar(select(Job).where(Job.request_id == "8" * 35 + "f")) is None
        )

    fresh = service.preview_uninstall(installation.owner_id)
    operation = service.uninstall(
        installation.owner_id,
        plan_digest=fresh.plan_digest,
        actor="admin",
        request_id="8" * 35 + "0",
    )
    replay = service.uninstall(
        installation.owner_id,
        plan_digest=fresh.plan_digest,
        actor="admin",
        request_id="8" * 35 + "0",
    )
    assert replay == operation
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == operation.id)
                .order_by(AgentOperation.node_id)
            )
        )
        revision = session.get(
            CatalogDocumentRevision,
            session.get(RecipeInstallation, installation.owner_id).recipe_revision_id,
        )
        assert len(children) == 2
        assert revision is not None
        assert {child.authority_revision for child in children} == {
            revision.content_digest
        }
        assert {child.payload["plan_digest"] for child in children} == {
            installation.plan_digest
        }


def test_uninstall_queue_rollback_and_request_key_are_owner_bound(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    first = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="9" * 35 + "a"
    )
    second = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="9" * 35 + "b"
    )
    first_plan = service.preview_uninstall(first.owner_id)
    second_plan = service.preview_uninstall(second.owner_id)
    service._agent_jobs = FailingQueue()

    with pytest.raises(RuntimeError, match="queue write failed"):
        service.uninstall(
            first.owner_id,
            plan_digest=first_plan.plan_digest,
            actor="admin",
            request_id="9" * 35 + "c",
        )
    with sessions() as session:
        assert session.get(RecipeInstallation, first.owner_id).state == "installed"
        assert (
            session.scalar(select(Job).where(Job.request_id == "9" * 35 + "c")) is None
        )

    service._agent_jobs = RecordingQueue()
    operation = service.uninstall(
        first.owner_id,
        plan_digest=first_plan.plan_digest,
        actor="admin",
        request_id="9" * 35 + "d",
    )
    with pytest.raises(RecipeOperationConflict, match="request key"):
        service.uninstall(
            second.owner_id,
            plan_digest=second_plan.plan_digest,
            actor="admin",
            request_id="9" * 35 + "d",
        )
    assert operation.owner_id == first.owner_id


@pytest.mark.parametrize(
    ("uninstall_state", "blocked"),
    (("queued", True), ("running", True), ("failed", False), ("succeeded", False)),
)
def test_start_fences_only_active_uninstall_operations_after_installation_lock(
    tmp_path: Path, uninstall_state: str, blocked: bool
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="9" * 35 + "e"
    )
    run_plan = service.preview_run(
        installation.owner_id, "fenced" if blocked else "allowed"
    )
    uninstall_plan = service.preview_uninstall(installation.owner_id)
    uninstall = service.uninstall(
        installation.owner_id,
        plan_digest=uninstall_plan.plan_digest,
        actor="admin",
        request_id="9" * 35 + "f",
    )
    with sessions.begin() as session:
        job = session.get(Job, uninstall.id)
        assert job is not None
        job.state = uninstall_state

    if blocked:
        with pytest.raises(RecipeOperationConflict, match="not runnable"):
            service.start(
                run_plan,
                plan_digest=run_plan.plan_digest,
                actor="admin",
                request_id="9" * 35 + "0",
            )
    else:
        started = service.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            actor="admin",
            request_id="9" * 35 + "0",
        )
        assert started.kind == "recipe.start"

    with sessions() as session:
        start_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.start"))
        )
        runs = tuple(session.scalars(select(RecipeRun)))
    assert len(start_jobs) == len(runs) == (0 if blocked else 1)


def test_different_uninstall_request_remains_blocked_by_active_operation(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="a" * 35 + "0"
    )
    plan = service.preview_uninstall(installation.owner_id)
    first = service.uninstall(
        installation.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="a" * 35 + "1",
    )

    with pytest.raises(RecipeOperationConflict, match="stale or blocked"):
        service.uninstall(
            installation.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="a" * 35 + "2",
        )

    with sessions() as session:
        parents = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.uninstall"))
        )
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
            )
        )
    assert [parent.id for parent in parents] == [first.id]
    assert {child.node_id for child in children} == set(nodes)


def test_run_status_projects_exact_rank_health_without_agent_secrets(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="9" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "qwen")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="a" * 36,
    )
    with sessions.begin() as session:
        ranks = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        )
        ranks[0].state = "running"
        ranks[0].evidence_digest = "1" * 64
        ranks[0].updated_at = NOW
        ranks[1].state = "failed"
        ranks[1].evidence_digest = "2" * 64
        ranks[1].updated_at = NOW

    status = service.run_status(start.owner_id)

    assert status.id == start.owner_id
    assert status.alias == "qwen"
    assert status.healthy is False
    assert [rank.state for rank in status.ranks] == ["running", "failed"]
    assert [rank.fresh for rank in status.ranks] == [True, True]
    assert [rank.age_seconds for rank in status.ranks] == [0.0, 0.0]
    with sessions() as session:
        assert all(
            session.get(AgentNode, node_id).state == "active" for node_id in nodes
        )


def test_legacy_snapshot_updates_single_run_and_ignores_exact_distributed_run(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="b" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "observed-gang")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="c" * 36,
    )
    while service.get(start.id).state == "running":
        with sessions() as session:
            operations = tuple(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == start.id,
                        AgentOperation.state == "queued",
                    )
                )
            )
        for operation in operations:
            service.record_node_result(
                start.id,
                operation.node_id,
                succeeded=True,
                evidence=start_evidence(operation.payload),
            )

    legacy_run_id = str(uuid.uuid4())
    with sessions.begin() as session:
        exact_run = session.get(RecipeRun, start.owner_id)
        assert exact_run.plan["observation_schema_version"] == 2
        exact_rank = session.scalar(
            select(RunNode).where(
                RunNode.run_id == exact_run.id,
                RunNode.node_id == nodes[0],
            )
        )
        assert exact_rank is not None
        exact_rank.state = "failed"
        session.add(
            RecipeRun(
                id=legacy_run_id,
                installation_id=exact_run.installation_id,
                mapping_id=exact_run.mapping_id,
                mapping_generation=exact_run.mapping_generation,
                run_generation=1,
                alias="legacy-single",
                plan_digest="d" * 64,
                plan={"schema_version": 1, "observation_schema_version": 1},
                state="running",
                route_state="withdrawn",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            RunNode(
                run_id=legacy_run_id,
                node_id=nodes[0],
                rank=0,
                role="single",
                state="running",
                port=9000,
                reserved_memory_bytes=1,
                updated_at=NOW,
            )
        )

    record_recipe_run_observations(
        sessions,
        nodes[0],
        NOW + timedelta(seconds=1),
        (
            RecipeRunObservation(start.owner_id, True),
            RecipeRunObservation(legacy_run_id, True),
        ),
    )
    with sessions() as session:
        exact_rank = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id,
                RunNode.node_id == nodes[0],
            )
        )
        legacy_rank = session.scalar(
            select(RunNode).where(RunNode.run_id == legacy_run_id)
        )
        assert exact_rank.state == "failed"
        assert exact_rank.updated_at.replace(tzinfo=UTC) == NOW
        assert legacy_rank.state == "running"
        assert legacy_rank.updated_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=1)

    record_recipe_run_observations(sessions, nodes[0], NOW + timedelta(seconds=2), ())
    with sessions() as session:
        exact_rank = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id,
                RunNode.node_id == nodes[0],
            )
        )
        legacy_rank = session.scalar(
            select(RunNode).where(RunNode.run_id == legacy_run_id)
        )
        assert exact_rank.state == "failed"
        assert legacy_rank.state == "failed"

    with sessions.begin() as session:
        assigned = prepare_exact_recipe_run_observation_nodes(
            session, nodes[1], NOW + timedelta(seconds=3), set()
        )
        assert [node.run_id for node in assigned] == [start.owner_id]
    with sessions() as session:
        exact_worker = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id,
                RunNode.node_id == nodes[1],
            )
        )
        assert exact_worker.state == "failed"

    with sessions.begin() as session:
        exact_run = session.get(RecipeRun, start.owner_id)
        exact_worker = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id,
                RunNode.node_id == nodes[1],
            )
        )
        exact_run.state = "starting"
        exact_worker.state = "running"
        exact_worker.updated_at = NOW + timedelta(seconds=4)
        assert (
            prepare_exact_recipe_run_observation_nodes(
                session, nodes[1], NOW + timedelta(seconds=5), set()
            )
            == ()
        )
    with sessions() as session:
        exact_worker = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id,
                RunNode.node_id == nodes[1],
            )
        )
        assert exact_worker.state == "running"


def test_exact_rank_inspection_grant_is_identity_bound_and_single_use(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="7" * 36
    )
    start = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="8" * 36,
        alias="observed-exact",
    )
    observation_node = nodes[1]
    certificate_serial = "serial-1"
    with sessions() as session:
        run = session.get(RecipeRun, start.owner_id)
        installed = session.get(RecipeInstallation, run.installation_id)
        run_node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == run.id, RunNode.node_id == observation_node
            )
        )
        start_job = session.get(Job, start.id)
        launch = start_job.result["launch_evidence"][observation_node]
        assert run_node.endpoint is None
        identity = {
            "schema_version": 1,
            "node_id": observation_node,
            "run_id": run.id,
            "installation_id": run.installation_id,
            "recipe_revision_id": installed.recipe_revision_id,
            "recipe_content_sha256": launch["recipe_content_sha256"],
            "mapping_id": run.mapping_id,
            "mapping_generation": run.mapping_generation,
            "run_generation": run.run_generation,
            "image_digest": launch["image_digest"],
            "artifact_set_digest": launch["artifact_set_digest"],
            "model_identity": launch["model_identity"],
            "rank": run_node.rank,
            "role": run_node.role,
            "world_size": launch["world_size"],
            "local_address": launch["local_address"],
            "master_address": launch["master_address"],
            "master_port": launch["master_port"],
            "port": run_node.port,
            "runtime_arguments_sha256": launch["runtime_arguments_sha256"],
        }
    authority = HostRuntimeAuthorityService(
        sessions,
        HostHelperGrantIssuer(ed25519.Ed25519PrivateKey.generate(), clock=lambda: NOW),
        clock=lambda: NOW,
    )
    identity_sha256, grant = authority.issue_recipe_run_observation_grant(
        node_id=observation_node,
        certificate_serial=certificate_serial,
        identity=identity,
        job_id=start.owner_id,
        operation_id=str(uuid.uuid4()),
        attempt=1,
        fence=str(uuid.uuid4()),
        request_sha256="d" * 64,
        expires_in_seconds=10,
    )
    assert (
        grant.claims.operation.values["observation_identity_sha256"] == identity_sha256
    )
    with pytest.raises(HostHelperAuthorityError, match="pending"):
        authority.issue_recipe_run_observation_grant(
            node_id=observation_node,
            certificate_serial=certificate_serial,
            identity=identity,
            job_id=start.owner_id,
            operation_id=str(uuid.uuid4()),
            attempt=1,
            fence=str(uuid.uuid4()),
            request_sha256="e" * 64,
            expires_in_seconds=10,
        )
    forged_receipt = signed_observation_receipt(
        grant,
        identity_sha256,
        node_id=observation_node,
        observed_at=NOW,
    )
    forged_receipt["signature"]["value"] = "0" * 128
    with (
        sessions.begin() as session,
        pytest.raises(HostHelperAuthorityError, match="signature"),
    ):
        authority.consume_recipe_run_observation_grant(
            session,
            node_id=observation_node,
            certificate_serial=certificate_serial,
            identity=identity,
            observed_at=NOW,
            received_at=NOW,
            signed_grant=grant.to_mapping(),
            helper_receipt=forged_receipt,
        )
    with sessions.begin() as session:
        assert authority.consume_recipe_run_observation_grant(
            session,
            node_id=observation_node,
            certificate_serial=certificate_serial,
            identity=identity,
            observed_at=NOW,
            received_at=NOW,
            signed_grant=grant.to_mapping(),
            helper_receipt=signed_observation_receipt(
                grant,
                identity_sha256,
                node_id=observation_node,
                observed_at=NOW,
            ),
        ) == (
            identity_sha256,
            True,
            hashlib.sha256(
                canonical_message(
                    signed_observation_receipt(
                        grant,
                        identity_sha256,
                        node_id=observation_node,
                        observed_at=NOW,
                    )
                )
            ).hexdigest(),
        )
    with (
        sessions.begin() as session,
        pytest.raises(HostHelperAuthorityError, match="replayed"),
    ):
        authority.consume_recipe_run_observation_grant(
            session,
            node_id=observation_node,
            certificate_serial=certificate_serial,
            identity=identity,
            observed_at=NOW + timedelta(seconds=1),
            received_at=NOW + timedelta(seconds=1),
            signed_grant=grant.to_mapping(),
            helper_receipt=signed_observation_receipt(
                grant,
                identity_sha256,
                node_id=observation_node,
                observed_at=NOW + timedelta(seconds=1),
            ),
        )

    second_identity_sha256, second_grant = authority.issue_recipe_run_observation_grant(
        node_id=observation_node,
        certificate_serial=certificate_serial,
        identity=identity,
        job_id=start.owner_id,
        operation_id=str(uuid.uuid4()),
        attempt=1,
        fence=str(uuid.uuid4()),
        request_sha256="f" * 64,
        expires_in_seconds=10,
    )
    assert second_identity_sha256 == identity_sha256
    assert second_grant.claims.request_id != grant.claims.request_id
    with sessions.begin() as session:
        assert authority.consume_recipe_run_observation_grant(
            session,
            node_id=observation_node,
            certificate_serial=certificate_serial,
            identity=identity,
            observed_at=NOW,
            received_at=NOW,
            signed_grant=second_grant.to_mapping(),
            helper_receipt=signed_observation_receipt(
                second_grant,
                second_identity_sha256,
                node_id=observation_node,
                observed_at=NOW,
                outcome="not-running",
            ),
        )[:2] == (identity_sha256, False)

    _, stale_grant = authority.issue_recipe_run_observation_grant(
        node_id=observation_node,
        certificate_serial=certificate_serial,
        identity=identity,
        job_id=start.owner_id,
        operation_id=str(uuid.uuid4()),
        attempt=1,
        fence=str(uuid.uuid4()),
        request_sha256="1" * 64,
        expires_in_seconds=10,
    )
    with (
        sessions.begin() as session,
        pytest.raises(HostHelperAuthorityError, match="stale"),
    ):
        authority.consume_recipe_run_observation_grant(
            session,
            node_id=observation_node,
            certificate_serial=certificate_serial,
            identity=identity,
            observed_at=NOW - timedelta(seconds=1),
            received_at=NOW,
            signed_grant=stale_grant.to_mapping(),
            helper_receipt=signed_observation_receipt(
                stale_grant,
                identity_sha256,
                node_id=observation_node,
                observed_at=NOW - timedelta(seconds=1),
            ),
        )
    with sessions() as session:
        worker = session.scalar(
            select(RunNode).where(
                RunNode.run_id == start.owner_id,
                RunNode.node_id == observation_node,
            )
        )
        assert worker.endpoint is None


def _queued_distributed_recovery_stop(tmp_path: Path, *, engine=None):
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True, engine=engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="i" * 36
    )
    started = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="r" * 36,
        alias="deadline-gang",
    )
    publisher = ConcurrentPublisher()
    service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(started.owner_id)
    failure_observed_at = NOW + timedelta(seconds=1)
    record_exact_empty_snapshot(sessions, nodes[1], failure_observed_at)
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )
    assert recovery.tick() is True
    with sessions() as session:
        stop_job = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == started.owner_id,
            )
        )
    return sessions, service, routes, publisher, started, stop_job, nodes


def _queued_distributed_recovery_restart(tmp_path: Path, *, engine=None):
    (
        sessions,
        service,
        routes,
        publisher,
        started,
        stop_job,
        nodes,
    ) = _queued_distributed_recovery_stop(tmp_path, engine=engine)
    service.record_node_result(stop_job.id, nodes[0], succeeded=True, evidence={})
    service.record_node_result(stop_job.id, nodes[1], succeeded=True, evidence={})
    with sessions() as session:
        restart = session.scalar(
            select(Job).where(
                Job.kind == "recipe.start",
                Job.payload["owner_id"].as_string() == started.owner_id,
                Job.id != started.id,
            )
        )
        worker_start = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[1],
            )
        )
    return (
        sessions,
        service,
        routes,
        publisher,
        started,
        restart,
        worker_start,
        nodes,
    )


def test_distributed_recovery_launches_all_ranks_before_collective_readiness(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        _routes,
        _publisher,
        _started,
        restart,
        _worker_start,
        _nodes,
    ) = _queued_distributed_recovery_restart(tmp_path)
    with sessions() as session:
        launches = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == restart.id)
                .order_by(AgentOperation.node_id)
            )
        )
    assert len(launches) == 2
    assert {item.payload["phase"] for item in launches} == {"rank-launch"}

    service.record_node_result(
        restart.id,
        launches[0].node_id,
        succeeded=True,
        evidence=start_evidence(launches[0].payload),
    )
    with sessions() as session:
        assert (
            session.query(AgentOperation).filter_by(parent_job_id=restart.id).count()
            == 2
        )

    service.record_node_result(
        restart.id,
        launches[1].node_id,
        succeeded=True,
        evidence=start_evidence(launches[1].payload),
    )
    with sessions() as session:
        operations = tuple(
            session.query(AgentOperation).filter_by(parent_job_id=restart.id)
        )
        readiness = tuple(
            item
            for item in operations
            if item.payload.get("phase") == "collective-readiness"
        )
        assert len(operations) == 3
        assert len(readiness) == 1
        assert readiness[0].payload["role"] == "entrypoint"


def test_distributed_recovery_deadline_is_enforced_during_stop_phase_advance(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        _routes,
        _publisher,
        started,
        stop_job,
        nodes,
    ) = _queued_distributed_recovery_stop(tmp_path)
    service._clock = lambda: NOW + timedelta(seconds=31)

    service.record_node_result(stop_job.id, nodes[0], succeeded=True, evidence={})

    with sessions() as session:
        worker_stop = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == stop_job.id,
                AgentOperation.node_id == nodes[1],
            )
        )
        run = session.get(RecipeRun, started.owner_id)
        stored_stop = session.get(Job, stop_job.id)
        assert worker_stop is None
        assert stored_stop.state == "failed"
        assert run.state == "failed"
        assert run.route_state == "withdrawn"


def test_distributed_recovery_deadline_is_enforced_before_phase_advance(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        _routes,
        _publisher,
        started,
        restart,
        worker_start,
        nodes,
    ) = _queued_distributed_recovery_restart(tmp_path)
    service._clock = lambda: NOW + timedelta(seconds=31)

    service.record_node_result(
        restart.id,
        nodes[1],
        succeeded=True,
        evidence=start_evidence(worker_start.payload),
    )

    with sessions() as session:
        owner_start = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
            )
        )
        run = session.get(RecipeRun, started.owner_id)
        stored_restart = session.get(Job, restart.id)
        assert owner_start is not None
        assert owner_start.state == "failed"
        assert stored_restart.state == "failed"
        assert run.state == "stopping"
        assert run.route_state == "withdrawn"


def test_distributed_recovery_deadline_is_rechecked_before_route_publication(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        routes,
        publisher,
        started,
        restart,
        worker_start,
        nodes,
    ) = _queued_distributed_recovery_restart(tmp_path)
    service.record_node_result(
        restart.id,
        nodes[1],
        succeeded=True,
        evidence=start_evidence(worker_start.payload),
    )
    with sessions() as session:
        owner_start = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
            )
        )
    service.record_node_result(
        restart.id,
        nodes[0],
        succeeded=True,
        evidence=start_evidence(owner_start.payload),
    )
    with sessions() as session:
        readiness = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
                AgentOperation.state == "queued",
            )
        )
    assert readiness is not None
    service.record_node_result(
        restart.id,
        nodes[0],
        succeeded=True,
        evidence=start_evidence(readiness.payload),
    )
    mark_current_exact_observations(sessions, started.owner_id, NOW)
    routes._clock = lambda: NOW + timedelta(seconds=31)
    publications_before = list(publisher.aliases)

    with pytest.raises(RuntimeError, match="deadline"):
        routes.publish_run(started.owner_id)

    assert publisher.aliases == publications_before
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run.state == "failed"
        assert run.route_state == "withdrawn"


def test_recovery_phase_deadline_is_resampled_after_waiting_for_job_lock(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    (
        sessions,
        service,
        _routes,
        _publisher,
        started,
        restart,
        worker_start,
        nodes,
    ) = _queued_distributed_recovery_restart(tmp_path, engine=postgres_engine)
    current = {"now": NOW}
    service._clock = lambda: current["now"]
    worker_pid: dict[str, int] = {}
    lock_started = threading.Event()

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if "FROM jobs" in statement and "FOR UPDATE" in statement:
            worker_pid["value"] = _postgres_backend_pid(connection)
            lock_started.set()

    blocker = postgres_engine.connect()
    transaction = blocker.begin()
    blocker_pid = _postgres_backend_pid(blocker)
    blocker.execute(select(Job).where(Job.id == restart.id).with_for_update())
    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        result = pool.submit(
            service.record_node_result,
            restart.id,
            nodes[1],
            succeeded=True,
            evidence=start_evidence(worker_start.payload),
        )
        assert lock_started.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=worker_pid["value"],
            blocker_pid=blocker_pid,
        )
        current["now"] = NOW + timedelta(seconds=31)
        transaction.commit()
        result.result(timeout=10)
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)

    with sessions() as session:
        owner_start = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
            )
        )
        assert owner_start is not None
        assert owner_start.state == "failed"
        assert session.get(Job, restart.id).state == "failed"  # type: ignore[union-attr]
        assert session.get(RecipeRun, started.owner_id).state == "stopping"  # type: ignore[union-attr]


def test_recovery_publication_crossing_deadline_is_immediately_withdrawn(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        routes,
        _publisher,
        started,
        restart,
        worker_start,
        nodes,
    ) = _queued_distributed_recovery_restart(tmp_path)
    service.record_node_result(
        restart.id,
        nodes[1],
        succeeded=True,
        evidence=start_evidence(worker_start.payload),
    )
    with sessions() as session:
        owner_start = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
            )
        )
    service.record_node_result(
        restart.id,
        nodes[0],
        succeeded=True,
        evidence=start_evidence(owner_start.payload),
    )
    complete_collective_readiness(sessions, service, restart.id, nodes[0])
    current = {"now": NOW}

    class DeadlineCrossingPublisher(ConcurrentPublisher):
        def publish(self, state, policy):
            generation = super().publish(state, policy)
            current["now"] = NOW + timedelta(seconds=31)
            return generation

    publisher = DeadlineCrossingPublisher()
    routes._publisher = publisher
    routes._clock = lambda: current["now"]

    with pytest.raises(RuntimeError, match="deadline"):
        routes.publish_run(started.owner_id)

    assert publisher.aliases == [("deadline-gang",), ()]
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        recovery = session.get(Job, restart.id)
        assert run.state == "failed"
        assert run.route_state == "withdrawn"
        assert run.route_generation == 2
        assert recovery.state == "failed"
        assert "deadline" in recovery.result["recovery_error"]
        assert recovery.result.get("recovery_route_published") is not True


def test_expired_recovery_route_is_unusable_when_compensating_withdrawal_fails(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        routes,
        _publisher,
        started,
        restart,
        worker_start,
        nodes,
    ) = _queued_distributed_recovery_restart(tmp_path)
    service.record_node_result(
        restart.id,
        nodes[1],
        succeeded=True,
        evidence=start_evidence(worker_start.payload),
    )
    with sessions() as session:
        owner_start = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
            )
        )
    service.record_node_result(
        restart.id,
        nodes[0],
        succeeded=True,
        evidence=start_evidence(owner_start.payload),
    )
    complete_collective_readiness(sessions, service, restart.id, nodes[0])

    current = {"now": NOW}
    live_root = tmp_path / "live-routes"
    runtime = AtomicRouteBundlePublisher(
        live_root,
        management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
        clock=lambda: current["now"],
    )
    atomic = AtomicRecipeRoutePublisher(runtime, clock=lambda: current["now"])

    class DeadlineCrossingWithdrawalFailure:
        def __init__(self) -> None:
            self.withdrawal_attempts = 0
            self.fail_withdrawal = True

        def publish_recipe(self, candidate):
            generation = atomic.publish_recipe(candidate)
            current["now"] = NOW + timedelta(seconds=31)
            return generation

        def publish_empty(self, route_digest):
            self.withdrawal_attempts += 1
            if self.fail_withdrawal:
                raise RuntimeError("synthetic route withdrawal failure")
            return atomic.publish_empty(
                route_digest,
                expires_at=current["now"] + timedelta(seconds=300),
            )

    failing = DeadlineCrossingWithdrawalFailure()
    routes._publisher = failing
    routes._clock = lambda: current["now"]

    with pytest.raises(RuntimeError, match="deadline"):
        routes.publish_run(started.owner_id)

    with pytest.raises(RouteRuntimeError, match="expired"):
        verify_active_route_bundle(live_root, clock=lambda: current["now"])
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        recovery = session.get(Job, restart.id)
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        assert run.state == "failed"
        assert run.route_state == "withdrawn"
        assert recovery.state == "failed"
        assert "deadline" in recovery.result["recovery_error"]
        assert recovery.result.get("recovery_route_published") is not True
        assert publication is not None
        assert publication.state == "withdrawal-pending"
        assert publication.lease_expires_at.replace(tzinfo=UTC) == (
            NOW + timedelta(seconds=30)
        )
    assert failing.withdrawal_attempts == 1

    failing.fail_withdrawal = False
    assert routes.maintain() is True
    with sessions() as session:
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        assert publication is not None
        assert publication.state == "routes-withdrawn"


def test_recovery_expiry_inside_real_supervisor_ack_commits_cleanup_retry(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        routes,
        _publisher,
        started,
        restart,
        worker_start,
        nodes,
    ) = _queued_distributed_recovery_restart(tmp_path)
    service.record_node_result(
        restart.id,
        nodes[1],
        succeeded=True,
        evidence=start_evidence(worker_start.payload),
    )
    with sessions() as session:
        owner_start = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
            )
        )
    service.record_node_result(
        restart.id,
        nodes[0],
        succeeded=True,
        evidence=start_evidence(owner_start.payload),
    )
    complete_collective_readiness(sessions, service, restart.id, nodes[0])

    current = {"now": NOW + timedelta(seconds=29)}
    live_root = tmp_path / "ack-routes"
    ack_path = tmp_path / "supervisor/ack.json"
    ack_path.parent.mkdir()

    def expire_while_waiting_for_ack(marker) -> None:
        acknowledgement = {
            "acknowledged_at": current["now"].isoformat(),
            "activation_sha256": marker.digest,
            "child_pid": 4321,
            "expires_at": marker.expires_at,
            "generation": marker.generation,
            "litellm_sha256": marker.litellm_sha256,
            "schema_version": 1,
            "state": marker.state,
        }
        acknowledgement_bytes = (
            json.dumps(acknowledgement, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()

        def acknowledge_at_exact_deadline(_seconds: float) -> None:
            ack_path.write_bytes(acknowledgement_bytes)
            current["now"] = NOW + timedelta(seconds=30)

        moments = iter((0.0, 0.1))
        FileSupervisorAcknowledger(
            ack_path,
            clock=lambda: current["now"],
            timeout_seconds=0.5,
            poll_seconds=0.1,
            monotonic=lambda: next(moments),
            sleep=acknowledge_at_exact_deadline,
        )(marker)

    runtime = AtomicRouteBundlePublisher(
        live_root,
        management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
        clock=lambda: current["now"],
        await_supervisor_ack=expire_while_waiting_for_ack,
    )
    atomic = AtomicRecipeRoutePublisher(runtime, clock=lambda: current["now"])

    class AcknowledgementCrossingWithdrawalFailure:
        def __init__(self) -> None:
            self.withdrawal_attempts = 0
            self.fail_withdrawal = True

        def publish_recipe(self, candidate):
            return atomic.publish_recipe(candidate)

        def publish_empty(self, route_digest):
            self.withdrawal_attempts += 1
            if self.fail_withdrawal:
                raise RuntimeError("synthetic route withdrawal failure")
            return atomic.publish_empty(
                route_digest,
                expires_at=current["now"] + timedelta(seconds=300),
            )

    failing = AcknowledgementCrossingWithdrawalFailure()
    routes._publisher = failing
    routes._clock = lambda: current["now"]

    with pytest.raises(RuntimeError, match="expired|deadline"):
        routes.publish_run(started.owner_id)

    with pytest.raises(RouteRuntimeError, match="expired"):
        verify_active_route_bundle(live_root, clock=lambda: current["now"])
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        recovery = session.get(Job, restart.id)
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        assert run.state == "failed"
        assert run.route_state == "withdrawn"
        assert recovery.state == "failed"
        assert "deadline" in recovery.result["recovery_error"]
        assert recovery.result.get("recovery_route_published") is not True
        assert publication is not None
        assert publication.state == "withdrawal-pending"
        assert publication.lease_expires_at.replace(tzinfo=UTC) == (
            NOW + timedelta(seconds=30)
        )
    assert failing.withdrawal_attempts == 1

    failing.fail_withdrawal = False
    runtime._await_supervisor_ack = None
    assert routes.maintain() is True
    with sessions() as session:
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        assert publication is not None
        assert publication.state == "routes-withdrawn"


def test_distributed_rank_loss_withdraws_route_when_recovery_authority_is_missing(
    tmp_path: Path,
) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="m" * 36
    )
    start = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="n" * 36,
        alias="failed-authority-gang",
    )
    publisher = ConcurrentPublisher()
    _service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(start.owner_id)
    failure_observed_at = NOW + timedelta(seconds=1)
    record_exact_empty_snapshot(sessions, nodes[1], failure_observed_at)
    with sessions.begin() as session:
        for presence in session.scalars(select(AgentPresence)):
            session.delete(presence)
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )

    assert recovery.tick() is True

    with sessions() as session:
        run = session.get(RecipeRun, start.owner_id)
        assert run.state == "failed"
        assert run.route_state == "withdrawn"
        assert "endpoint evidence is missing" in run.route_error
        assert not session.scalar(
            select(Job.id).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == start.owner_id,
            )
        )
    assert publisher.aliases[-1] == ()


def test_run_observation_snapshot_rejects_duplicate_and_naive_evidence(
    tmp_path: Path,
) -> None:
    sessions, _service, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    observation = RecipeRunObservation(str(uuid.uuid4()), True)

    with pytest.raises(ValueError, match="duplicated"):
        record_recipe_run_observations(
            sessions, nodes[0], NOW, (observation, observation)
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        record_recipe_run_observations(sessions, nodes[0], NOW.replace(tzinfo=None), ())


def test_multinode_start_is_bound_to_authenticated_fabric_rendezvous(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="d" * 36,
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "qwen-gang")
    assert run_plan.allowed is True
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="e" * 36,
    )

    with sessions() as session:
        job = session.get(Job, start.id)
        assert job is not None
        children = [
            entry["payload"] for phase in job.payload["phases"] for entry in phase
        ]
        assert [child["local_address"] for child in children] == [
            "192.168.100.3",
            "192.168.100.2",
        ]
        assert {child["master_address"] for child in children} == {"192.168.100.2"}
        assert {child["master_port"] for child in children} == {29500}
        assert {child["world_size"] for child in children} == {2}
        assert [child["endpoint_address"] for child in children] == [
            "192.168.100.3",
            "192.168.1.211",
        ]


def test_multinode_worker_endpoint_is_never_published_on_management_lan(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="a" * 35 + "1",
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )

    run_plan = service.preview_run(install.owner_id, "qwen-gang")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="a" * 35 + "2",
    )

    with sessions() as session:
        job = session.get(Job, start.id)
        assert job is not None
        children = {
            entry["node_id"]: entry["payload"]
            for phase in job.payload["phases"]
            for entry in phase
        }
    assert children[nodes[0]]["endpoint_address"] == "192.168.1.211"
    assert children[nodes[1]]["endpoint_address"] == "192.168.100.3"
    assert children[nodes[1]]["endpoint_address"] != "192.168.1.212"


def test_failed_multinode_start_queues_idempotent_stop_for_every_rank(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="f" * 35 + "0",
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "qwen-gang")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="f" * 35 + "1",
    )
    with sessions() as session:
        worker = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        assert worker is not None
    service.record_node_result(
        start.id, worker.node_id, succeeded=False, evidence={"code": "start.failed"}
    )

    with sessions() as session:
        cleanup = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == start.owner_id,
            )
        )
        assert cleanup is not None
        assert set(cleanup.targets) == set(nodes)
        cleanup_children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == cleanup.id)
            )
        )
        assert [child.node_id for child in cleanup_children] == [nodes[0]]
        assert set(cleanup_children[0].payload) == {
            "schema_version",
            "run_id",
            "plan_digest",
        }
        assert session.get(RecipeRun, start.owner_id).state == "stopping"


def test_concurrent_final_rank_results_serialize_gang_cleanup(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=3, engine=postgres_engine
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="d" * 36,
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "qwen-gang")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="e" * 36,
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == start.id,
                AgentOperation.node_id == nodes[1],
            )
        )
        assert child is not None
        evidence = start_evidence(child.payload)
    barrier = threading.Barrier(2)

    def result(node_id: str, succeeded: bool) -> None:
        barrier.wait()
        service.record_node_result(
            start.id,
            node_id,
            succeeded=succeeded,
            evidence=evidence if succeeded else {"code": "start.failed"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(result, nodes[1], True),
            pool.submit(result, nodes[2], False),
        ]
        for future in futures:
            future.result(timeout=10)

    with sessions() as session:
        cleanup = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == start.owner_id,
            )
        )
        assert cleanup is not None
        assert session.get(RecipeRun, start.owner_id).state == "stopping"
        assert set(cleanup.targets) == set(nodes)


def test_postgres_disjoint_stops_serialize_one_route_candidate(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "1"
    )
    first = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="e" * 35 + "2",
        alias="first",
    )
    second_run_id = clone_running_run(sessions, first.owner_id, alias="second")
    with sessions.begin() as session:
        for run_id in (first.owner_id, second_run_id):
            run = session.get(RecipeRun, run_id)
            run.plan = {**run.plan, "observation_schema_version": 2}
    mark_current_exact_observations(sessions, first.owner_id, NOW)
    mark_current_exact_observations(sessions, second_run_id, NOW)
    publisher = ConcurrentPublisher()
    service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(first.owner_id)
    routes.publish_run(second_run_id)
    plans = {
        first.owner_id: service.preview_stop(first.owner_id),
        second_run_id: service.preview_stop(second_run_id),
    }
    start = threading.Barrier(2)

    def stop(item: tuple[str, str]) -> str:
        run_id, request_key = item
        start.wait()
        return service.stop(
            run_id,
            plan_digest=plans[run_id].plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    requests = (
        (first.owner_id, "e" * 35 + "3"),
        (second_run_id, "e" * 35 + "4"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        operation_ids = list(pool.map(stop, requests))

    assert len(set(operation_ids)) == 2
    assert publisher.aliases[-1] == ()
    with sessions() as session:
        assert [
            (
                session.get(RecipeRun, run_id).state,
                session.get(RecipeRun, run_id).route_state,
            )
            for run_id, _request_key in requests
        ] == [("stopping", "withdrawn"), ("stopping", "withdrawn")]


def test_postgres_duplicate_stop_request_returns_same_operation(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "5"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="e" * 35 + "6",
    )
    publisher = ConcurrentPublisher()
    service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(run.owner_id)
    plan = service.preview_stop(run.owner_id)
    request_key = "e" * 35 + "7"
    start = threading.Barrier(2)

    def stop() -> str:
        start.wait()
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        operation_ids = list(pool.map(lambda _index: stop(), range(2)))

    assert len(set(operation_ids)) == 1
    with sessions() as session:
        assert (
            len(
                tuple(session.scalars(select(Job).where(Job.request_id == request_key)))
            )
            == 1
        )


def test_postgres_duplicate_uninstall_rechecks_replay_after_installation_lock(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="f" * 35 + "2"
    )
    plan = service.preview_uninstall(installation.owner_id)
    request_key = "f" * 35 + "3"
    available_before = queue.available
    role = threading.local()
    backend_pids: dict[str, int] = {}
    first_locked = threading.Event()
    second_lock_started = threading.Event()
    release_first = threading.Event()

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "second"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["second"] = _postgres_backend_pid(connection)
            second_lock_started.set()

    def after_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "first"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["first"] = _postgres_backend_pid(connection)
            first_locked.set()
            assert release_first.wait(timeout=10)

    def uninstall(label: str):
        role.value = label
        return service.uninstall(
            installation.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        )

    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    event.listen(postgres_engine, "after_cursor_execute", after_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(uninstall, "first")
        assert first_locked.wait(timeout=10)
        second = pool.submit(uninstall, "second")
        assert second_lock_started.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["second"],
            blocker_pid=backend_pids["first"],
        )
        release_first.set()
        first_view = first.result(timeout=10)
        second_view = second.result(timeout=10)
    finally:
        release_first.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)
        event.remove(postgres_engine, "after_cursor_execute", after_lock)

    assert second_view == first_view
    assert queue.available == available_before + 1
    with sessions() as session:
        parents = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.uninstall"))
        )
        children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == first_view.id
                )
            )
        )
    assert [parent.id for parent in parents] == [first_view.id]
    assert {child.node_id for child in children} == set(nodes)


def test_postgres_start_waiting_on_accepted_uninstall_is_rejected(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="f" * 35 + "4"
    )
    run_plan = service.preview_run(installation.owner_id, "must-not-start")
    uninstall_plan = service.preview_uninstall(installation.owner_id)
    available_before = queue.available
    role = threading.local()
    backend_pids: dict[str, int] = {}
    uninstall_locked = threading.Event()
    start_lock_started = threading.Event()
    release_uninstall = threading.Event()

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "start"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["start"] = _postgres_backend_pid(connection)
            start_lock_started.set()

    def after_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "uninstall"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["uninstall"] = _postgres_backend_pid(connection)
            uninstall_locked.set()
            assert release_uninstall.wait(timeout=10)

    def uninstall():
        role.value = "uninstall"
        return service.uninstall(
            installation.owner_id,
            plan_digest=uninstall_plan.plan_digest,
            actor="admin",
            request_id="f" * 35 + "5",
        )

    def start():
        role.value = "start"
        try:
            return service.start(
                run_plan,
                plan_digest=run_plan.plan_digest,
                actor="admin",
                request_id="f" * 35 + "6",
            )
        except RecipeOperationConflict as error:
            return error

    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    event.listen(postgres_engine, "after_cursor_execute", after_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        uninstall_future = pool.submit(uninstall)
        assert uninstall_locked.wait(timeout=10)
        start_future = pool.submit(start)
        assert start_lock_started.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["start"],
            blocker_pid=backend_pids["uninstall"],
        )
        release_uninstall.set()
        uninstall_view = uninstall_future.result(timeout=10)
        start_result = start_future.result(timeout=10)
    finally:
        release_uninstall.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)
        event.remove(postgres_engine, "after_cursor_execute", after_lock)

    assert isinstance(start_result, RecipeOperationConflict)
    assert "not runnable" in str(start_result)
    assert queue.available == available_before + 1
    with sessions() as session:
        start_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.start"))
        )
        runs = tuple(session.scalars(select(RecipeRun)))
        uninstall_children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == uninstall_view.id
                )
            )
        )
    assert start_jobs == ()
    assert runs == ()
    assert {child.node_id for child in uninstall_children} == set(nodes)


def test_changed_plan_or_reused_request_key_is_rejected(tmp_path: Path) -> None:
    _sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping_id, build_id)
    with pytest.raises(RecipeOperationConflict, match="plan digest"):
        service.install(plan, plan_digest="0" * 64, actor="admin", request_id="9" * 36)
    service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="a" * 36
    )
    with pytest.raises(RecipeOperationConflict, match="request key"):
        service.stop(
            "f" * 36,
            plan_digest="0" * 64,
            actor="admin",
            request_id="a" * 36,
        )
