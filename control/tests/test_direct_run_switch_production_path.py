from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import DistributionObject
from vonk_control.agent_api import AgentApiServices
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import AgentSource, TokenCodec
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.compiled_execution_plan import validate_compiled_launch_payload
from vonk_control.distribution import DistributionService, MemoryVerifiedObjectSource
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
    Job,
    NodeArtifact,
    RecipeBuild,
    RecipeInstallation,
    RuntimeImageAuthorization,
)
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchPhase,
    RunSwitchPlan,
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
    RuntimeImageReceipt,
    persist_runtime_image_receipt,
    prepare_runtime_image,
)
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .canonical_recipe_fixtures import canonical_example
from .preflight_fixtures import record_passing_preflight

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
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    def pull_and_export(
        self,
        reference: str,
        destination: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        progress: Callable[[str, int, int | None], None] | None = None,
    ) -> PulledImageEvidence:
        del reference, progress
        if self.events is not None:
            self.events.append("runtime-image-pulled")
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

    def inspect_archive(self, *_args: object, **_kwargs: object) -> PulledImageEvidence:
        # This double only exercises the published pull path; archive
        # inspection belongs to the controller-build path.
        raise NotImplementedError(
            "published transport double does not inspect archives"
        )


class _ModelSource(MemoryVerifiedObjectSource):
    """Verified source exposing the exact model set the plan selects."""

    def objects_for_set(
        self, artifact_set_sha256: str
    ) -> tuple[DistributionObject, ...]:
        del artifact_set_sha256
        return (
            DistributionObject(
                name="model.safetensors", sha256=MODEL_DIGEST, bytes=1024, kind="model"
            ),
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

    def verified_model_objects_for_set(
        self, digest: str
    ) -> tuple[dict[str, object], ...]:
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
    def inspect(
        self,
        session: Session,
        *,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        retention: str,
        now: datetime,
    ) -> ArtifactInspection:
        del session, model_content_sha256, recipe_revision_id, retention, now
        return ArtifactInspection(
            required_bytes=1024 * len(node_ids),
            reused_bytes=0,
            copied_bytes=1024 * len(node_ids),
            missing_nas_bytes=0,
            missing_spark_bytes=1024 * len(node_ids),
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
            dependency_model_content_sha256=(),
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

        parent = session.get(Job, parent_job_id)
        assert parent is not None
        value = AgentOperation(
            id=operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=operation,
            payload_digest=hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
            payload=dict(payload),
            authority_revision=authority_revision,
            workload_intent_ordinal=parent.payload.get("workload_intent_ordinal"),
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

    def __init__(
        self,
        sessions: sessionmaker[Session],
        operations: AgentJobService,
        distribution: DistributionService,
        *,
        clock: Callable[[], datetime],
        model_cache: object,
        runtime_image_preparer: Callable[..., object] | None,
        events: list[str],
        tamper_db: str | None = None,
    ) -> None:
        super().__init__(
            sessions,
            operations,
            distribution,
            clock=clock,
            model_cache=model_cache,
            runtime_image_preparer=runtime_image_preparer,
        )
        self.events = events
        self.assignments: dict[str, dict[str, object]] = {}
        self._children: dict[str, SimpleNamespace] = {}
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
                row = session.scalar(select(RuntimeImageAuthorization))
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
        workload_intent_ordinal,
    ) -> str:
        del (
            plan,
            phase,
            actor,
            request_key,
            cached,
            target_order,
            target_bytes,
            workload_intent_ordinal,
        )
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


def _seed(
    *, dual: bool = False, engine: Engine | None = None
) -> tuple[sessionmaker[Session], str, str, str]:
    engine = engine or create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    recipe_document = canonical_example("recipe-image.json")
    node_ids = (NODE_ID, "spk_" + "2" * 32) if dual else (NODE_ID,)
    capabilities = ["runtime.vonk.v1", "recipe.operations.v1"]
    if dual:
        capabilities.append("fabric.connected.mbps.200000")
        topology = recipe_document["topology"]
        topology.update(mode="distributed", name="dual", node_count=2)
        topology["parallelism"].update(backend="mp", tensor=2, world_size=2)
        topology["fabric"].update(
            connectivity="connected", minimum_bandwidth_mbps=200000
        )
        worker = deepcopy(topology["roles"][0])
        worker.update(name="worker", endpoint_owner=False)
        topology["roles"].append(worker)
        topology["start_order"] = ["worker", "entrypoint"]
        topology["stop_order"] = ["entrypoint", "worker"]
        recipe_document["models"][0]["files"][0]["roles"].append("worker")
    model_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    recipe_document = RecipeDefinition.model_validate(recipe_document).model_dump(
        mode="json"
    )
    model_document = ModelDefinition.model_validate(model_document).model_dump(
        mode="json"
    )
    recipe_digest = content_sha256(RecipeDefinition.model_validate(recipe_document))
    model_digest = content_sha256(ModelDefinition.model_validate(model_document))
    assert (
        model_digest
        == "e1e9de42be3e14bdb392cba65c9bbcbec6a4ea5b448597e0c32d187c5840029c"
    )
    with sessions.begin() as session:
        entities = CatalogEntityService(session, clock=lambda: NOW)
        model_revision = entities.create_draft(model_document, actor="test")
        entities.resolve(model_revision.id, actor="test")
        recipe_revision = entities.create_draft(recipe_document, actor="test")
        entities.resolve(recipe_revision.id, actor="test")
        revision_id = recipe_revision.id
        for index, node_id in enumerate(node_ids):
            serial = "serial-direct" if index == 0 else f"serial-direct-{index}"
            fingerprint = (
                "fingerprint-direct" if index == 0 else f"fingerprint-direct-{index}"
            )
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    architecture="linux-arm64",
                    capabilities=capabilities,
                )
            )
            session.flush()
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    fingerprint=fingerprint,
                    not_before=NOW,
                    not_after=NOW.replace(year=2027),
                )
            )
            session.flush()
            session.add(
                AgentPresence(
                    node_id=node_id,
                    certificate_serial=serial,
                    certificate_fingerprint=fingerprint,
                    management_address=f"10.0.0.{42 + index}",
                    observed_at=NOW,
                )
            )
    for index, node_id in enumerate(node_ids):
        InventoryRepository(sessions, clock=lambda: NOW).record(
            InventorySnapshotInput(
                node_id=node_id,
                observed_at=NOW,
                disk_total_bytes=10_000_000_000,
                disk_free_bytes=10_000_000_000,
                host_memory_total_bytes=10_000_000_000,
                host_memory_free_bytes=10_000_000_000,
                gpu_memory_total_bytes=10_000_000_000,
                gpu_memory_free_bytes=10_000_000_000,
                gpu_count=1,
                artifact_store_read_only=False,
                capabilities=tuple(capabilities),
                fabric_address=f"192.168.100.{10 + index}" if dual else None,
                fabric_bandwidth_mbps=200000 if dual else None,
                memory_pool="shared",
            )
        )
    mapping_service = ClusterMappingService(sessions)
    mapping_plan = mapping_service.preview(revision_id, node_ids, {}, "test")
    mapping_id = mapping_service.materialize(mapping_plan, actor="test", now=NOW)
    record_passing_preflight(sessions, NOW, floor=10)
    return sessions, revision_id, recipe_digest, mapping_id


def _make_service(
    tmp_path: Path,
    *,
    persist_db: bool = True,
    tamper_db: str | None = None,
    availability_key: str | None = None,
    dual: bool = False,
    engine: Engine | None = None,
):
    sessions, revision_id, recipe_digest, mapping_id = _seed(dual=dual, engine=engine)
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    events: list[str] = []
    if availability_key is not None:
        # The availability operation prepares the image and records the
        # recipe-level identity it admitted, before any launch runs.  The
        # launch then prepares the same archive again and records the compiled
        # identity the Spark agent compares against.
        availability_receipt = prepare_runtime_image(
            RecipeDefinition.model_validate(canonical_example("recipe-image.json")),
            runtime={"architecture": "linux-arm64", "interface": "vonk.runtime.v1"},
            storage=storage,
            transport=_Transport(events),
            now=NOW,
        )
        with sessions.begin() as session:
            persist_runtime_image_receipt(
                session,
                recipe_revision_id=revision_id,
                original_content_digest=recipe_digest,
                effective_execution_key=availability_key,
                receipt=availability_receipt,
                verified_at=NOW,
            )
        events.append("availability-receipt-recorded")

    def prepare_and_persist(
        document,
        runtime_spec,
        build,
        *,
        before_publish: Callable[[RuntimeImageReceipt], object] | None = None,
    ):
        assert build is None
        receipt = prepare_runtime_image(
            RecipeDefinition.model_validate(document),
            runtime=runtime_spec["runtime"],
            storage=storage,
            transport=_Transport(events),
            now=NOW,
            before_publish=before_publish,
        )
        if persist_db:
            with sessions.begin() as session:
                persist_runtime_image_receipt(
                    session,
                    recipe_revision_id=revision_id,
                    original_content_digest=recipe_digest,
                    effective_execution_key=runtime_spec["identity"][
                        "execution_sha256"
                    ],
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
                select(RuntimeImageAuthorization).where(
                    RuntimeImageAuthorization.recipe_revision_id == revision_id,
                    RuntimeImageAuthorization.effective_execution_key == key,
                    RuntimeImageAuthorization.source == "published",
                    RuntimeImageAuthorization.state == "authorized",
                )
            )
            if row is None:
                raise RuntimeError("missing durable direct receipt")
            assert row.oci_archive_sha256 is not None
            return storage.read_receipt(row.oci_archive_sha256)

    compiler = ControllerExecutionPlanService(
        model_cache,
        runtime_image_resolver=resolve_image,
    )

    admission = InstallAdmissionService(
        sessions,
        disk_floor_bytes=10,
        compiled_plan_provider=compiler.compile_installation,
    )
    queue = _Queue()
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=admission,
        run_admission=RunAdmissionService(
            sessions, inventory_max_age=300, memory_floor_bytes=50
        ),
        agent_jobs=queue,
        clock=lambda: NOW,
        mappings=ClusterMappingService(sessions),
    )
    source = _ModelSource()
    # This fixture source owns no Controller image cache, so it declares its
    # published archive exactly as MemoryVerifiedObjectSource intends. The real
    # path reads the receipt in the cache root instead.
    source.register_runtime_image(PLATFORM_DIGEST, ARCHIVE_DIGEST)
    executor = _TargetExecutor(
        sessions,
        AgentJobService(sessions, clock=lambda: NOW),
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
        published_image_receipt=storage.find_published,
        memory_floor_bytes=50,
    )
    return service, sessions, revision_id, recipe_digest, mapping_id, executor, events


def _seed_runtime_image_owner(
    sessions: sessionmaker[Session],
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
    *,
    actor: str = "test",
) -> tuple[str, str, dict[str, object]]:
    """Create one current RunSwitch checkpoint for the isolated callback tests."""

    operation_id = str(uuid.uuid4())
    request_key = str(uuid.uuid4())
    ordinal = 1
    target_ids = [node.node_id for node in plan.spark_group.nodes]
    progress: dict[str, object] = {
        "phase_index": phase.index,
        "item_index": 0,
        "phase": phase.kind,
        "subphase": phase.subphase,
        "completed_phases": [],
        "workload_intent_ordinal": ordinal,
    }
    payload = {
        "schema_version": 2,
        "operation_kind": "recipe.run-switch.v2",
        "action": plan.action,
        "plan_digest": plan.plan_digest,
        "plan": plan.model_dump(mode="json"),
        "workload_intent_ordinal": ordinal,
    }
    with sessions.begin() as session:
        for node_id in target_ids:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal = ordinal
        session.add(
            Job(
                id=operation_id,
                request_id=request_key,
                kind="recipe.run-switch.v2",
                state="running",
                actor=actor,
                authority_revision=plan.recipe_content_sha256 or plan.plan_digest,
                targets=target_ids,
                payload_digest=hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode()
                ).hexdigest(),
                payload=payload,
                result=progress,
                current_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return operation_id, request_key, progress


def test_dual_spark_preparation_authorizes_both_execution_roles_for_one_image(
    tmp_path: Path,
) -> None:
    service, sessions, revision_id, _digest, _mapping, _executor, events = (
        _make_service(tmp_path, dual=True)
    )
    node_ids = [NODE_ID, "spk_" + "2" * 32]
    request = RunSwitchPreviewRequest(
        model_content_sha256="e1e9de42be3e14bdb392cba65c9bbcbec6a4ea5b448597e0c32d187c5840029c",
        recipe_revision_id=revision_id,
        spark_group=SparkGroup(
            nodes=[
                SparkGroupNode(
                    node_id=node_ids[0], rank=0, role="entrypoint", endpoint_owner=True
                ),
                SparkGroupNode(
                    node_id=node_ids[1], rank=1, role="worker", endpoint_owner=False
                ),
            ]
        ),
        alias="synthetic-dual",
    )
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(mode="json"), request_key=str(uuid.uuid4())
        ),
        actor="test",
    )
    for _ in range(20):
        service._advance(operation.operation_id)
        with sessions() as session:
            row = session.get(Job, operation.operation_id)
            assert row is not None
            if (
                row.state == "failed"
                or (row.result or {}).get("subphase") == "runtime-install"
            ):
                break
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        assert row.state == "running", (row.status_reason, events)
        installation = session.scalar(select(RecipeInstallation))
        assert installation is not None
        plans = require_mapping(installation.plan, "installation plan")[
            "compiled_execution_plans"
        ]
        plans = require_mapping(plans, "compiled execution plans")
        assert set(plans) == set(node_ids)
        for plan in plans.values():
            validate_compiled_launch_payload(plan)
        receipts = list(session.scalars(select(RuntimeImageAuthorization)))
        assert len({receipt.effective_execution_key for receipt in receipts}) == 2
        assert {receipt.oci_archive_sha256 for receipt in receipts} == {ARCHIVE_DIGEST}
    assert events.count("runtime-image-pulled") == 1


def test_published_authorization_does_not_make_missing_archive_ready(
    tmp_path: Path,
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, events = _make_service(
        tmp_path, availability_key="a" * 64
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    with sessions() as session:
        authorization = session.scalar(select(RuntimeImageAuthorization))
        assert authorization is not None
        archive_digest = authorization.oci_archive_sha256
        image_bytes = authorization.image_bytes
        approved_identity = (
            authorization.original_content_digest,
            authorization.registry_manifest_digest,
            authorization.platform_manifest_digest,
            authorization.local_image_config_id,
            authorization.oci_archive_sha256,
            authorization.image_bytes,
            authorization.build_id,
        )
    approved_receipt = storage.read_receipt(archive_digest)
    # Keep the SQL authorization and receipt index, but remove the actual
    # archive. A Spark-local copy must not make the Controller cache look ready:
    # compile/install still needs the exact Controller receipt.
    (storage.root / archive_digest).unlink()
    with sessions.begin() as session:
        session.add(
            NodeArtifact(
                node_id=NODE_ID,
                kind="image",
                digest=PLATFORM_DIGEST.removeprefix("sha256:"),
                source="test-spark-cache",
                size_bytes=image_bytes,
                state="verified",
                ref_count=1,
                verified_at=NOW,
                updated_at=NOW,
            )
        )

    request = _direct_request(revision_id)
    plan = service.preview(request, actor="test")

    assert plan.runtime_storage.image_digest == PLATFORM_DIGEST
    assert plan.runtime_storage.oci_layout_sha256 == ARCHIVE_DIGEST
    assert plan.runtime_storage.image_bytes == image_bytes
    assert plan.runtime_storage.missing_nas_bytes == image_bytes
    assert plan.runtime_storage.nas_coverage == "partial"
    assert plan.preparation is not None
    assert plan.preparation.runtime_image.controller.state == "missing"
    assert plan.preparation.runtime_image.controller.verified_bytes == 0
    assert plan.preparation.runtime_image.controller.missing_bytes == image_bytes
    assert plan.preparation.ready is False
    assert any(
        phase.kind == "prepare" and phase.subphase == "runtime-image"
        for phase in plan.phases
    )

    # The durable worker preparer reuses the same pinned registry digest and
    # writes its complete managed receipt and SQL authorization before the
    # restored bytes can be admitted for compilation.
    phase = next(
        phase
        for phase in plan.phases
        if phase.kind == "prepare" and phase.subphase == "runtime-image"
    )
    operation_id, request_key, progress = _seed_runtime_image_owner(
        sessions, plan, phase
    )
    del operation_id
    recovered = executor._prepare_runtime_image(
        plan,
        phase,
        item_index=0,
        actor="test",
        request_key=request_key,
        progress=progress,
    )
    assert recovered is not None
    assert events.count("runtime-image-pulled") == 2
    assert "runtime-image-db-committed" in events
    recovered_receipt = require_mapping(
        recovered["runtime_image"], "recovered runtime image receipt"
    )
    assert recovered_receipt == approved_receipt.to_mapping()
    actual_receipt = storage.find_published(
        REGISTRY_DIGEST,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )
    assert actual_receipt is not None
    assert actual_receipt.to_mapping() == approved_receipt.to_mapping()
    with sessions() as session:
        authorizations = list(
            session.scalars(
                select(RuntimeImageAuthorization).where(
                    RuntimeImageAuthorization.recipe_revision_id == revision_id,
                    RuntimeImageAuthorization.registry_manifest_digest
                    == REGISTRY_DIGEST,
                )
            )
        )
        assert authorizations
        assert {
            (
                row.original_content_digest,
                row.registry_manifest_digest,
                row.platform_manifest_digest,
                row.local_image_config_id,
                row.oci_archive_sha256,
                row.image_bytes,
                row.build_id,
            )
            for row in authorizations
        } == {approved_identity}

    restored = service.preview(request, actor="test")
    assert restored.allowed is True
    assert restored.runtime_storage.image_digest == plan.runtime_storage.image_digest
    assert (
        restored.runtime_storage.oci_layout_sha256
        == plan.runtime_storage.oci_layout_sha256
    )
    assert restored.runtime_storage.image_bytes == plan.runtime_storage.image_bytes
    assert restored.runtime_storage.missing_nas_bytes == 0
    assert restored.runtime_storage.nas_coverage == "complete"
    assert restored.preparation is not None
    assert restored.preparation.runtime_image.controller.state == "ready"
    # This fixture deliberately does not back the model bytes with managed
    # storage, so image recovery must not overstate the whole rollout.
    assert restored.preparation.ready is False
    assert all(phase.subphase != "runtime-image" for phase in restored.phases)


def test_direct_published_image_real_run_switch_path_persists_receipt_before_compile_and_uses_platform_identity(
    tmp_path: Path,
) -> None:
    service, sessions, revision_id, recipe_digest, mapping_id, executor, events = (
        _make_service(tmp_path)
    )
    del mapping_id
    request = RunSwitchPreviewRequest(
        model_content_sha256="e1e9de42be3e14bdb392cba65c9bbcbec6a4ea5b448597e0c32d187c5840029c",
        recipe_revision_id=revision_id,
        spark_group=SparkGroup(
            nodes=[
                SparkGroupNode(
                    node_id=NODE_ID, rank=0, role="entrypoint", endpoint_owner=True
                )
            ]
        ),
        alias="synthetic-tiny",
    )
    preview = service.preview(request, actor="test")
    assert preview.allowed is True
    assert preview.recipe_build_id is None
    assert all(phase.subphase != "container-build" for phase in preview.phases)
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(mode="json"), request_key=str(uuid.uuid4())
        ),
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
        results = require_sequence(progress.get("phase_results", []), "phase results")
        assert events.index("runtime-image-db-committed") < events.index("target-copy")
        assert any(
            item.get("compiled_plan_persisted") is True
            for item in results
            if isinstance(item, dict)
        )
        runtime_result = require_mapping(
            next(
                item
                for item in results
                if isinstance(item, dict) and "runtime_image" in item
            ),
            "runtime result",
        )
        runtime = require_mapping(runtime_result["runtime_image"], "runtime image")
        assert runtime["registry_manifest_digest"] == REGISTRY_DIGEST
        assert runtime["platform_manifest_digest"] == PLATFORM_DIGEST
        assert runtime["local_image_config_id"] == CONFIG_DIGEST
        assert runtime["oci_archive_sha256"] == ARCHIVE_DIGEST
        assert runtime_result["image_digest"] == PLATFORM_DIGEST
        installation = session.scalar(select(RecipeInstallation))
        assert installation is not None
        assert installation.recipe_build_id is None
        installation_plan = require_mapping(installation.plan, "installation plan")
        compiled_plans = require_mapping(
            installation_plan["compiled_execution_plans"], "compiled execution plans"
        )
        compiled = require_mapping(compiled_plans[NODE_ID], "compiled plan")
        compiled_runtime = require_mapping(
            compiled["runtime_image"], "compiled runtime image"
        )
        assert compiled_runtime["image_digest"] == PLATFORM_DIGEST
        assert compiled_runtime["registry_manifest_digest"] == REGISTRY_DIGEST
        assert compiled_runtime["platform_manifest_digest"] == PLATFORM_DIGEST
        assert compiled_runtime["local_image_config_id"] == CONFIG_DIGEST
        distribution_object = require_mapping(
            compiled_runtime["distribution_object"], "compiled distribution object"
        )
        assert distribution_object["sha256"] == ARCHIVE_DIGEST
        assert session.query(RecipeBuild).count() == 0
        assert session.query(RuntimeImageAuthorization).count() == 1
        persisted = session.scalar(select(RuntimeImageAuthorization))
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
        installation_plan = require_mapping(installation.plan, "installation plan")
        compiled_plans = require_mapping(
            installation_plan["compiled_execution_plans"], "compiled execution plans"
        )
        compiled = require_mapping(compiled_plans[NODE_ID], "compiled plan")
        assert validate_compiled_launch_payload(compiled) == compiled
        installation_id = installation.id
        compiled_spec = compiled

    assert installation_id is not None
    response = _read_spec_endpoint(sessions, tmp_path, installation_id)
    assert response.status_code == 200
    assert response.json() == compiled_spec
    assert (
        response.json()["runtime_image"]["registry_manifest_digest"] == REGISTRY_DIGEST
    )
    assert (
        response.json()["runtime_image"]["platform_manifest_digest"] == PLATFORM_DIGEST
    )

    assert executor.assignments[NODE_ID]["oci_image_digest"] == PLATFORM_DIGEST
    assert executor.assignments[NODE_ID]["oci_archive_sha256"] == ARCHIVE_DIGEST


def _direct_request(revision_id: str) -> RunSwitchPreviewRequest:
    return RunSwitchPreviewRequest(
        model_content_sha256="e1e9de42be3e14bdb392cba65c9bbcbec6a4ea5b448597e0c32d187c5840029c",
        recipe_revision_id=revision_id,
        spark_group=SparkGroup(
            nodes=[
                SparkGroupNode(
                    node_id=NODE_ID, rank=0, role="entrypoint", endpoint_owner=True
                )
            ]
        ),
        alias="synthetic-tiny",
    )


class _NoopJobs:
    def list(self, *, limit: int = 100):
        return []

    def get(self, job_id: str):
        raise KeyError(job_id)

    def enqueue(self, *_args, **_kwargs):
        raise AssertionError("the installation spec route must not enqueue work")

    def list_page(self, **_kwargs):
        return [], None, 0


def _read_spec_endpoint(
    sessions: sessionmaker[Session], tmp_path: Path, installation_id: str
):
    presence = AgentPresenceService(
        sessions,
        ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: NOW,
    )
    operations = AgentJobService(sessions, clock=lambda: NOW)

    def observe_contact(session: Session, source: AgentSource) -> None:
        presence.observe_in_session(session, source)

    operations.set_contact_consumer(observe_contact)
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
            f"/agent/recipe-installations/{installation_id}/spec",
            headers=headers,
        )


def test_direct_run_switch_accepts_the_receipt_recorded_by_availability(
    tmp_path: Path,
) -> None:
    """A launch must run on the archive the availability operation prepared.

    The availability operation records the recipe-level identity it admitted;
    the launch then records the compiled identity the Spark agent compares the
    plan against.  Both describe one verified archive, so the launch must
    proceed and its own row must carry the compiled identity.  Refusing the
    second identity left every prepared recipe unusable at apply time, which is
    what stalled the Spark candidate lane.
    """

    availability_key = "a" * 64
    service, sessions, revision_id, _recipe_digest, _mapping_id, _executor, events = (
        _make_service(
            tmp_path,
            availability_key=availability_key,
        )
    )
    request = _direct_request(revision_id)
    preview = service.preview(request, actor="test")
    assert preview.allowed is True
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(mode="json"), request_key=str(uuid.uuid4())
        ),
        actor="test",
    )
    for _ in range(20):
        service._advance(operation.operation_id)
        with sessions() as session:
            row = session.get(Job, operation.operation_id)
            assert row is not None
            if row.state in {"succeeded", "failed"}:
                break
            progress = row.result or {}
            if (
                progress.get("phase") == "prepare"
                and progress.get("subphase") == "runtime-install"
            ):
                break
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        assert row.state == "running", (row.status_reason, events, row.result)
        installation = session.scalar(select(RecipeInstallation))
        assert installation is not None
        installation_plan = require_mapping(installation.plan, "installation plan")
        compiled_plans = require_mapping(
            installation_plan["compiled_execution_plans"], "compiled execution plans"
        )
        compiled = require_mapping(compiled_plans[NODE_ID], "compiled plan")
        compiled_identity = require_mapping(compiled["identity"], "compiled identity")
        launch_identity = compiled_identity["execution_sha256"]
        receipts = list(session.scalars(select(RuntimeImageAuthorization)))
        assert {item.effective_execution_key for item in receipts} == {
            availability_key,
            launch_identity,
        }
        launch_row = next(
            item
            for item in receipts
            if item.effective_execution_key != availability_key
        )
        # This is the equality the Spark agent authorizes the install with.
        assert launch_row.effective_execution_key == launch_identity
        assert launch_row.oci_archive_sha256 is not None


def test_direct_run_switch_rejects_filesystem_only_receipt_before_compile(
    tmp_path: Path,
) -> None:
    service, sessions, revision_id, _recipe_digest, _mapping_id, _executor, _events = (
        _make_service(
            tmp_path,
            persist_db=False,
        )
    )
    request = _direct_request(revision_id)
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(mode="json"), request_key=str(uuid.uuid4())
        ),
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
        assert session.query(RuntimeImageAuthorization).count() == 0
        assert session.query(RecipeInstallation).count() == 0
        assert session.query(RecipeBuild).count() == 0


def test_direct_run_switch_rejects_conflicting_db_receipt_during_target_copy(
    tmp_path: Path,
) -> None:
    service, sessions, revision_id, _recipe_digest, _mapping_id, _executor, _events = (
        _make_service(
            tmp_path,
            tamper_db="platform",
        )
    )
    request = _direct_request(revision_id)
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(mode="json"), request_key=str(uuid.uuid4())
        ),
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
    service, sessions, revision_id, _recipe_digest, _mapping_id, _executor, _events = (
        _make_service(
            tmp_path,
            tamper_db="archive",
        )
    )
    request = _direct_request(revision_id)
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(mode="json"), request_key=str(uuid.uuid4())
        ),
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
