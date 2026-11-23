"""Run/Switch operations: restart planner."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx2
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    RunState,
    RunSwitchCode,
)
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.telemetry import NodeInventorySnapshot
from vonk_control.operation_api.contracts import OperationQuery
from vonk_control.operations.item_contract import operation_item
from vonk_control.preparation_contract import RuntimeImageIdentity
from vonk_control.recipe_operations.errors import RecipeOperationConflict
from vonk_control.run_switch_contract.evidence import RunSwitchPhase, RunSwitchReason
from vonk_control.run_switch_contract.operations import (
    RunSwitchOperationResult,
)
from vonk_control.run_switch_contract.phase_results import (
    RunSwitchFinalVerifyResult,
)
from vonk_control.run_switch_contract.plans import RunSwitchAssessment, RunSwitchPlan
from vonk_control.run_switch_contract.requests import (
    RunSwitchApplyRequest,
    RunSwitchProfileStopScope,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations.errors import (
    RunSwitchIssuedWorkloadPending,
)
from vonk_control.run_switch_operations.executor import RecipeLifecyclePhaseExecutor
from vonk_control.run_switch_operations.identity_helpers import _node_missing_bytes
from vonk_control.run_switch_operations.image_receipts import (
    _planned_transfer_bytes,
)
from vonk_control.run_switch_operations.service import RunSwitchOperationService
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .run_switch_operations_support import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    SynchronousPhaseExecutor,
    _ObservingLifecycle,
    _parked_start_switch,
    _PlannerWorld,
    _request,
    _result,
    _service,
)
from .runtime_identity_support import (
    set_inventory_identity,
)
from .runtime_image_fixtures import install_image_of_replaced_source
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
    started_recipe,
)


def test_parked_switch_observes_recovered_child_after_controller_restart(
    tmp_path: Path,
) -> None:
    """A parked parent must not miss its child's later exact success."""
    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]
    with service._sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        row.state = "observing"
    restarted = RunSwitchOperationService(
        service._sessions,
        lifecycle=service._lifecycle,
        clock=lambda: now[0],
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    assert isinstance(service._lifecycle, _ObservingLifecycle)
    service._lifecycle._healthy = True
    for _ in range(4):
        restarted.tick()
    recovered = restarted.get(operation.operation_id)
    assert recovered.state == "succeeded", recovered.status_reason
    assert _result(recovered).completed_phases.count("start") == 1


@pytest.mark.parametrize("ending", ["recovered", "cancelled", "superseded", "revoked"])
def test_temporary_phase_failure_preserves_exact_intent_across_restart(
    tmp_path: Path, ending: str
) -> None:
    class InterruptedTransfer(RecordingArtifactExecutor):
        def __init__(self):
            super().__init__()
            self.unavailable = True
            self.identities = []

        def execute(self, plan, phase, **kwargs):
            if phase.kind == "transfer":
                self.identities.append((plan.plan_digest, kwargs["request_key"]))
                if self.unavailable:
                    raise httpx2.ConnectError("NAS temporarily disconnected")
            return super().execute(plan, phase, **kwargs)

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    executor = InterruptedTransfer()
    now = [NOW]

    def restarted():
        result = _service(
            sessions,
            now[0],
            lifecycle,
            executor,
            artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        )
        result._clock = lambda: now[0]
        return result

    service = restarted()
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        original = dict(row.payload)
    for _ in range(4):
        assert service.tick()
        waiting = service.get(operation.operation_id)
        assert waiting.state == "running", waiting.status_reason
        assert waiting.result is not None and waiting.next_attempt_at is not None
        due = waiting.next_attempt_at
        assert now[0] < due <= now[0] + timedelta(seconds=60)
        service = restarted()
        assert service.tick() is False
        now[0] = due
    if ending == "cancelled":
        service.cancel(
            operation.operation_id,
            actor="admin",
            request_key=str(uuid.uuid4()),
            reason="Stop recovery",
        )
    elif ending in {"superseded", "revoked"}:
        with sessions.begin() as session:
            node = session.get(AgentNode, nodes[0])
            assert node is not None
            if ending == "superseded":
                node.workload_intent_ordinal += 1
            else:
                node.revoked_at = now[0]
    executor.unavailable = False
    service = restarted()
    service.tick()
    current = service.get(operation.operation_id)
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and row.payload == original
    if ending == "recovered":
        assert current.state == "running"
        assert current.result is not None
        assert "transfer" in current.result.completed_phases
        assert len(executor.identities) == 5
    else:
        assert current.state == ("failed" if ending == "revoked" else "cancelled")
        assert len(executor.identities) == 4
    assert len(set(executor.identities)) == 1


@pytest.mark.parametrize("kind", ["artifact-job-cancellation", "recipe.stop"])
def test_late_dependency_receipt_resumes_original_checkpoint_after_deadline(
    tmp_path: Path, kind: str
) -> None:
    class PendingDependency(RecordingArtifactExecutor):
        pending = True
        calls_while_pending = 0

        def execute(self, plan, phase, **kwargs):
            if self.pending:
                self.calls_while_pending += 1
                raise RunSwitchIssuedWorkloadPending(
                    kind=kind,
                    owner_id="exact-old-owner",
                    job_id=dependency,
                    observe_due_at=NOW,
                    observation_deadline=NOW + timedelta(seconds=120),
                )
            return super().execute(plan, phase, **kwargs)

    dependency = str(uuid.uuid4())
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    executor = PendingDependency()
    now = [NOW + timedelta(seconds=121)]
    service = _service(
        sessions,
        now[0],
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    assert service.tick()
    held = service.get(operation.operation_id)
    assert held.state == "running", held.status_reason
    assert held.result is not None
    assert held.next_attempt_at is not None
    assert held.next_attempt_at == now[0] + timedelta(seconds=60)
    assert held.status_reason is not None and dependency in held.status_reason
    assert service.tick() is False
    executor.pending = False
    now[0] = held.next_attempt_at
    restarted = _service(
        sessions,
        now[0],
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    assert restarted.tick()
    recovered = restarted.get(operation.operation_id)
    assert recovered.state == "running"
    assert (
        recovered.result is not None and "transfer" in recovered.result.completed_phases
    )
    assert executor.calls_while_pending == 1


def test_shared_admission_contention_preserves_operation_for_retry(
    tmp_path: Path,
) -> None:
    """A busy child enqueue must retain intent instead of escaping the worker."""
    from vonk_control.admission_locking import AdmissionLockBusy

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )

    class BusyExecutor(RecordingArtifactExecutor):
        def execute(self, *args, **kwargs):
            raise AdmissionLockBusy("admission lock node is busy")

    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=BusyExecutor(),
    )
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    for _ in range(12):
        service.tick()
        with sessions() as session:
            job = session.get(Job, operation.operation_id)
            assert job is not None
            if job.status_reason and "capacity writer" in job.status_reason:
                assert job.state == "running"
                break
    else:
        pytest.fail("shared admission contention did not schedule a durable retry")


@pytest.mark.usefixtures("damaged_json_rows")
def test_stored_run_switch_with_retired_fields_stays_readable_and_new_work_proceeds(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    request = _request(sessions, nodes[0])
    old = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    with sessions.begin() as session:
        job = session.get(Job, old.operation_id)
        assert job is not None and isinstance(job.payload, dict)
        payload = dict(job.payload)
        plan = dict(cast("dict[str, object]", payload["plan"]))
        # A field a newer Controller retired must not make history unreadable.
        plan["retired_plan_field"] = {"any": "shape"}
        payload["plan"] = plan
        job.payload = payload

    item = operation_item(
        service.activity_provider()
        .list_operations(
            OperationQuery(after=None, limit=10, state=None, node_id=nodes[0])
        )
        .items[0]
    ).model_dump(mode="json")

    assert item["id"] == old.operation_id
    assert item["kind"] != "run-switch-unreadable"
    assert service.get(old.operation_id).operation_id == old.operation_id


SPARK_A = "spk_" + "a" * 32


SPARK_B = "spk_" + "b" * 32


def _uneven_dual_preparation(tmp_path: Path, *, image_by_node, model_by_node):
    """Prepare a two-Spark group whose presence is uneven and byte counts odd."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    group = SparkGroup(
        nodes=[
            SparkGroupNode(
                node_id=node_id,
                rank=rank,
                role="entrypoint" if rank == 0 else "worker",
                endpoint_owner=rank == 0,
            )
            for rank, node_id in enumerate((SPARK_A, SPARK_B))
        ]
    )
    image_bytes = plan.runtime_storage.image_bytes
    assert image_bytes is not None
    runtime_storage = plan.runtime_storage.model_copy(
        update={
            "image_bytes": 1025,
            "missing_image_distribution_bytes": sum(image_by_node.values()),
            "missing_image_distribution_bytes_by_node": image_by_node,
        }
    )
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, request.recipe_revision_id)
        inspection = CompleteArtifactInspector().inspect(
            session,
            model_content_sha256=request.model_content_sha256,
            recipe_revision_id=request.recipe_revision_id,
            node_ids=(SPARK_A, SPARK_B),
            now=NOW,
        )
    inspection = replace(
        inspection,
        missing_spark_bytes=sum(model_by_node.values()),
        missing_spark_bytes_by_node=model_by_node,
    )
    preparation = RunSwitchOperationService._preparation(
        revision=revision,
        group=group,
        inspection=inspection,
        build=None,
        build_candidate=None,
        runtime_storage=runtime_storage,
        now=NOW,
        reasons=(),
    )
    return plan, group, runtime_storage, inspection, preparation


def test_image_present_on_one_spark_with_odd_bytes_is_prepared_per_node(
    tmp_path: Path,
) -> None:
    """Regression: total / N raised run-switch.per-target-byte-evidence-invalid."""

    *_, preparation = _uneven_dual_preparation(
        tmp_path,
        image_by_node={SPARK_A: 0, SPARK_B: 1025},
        model_by_node={SPARK_A: 0, SPARK_B: 0},
    )
    assert preparation is not None
    states = {t.node_id: t for t in preparation.runtime_image.targets}
    assert states[SPARK_A].state == "ready"
    assert (states[SPARK_A].present_bytes, states[SPARK_A].missing_bytes) == (
        1025,
        0,
    )
    assert states[SPARK_B].state == "unknown"
    assert (states[SPARK_B].present_bytes, states[SPARK_B].missing_bytes) == (
        0,
        1025,
    )
    assert not preparation.ready


def test_model_present_on_one_spark_is_prepared_per_node(tmp_path: Path) -> None:
    *_, preparation = _uneven_dual_preparation(
        tmp_path,
        image_by_node={SPARK_A: 0, SPARK_B: 0},
        model_by_node={SPARK_A: 1024, SPARK_B: 0},
    )
    assert preparation is not None
    states = {t.node_id: t for t in preparation.model.targets}
    assert states[SPARK_A].state == "unknown"
    assert states[SPARK_A].missing_bytes == 1024
    assert states[SPARK_B].state == "ready"
    assert states[SPARK_B].present_bytes == 1024


def test_missing_per_node_evidence_means_missing_here_never_an_error(
    tmp_path: Path,
) -> None:
    plan, group, runtime_storage, inspection, _ = _uneven_dual_preparation(
        tmp_path,
        image_by_node={SPARK_A: 0, SPARK_B: 1025},
        model_by_node={SPARK_A: 0, SPARK_B: 0},
    )
    # A plan or inspection persisted before node-keyed evidence existed.
    legacy_storage = runtime_storage.model_copy(
        update={"missing_image_distribution_bytes_by_node": None}
    )
    preparation = RunSwitchOperationService._preparation(
        revision=None,
        group=group,
        inspection=inspection,
        build=None,
        build_candidate=None,
        runtime_storage=legacy_storage,
        now=NOW,
        reasons=(),
    )
    assert preparation is None  # no revision: unchanged honest-identity rule
    assert _node_missing_bytes(None, SPARK_A, 1025, 2) is None
    assert _node_missing_bytes(None, SPARK_A, 0, 2) == 0
    assert _node_missing_bytes(None, SPARK_A, 1025, 1) == 1025
    assert _node_missing_bytes({SPARK_B: 5}, SPARK_B, 9, 2) == 5

    uneven = plan.model_copy(
        update={
            "spark_group": group,
            "storage": plan.storage.model_copy(
                update={
                    "missing_spark_bytes": 1024,
                    "missing_spark_bytes_by_node": {SPARK_A: 0, SPARK_B: 1024},
                }
            ),
            "runtime_storage": runtime_storage,
        }
    )
    _total, per_node = _planned_transfer_bytes(uneven)
    assert per_node == {SPARK_A: 0, SPARK_B: 1024 + 1025}


def test_stop_verification_keeps_waiting_for_a_lost_run_that_is_not_stopped(
    tmp_path: Path,
) -> None:
    """A lost run still owning residue is not a verified stop and not a failure.

    Wrong implementation this catches: the wait was keyed on the active-run
    set, which excludes ``lost``, so the stop verification failed outright
    instead of waiting for the teardown of the residue to converge.
    """

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    ranks = [
        SimpleNamespace(
            node_id=nodes[0], rank=0, role="entrypoint", state="running", fresh=True
        )
    ]
    status = SimpleNamespace(
        state="lost",
        route_state="published",
        route_error=None,
        healthy=False,
        run_generation=1,
        ranks=ranks,
        recovery_owners=(),
        route_recovery_pending=False,
        route_next_attempt_at=None,
        observation_deadline_at=None,
    )
    stub = SimpleNamespace(run_status=lambda _run_id: status)
    service = RecipeLifecyclePhaseExecutor(
        stub,  # type: ignore[arg-type]
        sessions,
        ClusterMappingService(sessions),
        lifecycle._clock,
    )
    plan = SimpleNamespace(
        action="stop", run_id=str(uuid.uuid4()), profile_stop_scope=None, alias="x"
    )

    execution = service.execute(
        plan,  # type: ignore[arg-type]
        RunSwitchPhase(
            index=0,
            kind="final_verify",
            state="planned",
            detail="Verify lifecycle state",
        ),
        item_index=0,
        actor="admin",
        request_key=str(uuid.uuid4()),
        progress=RunSwitchOperationResult(),
    )

    assert execution.waiting is True
    assert execution.result is not None
    assert isinstance(execution.result, RunSwitchFinalVerifyResult)
    assert execution.result.final_verified is False


def test_installation_verification_refuses_a_lost_run_that_still_has_residue(
    tmp_path: Path,
) -> None:
    """A lost run with residue keeps its installation from being verified.

    Wrong implementation this catches: the active-run count excluded ``lost``,
    so an installation whose lost run still owned ranks verified as free.
    """

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    with sessions.begin() as session:
        row = session.get(RecipeRun, run.owner_id)
        assert row is not None
        row.state = "lost"
        row.route_state = "withdrawn"
    service = RecipeLifecyclePhaseExecutor(
        lifecycle,
        sessions,
        ClusterMappingService(sessions),
        lifecycle._clock,
    )

    def bound(session, _plan, installation_id, _mapping_id, _digest):
        stored = session.get(RecipeInstallation, installation_id)
        members = tuple(
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
        return stored, members

    service._bound_installation = bound  # type: ignore[method-assign,assignment]
    plan = SimpleNamespace(
        action="install",
        installation_id=installation.owner_id,
        mapping=SimpleNamespace(mapping_id=mapping_id),
    )

    with pytest.raises(Exception) as _ending:
        service.execute(
            plan,  # type: ignore[arg-type]
            RunSwitchPhase(
                index=0,
                kind="final_verify",
                state="planned",
                detail="Verify lifecycle state",
            ),
            item_index=0,
            actor="admin",
            request_key=str(uuid.uuid4()),
            progress=RunSwitchOperationResult(),
        )


@pytest.mark.parametrize(
    ("run_state", "released_after_sample", "admitted"),
    [
        ("stopped", True, True),
        # Wrong implementation: a run that failed without a Stop receipt may
        # still hold its memory, so its released claim is never credited.
        ("failed", True, False),
        # Wrong implementation: a release the sample already includes was
        # credited a second time.
        ("stopped", False, False),
    ],
)
def test_memory_freed_by_a_confirmed_stop_after_the_sample_is_not_charged_again(
    tmp_path: Path, run_state: str, released_after_sample: bool, admitted: bool
) -> None:
    """The inventory sample can predate the Stop that freed the memory.

    The load after a failed one was refused ("Observed free capacity less
    definite claims ... leaves N bytes before the required reserve") because the
    sample, up to five minutes old, still counted the memory of a run the
    Controller had since stopped and released.
    """
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id, "old", now=lifecycle._clock()
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan, actor="admin", now=lifecycle._clock()
    )
    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = run_state
        run.route_state = "withdrawn"
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "stopped"
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
            )
        ):
            reservation.state = "released"
            reservation.released_at = lifecycle._clock() - timedelta(
                seconds=10 if released_after_sample else 120
            )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    # The sample still shows the stopped run's memory in use: free is short of
    # the demand plus the reserve, while the total is ample.
    free = required + floor - 52
    total = free + 1_000_000
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.observed_at = lifecycle._clock() - timedelta(seconds=60)
        snapshot.received_at = lifecycle._clock() - timedelta(seconds=60)
        snapshot.host_memory_total_bytes = total
        snapshot.host_memory_free_bytes = free
        snapshot.gpu_memory_total_bytes = total
        snapshot.gpu_memory_free_bytes = free
    plan = service.preview(request, actor="admin")

    # The released claim is credited exactly when the sample predates the
    # Stop; without the credit the estimate is tried, with a warning, as the
    # only claim on the Spark.
    assert plan.allowed
    available = plan.fit_current.nodes[0].memory_available_bytes
    assert available is not None
    assert (available >= required + floor) is admitted
    assert any("only claim" in warning.detail for warning in plan.warnings) is (
        not admitted
    )

    # A refused preview must not poison admission when fresh inventory fits.
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.observed_at = lifecycle._clock()
        snapshot.received_at = lifecycle._clock()
        snapshot.host_memory_free_bytes = total
        snapshot.gpu_memory_free_bytes = total
    assert service.preview(request, actor="admin").allowed


@pytest.mark.parametrize(
    "database", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)]
)
def test_dual_profile_disk_review_does_not_decode_unrelated_catalog_documents(
    tmp_path, request, database
):
    """Catches eviction fitting each rank by fetching every catalog head's JSON."""
    from collections import Counter

    from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName
    from vonk_control.fleet_profile_contract.definitions import (
        FleetProfileAssignmentInput,
        FleetProfileInput,
    )
    from vonk_control.fleet_profiles.factory import (
        build_production_fleet_profile_service,
    )
    from vonk_control.models.catalog import CatalogDocument
    from vonk_forge_contracts import document_sha256, read_recipe

    decoded = Counter()

    def decode(payload):
        value = json.loads(payload)
        if (
            isinstance(value, dict)
            and value.get("kind") == RunSwitchReasonScopeName.RECIPE
        ):
            decoded[value["identity"]["slug"]] += 1
        return value

    url = (
        request.getfixturevalue("postgres_engine").url
        if database == "postgres"
        else f"sqlite:///{tmp_path / 'preview.sqlite'}"
    )
    engine = create_engine(url, json_deserializer=decode)
    try:
        sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
            tmp_path, nodes=2, engine=engine
        )
        with sessions.begin() as session:
            template = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == RunSwitchReasonScopeName.RECIPE
                )
            )
            assert template is not None
            # Model a large synced library without paying ingress's compiler
            # work for every irrelevant recipe. These are current typed documents.
            recipe = read_recipe(template.document)
            for index in range(628):
                slug = f"unrelated-{index}"
                definition = recipe.model_copy(
                    update={
                        "identity": recipe.identity.model_copy(update={"slug": slug})
                    }
                )
                document = definition.model_dump(mode="json", exclude_none=True)
                root = CatalogDocument(
                    kind=template.kind,
                    publisher=template.publisher,
                    slug=slug,
                    title=template.slug,
                    created_by="admin",
                    created_at=NOW,
                    updated_at=NOW,
                )
                session.add(root)
                session.flush()
                session.add(
                    CatalogDocumentRevision(
                        document_id=root.id,
                        kind=template.kind,
                        publisher=template.publisher,
                        slug=slug,
                        revision_number=1,
                        schema_version=template.schema_version,
                        state=template.state,
                        document=document,
                        content_digest=document_sha256(document),
                        projected=template.projected,
                        source_bundle_sha256=template.source_bundle_sha256,
                        created_by="admin",
                        created_at=NOW,
                    )
                )
            selector = f"{template.publisher}/{template.slug}"
            # Force the production fit into eviction assessment for both ranks.
            for snapshot in session.scalars(select(NodeInventorySnapshot)):
                snapshot.disk_free_bytes = 0
                snapshot.unused_model_bytes = 0
        switch = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
        profiles = build_production_fleet_profile_service(
            sessions, clock=lambda: NOW, run_switch_operations=switch
        )
        profile = profiles.create(
            FleetProfileInput(
                name="Dual disk review",
                assignments=[
                    FleetProfileAssignmentInput(
                        recipe_selector=selector, spark_ids=list(nodes)
                    )
                ],
            ),
            actor="admin",
        )
        decoded.clear()
        preview = profiles.preview(profile.id)
        assert len(preview.assessments) == 1
        assert len(preview.assessments[0].assessment.fit_current.nodes) == 2
        assert any(
            reason.code == RunSwitchCode.INSUFFICIENT_DISK
            for reason in preview.assessments[0].assessment.blockers
        )
        assert not any(slug.startswith("unrelated-") for slug in decoded), decoded
    finally:
        engine.dispose()


@pytest.mark.parametrize("unused_models", (None, 5))
def test_disk_review_counts_orphan_model_copies_and_scopes_unmeasured_cleanup(
    tmp_path, unused_models: int | None
):
    """Catches a disk refusal claiming only zero removable bytes despite an
    agent reporting reclaimable orphan models; also proves inventory recovery.
    """
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    request = _request(sessions, nodes[0])
    initial = service.preview(request, actor="admin")
    required = initial.fit_current.nodes[0].disk_required_bytes
    assert required is not None
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = 0
        snapshot.unused_model_bytes = unused_models
    short = service.preview(request, actor="admin")
    refusal = next(
        reason
        for reason in short.blockers
        if reason.code == RunSwitchCode.INSUFFICIENT_DISK
    )
    assert "Only 0 bytes" not in refusal.detail
    if unused_models is None:
        assert "Model-copy reclamation is unknown" in refusal.detail
    else:
        assert f"{unused_models} bytes of reclaimable model copies" in refusal.detail
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.unused_model_bytes = snapshot.disk_total_bytes
    assert service.preview(request, actor="admin").allowed


@pytest.mark.parametrize("memory", ("none", "conditional", "infeasible"))
@pytest.mark.parametrize("missing_ranks", (0, 1))
@pytest.mark.parametrize("lifecycle_result", ("allowed", "disallowed", "conflict"))
@pytest.mark.parametrize("stoppable", (False, True))
def test_planner_entrypoints_return_valid_contracts(
    planner_world: _PlannerWorld,
    monkeypatch: pytest.MonkeyPatch,
    memory: str,
    missing_ranks: int,
    lifecycle_result: str,
    stoppable: bool,
) -> None:
    """Catch policy validators rejecting blocked plans or independently sampled fits.

    The PostgreSQL matrix below owns end-to-end admission proof. This Cartesian
    property guards every planner's producer/JSON/consumer boundary in memory.

    Only real events are faked. ``disallowed`` is the lifecycle's own refusal
    when a declared-missing rank re-enrols (rank membership changed), and
    ``conflict`` its planning conflict. The alternating ``observed_role_peaks``
    fake models peak rows that disappear between two fits of one preview: each
    fit reads them in its own READ COMMITTED statement, so a concurrent
    retention delete can leave the current fit infeasible while the after-Stop
    fit is conditional.
    """
    from vonk_control.recipe_operations.impact_plans import StopPlan
    from vonk_control.run_switch_operations import resource_fit

    world = planner_world
    with world.sessions.begin() as session:
        run = session.get(RecipeRun, world.run_id)
        assert run is not None
        run.state = RunState.RUNNING if stoppable else RunState.STOPPED
        for rank in session.scalars(select(RunNode)):
            rank.state = run.state
        for node in session.scalars(select(AgentNode)):
            node.revoked_at = (
                NOW
                if missing_ranks
                and node.node_id == world.original_group.nodes[-1].node_id
                else None
            )
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            total, free = (
                (100, 50)
                if memory == "infeasible"
                else (10_000, 100 if memory == "conditional" else 8_000)
            )
            snapshot.host_memory_total_bytes = snapshot.gpu_memory_total_bytes = total
            snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = free
    preview_stop = world.lifecycle.preview_stop

    def lifecycle_stop(
        run_id: str, *, profile_target_node_ids: Sequence[str] | None = None
    ) -> StopPlan:
        if lifecycle_result == "conflict":
            raise RecipeOperationConflict("Injected lifecycle planning conflict")
        plan = preview_stop(run_id, profile_target_node_ids=profile_target_node_ids)
        if lifecycle_result == "disallowed":
            return replace(plan, allowed=False)
        return plan

    monkeypatch.setattr(world.lifecycle, "preview_stop", lifecycle_stop)
    fits = 0

    def observed_peaks(_session: Session, _digest: str) -> dict[str, int]:
        nonlocal fits
        fits += 1
        # Each _fit reads this owner independently. A changed observation can
        # make current demand infeasible while the after-stop fit is conditional.
        return (
            {node.role: 20_000 for node in world.request.spark_group.nodes}
            if memory == "conditional" and fits % 2
            else {}
        )

    monkeypatch.setattr(resource_fit, "observed_role_peaks", observed_peaks)
    fits = 0
    plan = world.planner.preview(world.request, actor="admin")
    outputs: list[RunSwitchAssessment] = [plan]
    with world.sessions() as session:
        fits = 0
        outputs.append(
            world.planner.recheck_resources_in_session(
                session, world.request, plan.assessment()
            )
        )
    outputs.append(world.planner.preview_stop(world.run_id, actor="admin"))
    if len(world.original_group.nodes) > 1:
        scope = RunSwitchProfileStopScope(
            original_group=world.original_group,
            target_node_ids=sorted(
                node.node_id for node in world.original_group.nodes[:-1]
            ),
            missing_node_ids=[world.original_group.nodes[-1].node_id],
        )
        outputs.append(
            world.planner.preview_profile_stop(world.run_id, scope, actor="admin")
        )
    outputs.append(world.planner.preview_cleanup(world.installation_id, actor="admin"))
    fits = 0
    outputs.append(world.planner.inspect_request(world.request, actor="admin"))
    for output in outputs:
        assert output.allowed == (not output.blockers)
        assert all(isinstance(reason, RunSwitchReason) for reason in output.blockers)
        contract = (
            RunSwitchPlan if isinstance(output, RunSwitchPlan) else RunSwitchAssessment
        )
        assert contract.model_validate_json(output.model_dump_json()) == output


def test_profile_partial_stop_waits_with_lifecycle_scope_blocker(
    tmp_path: Path,
    postgres_engine,
) -> None:
    """A re-enrolled rank must produce a typed wait, not a validator exception."""
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid.uuid4()),
        alias="old",
    )
    planner = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    group = planner.preview_stop(run.owner_id, actor="admin").spark_group
    scope = RunSwitchProfileStopScope(
        original_group=group, target_node_ids=[nodes[0]], missing_node_ids=[nodes[1]]
    )
    # nodes[1] was declared missing but is enrolled (revoked_at is None): the
    # real lifecycle reports the changed rank membership as a disallowed Stop.
    plan = planner.preview_profile_stop(run.owner_id, scope, actor="admin")
    assert not plan.allowed and not plan.stops
    assert [reason.code for reason in plan.blockers] == [
        RunSwitchCode.STOP_PLAN_UNAVAILABLE
    ]
    with sessions.begin() as session:
        for node in session.scalars(select(AgentNode)):
            node.workload_intent_ordinal = 1
    child = planner.apply_profile_stop(
        run.owner_id,
        scope,
        plan_digest=plan.plan_digest,
        request_key=str(uuid.uuid4()),
        actor="admin",
        workload_intent_ordinal=1,
        profile_application_id=str(uuid.uuid4()),
    )
    assert child.state == LifecycleState.OBSERVING
    assert _result(child).retry_reason == RunSwitchCode.STOP_PLAN_UNAVAILABLE
    assert child.blockers[0].detail == RunSwitchCode.STOP_PLAN_UNAVAILABLE
    assert child.next_attempt_at is not None and child.next_attempt_at > NOW
    assert _result(child).observation_deadline_at is not None
    # A blocked partial Stop must not poison the next workload request on the
    # same Sparks. The switch below replaces this run on both of the child's
    # Sparks, so it is exactly what a child holding them would block. A Stop
    # takes no reservations: the child's only claim is the Sparks' workload-
    # intent ordinal (1). The switch is admitted without a blocker and moves
    # both Sparks to ordinal 2, so `_apply_plan`'s ordinal fence now refuses
    # the child's ordinal (SUPERSEDED), not the switch: the latest request steers.
    switch = _request(sessions, nodes[0], action="switch").model_copy(
        update={"spark_group": group}
    )
    fresh = planner.apply(
        RunSwitchApplyRequest(**switch.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    assert fresh.operation_id != child.operation_id
    assert fresh.state == LifecycleState.QUEUED and not fresh.blockers
    assert sorted(fresh.node_ids) == sorted(nodes)
    with sessions() as session:
        child_job = session.get(Job, child.operation_id)
        fresh_job = session.get(Job, fresh.operation_id)
        assert child_job is not None and fresh_job is not None
        assert child_job.payload["workload_intent_ordinal"] == 1
        assert fresh_job.payload["workload_intent_ordinal"] == 2
        assert {
            node.workload_intent_ordinal for node in session.scalars(select(AgentNode))
        } == {2}


@pytest.mark.parametrize(
    ("running_count", "load_count"), [(2, 1), (1, 2), (1, 1), (2, 2)]
)
@pytest.mark.parametrize("memory_shortfall", [False, True])
def test_planner_outputs_validate_across_stop_and_load_topologies(
    tmp_path: Path,
    postgres_engine,
    running_count: int,
    load_count: int,
    memory_shortfall: bool,
) -> None:
    """Catch a planner whose whole-run stops violate its own target-fit contract."""
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, mapping_node_count=running_count, engine=postgres_engine
    )
    installed = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes[:running_count],
        request_id=str(uuid.uuid4()),
    )
    run_plan = lifecycle._run_admission.plan_run(installed.owner_id, "old", now=NOW)
    run_id = lifecycle._run_admission.accept_run(run_plan, actor="admin", now=NOW)
    request = _request(sessions, nodes[0], action="switch")
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        assert (
            build.image_digest is not None
            and build.oci_layout_sha256 is not None
            and build.image_bytes is not None
        )
        expected_image = RuntimeImageIdentity(
            image_digest=build.image_digest,
            oci_layout_sha256=build.oci_layout_sha256,
            image_bytes=build.image_bytes,
            architecture="linux-arm64",
            runtime_interface="vllm",
            build_id=build.id,
        )
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = LifecycleState.RUNNING
        for rank in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            rank.state = LifecycleState.RUNNING
            rank.reserved_memory_bytes = 7_800 if memory_shortfall else 100
        for claim in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        ):
            claim.amount_bytes = 7_800 if memory_shortfall else 100
        original = session.get(CatalogDocumentRevision, request.recipe_revision_id)
        assert original is not None
        document = RecipeDefinition.model_validate(original.document).model_dump(
            mode="json"
        )
        document["release"]["version"] = "2.0.1"
        topology = document["topology"]
        role = topology["roles"][0]
        topology.update(
            {
                "name": f"nodes_{load_count}",
                "node_count": load_count,
                "parallelism": {
                    "tensor": load_count,
                    "pipeline": 1,
                    "data": 1,
                    "backend": "tcp",
                },
                "roles": [role]
                + (
                    [{**role, "name": "worker", "endpoint_owner": False}]
                    if load_count == 2
                    else []
                ),
                "start_order": ["worker", "entrypoint"]
                if load_count == 2
                else ["entrypoint"],
            }
        )
        document["models"][0]["files"][0]["roles"] = [
            item["name"] for item in topology["roles"]
        ]
        RecipeDefinition.model_validate(document)
        target = CatalogDocumentRevision(
            id=str(uuid.uuid4()),
            document_id=original.document_id,
            kind=original.kind,
            publisher=original.publisher,
            slug=original.slug,
            revision_number=2,
            schema_version=2,
            state=original.state,
            document=document,
            content_digest=document_sha256(document),
            projected={},
            created_by="admin",
            created_at=NOW,
        )
        session.add(target)
        session.flush()
        request = request.model_copy(
            update={
                "recipe_revision_id": target.id,
                "spark_group": SparkGroup(
                    nodes=[
                        SparkGroupNode(
                            node_id=node,
                            rank=rank,
                            role="entrypoint" if rank == 0 else "worker",
                            endpoint_owner=rank == 0,
                        )
                        for rank, node in enumerate(nodes[:load_count])
                    ]
                ),
            }
        )
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.inspect_request(
        request,
        actor="admin",
        replaced_run_ids=frozenset({run_id}),
        expected_runtime_image=expected_image,
    )
    if memory_shortfall:
        demand = baseline.fit_current.nodes[0]
        assert (
            demand.memory_required_bytes is not None
            and demand.memory_floor_bytes is not None
        )
        with sessions.begin() as session:
            snapshot = session.scalar(
                select(NodeInventorySnapshot).where(
                    NodeInventorySnapshot.node_id == nodes[0]
                )
            )
            assert snapshot is not None
            total = 7_800 + demand.memory_required_bytes + demand.memory_floor_bytes - 1
            free = demand.memory_required_bytes + demand.memory_floor_bytes + 10
            snapshot.host_memory_total_bytes = snapshot.gpu_memory_total_bytes = total
            snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = free
    plan = service.inspect_request(
        request,
        actor="admin",
        replaced_run_ids=frozenset({run_id}),
        expected_runtime_image=expected_image,
    )
    assert plan.allowed, plan.blockers
    assert len(plan.stops) == 1
    assert plan.stops[0].node_ids == list(nodes[:running_count])
    assert [node.node_id for node in plan.fit_current.nodes] == list(nodes[:load_count])
    assert (plan.post_stop_memory_check is not None) == memory_shortfall
    assert RunSwitchPlan.model_validate_json(plan.model_dump_json()) == plan
    # Profile projection and admission refresh are consumers of the same contract.
    assessment = plan.assessment()
    with sessions() as session:
        refreshed = service.recheck_resources_in_session(session, request, assessment)
    assert (
        RunSwitchAssessment.model_validate_json(refreshed.model_dump_json())
        == refreshed
    )


@pytest.mark.parametrize("during_builder", [True, False])
def test_plan_reuses_inventory_when_a_new_sample_lands_between_read_points(
    tmp_path: Path, postgres_engine, monkeypatch, during_builder: bool
) -> None:
    """Catch READ COMMITTED mixing builder/current/post-stop inventory samples."""
    from vonk_control.recipe_builds.common import _reopen_build_attempt
    from vonk_control.run_switch_operations import (
        build_selection,
        planning_helpers,
        resource_fit,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    node_id = nodes[0]
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    admission = lifecycle._run_admission.plan_run(installed.owner_id, "old", now=NOW)
    run_id = lifecycle._run_admission.accept_run(admission, actor="admin", now=NOW)
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        node = session.get(AgentNode, node_id)
        build = session.get(RecipeBuild, build_id)
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert (
            run is not None
            and node is not None
            and build is not None
            and snapshot is not None
        )
        run.state = LifecycleState.RUNNING
        for rank in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            rank.state = LifecycleState.RUNNING
        set_inventory_identity(node, binary_digest="a" * 64)
        snapshot.capabilities = [*snapshot.capabilities, "recipe.build.v1"]
        if during_builder:
            _reopen_build_attempt(build, now=NOW)
            # An installation of the needed image starts without any build; the
            # builder's inventory is read only when the running one was built
            # from a source bundle this revision has since replaced.
            # The revision records the bundle its build was imported with.
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == build.recipe_revision_id)
                .values(source_bundle_sha256=build.source_bundle_sha256)
            )
            installation = session.get(RecipeInstallation, installed.owner_id)
            assert installation is not None
            install_image_of_replaced_source(session, installation)

    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    request = _request(sessions, node_id, action="switch")
    read = planning_helpers._latest_inventory
    reads = []

    def read_then_publish(session, selected_node, **kwargs):
        sample, evidence = read(session, selected_node, **kwargs)
        reads.append(evidence)
        if len(reads) == 1:
            assert sample is not None
            # Commit through a separate connection after the first SELECT. The
            # old code's next SELECT sees this sample under READ COMMITTED.
            values = {
                attribute.key: getattr(sample, attribute.key)
                for attribute in NodeInventorySnapshot.__mapper__.column_attrs
                if attribute.key != "id"
            }
            values.update(
                id=str(uuid.uuid4()),
                observed_at=sample.observed_at + timedelta(seconds=1),
                evidence_digest="b" * 64,
                disk_free_bytes=sample.disk_free_bytes - 1,
            )
            with sessions.begin() as writer:
                writer.add(NodeInventorySnapshot(**values))
        return sample, evidence

    # Cover the old imports too so reverting the production fix demonstrates
    # the validator error, rather than silently disabling fault injection.
    monkeypatch.setattr(planning_helpers, "_latest_inventory", read_then_publish)
    monkeypatch.setattr(
        build_selection, "_latest_inventory", read_then_publish, raising=False
    )
    monkeypatch.setattr(
        resource_fit, "_latest_inventory", read_then_publish, raising=False
    )
    plan = service.preview(request, actor="admin")
    assert len(reads) == 1
    assert plan.stops
    sample = reads[0]
    uncertainty = plan.fit_current.nodes[0].memory_usage_uncertainty
    assert uncertainty is not None
    assert uncertainty.inventory_observed_at == sample.observed_at
    assert uncertainty.inventory_evidence_digest == sample.evidence_digest
    assert {
        item.evidence_digest for item in plan.freshness if item.source == sample.source
    } == {sample.evidence_digest}
    assert RunSwitchPlan.model_validate_json(plan.model_dump_json()) == plan
    # A new plan has a new sample scope and observes the newly committed row.
    monkeypatch.setattr(planning_helpers, "_latest_inventory", read)
    newer = service.preview(request, actor="admin")
    assert any(item.evidence_digest == "b" * 64 for item in newer.freshness)
    if during_builder:
        assert plan.build.build_id == build_id
        assert plan.build.image_digest is None
        assert plan.build.builder_node_id == node_id
        reviewed = plan.assessment()
        with sessions() as session:
            refreshed = service.recheck_resources_in_session(session, request, reviewed)
        assert refreshed.allowed
        assert sum(item.source == sample.source for item in plan.freshness) == 1
        uncertainty = refreshed.fit_current.nodes[0].memory_usage_uncertainty
        assert uncertainty is not None
        assert uncertainty.inventory_evidence_digest == "b" * 64
        # Previously stored reviews can already contain the duplicate. Every
        # occurrence must refresh, so a last-wins consumer cannot retain t0.
        duplicate = reviewed.model_copy(
            update={"freshness": [*reviewed.freshness, sample]}
        )
        with sessions() as session:
            refreshed = service.recheck_resources_in_session(
                session, request, duplicate
            )
        assert refreshed.allowed
        assert all(
            item.evidence_digest == "b" * 64
            for item in refreshed.freshness
            if item.source == sample.source
        )


@pytest.mark.parametrize("contract_error", [False, True])
def test_low_level_run_preview_propagates_contract_defects(
    tmp_path: Path, monkeypatch, contract_error: bool
) -> None:
    """Catch low-level planner defects being mislabeled as unavailable evidence."""
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    valid = service.preview(request, actor="admin").assessment()

    def defective(*_args, **_kwargs):
        if contract_error:
            return RunSwitchAssessment.model_validate(
                {**valid.model_dump(), "allowed": False}
            )
        raise ValueError("run evidence temporarily unavailable")

    monkeypatch.setattr(lifecycle, "preview_run", defective)
    if contract_error:
        with pytest.raises(ValidationError, match="admission verdict"):
            service.preview(request, actor="admin")
    else:
        plan = service.preview(request, actor="admin")
        assert any(
            item.code == RunSwitchCode.RUN_ADMISSION_UNAVAILABLE
            for item in plan.blockers
        )
