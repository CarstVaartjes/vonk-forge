from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import DistributionObject
from vonk_control.agent_api import AgentApiServices
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import TokenCodec
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.compiled_execution_plan import validate_compiled_launch_payload
from vonk_control.distribution import DistributionService
from vonk_control.distribution_executor import CompositeDistributionPhaseExecutor
from vonk_control.execution_plan_service import ControllerExecutionPlanService
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentPresence,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RuntimeImageReceipt,
)
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    ArtifactInspection,
    PhaseExecution,
    RunSwitchOperationService,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    persist_runtime_image_receipt,
    prepare_runtime_image,
)
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
REGISTRY_DIGEST = "sha256:" + "d" * 64
PLATFORM_DIGEST = "sha256:" + "e" * 64
CONFIG_DIGEST = "sha256:" + "c" * 64
MODEL_DIGEST = "c" * 64
MODEL_SET_DIGEST = "f" * 64
ARCHIVE = b"direct-published-runtime-archive"
ARCHIVE_DIGEST = hashlib.sha256(ARCHIVE).hexdigest()
NODE_ID = "spk_" + "1" * 32


class _Transport:
    def pull_and_export(
        self,
        _reference: str,
        destination: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> PulledImageEvidence:
        destination.write_bytes(ARCHIVE)
        return PulledImageEvidence(
            manifest_digest=PLATFORM_DIGEST,
            requested_manifest_digest=REGISTRY_DIGEST,
            config_id=CONFIG_DIGEST,
            local_reference="localhost/vonk/direct@" + PLATFORM_DIGEST,
            architecture=expected_architecture,
            runtime_interface="v1",
            archive_sha256=ARCHIVE_DIGEST,
            archive_bytes=len(ARCHIVE),
        )


class _ModelCache:
    def __init__(self, recipe_digest: str) -> None:
        self.recipe_digest = recipe_digest

    def resolve_artifact_set(self, **kwargs: object) -> SimpleNamespace:
        assert kwargs["recipe_revision_sha256"] == self.recipe_digest
        return SimpleNamespace(
            digest=MODEL_SET_DIGEST,
            recipe_revision_sha256=self.recipe_digest,
        )

    def verified_model_objects_for_set(self, digest: str) -> tuple[dict[str, object], ...]:
        assert digest == MODEL_SET_DIGEST
        return (
            {
                "model_content_sha256": "e1e9de42be3e14bdb392cba65c9bbcbec6a4ea5b448597e0c32d187c5840029c",
                "file_id": "weights",
                "path": "model.safetensors",
                "sha256": MODEL_DIGEST,
                "bytes": 1024,
                "roles": ["weights"],
                "distribution_object": {
                    "name": "model.safetensors",
                    "sha256": MODEL_DIGEST,
                    "bytes": 1024,
                    "kind": "model",
                },
            },
        )


class _Inspector:
    def inspect(self, _session, **_kwargs: object) -> ArtifactInspection:
        return ArtifactInspection(
            required_bytes=1024,
            reused_bytes=0,
            copied_bytes=1024,
            missing_nas_bytes=0,
            missing_spark_bytes=1024,
            reclaimable_bytes=0,
            nas_coverage="complete",
            spark_coverage="partial",
            artifact_digests=(MODEL_DIGEST,),
            reclaimable_digests=(),
            freshness=(),
            blockers=(),
            warnings=(),
            artifact_set_sha256=MODEL_SET_DIGEST,
            artifact_set_bytes=1024,
            dependency_model_version_sha256=(),
        )


class _Queue:
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
        from vonk_control.models import AgentOperation

        value = AgentOperation(
            id=operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=operation,
            payload_digest=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            payload=dict(payload),
            authority_revision=authority_revision,
            state="queued",
            current_attempt=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(value)
        return value

    def notify_available(self) -> None:
        self.available += 1


class _TargetExecutor(CompositeDistributionPhaseExecutor):
    """Use production receipt/assignment logic with deterministic child evidence."""

    def __init__(self, *args: object, events: list[str], tamper_db: str | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.events = events
        self.assignments: dict[str, dict[str, object]] = {}
        self._children: dict[str, object] = {}
        self._tamper_db = tamper_db
        self._did_tamper = False

    def execute(self, plan, phase, **kwargs):
        self.events.append(phase.subphase or phase.kind)
        if phase.subphase == "model-download":
            return PhaseExecution(
                result={
                    "artifact_set_sha256": MODEL_SET_DIGEST,
                    "coverage": "complete",
                    "downloaded_bytes": 1024,
                    "total_bytes": 1024,
                }
            )
        if phase.subphase == "target-copy" and self._tamper_db and not self._did_tamper:
            with self._sessions.begin() as session:
                row = session.scalar(select(RuntimeImageReceipt))
                assert row is not None
                if self._tamper_db == "platform":
                    row.platform_manifest_digest = "sha256:" + "a" * 64
                else:
                    row.oci_archive_sha256 = "a" * 64
            self._did_tamper = True
        return super().execute(plan, phase, **kwargs)

    def _ensure_child(
        self,
        plan,
        phase,
        *,
        actor,
        request_key,
        cached,
        assignments,
        target_order,
        target_bytes=None,
    ) -> str:
        del plan, phase, actor, request_key, cached, target_order, target_bytes
        child_id = str(uuid.uuid4())
        self.assignments.update(
            {node_id: value.to_mapping() for node_id, value in assignments.items()}
        )
        self._children[child_id] = SimpleNamespace(state="succeeded")
        return child_id

    def get(self, operation_id: str):
        child = self._children[operation_id]
        evidence = [
            {
                "node_id": node_id,
                "verified": True,
                "verified_digests": [MODEL_DIGEST],
                "verified_image_digest": assignment["oci_image_digest"],
                "verified_registry_manifest_digest": REGISTRY_DIGEST,
                "imported_image_digest": assignment["oci_image_digest"],
                "verified_oci_layout_sha256": assignment["oci_archive_sha256"],
            }
            for node_id, assignment in self.assignments.items()
        ]
        return SimpleNamespace(
            state=child.state,
            result={
                "progress": {"phase": "transfer", "completed_bytes": 0},
                "members": [],
                "evidence": evidence,
            },
        )


def _seed() -> tuple[sessionmaker[Session], str, str, str]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    recipe_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text()
    )
    model_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    recipe_document = RecipeDefinition.model_validate(recipe_document).model_dump(mode="json")
    model_document = ModelDefinition.model_validate(model_document).model_dump(mode="json")
    recipe_digest = content_sha256(RecipeDefinition.model_validate(recipe_document))
    model_digest = content_sha256(ModelDefinition.model_validate(model_document))
    assert model_digest == "e1e9de42be3e14bdb392cba65c9bbcbec6a4ea5b448597e0c32d187c5840029c"
    revision_id = str(uuid.uuid4())
    model_root_id = str(uuid.uuid4())
    recipe_root_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add_all(
            [
                CatalogDocument(
                    id=recipe_root_id,
                    kind="recipe",
                    publisher="vonk-forge",
                    slug="synthetic-tiny-image",
                    title="Synthetic Tiny image",
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CatalogDocument(
                    id=model_root_id,
                    kind="model",
                    publisher="vonk-forge",
                    slug="synthetic-tiny-fp16",
                    title="Synthetic Tiny",
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CatalogDocumentRevision(
                    id=revision_id,
                    document_id=recipe_root_id,
                    kind="recipe",
                    publisher="vonk-forge",
                    slug="synthetic-tiny-image",
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=recipe_document,
                    content_digest=recipe_digest,
                    artifact_key="b" * 64,
                    execution_key="a" * 64,
                    projected={},
                    created_by="test",
                    created_at=NOW,
                ),
                CatalogDocumentRevision(
                    id=str(uuid.uuid4()),
                    document_id=model_root_id,
                    kind="model",
                    publisher="vonk-forge",
                    slug="synthetic-tiny-fp16",
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=model_document,
                    content_digest=model_digest,
                    projected={},
                    created_by="test",
                    created_at=NOW,
                ),
                AgentNode(
                    node_id=NODE_ID,
                    state="active",
                    architecture="linux-arm64",
                    capabilities=["runtime.vonk.v1", "recipe.operations.v1"],
                ),
                AgentCertificate(
                    serial="serial-direct",
                    node_id=NODE_ID,
                    fingerprint="fingerprint-direct",
                    not_before=NOW,
                    not_after=NOW.replace(year=2027),
                ),
                AgentPresence(
                    node_id=NODE_ID,
                    certificate_serial="serial-direct",
                    certificate_fingerprint="fingerprint-direct",
                    management_address="10.0.0.42",
                    observed_at=NOW,
                ),
            ]
        )
    InventoryRepository(sessions, clock=lambda: NOW).record(
        InventorySnapshotInput(
            node_id=NODE_ID,
            observed_at=NOW,
            disk_total_bytes=10_000_000_000,
            disk_free_bytes=10_000_000_000,
            host_memory_total_bytes=10_000_000_000,
            host_memory_free_bytes=10_000_000_000,
            gpu_memory_total_bytes=10_000_000_000,
            gpu_memory_free_bytes=10_000_000_000,
            gpu_count=1,
            artifact_store_read_only=False,
            capabilities=("runtime.vonk.v1", "recipe.operations.v1"),
        )
    )
    mapping_service = ClusterMappingService(sessions)
    mapping_plan = mapping_service.preview(revision_id, (NODE_ID,), {}, "test")
    mapping_id = mapping_service.materialize(mapping_plan, actor="test", now=NOW)
    return sessions, revision_id, recipe_digest, mapping_id


def _make_service(tmp_path: Path, *, persist_db: bool = True, tamper_db: str | None = None):
    sessions, revision_id, recipe_digest, mapping_id = _seed()
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    events: list[str] = []

    def prepare_and_persist(document, runtime_spec, build):
        assert build is None
        receipt = prepare_runtime_image(
            RecipeDefinition.model_validate(document),
            runtime=runtime_spec["runtime"],
            storage=storage,
            transport=_Transport(),
            now=NOW,
        )
        if persist_db:
            with sessions.begin() as session:
                persist_runtime_image_receipt(
                    session,
                    recipe_revision_id=revision_id,
                    original_content_digest=recipe_digest,
                    effective_execution_key=runtime_spec["identity"]["execution_sha256"],
                    receipt=receipt,
                    verified_at=NOW,
                )
                events.append("runtime-image-db-committed")
        else:
            events.append("runtime-image-filesystem-only")
        return receipt

    model_cache = _ModelCache(recipe_digest)

    def resolve_image(document, image_digest, runtime_spec):
        del document
        assert image_digest == REGISTRY_DIGEST
        key = runtime_spec["identity"]["execution_sha256"]
        with sessions() as session:
            row = session.scalar(
                select(RuntimeImageReceipt).where(
                    RuntimeImageReceipt.recipe_revision_id == revision_id,
                    RuntimeImageReceipt.effective_execution_key == key,
                    RuntimeImageReceipt.source == "published",
                    RuntimeImageReceipt.state == "verified",
                )
            )
            if row is None:
                raise RuntimeError("missing durable direct receipt")
            return {
                "image_digest": row.platform_manifest_digest,
                "oci_layout_sha256": row.oci_archive_sha256,
                "image_bytes": row.image_bytes,
                "source": row.source,
                "build_id": row.build_id,
                "registry_manifest_digest": row.registry_manifest_digest,
                "platform_manifest_digest": row.platform_manifest_digest,
                "local_image_config_id": row.local_image_config_id,
                "architecture": row.architecture,
                "runtime_interface": row.runtime_interface,
                "runtime_interface_label": row.runtime_interface_label,
                "local_image_reference": None,
                "distribution_object": {
                    "name": "image.oci.tar",
                    "sha256": row.oci_archive_sha256,
                    "bytes": row.image_bytes,
                    "kind": "oci-archive",
                },
            }

    compiler = ControllerExecutionPlanService(
        model_cache,
        runtime_image_resolver=resolve_image,
    )

    admission = InstallAdmissionService(
        sessions,
        sizes=SimpleNamespace(),
        disk_floor_bytes=10,
        compiled_plan_provider=compiler.compile_installation,
    )
    queue = _Queue()
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=admission,
        run_admission=RunAdmissionService(sessions, inventory_max_age=300, memory_floor_bytes=50),
        agent_jobs=queue,
        clock=lambda: NOW,
        mappings=ClusterMappingService(sessions),
    )
    source = SimpleNamespace(
        objects_for_set=lambda digest: (
            DistributionObject("model.safetensors", MODEL_DIGEST, 1024, "model"),
        )
    )
    executor = _TargetExecutor(
        sessions,
        None,
        DistributionService(source, sessions=sessions),
        clock=lambda: NOW,
        model_cache=model_cache,
        runtime_image_preparer=prepare_and_persist,
        events=events,
        tamper_db=tamper_db,
    )
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        mappings=ClusterMappingService(sessions),
        artifacts=_Inspector(),
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    return service, sessions, revision_id, recipe_digest, mapping_id, executor, events


def test_direct_published_image_real_run_switch_path_persists_receipt_before_compile_and_uses_platform_identity(
    tmp_path: Path,
) -> None:
    service, sessions, revision_id, recipe_digest, mapping_id, executor, events = _make_service(tmp_path)
    del mapping_id
    request = RunSwitchPreviewRequest(
        model_version_sha256="e1e9de42be3e14bdb392cba65c9bbcbec6a4ea5b448597e0c32d187c5840029c",
        recipe_revision_id=revision_id,
        spark_group=SparkGroup(
            nodes=[SparkGroupNode(node_id=NODE_ID, rank=0, role="entrypoint", endpoint_owner=True)]
        ),
        alias="synthetic-tiny",
    )
    preview = service.preview(request, actor="test")
    assert preview.allowed is True
    assert preview.recipe_build_id is None
    assert all(phase.subphase != "container-build" for phase in preview.phases)
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(mode="json"), request_key=str(uuid.uuid4())),
        actor="test",
    )
    for _ in range(20):
        service._advance(operation.operation_id)
        with sessions() as session:
            row = session.get(Job, operation.operation_id)
            assert row is not None
            if row.state == "succeeded":
                break
            progress = row.result or {}
            phase = progress.get("phase")
            if phase == "prepare" and progress.get("subphase") == "runtime-install":
                break
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        assert row.state == "running", (row.status_reason, events, row.result)
        progress = row.result or {}
        results = progress.get("phase_results", [])
        assert events.index("runtime-image-db-committed") < events.index("target-copy")
        assert any(item.get("compiled_plan_persisted") is True for item in results if isinstance(item, dict))
        runtime_result = next(item for item in results if isinstance(item, dict) and "runtime_image" in item)
        runtime = runtime_result["runtime_image"]
        assert runtime["registry_manifest_digest"] == REGISTRY_DIGEST
        assert runtime["platform_manifest_digest"] == PLATFORM_DIGEST
        assert runtime["local_image_config_id"] == CONFIG_DIGEST
        assert runtime["oci_archive_sha256"] == ARCHIVE_DIGEST
        assert runtime_result["image_digest"] == PLATFORM_DIGEST
        installation = session.scalar(select(RecipeInstallation))
        assert installation is not None
        assert installation.recipe_build_id is None
        compiled = installation.plan["compiled_execution_plans"][NODE_ID]
        assert compiled["runtime_image"]["image_digest"] == PLATFORM_DIGEST
        assert compiled["runtime_image"]["registry_manifest_digest"] == REGISTRY_DIGEST
        assert compiled["runtime_image"]["platform_manifest_digest"] == PLATFORM_DIGEST
        assert compiled["runtime_image"]["local_image_config_id"] == CONFIG_DIGEST
        assert compiled["runtime_image"]["distribution_object"]["sha256"] == ARCHIVE_DIGEST
        assert session.query(RecipeBuild).count() == 0
        assert session.query(RuntimeImageReceipt).count() == 1
        persisted = session.scalar(select(RuntimeImageReceipt))
        assert persisted is not None
        assert persisted.original_content_digest == recipe_digest
        assert persisted.registry_manifest_digest == REGISTRY_DIGEST
        assert persisted.platform_manifest_digest == PLATFORM_DIGEST

    # Advance once more so the actual lifecycle queues the Spark install child
    # after the persisted Controller spec and target verification.
    service._advance(operation.operation_id)
    installation_id = None
    compiled_spec = None
    with sessions() as session:
        operation_row = session.get(Job, operation.operation_id)
        assert operation_row is not None
        progress = operation_row.result or {}
        child_id = progress.get("child_operation_id")
        assert isinstance(child_id, str)
        child = session.get(Job, child_id)
        assert child is not None and child.kind == "recipe.install"
        installation = session.scalar(select(RecipeInstallation))
        assert installation is not None and installation.state == "installing"
        compiled = installation.plan["compiled_execution_plans"][NODE_ID]
        assert validate_compiled_launch_payload(compiled) == compiled
        installation_id = installation.id
        compiled_spec = compiled

    assert installation_id is not None
    response = _read_spec_endpoint(sessions, tmp_path, installation_id)
    assert response.status_code == 200
    assert response.json() == compiled_spec
    assert response.json()["runtime_image"]["registry_manifest_digest"] == REGISTRY_DIGEST
    assert response.json()["runtime_image"]["platform_manifest_digest"] == PLATFORM_DIGEST

    assert executor.assignments[NODE_ID]["oci_image_digest"] == PLATFORM_DIGEST
    assert executor.assignments[NODE_ID]["oci_archive_sha256"] == ARCHIVE_DIGEST


def _direct_request(revision_id: str) -> RunSwitchPreviewRequest:
    return RunSwitchPreviewRequest(
        model_version_sha256="e1e9de42be3e14bdb392cba65c9bbcbec6a4ea5b448597e0c32d187c5840029c",
        recipe_revision_id=revision_id,
        spark_group=SparkGroup(
            nodes=[SparkGroupNode(node_id=NODE_ID, rank=0, role="entrypoint", endpoint_owner=True)]
        ),
        alias="synthetic-tiny",
    )


class _NoopJobs:
    def list(self):
        return []

    def get(self, _job_id):
        raise KeyError(_job_id)

    def enqueue(self, *_args, **_kwargs):
        raise AssertionError("the installation spec route must not enqueue work")


def _read_spec_endpoint(sessions: sessionmaker[Session], tmp_path: Path, installation_id: str):
    presence = AgentPresenceService(
        sessions,
        ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
    )
    operations = AgentJobService(sessions, clock=lambda: NOW)
    operations.set_contact_consumer(presence.observe_in_session)
    root = tmp_path / "agent-api"
    services = AgentApiServices(
        enrollment=None,
        operations=operations,
        sessions=sessions,
        clock=lambda: NOW,
        presence=presence,
        artifact_root=root / "artifacts",
        source_bundles=SourceBundleStore(root / "source-bundles"),
    )
    services.artifact_root.mkdir(parents=True)
    app = create_app(
        jobs=_NoopJobs(),
        tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(),
        fleet=dict,
        now=lambda: int(NOW.timestamp()),
        agent=services,
        trusted_agent_proxy_auth=b"p" * 32,
    )
    headers = {
        "x-vonk-agent-node": NODE_ID,
        "x-vonk-agent-serial": "serial-direct",
        "x-vonk-agent-fingerprint": "fingerprint-direct",
        "x-vonk-agent-verified": "1",
        "x-vonk-agent-proxy-auth": "p" * 32,
        "x-vonk-agent-source": "10.0.0.42",
    }
    with TestClient(app) as client:
        return client.get(
            f"/agent/v1/recipe-installations/{installation_id}/spec",
            headers=headers,
        )


def test_direct_run_switch_rejects_filesystem_only_receipt_before_compile(
    tmp_path: Path,
) -> None:
    service, sessions, revision_id, _recipe_digest, _mapping_id, _executor, _events = _make_service(
        tmp_path,
        persist_db=False,
    )
    request = _direct_request(revision_id)
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(mode="json"), request_key=str(uuid.uuid4())),
        actor="test",
    )
    for _ in range(10):
        service._advance(operation.operation_id)
        with sessions() as session:
            row = session.get(Job, operation.operation_id)
            assert row is not None
            if row.state == "failed":
                break
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and row.state == "failed"
        assert "install-preparation-failed" in (row.status_reason or "")
        assert session.query(RuntimeImageReceipt).count() == 0
        assert session.query(RecipeInstallation).count() == 0
        assert session.query(RecipeBuild).count() == 0


def test_direct_run_switch_rejects_conflicting_db_receipt_during_target_copy(
    tmp_path: Path,
) -> None:
    service, sessions, revision_id, _recipe_digest, _mapping_id, _executor, _events = _make_service(
        tmp_path,
        tamper_db="platform",
    )
    request = _direct_request(revision_id)
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(mode="json"), request_key=str(uuid.uuid4())),
        actor="test",
    )
    for _ in range(10):
        service._advance(operation.operation_id)
        with sessions() as session:
            row = session.get(Job, operation.operation_id)
            assert row is not None
            if row.state == "failed":
                break
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and row.state == "failed"
        assert "receipt authority changed" in (row.status_reason or "")
        assert session.query(RecipeInstallation).count() == 1
        assert session.query(RecipeBuild).count() == 0


def test_direct_run_switch_rejects_conflicting_db_archive_during_target_copy(
    tmp_path: Path,
) -> None:
    service, sessions, revision_id, _recipe_digest, _mapping_id, _executor, _events = _make_service(
        tmp_path,
        tamper_db="archive",
    )
    request = _direct_request(revision_id)
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(mode="json"), request_key=str(uuid.uuid4())),
        actor="test",
    )
    for _ in range(10):
        service._advance(operation.operation_id)
        with sessions() as session:
            row = session.get(Job, operation.operation_id)
            assert row is not None
            if row.state == "failed":
                break
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and row.state == "failed"
        assert "receipt authority changed" in (row.status_reason or "")
        assert session.query(RecipeInstallation).count() == 1
        assert session.query(RecipeBuild).count() == 0
