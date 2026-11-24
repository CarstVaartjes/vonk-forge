"""Shared helpers for the recipe_operations tests."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
)

from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.host_helper import (
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    HostRuntimeRequest,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.recipe_operations import RecipeStartPayload, RecipeStopPayload
from vonk_agent_protocol.route_activation import SupervisorPhase
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.agent_contacts import record_contact
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.catalog.entities import _digest
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.distributed_recovery.coordinator import DistributedRecoveryCoordinator
from vonk_control.execution_plan.service import (
    ControllerExecutionPlanService,
)
from vonk_control.gateway.litellm import LiteLlmGeneration, ServedModelInfo
from vonk_control.host_authority.helper import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.host_authority.runtime_plan import derive_runtime_plan_binding
from vonk_control.install_admission.service import InstallAdmissionService
from vonk_control.inventory.items import inventory_items
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.model_primitives import Base
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import (
    CatalogDocument,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
)
from vonk_control.models.execution import (
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.models.fleet import AgentCertificate, AgentNode, User
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operations.observation_helpers import (
    prepare_exact_recipe_run_observation_nodes,
)
from vonk_control.recipe_operations.service import RecipeOperationService
from vonk_control.recipe_routes.publisher import AtomicRecipeRoutePublisher
from vonk_control.recipe_routes.service import RecipeRouteService
from vonk_control.runs.admission import RunAdmissionService
from vonk_control.runtime.adapters import resolve_runtime_adapter
from vonk_control.runtime_image_preparation.contracts import PulledImageEvidence
from vonk_control.runtime_image_preparation.preparation import prepare_runtime_image
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage

from .agent_fences import fenced_operation
from .canonical_recipe_fixtures import canonical_example
from .preflight_fixtures import record_passing_preflight
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    claim_agent,
    inventory_items_fixture,
)
from .runtime_image_fixtures import place_test_image

_FIXTURE_ADAPTER = resolve_runtime_adapter("vllm", {"node_count": 1})


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
            workload_intent_ordinal=session.get(Job, parent_job_id).payload.get(
                "workload_intent_ordinal"
            ),
            state="queued",
            current_attempt=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(row)
        return row

    def notify_available(self) -> None:
        self.available += 1

    def request_superseded_workload_cancellation_in_session(
        self, session, targets, ordinal, now
    ) -> None:
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, targets, ordinal, now
        )


class FailingQueue(RecordingQueue):
    def enqueue_in_session(self, *args, **kwargs):
        raise RuntimeError("queue write failed")


class ConcurrentPublisher(AtomicRecipeRoutePublisher):
    """In-memory route publisher stand-in that never touches a route bundle."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._generation = 0
        self.aliases: list[tuple[str, ...]] = []

    def served_model_info(self):
        return ServedModelInfo()

    def publish_recipe(self, candidate, *, required=SupervisorPhase.READY):
        state = candidate.state
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


def _required[T](value: T | None) -> T:
    """Return a persisted row the test itself just wrote, asserting presence."""

    assert value is not None
    return value


def _job_phases(payload: Mapping[str, object]) -> list[list[Mapping[str, object]]]:
    """Return a persisted job payload's nested phase groups as mappings."""

    return [
        [
            require_mapping(entry, "job phase entry")
            for entry in require_sequence(phase, "job phase")
        ]
        for phase in require_sequence(payload["phases"], "job phases")
    ]


def _typed_stop_payloads(
    payload: Mapping[str, object],
) -> tuple[RecipeStopPayload, ...]:
    """Validate each retained Stop phase through the agent wire contract."""

    return tuple(
        RecipeStopPayload.model_validate_json(
            canonical_message(entry["payload"]), strict=True
        )
        for phase in _job_phases(payload)
        for entry in phase
    )


def _synthetic_model_content_sha256() -> str:
    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    return document_sha256(document)


class _CanonicalModelCache:
    """Small exact cache authority used by the canonical operation fixture."""

    artifact_set_sha256 = "f" * 64
    file_sha256 = "c" * 64

    def resolve_artifact_set(self, **_kwargs):
        return SimpleNamespace(
            digest=self.artifact_set_sha256,
            artifacts=(
                SimpleNamespace(
                    artifact_id="weights",
                    path="model.safetensors",
                    sha256=self.file_sha256,
                    expected_bytes=1024,
                    roles=("weights",),
                    model_content_sha256=_synthetic_model_content_sha256(),
                ),
            ),
        )

    def adopt_covered_set(self, manifest) -> None:
        assert manifest.digest == self.artifact_set_sha256


def _placement(payload: object) -> dict[str, Any]:
    """The compiled placement a Start/Stop/job payload carries."""

    document = json.loads(canonical_message(payload))
    return document["compiled_execution_plan"]["runtime"]["placement"]


def start_evidence(payload: Mapping[str, object]) -> dict[str, object]:
    """The serving rank reports its ready endpoint; every other rank reports {}."""

    placement = _placement(payload)
    address = placement["endpoint_address"]
    if payload.get("phase") == "rank-launch" or address is None:
        return {}
    host = f"[{address}]" if ":" in address else address
    return {"endpoint": f"http://{host}:{placement['port']}"}


class _CanonicalImageTransport:
    def pull_and_export(
        self,
        reference: str,
        destination: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        progress: Callable[[str, int, int | None], None] | None = None,
    ) -> PulledImageEvidence:
        raise NotImplementedError(
            "the canonical fixture prepares only the stored archive"
        )

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
            config_id="sha256:" + "4" * 64,
            local_reference="localhost/vonk/fixture@sha256:" + "4" * 64,
            architecture=expected_architecture,
            runtime_interface="v1",
            archive_sha256=expected_archive_sha256,
            archive_bytes=expected_archive_bytes,
        )


def setup_services(
    tmp_path: Path,
    *,
    nodes: int = 1,
    mapping_node_count: int | None = None,
    endpoint_owner_rank_one: bool = False,
    distributed_lifecycle: bool = False,
    start_order: tuple[str, ...] | None = None,
    recipe_transform: Callable[[dict[str, object]], None] | None = None,
    model_transform: Callable[[dict[str, object]], None] | None = None,
    engine=None,
    create_schema: bool = True,
    route_withdrawer=None,
    distributed_start_timeout_seconds: int = 60,
    model_artifact: bool = False,
):
    placement_count = nodes if mapping_node_count is None else mapping_node_count
    assert 1 <= placement_count <= nodes
    engine = engine or create_engine(
        f"sqlite:///{tmp_path / 'operations.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    if create_schema:
        Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_ids = tuple("spk_" + f"{index + 1:032x}" for index in range(nodes))
    with sessions.begin() as session:
        session.add(User(subject="admin", role="administrator"))
        for index, node_id in enumerate(node_ids):
            serial = f"serial-{index}"
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    inventory_snapshot=inventory_items_fixture(
                        node_id,
                        observed_at=None,
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
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
            record_contact(
                session,
                node_id,
                contact_certificate_serial=serial,
                management_address=f"192.168.1.{211 + index}",
                management_observed_at=NOW,
            )
    inventory = InventoryRepository(sessions, clock=lambda: NOW)
    capabilities = (
        "runtime.vonk.v1",
        "recipe.image.pull.v1",
        "recipe.operations.v1",
    ) + (
        (
            "fabric.connected.mbps.200000",
            "recipe.start.two-phase.v1",
            "recipe.run.inspect.exact.v1",
        )
        if nodes > 1
        else ()
    )
    for index, node_id in enumerate(node_ids):
        inventory.record(
            NodeInventoryReport.model_validate_json(
                canonical_message(
                    {
                        "node_id": node_id,
                        "observed_at": NOW,
                        "disk_total_bytes": 10_000,
                        "disk_free_bytes": 8_000,
                        "host_memory_total_bytes": 10_000,
                        "host_memory_free_bytes": 8_000,
                        "gpu_memory_total_bytes": 10_000,
                        "gpu_memory_free_bytes": 8_000,
                        "gpu_count": 1,
                        "artifact_store_read_only": False,
                        "capabilities": capabilities,
                        "fabric_address": f"192.168.100.{index + 2}"
                        if nodes > 1
                        else None,
                        "fabric_bandwidth_mbps": 200000 if nodes > 1 else None,
                        "memory_pool": "shared",
                        "schema_version": 1,
                        "nvidia_driver_version": "unknown",
                        "container_runtime_version": "unknown",
                    }
                )
            )
        )
    document = canonical_example("recipe-source-build.json")
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
            "working_bytes": 20,
            "safety_margin_bytes": 10,
        },
        "memory": {"peak_bytes": 225, "reserve_bytes": 0},
    }
    if placement_count > 1:
        worker = json.loads(json.dumps(role))
        worker.update(
            {"name": "worker", "count": placement_count - 1, "endpoint_owner": False}
        )
        roles = [role, worker]
        if endpoint_owner_rank_one:
            roles = [worker, role]
        document["topology"] = {
            **document["topology"],
            "name": f"nodes_{placement_count}",
            "node_count": placement_count,
            "parallelism": {
                "tensor": placement_count,
                "pipeline": 1,
                "data": 1,
                "backend": "tcp",
            },
            "roles": roles,
            "start_order": list(start_order or ("worker", "entrypoint")),
        }
        document["models"][0]["files"][0]["roles"] = ["entrypoint", "worker"]
        if distributed_lifecycle:
            document["topology"]["parallelism"]["backend"] = "mp"
            document["runtime"]["lifecycle"] = {"stop_timeout_seconds": 30}
    if recipe_transform is not None:
        recipe_transform(document)
    if model_transform is not None:
        model_transform(model_document)
    RecipeDefinition.model_validate(document)
    model_definition = ModelDefinition.model_validate(model_document)
    recipe_digest = document_sha256(document)
    model_digest = document_sha256(model_document)
    canonical_recipe_document = document
    canonical_model_document = model_document
    recipe_revision_id = str(uuid.uuid4())
    with sessions.begin() as session:
        recipe_catalog = CatalogDocument(
            kind="recipe",
            publisher=document["identity"]["publisher"],
            slug=document["identity"]["slug"],
            title=document["identity"]["slug"],
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
            title=model_document["identity"]["slug"],
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(model_catalog)
        session.flush()
        model_revision_id = str(uuid.uuid4())
        session.add(
            CatalogDocumentRevision(
                id=model_revision_id,
                document_id=model_catalog.id,
                kind="model",
                publisher=model_catalog.publisher,
                slug=model_catalog.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=canonical_model_document,
                content_digest=model_digest,
                artifact_key=(
                    _digest(
                        {
                            "files": [
                                item.model_dump(mode="json")
                                for item in model_definition.files
                            ],
                            "format": model_definition.format.model_dump(mode="json"),
                        }
                    )
                    if model_artifact
                    else None
                ),
                projected={},
                created_by="admin",
                created_at=NOW,
            )
        )
        session.flush()
        # An active recipe revision is always bound to its models, as the catalog
        # writes it.
        session.add(
            CatalogRecipeModelReference(
                recipe_revision_id=revision.id,
                recipe_kind="recipe",
                selection_id=str(document["models"][0]["id"]),
                model_revision_id=model_revision_id,
                model_kind="model",
                model_publisher=model_catalog.publisher,
                model_slug=model_catalog.slug,
                model_content_digest=model_digest,
            )
        )
        session.flush()
    mappings = ClusterMappingService(sessions)
    mapping_plan = mappings.preview(
        revision.id, node_ids[:placement_count], {}, "admin"
    )
    mapping_id = mappings.materialize(mapping_plan, actor="admin", now=NOW)
    image_archive = b"canonical-runtime-image-archive"[:30]
    image_archive_sha256 = hashlib.sha256(image_archive).hexdigest()

    runtime_image_storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
    place_test_image(runtime_image_storage, image_archive_sha256, len(image_archive))
    with sessions.begin() as session:
        build = RecipeBuild(
            recipe_revision_id=revision.id,
            builder_node_id=node_ids[0],
            source_bundle_sha256="d" * 64,
            build_input_sha256="e" * 64,
            state="succeeded",
            policy_report={
                "passed": True,
                "source_bundle_sha256": "d" * 64,
                "dockerfile": "Dockerfile",
                "findings": [],
                "builder_binary_digest": None,
                "artifact_format": "docker-archive-v1",
            },
            plan={
                "adapter": _FIXTURE_ADAPTER.to_wire().model_dump(mode="json"),
                "build_id": "00000000-0000-4000-8000-000000000000",
                "recipe_revision_id": revision.id,
                "recipe_content_sha256": recipe_digest,
                "source_bundle_sha256": "d" * 64,
                "source_bundle_bytes": 1,
                "build_input_sha256": "e" * 64,
                "base_images": [],
                "base_image_storage_bytes": 0,
                "capabilities": [],
                "dockerfile": "Dockerfile",
                "network": {"hosts": []},
                "options": {
                    "additional_contexts": [],
                    "annotations": [],
                    "environment": [],
                    "format": "oci",
                    "identity_label": False,
                    "ignorefile": None,
                    "jobs": 1,
                    "labels": [],
                    "layer_compression": "disabled",
                    "layer_labels": [],
                    "layers": True,
                    "no_hostname": False,
                    "no_hosts": False,
                    "omit_history": False,
                    "os_features": [],
                    "os_version": None,
                    "shm_bytes": 65536,
                    "skip_unused_stages": False,
                    "squash": "none",
                    "timestamp": None,
                    "unset_environment": [],
                    "unset_labels": [],
                },
                "limits": {
                    "cpu_cores": 1,
                    "memory_bytes": 1,
                    "output_bytes": 1,
                    "processes": 1,
                    "temporary_bytes": 1,
                    "timeout_seconds": 1,
                },
            },
            image_digest="sha256:" + "1" * 64,
            oci_layout_sha256=image_archive_sha256,
            image_bytes=30,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(build)
        session.flush()
        build.plan = {**build.plan, "build_id": build.id}
        build_id = build.id
    canonical_cache = _CanonicalModelCache()

    def prepare_canonical_runtime_image(document, runtime_spec, build):
        return prepare_runtime_image(
            document,
            runtime=runtime_spec.runtime,
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
        distributed_start_timeout_seconds=distributed_start_timeout_seconds,
    )
    record_passing_preflight(sessions, NOW)
    return sessions, service, queue, mapping_id, build_id, node_ids


def record_exact_empty_snapshot(
    sessions: sessionmaker[Session], node_id: str, observed_at: datetime
) -> None:
    with sessions.begin() as session:
        prepare_exact_recipe_run_observation_nodes(session, node_id, observed_at, set())


def mark_current_exact_observations(
    sessions: sessionmaker[Session], run_id: str, observed_at: datetime
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
            node.observation_process_running = True
            node.observation_observed_at = observed_at
            node.observation_endpoint_ready = (
                True if node.role == "entrypoint" else None
            )
            node.updated_at = observed_at


def _issue_exact_stop_grant(
    sessions: sessionmaker[Session],
    *,
    node_id: str,
    certificate_serial: str,
    grant_now: datetime = NOW,
    lease_seconds: int = 600,
):
    """Claim a queued Stop and exercise the production exact-plan signer."""

    runtime_identity = {
        **PACKAGED_RUNTIME_IDENTITY,
        "architecture": "linux-arm64",
    }
    queue = AgentJobService(sessions, clock=lambda: grant_now)
    with sessions() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        fingerprint = inventory_items(node).preflight_fingerprint
    claim = claim_agent(
        queue,
        node_id,
        certificate_serial,
        runtime_identity=runtime_identity,
        preflight_fingerprint=fingerprint,
    )
    assert claim is not None
    assert claim.operation.value == "recipe.stop"
    stop = RecipeStopPayload.model_validate_json(
        canonical_message(claim.payload), strict=True
    )
    request = HostRuntimeRequest(
        action="stop",
        fence=claim.fence,
        arguments=[],
        stop_plan=stop,
        run_generation=stop.run_generation,
    )
    with sessions() as session:
        parent = _required(
            session.get(Job, fenced_operation(sessions, claim).parent_job_id)
        )
        operation = _required(
            session.get(AgentOperation, fenced_operation(sessions, claim).id)
        )
        binding = derive_runtime_plan_binding(
            session,
            parent=parent,
            operation=operation,
            node_id=node_id,
            action=ContainerRuntimeAction.STOP,
            stop_plan_sha256=hashlib.sha256(canonical_message(stop)).hexdigest(),
        )
    signer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
        clock=lambda: grant_now,
    )
    authority = HostRuntimeAuthorityService(sessions, signer, clock=lambda: grant_now)
    grant = authority.issue_grant(
        node_id=node_id,
        fence=claim.fence,
        action=ContainerRuntimeAction.STOP,
        request_sha256=hashlib.sha256(canonical_message(request)).hexdigest(),
        certificate_serial=certificate_serial,
        start_plan_sha256=binding.start_plan_sha256,
        stop_plan_sha256=binding.stop_plan_sha256,
        run_generation=binding.run_generation,
        runtime_run_id=binding.runtime_run_id,
        runtime_target_id=binding.runtime_target_id,
        runtime_installation_id=binding.runtime_installation_id,
    )
    operation_claim = grant.claims.operation
    assert isinstance(operation_claim, ExecuteContainerRuntimeRequestOperation)
    assert operation_claim.stop_plan_sha256 == binding.stop_plan_sha256
    assert operation_claim.runtime_target_id == stop.target_runtime_id
    signer.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )
    return claim, stop, grant


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
    complete_started_recipe(sessions, service, operation.id)
    return operation


def complete_started_recipe(
    sessions, service: RecipeOperationService, operation_id: str
) -> None:
    operation = service.get(operation_id)
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
    mark_current_exact_observations(sessions, operation.owner_id, service._clock())


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
    mark_current_exact_observations(sessions, run_id, service._clock())


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
        maximum_age_seconds=120,
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
        source_start = session.scalar(
            select(Job)
            .where(
                Job.kind == "recipe.start",
                Job.payload["owner_id"].as_string() == source_run_id,
            )
            .order_by(Job.created_at, Job.id)
        )
        assert source_start is not None
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
                run_generation=source.run_generation,
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
        source_phases = source_start.payload.get("phases")
        assert isinstance(source_phases, list)
        assert source_phases
        start_job_id = str(uuid.uuid4())
        cloned_phases: list[list[dict[str, object]]] = []
        cloned_operations: list[AgentOperation] = []
        cloned_targets: set[str] = set()
        for phase in source_phases:
            assert isinstance(phase, list)
            assert phase
            cloned_phase: list[dict[str, object]] = []
            for raw_entry in phase:
                assert isinstance(raw_entry, Mapping)
                node_id = raw_entry.get("node_id")
                raw_payload = raw_entry.get("payload")
                assert isinstance(node_id, str)
                assert isinstance(raw_payload, Mapping)
                cloned_targets.add(node_id)
                source_payload = RecipeStartPayload.model_validate_json(
                    canonical_message(raw_payload), strict=True
                )
                cloned_payload = source_payload.model_copy(
                    update={"run_id": run_id, "plan_digest": authority, "alias": alias}
                )
                payload = json.loads(canonical_message(cloned_payload))
                operation_id = str(uuid.uuid4())
                cloned_phase.append(
                    {
                        "operation_id": operation_id,
                        "node_id": node_id,
                        "payload": payload,
                    }
                )
                cloned_operations.append(
                    AgentOperation(
                        id=operation_id,
                        parent_job_id=start_job_id,
                        node_id=node_id,
                        kind="recipe.start",
                        payload_digest=hashlib.sha256(
                            canonical_message(payload)
                        ).hexdigest(),
                        payload=payload,
                        authority_revision=source_start.authority_revision,
                        workload_intent_ordinal=source_start.payload.get(
                            "workload_intent_ordinal"
                        ),
                        state="succeeded",
                        current_attempt=1,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            cloned_phases.append(cloned_phase)
        cloned_start_payload = dict(source_start.payload)
        cloned_start_payload["owner_id"] = run_id
        cloned_start_payload["plan_digest"] = authority
        cloned_start_payload["phases"] = cloned_phases
        cloned_start = Job(
            id=start_job_id,
            request_id=str(uuid.uuid4()),
            kind="recipe.start",
            state="succeeded",
            actor="admin",
            authority_revision=source_start.authority_revision,
            targets=sorted(cloned_targets),
            payload_digest=hashlib.sha256(
                canonical_message(cloned_start_payload)
            ).hexdigest(),
            payload=cloned_start_payload,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(cloned_start)
        session.flush()
        session.add_all(cloned_operations)
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


def _recovery_deadline(job: Job) -> datetime:
    marker = job.payload["recovery"]
    assert isinstance(marker, dict)
    deadline = marker["deadline"]
    assert isinstance(deadline, str)
    return datetime.fromisoformat(deadline)


def _queued_distributed_recovery_stop(
    tmp_path: Path, *, engine=None, startup_budget=60
):
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        distributed_lifecycle=True,
        engine=engine,
        distributed_start_timeout_seconds=startup_budget,
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
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: NOW,
        start_timeout_seconds=startup_budget,
    )
    assert recovery.tick() is True
    with sessions() as session:
        stop_job = _required(
            session.scalar(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == started.owner_id,
                )
            )
        )
    return sessions, service, routes, publisher, started, stop_job, nodes


def _queued_distributed_recovery_restart(
    tmp_path: Path, *, engine=None, startup_budget=60
):
    (
        sessions,
        service,
        routes,
        publisher,
        started,
        stop_job,
        nodes,
    ) = _queued_distributed_recovery_stop(
        tmp_path, engine=engine, startup_budget=startup_budget
    )
    service.record_node_result(stop_job.id, nodes[0], succeeded=True, evidence={})
    service.record_node_result(stop_job.id, nodes[1], succeeded=True, evidence={})
    with sessions() as session:
        restart = _required(
            session.scalar(
                select(Job).where(
                    Job.kind == "recipe.start",
                    Job.payload["owner_id"].as_string() == started.owner_id,
                    Job.id != started.id,
                )
            )
        )
        worker_start = _required(
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == restart.id,
                    AgentOperation.node_id == nodes[1],
                )
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
