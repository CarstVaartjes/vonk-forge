"""Fleet profiles: composite switch adapter."""

from __future__ import annotations

import datetime as _datetime
from datetime import UTC
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, select, update

from vonk_agent_protocol.agent_words import ProfileChildJobKind
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.distribution import DistributionObject
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.distribution.assignment_contract import NodeDistributionAssignment
from vonk_control.fleet_profile_contract.applications import (
    FleetProfileApplicationProgress,
)
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileInput,
)
from vonk_control.fleet_profile_contract.switch_state import (
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildResult,
)
from vonk_control.fleet_profiles.run_switch_adapter import RunSwitchFleetProfileAdapter
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.jobs.documents import RunSwitchJobPayload
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.models.fleet import (
    AgentNode,
    AgentNodeContact,
    FleetProfileApplication,
)
from vonk_control.models.jobs import Job
from vonk_control.models.mapping import ClusterMapping, ClusterMappingNode
from vonk_control.run_switch_contract.operations import RunSwitchOperationResult
from vonk_control.run_switch_operations.results import _persisted_result
from vonk_control.run_switch_operations.service import RunSwitchOperationService

from .fleet_profiles_support import (
    NOW,
    _database,
    _follow_profile_retry,
    _installed_pair_plan,
    _node_id,
    _seed,
    _seed_dual_solo_without_runtime_state,
    _SwitchAdapter,
    _uuid,
)
from .recipe_operations_support import setup_services
from .run_switch_operations_support import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _request,
)
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture


def test_composite_switch_owns_unlisted_scoped_runtime_conflict_once() -> None:
    sessions = _database()
    dual_revision_id, _solo_revision_id = _seed_dual_solo_without_runtime_state(
        sessions
    )
    with sessions.begin() as session:
        dual_recipe_digest = session.scalar(
            select(CatalogDocumentRevision.content_digest).where(
                CatalogDocumentRevision.id == dual_revision_id
            )
        )
        assert dual_recipe_digest is not None
        session.add(
            ClusterMapping(
                id=_uuid(600),
                recipe_revision_id=dual_revision_id,
                topology_name="pair",
                generation=1,
                node_count=2,
                state="ready",
                parameters={},
                placement_digest="a" * 64,
                endpoint_owner_node_id=_node_id(1),
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                ClusterMappingNode(
                    id=_uuid(601),
                    mapping_id=_uuid(600),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=NOW,
                ),
                ClusterMappingNode(
                    id=_uuid(602),
                    mapping_id=_uuid(600),
                    node_id=_node_id(2),
                    rank=1,
                    role="worker",
                    endpoint_owner=False,
                    created_at=NOW,
                ),
            ]
        )
        session.add(
            RecipeBuild(
                id=_uuid(603),
                recipe_revision_id=dual_revision_id,
                builder_node_id=_node_id(1),
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest="sha256:" + "d" * 64,
                oci_layout_sha256="e" * 64,
                image_bytes=1024,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            RecipeInstallation(
                id=_uuid(604),
                recipe_revision_id=dual_revision_id,
                mapping_id=_uuid(600),
                mapping_generation=1,
                recipe_build_id=_uuid(603),
                image_digest="sha256:" + "d" * 64,
                plan_digest="f" * 64,
                plan=_installed_pair_plan(
                    mapping_id=_uuid(600),
                    build_id=_uuid(603),
                    recipe_revision_id=dual_revision_id,
                    recipe_digest=dual_recipe_digest,
                ),
                state="installed",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                InstallationNode(
                    id=_uuid(605),
                    installation_id=_uuid(604),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=1,
                    installed_bytes=1,
                    updated_at=NOW,
                ),
                InstallationNode(
                    id=_uuid(606),
                    installation_id=_uuid(604),
                    node_id=_node_id(2),
                    rank=1,
                    role="worker",
                    state="installed",
                    required_bytes=1,
                    installed_bytes=1,
                    updated_at=NOW,
                ),
            ]
        )
        session.add(
            RecipeRun(
                id=_uuid(607),
                installation_id=_uuid(604),
                mapping_id=_uuid(600),
                mapping_generation=1,
                alias="unlisted-dual",
                plan_digest="1" * 64,
                plan={},
                state="running",
                route_state="published",
                route_generation=1,
                route_digest="2" * 64,
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                RunNode(
                    id=_uuid(608),
                    run_id=_uuid(607),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8000,
                    reserved_memory_bytes=1,
                    updated_at=NOW,
                ),
                RunNode(
                    id=_uuid(609),
                    run_id=_uuid(607),
                    node_id=_node_id(2),
                    rank=1,
                    role="worker",
                    state="running",
                    port=8001,
                    reserved_memory_bytes=1,
                    updated_at=NOW,
                ),
            ]
        )

    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Two solo assignments",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
                        "spark_ids": [_node_id(1)],
                        "desired_state": "running",
                        "assignment_name": "solo-one",
                    },
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
                        "spark_ids": [_node_id(2)],
                        "desired_state": "running",
                        "assignment_name": "solo-two",
                    },
                ],
            }
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)

    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]
    assert preview.summary.stops == 1
    assert set(preview.steps[0].node_ids) == {_node_id(1), _node_id(2)}
    application = service.apply(
        profile.id,
        request_key=_uuid(610),
        actor="admin",
    )
    assert service.tick() is True
    assert len(adapter.starts) == 1
    assert set(adapter.starts[0]["assignment_scopes"]) == {
        (_node_id(1),),
        (_node_id(2),),
    }
    assert service.application(application.id).current_operation_id is not None


def test_all_idle_profile_has_explicit_scope_and_no_preparation() -> None:
    sessions = _database()
    _seed(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    _node_id(2),
                    observed_at=NOW,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(protocol_version=1, last_seen_at=NOW),
            )
        )
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    profile = service.create(
        FleetProfileInput(
            name="All idle",
            assignments=[],
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)

    assert preview.allowed is True
    assert preview.scope.idle_node_ids == [_node_id(1), _node_id(2)]
    assert preview.steps == []
    assert preview.preparations == []

    application = service.apply(
        profile.id,
        request_key=_uuid(801),
        actor="admin",
    )
    assert application.state == "succeeded"
    assert adapter.cancellations == []
    with sessions() as session:
        assert [
            node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode).order_by(AgentNode.node_id))
        ] == [0, 0]
    assert application.result is not None
    assert application.result.changed is False
    assert application.result.completed_steps == 0
    readback = service.application(application.id)
    assert readback.result is not None
    assert readback.result == application.result


@pytest.mark.usefixtures("damaged_json_rows")
def test_all_idle_profile_supersedes_a_queued_load_without_a_run(
    tmp_path: Path,
) -> None:

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    planner = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        memory_floor_bytes=0,
    )
    plan = planner.preview(_request(sessions, nodes[0]), actor="admin")
    payload = RunSwitchJobPayload(
        schema_version=2,
        operation_kind=ProfileChildJobKind.RUN_SWITCH.value,
        action=plan.action,
        plan_digest=plan.plan_digest,
        plan=plan,
        progress=RunSwitchOperationResult(workload_intent_ordinal=1),
        workload_intent_ordinal=1,
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, _node_id(1))
        assert node is not None
        node.workload_intent_ordinal = 1
        session.add(
            Job(
                id=_uuid(820),
                request_id=_uuid(821),
                kind="recipe.run-switch.v2",
                state="queued",
                actor="admin",
                authority_revision="a" * 64,
                targets=[_node_id(1)],
                payload_digest="b" * 64,
                payload=payload.model_dump(mode="json"),
                result=payload.progress.model_dump(mode="json"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    profile = service.create(
        FleetProfileInput(name="Idle", assignments=[]), actor="admin"
    )

    preview = service.preview(profile.id)

    assert [step.node_ids for step in preview.steps] == [[_node_id(1)]]
    application = service.apply(
        profile.id,
        request_key=_uuid(822),
        actor="admin",
    )
    assert application.state == "queued"
    assert adapter.cancellations == [((_node_id(1),), 2)]
    with sessions() as session:
        node = session.get(AgentNode, _node_id(1))
        assert node is not None
        assert node.workload_intent_ordinal == 2


def test_production_profile_adapter_binds_one_real_run_switch_child(
    tmp_path: Path,
) -> None:
    """The profile child delegates exact preparation and run admission to RunSwitch."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    now = [lifecycle._clock()]
    lifecycle._clock = lambda: now[0]
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Production child",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "production-child",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]
    application = service.apply(
        profile.id,
        request_key=_uuid(640),
        actor="admin",
    )

    assert service.tick() is True
    current = service.application(application.id)
    assert current.current_operation_id == application.id
    adapter_progress = current.progress.switch_adapter
    assert isinstance(adapter_progress, FleetProfileSwitchAdapterState)
    child_id = adapter_progress.pending_children[0].operation_id
    assert isinstance(child_id, str)
    child = run_switch.get(child_id)
    assert child.kind == "recipe.run-switch.v2"
    assert child.node_ids == list(nodes)
    assert child.action == "switch"
    assert current.progress.child_progress is not None
    assert current.progress.child_progress.node_ids == list(nodes)
    assert current.progress.child_progress.phase == "runtime-install"
    with sessions() as session:
        stored = session.get(FleetProfileApplication, application.id)
        assert stored is not None
        persisted_progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(stored.progress)
        )
        assert isinstance(
            persisted_progress.switch_adapter, FleetProfileSwitchAdapterState
        )

    replay = adapter.start(
        application_id=application.id,
        assignments=tuple(service._application_assignments(application.id)),
        scope_node_ids=nodes,
        actor="admin",
        request_id=_uuid(641),
    )
    assert not isinstance(replay, Residue)
    assert replay.id == application.id
    assert replay.progress is not None
    assert replay.progress.node_ids == list(nodes)

    restarted_run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    restarted_adapter = RunSwitchFleetProfileAdapter(sessions, restarted_run_switch)
    restarted_service = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=restarted_adapter,
        assessment_provider=restarted_adapter.assess,
    )
    assert restarted_service.tick() is True
    _follow_profile_retry(restarted_service, application.id, now, assert_no_work=True)
    resumed = restarted_service.application(application.id)
    assert resumed.current_operation_id == application.id
    assert resumed.progress.switch_adapter is not None
    assert any(
        child.operation_id == child_id
        for child in resumed.progress.switch_adapter.pending_children
    )


def _transfer_result(nodes: tuple[str, ...]) -> dict[str, object]:
    """Return one real Run/Switch result document carrying a transfer phase."""

    node_id = nodes[0]
    archive_sha256 = "1" * 64
    assignment = NodeDistributionAssignment(
        assignment_id=_uuid(648),
        plan_digest="c" * 64,
        generation=1,
        node_id=node_id,
        expires_at=_datetime.datetime(2026, 9, 12, tzinfo=UTC),
        model_artifact_set_sha256="d" * 64,
        objects=(
            DistributionObject(name="model", sha256="e" * 64, bytes=10, kind="model"),
        ),
        oci_image_digest="sha256:" + "f" * 64,
        oci_image_config_digest="sha256:" + "9" * 64,
        oci_archive_sha256=archive_sha256,
    )
    return {
        "phase_index": 3,
        "completed_phases": ["prepare", "transfer"],
        "phase_results": [
            {
                "phase": "transfer",
                "subphase": "target-copy",
                "cached_nodes": [],
                "assignments": {
                    node_id: assignment.model_dump(mode="json"),
                },
            }
        ],
    }


def test_completed_switch_child_keeps_its_run_switch_receipt(tmp_path: Path) -> None:
    """A finished profile step must persist the child's public result tree.

    The acceptance canary reads the Run/Switch receipt back out of the profile
    application's step results.  The adapter used to answer a completed child
    from its mirrored state, whose result is the adapter's own summary, so the
    step recorded no receipt and the run could not be qualified.  The receipt
    now travels with the completed child, so it survives the tick that finished
    it and any later restart.
    """

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Completed switch child",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "completed-switch-child",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    application = service.apply(
        profile.id,
        request_key=_uuid(647),
        actor="admin",
    )
    assert service.tick() is True
    started = service.application(application.id)
    assert started.progress.switch_adapter is not None
    child_id = started.progress.switch_adapter.pending_children[0].operation_id
    assert isinstance(child_id, str)

    # The child finishes with its public Run/Switch result tree, exactly as the
    # real service persists one.  A transfer phase result is part of it: its
    # distribution objects are contract tuples that a JSON round trip decodes
    # as arrays, which is where the receipt used to be lost on the way back.
    with sessions.begin() as session:
        job = session.get(Job, child_id)
        assert job is not None
        job.state = "succeeded"
        job.status_reason = None
        job.result = _persisted_result(
            RunSwitchOperationResult.model_validate_json(
                canonical_message(_transfer_result(nodes))
            )
        )
        job.updated_at = lifecycle._clock()

    assert service.tick() is True
    completed = service.application(application.id)
    assert completed.state == "succeeded", completed.status_reason
    step_results = completed.progress.step_results
    receipts = [
        value.result
        for value in step_results.values()
        if isinstance(value.result, FleetProfileSwitchChildResult)
    ]
    assert len(receipts) == 1
    # The receipt names the plan step it completed, not the child's own
    # operation kind: a switch-adapter child used to record no kind at all.
    assert {value.kind for value in step_results.values()} == {"switch"}
    receipt = receipts[0]
    assert receipt.run_switch_operation_id == child_id
    assert receipt.run_switch.phase_index == 3
    completed_phase = receipt.run_switch.phase_results[0]
    assert (completed_phase.phase, completed_phase.subphase) == (
        "transfer",
        "target-copy",
    )

    # A restart reads the same receipt out of the persisted application.
    restarted = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    persisted = restarted.application(application.id)
    assert persisted.progress.step_results == completed.progress.step_results
    assert any(
        isinstance(value.result, FleetProfileSwitchChildResult)
        for value in persisted.progress.step_results.values()
    )


def test_waiting_switch_child_keeps_the_profile_running(tmp_path: Path) -> None:
    """A Run/Switch child that is waiting on its own observation is in progress.

    Background runtime-image preparation and overdue start observations park
    the child in ``waiting`` and resume it automatically. The profile used to
    fail the whole load with "Run/Switch child returned waiting".
    """

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    now = [lifecycle._clock()]
    lifecycle._clock = lambda: now[0]
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Waiting switch child",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "waiting-switch-child",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    application = service.apply(
        profile.id,
        request_key=_uuid(648),
        actor="admin",
    )
    assert service.tick() is True
    started = service.application(application.id)
    assert started.progress.switch_adapter is not None
    child_id = started.progress.switch_adapter.pending_children[0].operation_id
    assert isinstance(child_id, str)

    with sessions.begin() as session:
        job = session.get(Job, child_id)
        assert job is not None
        job.state = "observing"
        job.status_reason = "Runtime image preparation is running in the background"
        job.updated_at = lifecycle._clock()

    service.tick()
    waiting = service.application(application.id)
    assert waiting.state in {"queued", "running"}, waiting.status_reason
    assert waiting.progress.switch_adapter is not None
    assert any(
        child.operation_id == child_id
        for child in waiting.progress.switch_adapter.pending_children
    )

    with sessions.begin() as session:
        job = session.get(Job, child_id)
        assert job is not None
        job.state = "succeeded"
        job.status_reason = None
        job.result = _persisted_result(
            RunSwitchOperationResult.model_validate_json(
                canonical_message(_transfer_result(nodes))
            )
        )
        job.updated_at = lifecycle._clock()

    _follow_profile_retry(service, application.id, now, assert_no_work=True)
    assert service.tick() is True
    completed = service.application(application.id)
    assert completed.state == "succeeded", completed.status_reason


def test_switch_adapter_joins_the_callers_row_transaction(tmp_path: Path) -> None:
    """Advancing a child must reuse the tick's transaction, not race its row lock.

    The worker's tick holds the application row and then reads the switch child.
    An adapter that opens its own transaction waits on that same row in
    PostgreSQL: the worker deadlocks against itself, the child never advances
    and the run stays parked. SQLite has no ``SELECT ... FOR UPDATE``, so the
    caller writes the row instead to hold the same writer lock; an adapter that
    opens its own writer then fails here with "database is locked".
    """

    engine = create_engine(
        f"sqlite:///{tmp_path / 'operations.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 0.25},
    )
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, engine=engine
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
    service = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Joined transaction",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "joined-transaction",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    application = service.apply(
        profile.id,
        request_key=_uuid(645),
        actor="admin",
    )
    assert service.tick() is True
    started = service.application(application.id)
    assert started.progress.switch_adapter is not None
    child_id = started.progress.switch_adapter.pending_children[0].operation_id
    assert isinstance(child_id, str)

    caller_note = "caller-owned transaction witness"
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id, with_for_update=True)
        assert row is not None
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == application.id)
            .values(status_reason=caller_note)
        )

        child = adapter.get(application.id, session=session)

        assert child.state in {"queued", "running"}
        stored = session.get(FleetProfileApplication, application.id)
        assert stored is not None
        assert stored.status_reason == caller_note
        progress = FleetProfileApplicationProgress.model_validate(stored.progress)
        assert isinstance(progress.switch_adapter, FleetProfileSwitchAdapterState)
        assert any(
            child.operation_id == child_id
            for child in progress.switch_adapter.pending_children
        )

    committed = service.application(application.id).progress.switch_adapter
    assert committed is not None
    assert any(child.operation_id == child_id for child in committed.pending_children)


def test_profile_tick_advances_a_switch_child_on_postgres(
    tmp_path: Path, postgres_engine: Engine
) -> None:
    """PostgreSQL takes a real row lock, so the tick must reuse its own session.

    ``SELECT ... FOR UPDATE`` blocks another writer of the same row for real
    here, which is what stalled the deployed worker: the adapter opened a second
    transaction for the application row the tick already held. With a short
    ``lock_timeout`` the wrong implementation surfaces as
    ``canceling statement due to lock timeout`` instead of a worker that stops
    advancing forever.
    """

    engine = create_engine(
        postgres_engine.url.render_as_string(hide_password=False),
        connect_args={"options": "-c lock_timeout=3000"},
    )
    try:
        sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
            tmp_path, engine=engine
        )
        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
        assert revision is not None
        now = [lifecycle._clock()]
        lifecycle._clock = lambda: now[0]
        run_switch = RunSwitchOperationService(
            sessions,
            lifecycle=lifecycle,
            clock=lifecycle._clock,
            artifacts=CompleteArtifactInspector(),
            artifact_phase_executor=RecordingArtifactExecutor(),
            memory_floor_bytes=50,
        )
        adapter = RunSwitchFleetProfileAdapter(sessions, run_switch)
        service = FleetProfileService(
            sessions,
            clock=lifecycle._clock,
            switch_adapter=adapter,
            assessment_provider=adapter.assess,
        )
        profile = service.create(
            FleetProfileInput.model_validate(
                {
                    "name": "Postgres transaction",
                    "assignments": [
                        {
                            "recipe_selector": f"vonk-forge/{revision.slug}",
                            "spark_ids": list(nodes),
                            "desired_state": "running",
                            "assignment_name": "postgres-transaction",
                        }
                    ],
                }
            ),
            actor="admin",
        )
        preview = service.preview(profile.id)
        assert preview.allowed is True
        application = service.apply(
            profile.id,
            request_key=_uuid(646),
            actor="admin",
        )

        assert service.tick() is True
        started = service.application(application.id)
        assert started.current_operation_id == application.id
        assert started.progress.switch_adapter is not None
        child_id = started.progress.switch_adapter.pending_children[0].operation_id
        assert isinstance(child_id, str)
        # The next pass reads under the same row lock and persists its first
        # observation deadline; the following before-due pass does no work.
        assert service.tick() is True
        _follow_profile_retry(service, application.id, now, assert_no_work=True)
        resumed = service.application(application.id)
        assert resumed.progress.switch_adapter is not None
        assert any(
            child.operation_id == child_id
            for child in resumed.progress.switch_adapter.pending_children
        )
    finally:
        engine.dispose()
