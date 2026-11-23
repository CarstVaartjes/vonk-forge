from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    LifecycleState,
    RunState,
    RunSwitchCode,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.preparation_contract import RuntimeImageIdentity
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
)
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchAssessment,
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    RunSwitchProfileStopScope,
    RunSwitchReason,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    RunSwitchOperationService,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .recipe_operations_support import (
    NOW,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .run_switch_operations_support import (
    RecordingArtifactExecutor,
    SynchronousPhaseExecutor,
    _request,
    _result,
    _service,
)
from .runtime_identity_support import (
    set_inventory_identity,
)
from .runtime_image_fixtures import install_image_of_replaced_source
from .test_lifecycle_preflight import _clear


@pytest.mark.parametrize(
    "database", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)]
)
def test_dual_profile_disk_review_does_not_decode_unrelated_catalog_documents(
    tmp_path, request, database
):
    """Catches eviction fitting each rank by fetching every catalog head's JSON."""
    from collections import Counter

    from sqlalchemy import create_engine
    from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName
    from vonk_control.fleet_profile_contract import (
        FleetProfileAssignmentInput,
        FleetProfileInput,
    )
    from vonk_control.fleet_profiles import build_production_fleet_profile_service
    from vonk_control.models import CatalogDocument
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


@dataclass
class _PlannerWorld:
    sessions: sessionmaker[Session]
    lifecycle: RecipeOperationService
    planner: RunSwitchOperationService
    request: RunSwitchPreviewRequest
    run_id: str
    installation_id: str
    original_group: SparkGroup


@pytest.fixture(
    scope="module", params=[(old, new) for old in (1, 2, 3) for new in (1, 2)]
)
def planner_world(
    request: pytest.FixtureRequest, tmp_path_factory
) -> Iterator[_PlannerWorld]:
    """Real planner/storage in memory; only mutable lifecycle observations are faked."""
    running_count, load_count = request.param
    engine = create_engine("sqlite://")
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path_factory.mktemp("planner-world"),
        nodes=3,
        mapping_node_count=running_count,
        engine=engine,
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
    planner = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    original_group = planner.preview_stop(run_id, actor="admin").spark_group
    incoming = _request(sessions, nodes[0], action="switch")
    with sessions.begin() as session:
        original = session.get(CatalogDocumentRevision, incoming.recipe_revision_id)
        assert original is not None
        recipe = RecipeDefinition.model_validate(original.document)
        entry = recipe.topology.roles[0].model_copy(update={"count": 1})
        roles = [entry]
        if load_count == 2:
            roles.append(
                entry.model_copy(update={"name": "worker", "endpoint_owner": False})
            )
        topology = recipe.topology.model_copy(
            update={
                "node_count": load_count,
                "roles": roles,
                "parallelism": recipe.topology.parallelism.model_copy(
                    update={"tensor": load_count}
                ),
                "start_order": [role.name for role in reversed(roles)],
            }
        )
        recipe = recipe.model_copy(update={"topology": topology})
        document = recipe.model_dump(mode="json")
        # A model file follows the current recipe's declared roles.
        for model in document["models"]:
            for file in model["files"]:
                file["roles"] = [role.name for role in roles]
        recipe = RecipeDefinition.model_validate(document)
        target = CatalogDocumentRevision(
            document_id=original.document_id,
            kind=original.kind,
            publisher=original.publisher,
            slug=original.slug,
            revision_number=2,
            schema_version=2,
            state=original.state,
            document=recipe.model_dump(mode="json"),
            content_digest=document_sha256(recipe.model_dump(mode="json")),
            projected={},
            created_by="admin",
            created_at=NOW,
        )
        session.add(target)
        session.flush()
        incoming = incoming.model_copy(
            update={
                "recipe_revision_id": target.id,
                "spark_group": SparkGroup(
                    nodes=[
                        SparkGroupNode(
                            node_id=node,
                            rank=rank,
                            role=roles[rank].name,
                            endpoint_owner=rank == 0,
                        )
                        for rank, node in enumerate(nodes[:load_count])
                    ]
                ),
            }
        )
    try:
        yield _PlannerWorld(
            sessions,
            lifecycle,
            planner,
            incoming,
            run_id,
            installed.owner_id,
            original_group,
        )
    finally:
        engine.dispose()


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
    from vonk_control.recipe_action_plans import StopPlan
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


def _probing_switch(tmp_path, *, probe=True, engine=None):
    """A load whose first phase probes its Spark; the probe stays unanswered."""
    sessions, lifecycle, queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, engine=engine
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    # No receipt is reusable, so the first phase issues a probe on the Spark.
    _clear(sessions)
    switch = SimpleNamespace(
        sessions=sessions,
        queue=queue,
        node=nodes[0],
        service=service,
        request=request,
        plan=plan,
        operation=operation,
    )
    if probe:
        for _ in range(4):
            if _probe_ids(switch):
                break
            service.tick()
    return switch


def _probe_ids(switch) -> tuple[str, str] | None:
    """The run-switch's issued probe: its child job and its Spark order."""
    from vonk_agent_protocol import AgentOperation as OperationKind

    with switch.sessions() as session:
        probe = session.scalar(
            select(AgentOperation).where(
                AgentOperation.kind == OperationKind.RUNTIME_PREFLIGHT.value
            )
        )
        return None if probe is None else (probe.parent_job_id, probe.id)


def _cancel(switch) -> None:
    switch.service.cancel(
        switch.operation.operation_id,
        actor="admin",
        request_key=str(uuid.uuid4()),
        reason="The operator chose another model",
    )
    for _ in range(3):
        if switch.service.get(switch.operation.operation_id).state == "cancelled":
            break
        switch.service.tick()
    assert switch.service.get(switch.operation.operation_id).state == "cancelled"


def _probe_states(switch, ids) -> tuple[str, str]:
    with switch.sessions() as session:
        job = session.get(Job, ids[0])
        probe = session.get(AgentOperation, ids[1])
        assert job is not None and probe is not None
        return job.state, probe.state


def _admits_a_fresh_load(switch) -> None:
    fresh = switch.service.apply(
        RunSwitchApplyRequest(
            **switch.request.model_dump(),
            plan_digest=switch.plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert fresh.state not in {"cancelled", "failed"}


def test_cancel_ends_the_queued_runtime_probe_with_its_run_switch(tmp_path):
    """Catches a cancelled run-switch leaving its unclaimed probe queued.

    Incident 2026-10-26: cancelled loads left their preflight children queued
    on a Spark whose claim lane a build held; they would have run (and held the
    lane) for an owner that no longer wanted them.
    """
    switch = _probing_switch(tmp_path)
    ids = _probe_ids(switch)
    assert ids is not None
    assert _probe_states(switch, ids)[1] == LifecycleState.QUEUED.value
    _cancel(switch)
    cancelled = LifecycleState.CANCELLED.value
    assert _probe_states(switch, ids) == (cancelled, cancelled)
    _admits_a_fresh_load(switch)


def test_cancel_while_the_probe_is_issued_still_ends_it(tmp_path):
    """Catches the probe a round commits after the cancel's terminal write.

    The probe is committed before the round saves its checkpoint; a cancel in
    between ended the run-switch from a checkpoint that did not name it.
    """
    switch = _probing_switch(tmp_path, probe=False)
    executor = switch.service._phase_executor
    issue = executor.preflight

    def cancel_after_issue(*arguments, **keywords):
        issued = issue(*arguments, **keywords)
        _cancel(switch)
        return issued

    executor.preflight = cancel_after_issue
    switch.service.tick()
    ids = _probe_ids(switch)
    assert ids is not None
    cancelled = LifecycleState.CANCELLED.value
    assert _probe_states(switch, ids) == (cancelled, cancelled)
    executor.preflight = issue
    _admits_a_fresh_load(switch)


def test_cancel_leaves_a_claimed_probe_to_report_its_own_end(tmp_path):
    """Catches a cancel overwriting the probe a Spark is running."""
    switch = _probing_switch(tmp_path)
    ids = _probe_ids(switch)
    assert ids is not None
    with switch.sessions.begin() as session:
        probe = session.get(AgentOperation, ids[1])
        assert probe is not None
        probe.state = LifecycleState.RUNNING.value
        probe.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=probe.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=NOW + timedelta(seconds=30),
                agent_certificate_serial="serial-0",
                state=LifecycleState.RUNNING.value,
            )
        )
    _cancel(switch)
    job_state, probe_state = _probe_states(switch, ids)
    assert probe_state == LifecycleState.RUNNING.value
    assert job_state != LifecycleState.CANCELLED.value
    _admits_a_fresh_load(switch)


def test_a_queued_probe_names_its_spark_holder_as_the_load_reason(tmp_path):
    """Catches the load reason staying at the reason its probe round started.

    Incident 2026-10-26: the load kept the reason its probe round started with
    for hours while its probe sat queued behind another order holding the Spark.
    """
    from vonk_agent_protocol import AgentOperation as OperationKind
    from vonk_agent_protocol import WaitReason
    from vonk_control.agent_jobs.waits import refresh_queue_waits

    from .test_lifecycle_preflight import _heartbeat, _heartbeating_build

    sessions, lifecycle, _, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    now = [NOW]
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    _clear(sessions)
    for _ in range(4):
        view = service.get(operation.operation_id)
        if (
            view.result is not None
            and view.result.preflight is not None
            and (view.result.preflight.pending_jobs)
        ):
            break
        service.tick()
    holder = _heartbeating_build(
        sessions, nodes[0], now[0], kind=OperationKind.RECIPE_STOP
    )
    for _ in range(3):
        now[0] += timedelta(seconds=61)
        _heartbeat(sessions, holder, nodes[0], now[0])
        service.tick()
    with sessions.begin() as session:
        refresh_queue_waits(session, now[0], node_id=nodes[0])
    now[0] += timedelta(seconds=61)
    service.tick()

    view = service.get(operation.operation_id)
    assert view.status_reason == WaitReason.SPARK_BUSY
    assert view.wait is not None and view.wait.reason == WaitReason.SPARK_BUSY
    assert view.blockers[0].operation is not None
    assert view.blockers[0].operation.id == holder
    assert view.blockers[0].node_ids == [nodes[0]]
    # A fresh request for the same load is still admitted behind the wait.
    fresh = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert fresh.state not in {"cancelled", "failed"}


def test_a_live_probe_keeps_the_dependency_window_waiting(tmp_path):
    """Catches a switch ended as exhausted while its own probe is still live.

    A probe ends by its own report, the gone-target sweep, supersession or the
    parent's cancel, so the dependency window only runs while none is live.
    """
    from datetime import timedelta

    from vonk_control.run_switch_operations import _read_progress
    from vonk_control.run_switch_operations.observation_window import (
        DEPENDENCY_WINDOW,
        checkpoint_verdict,
    )
    from vonk_control.stall_window import Verdict

    switch = _probing_switch(tmp_path)
    assert _probe_ids(switch) is not None
    late = NOW + DEPENDENCY_WINDOW + timedelta(hours=1)
    with switch.sessions() as session:
        job = session.get(Job, switch.operation.operation_id)
        assert job is not None
        progress = _read_progress(job.result)
        assert checkpoint_verdict(job, progress, late) is Verdict.WAITING


@pytest.mark.parametrize("field", ["child_operation_id", "recovery_child_operation_id"])
def test_a_failed_run_switch_ends_its_unclaimed_probe_and_transfer(tmp_path, field):
    """Catches idempotent children outliving a run-switch that failed.

    The same class as the cancelled probe: a queued transfer child of an ended
    owner kept retrying for nobody, whether its phase child or the child a
    damaged checkpoint's recovery recorded. Copied bytes stay for reuse.
    """
    from dataclasses import replace

    from vonk_control.lifecycle import State
    from vonk_control.lifecycle.run_switch import RunSwitchAdapter
    from vonk_control.run_switch_operations import _read_progress

    from .agent_jobs_support import distribution_payload

    switch = _probing_switch(tmp_path)
    probe = _probe_ids(switch)
    assert probe is not None
    with switch.sessions.begin() as session:
        transfer = Job(
            id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()),
            kind="artifact.distribution.v1",
            state=LifecycleState.QUEUED.value,
            actor="admin",
            authority_revision="a" * 64,
            targets=[switch.node],
            payload_digest="a" * 64,
            payload={"workload_intent_ordinal": 1},
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(transfer)
        session.flush()
        transfer_order = str(uuid.uuid4())
        switch.queue.enqueue_in_session(
            session,
            transfer.id,
            switch.node,
            "artifact.distribution.v1",
            "a" * 64,
            distribution_payload(),
            operation_id=transfer_order,
        )
        job = session.get(Job, switch.operation.operation_id)
        assert job is not None
        progress = _read_progress(job.result)
        setattr(progress, field, transfer.id)
        adapter = RunSwitchAdapter(session)
        before = adapter.lifecycle(job, progress, NOW)
        adapter.apply(job, progress, before, replace(before, state=State.FAILED), NOW)
    cancelled = LifecycleState.CANCELLED.value
    assert _probe_states(switch, probe) == (cancelled, cancelled)
    assert _probe_states(switch, (transfer.id, transfer_order)) == (
        cancelled,
        cancelled,
    )
    _admits_a_fresh_load(switch)


def test_postgres_a_probe_a_writer_held_at_the_end_is_ended_on_the_next_check(
    tmp_path, postgres_engine
):
    """Catches an ended owner leaving the probe another writer held queued.

    The abandon never waits for a lock: a probe a claim or result holds at the
    terminal write is skipped. The owner's terminal re-check offers it again
    until nothing it issued is left queued.
    """
    switch = _probing_switch(tmp_path, engine=postgres_engine)
    now = [NOW]
    switch.service._clock = lambda: now[0]
    ids = _probe_ids(switch)
    assert ids is not None
    holder = switch.sessions()
    try:
        holder.begin()
        holder.scalar(select(Job).where(Job.id == ids[0]).with_for_update())
        _cancel(switch)
        assert _probe_states(switch, ids)[1] == LifecycleState.QUEUED.value
    finally:
        holder.rollback()
        holder.close()
    # The worker re-drives the ended owner when its terminal wait is due.
    with switch.sessions() as session:
        ended = session.get(Job, switch.operation.operation_id)
        assert ended is not None and ended.next_action_at is not None
        now[0] = ended.next_action_at.replace(tzinfo=UTC)
    switch.service.redrive(switch.operation.operation_id)
    cancelled = LifecycleState.CANCELLED.value
    assert _probe_states(switch, ids) == (cancelled, cancelled)
    with switch.sessions() as session:
        ended = session.get(Job, switch.operation.operation_id)
        assert ended is not None and ended.next_action_at is None
    _admits_a_fresh_load(switch)
