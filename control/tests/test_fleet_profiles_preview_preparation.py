"""Fleet profiles: preview preparation."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Table, select, update
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.state_machines import DesiredAssignmentState
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileAssignmentInput,
    FleetProfileInput,
    FleetProfileView,
)
from vonk_control.fleet_profiles.run_switch_adapter import RunSwitchFleetProfileAdapter
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocument, CatalogDocumentRevision
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.models.fleet import (
    AgentNode,
    AgentNodeContact,
    FleetProfile,
    FleetProfileApplication,
)
from vonk_control.models.mapping import ClusterMapping, ClusterMappingNode
from vonk_control.preparation_contract import (
    RolloutPreparation,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .fleet_profiles_support import (
    NOW,
    _assessment,
    _database,
    _exact_preparation,
    _input,
    _installed_pair_plan,
    _nested_object,
    _node_id,
    _recipe_document,
    _seed,
    _SwitchAdapter,
    _uuid,
)
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture


def test_production_profile_adapter_routes_all_idle_to_one_complete_stop_child(
    tmp_path: Path,
) -> None:
    """An empty desired set still stops a complete in-scope run through RunSwitch."""

    from vonk_control.run_switch_operations.service import RunSwitchOperationService

    from .recipe_operations_support import (
        installed_recipe,
        setup_services,
        started_recipe,
    )
    from .run_switch_operations_support import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=_uuid(642),
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=_uuid(643),
    )
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
        FleetProfileInput(
            name="All idle through RunSwitch",
            assignments=[],
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]

    application = service.apply(
        profile.id,
        request_key=_uuid(644),
        actor="admin",
    )
    assert service.tick() is True
    current = service.application(application.id)
    assert current.current_operation_id == application.id
    state = current.progress.switch_adapter
    assert state is not None
    child_id = state.pending_children[0].operation_id
    assert isinstance(child_id, str)
    child = run_switch.get(child_id)
    assert child.kind == "recipe.stop.v2"
    assert child.action == "stop"
    assert child.node_ids == list(nodes)
    with sessions() as session:
        stored_run = session.get(RecipeRun, run.owner_id)
        assert stored_run is not None and stored_run.state == "running"
        assert (
            session.scalar(select(RecipeRun.id).where(RecipeRun.id != run.owner_id))
            is None
        )


def test_all_idle_profile_stops_a_lost_run_that_still_has_residue(
    tmp_path: Path,
) -> None:
    """A lost run owns capacity and residue: an idle profile must still stop it.

    Wrong implementation this catches: stop selection read the active-run set,
    which excludes ``lost``, so the profile saw nothing to stop and the residue
    stayed on the Spark forever.
    """

    from vonk_control.run_switch_operations.service import RunSwitchOperationService

    from .recipe_operations_support import (
        installed_recipe,
        setup_services,
        started_recipe,
    )
    from .run_switch_operations_support import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=_uuid(1642),
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=_uuid(1643),
    )
    with sessions.begin() as session:
        lost = session.get(RecipeRun, run.owner_id)
        assert lost is not None
        lost.state = "lost"
        assert session.scalar(select(RunNode.id).where(RunNode.run_id == lost.id))
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
        FleetProfileInput(name="All idle over a lost run", assignments=[]),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]

    application = service.apply(
        profile.id,
        request_key=_uuid(1644),
        actor="admin",
    )
    assert service.tick() is True
    state = service.application(application.id).progress.switch_adapter
    assert state is not None
    child_id = state.pending_children[0].operation_id
    assert isinstance(child_id, str)
    child = run_switch.get(child_id)
    assert child.kind == "recipe.stop.v2"
    assert child.node_ids == list(nodes)


def test_profile_preparations_are_stably_ordered_and_reuse_identity() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
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
        session.add(
            RecipeBuild(
                id=_uuid(60),
                recipe_revision_id=revision_id,
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
    assignments = [
        FleetProfileAssignmentInput(
            recipe_selector="vonk-forge/synthetic-tiny-build",
            spark_ids=[_node_id(2)],
            desired_state=DesiredAssignmentState.INSTALLED,
        ),
        FleetProfileAssignmentInput(
            recipe_selector="vonk-forge/synthetic-tiny-build",
            spark_ids=[_node_id(1)],
            desired_state=DesiredAssignmentState.INSTALLED,
        ),
    ]
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    profile = service.create(
        FleetProfileInput(
            name="Two cached copies",
            assignments=assignments,
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)

    next_observation = NOW

    def changing_observation_provider(
        _session, _assignment, node_ids
    ) -> RolloutPreparation:
        nonlocal next_observation
        result = _exact_preparation(node_ids, observed_at=next_observation)
        next_observation += timedelta(seconds=1)
        return result

    digest_service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        assessment_provider=lambda *args, **_kwargs: _assessment(
            changing_observation_provider(*args)
        ),
    )
    assert (
        digest_service.preview(profile.id).plan_digest
        == digest_service.preview(profile.id).plan_digest
    )

    assert [item.assignment_id for item in preview.preparations] == sorted(
        item.assignment_id for item in preview.preparations
    )
    assert len(preview.preparations) == 2
    assert {
        item.preparation.model.artifact_set_sha256 for item in preview.preparations
    } == {preview.preparations[0].preparation.model.artifact_set_sha256}
    assert {
        item.preparation.runtime_image.image_digest for item in preview.preparations
    } == {"sha256:" + "d" * 64}
    assert {
        item.preparation.model.artifact_set_bytes for item in preview.preparations
    } == {100}


def test_profile_rejects_preparation_evidence_for_another_scope() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids)).model_copy(
                update={"preparation": _exact_preparation((_node_id(2),))}
            )
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")

    preview = service.preview(profile.id)

    assert preview.allowed is False
    assert preview.preparations == []


def test_profile_contract_rejects_ambiguous_or_incomplete_assignments() -> None:
    value = _input(_uuid(2)).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = [_node_id(1), _node_id(1)]
    with pytest.raises(ValidationError):
        FleetProfileInput.model_validate(value)

    value = _input(_uuid(2)).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = []
    with pytest.raises(ValidationError):
        FleetProfileInput.model_validate(value)


def test_profile_create_is_server_owned_validated_and_digest_stable() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)

    created = service.create(_input(revision_id), actor="admin")
    loaded = service.get(created.id)
    listed = service.list()

    assert created.definition == loaded.definition
    assert created.revision == loaded.revision
    # The list is the overview: same definition, cache truth not observed.
    assert [p.id for p in listed.profiles] == [loaded.id]
    assert listed.profiles[0].revision == loaded.revision
    assert listed.profiles[0].definition == loaded.definition
    assert created.name == "Studio ready"
    assert created.assignments[0].recipe.name == "Synthetic Tiny build"
    assert created.assignments[0].spark_ids == [_node_id(1)]
    assert len(created.profile_digest) == 64
    assert created.profile_digest == loaded.profile_digest


def test_profile_validation_rejects_unknown_sparks_and_recipe_topology_drift() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = [_node_id(9)]
    profile = service.create(FleetProfileInput.model_validate(value), actor="admin")
    preview = service.preview(profile.id)
    assert not preview.allowed

    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = [_node_id(1), _node_id(2)]
    value["name"] = "Topology drift"
    profile = service.create(FleetProfileInput.model_validate(value), actor="admin")
    preview = service.preview(profile.id)
    assert not preview.allowed


def test_profile_validation_rejects_rank_order_that_mapping_would_rewrite() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    revisions = CatalogDocumentRevision.__table__
    assert isinstance(revisions, Table)
    document = _recipe_document()
    topology = _nested_object(document, "topology")
    leader = deepcopy(_nested_object(document, "topology", "roles", 0))
    leader.update({"name": "leader", "count": 1, "endpoint_owner": True})
    worker = deepcopy(leader)
    worker.update({"name": "worker", "endpoint_owner": False})
    topology.update(
        {
            "name": "pair",
            "node_count": 2,
            "roles": [leader, worker],
            "parallelism": {
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "nccl",
            },
            "start_order": ["worker", "leader"],
        }
    )
    _nested_object(document, "models", 0, "files", 0)["roles"] = ["leader", "worker"]
    RecipeDefinition.model_validate(document)
    with sessions.begin() as session:
        session.execute(
            revisions.update()
            .where(CatalogDocumentRevision.id == revision_id)
            .values(
                document=document,
                content_digest=document_sha256(document),
            )
        )
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

    value = _input(revision_id).model_dump(mode="json")
    value["assignments"][0]["spark_ids"] = [_node_id(2), _node_id(1)]
    with pytest.raises(ValidationError):
        FleetProfileInput.model_validate(value)


def test_profile_preview_explains_prerequisites_then_builds_one_atomic_plan() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert preview.assignments[0].current_state == "not-placed"
    assert preview.assignments[0].actions == ["switch"]
    assert [step.kind for step in preview.steps] == ["switch"]
    assert len(preview.plan_digest) == 64

    application = service.apply(
        profile.id,
        request_key=_uuid(4),
        actor="admin",
    )
    replay = service.apply(
        profile.id,
        request_key=_uuid(4),
        actor="admin",
    )
    assert application == replay
    assert application.state == "queued"
    assert application.total_steps == 1
    assert application.profile_digest == preview.profile_digest
    with sessions() as session:
        stored = session.get(FleetProfileApplication, application.id)
        assert stored is not None
        stored_plan = stored.plan
        assert stored_plan["scope"] == {
            "node_ids": [_node_id(1)],
            "idle_node_ids": [],
        }


def test_profile_apply_uses_latest_saved_profile_when_digest_is_stale() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")

    updated = service.update(
        profile.id,
        _input(revision_id, name="Studio exact").model_copy(
            update={"expected_revision": profile.revision}
        ),
        actor="admin",
    )
    assert updated.profile_digest != profile.profile_digest
    application = service.apply(profile.id, request_key=_uuid(5), actor="admin")
    assert application.profile_digest == updated.profile_digest
    assert application.state == "queued"


@pytest.mark.parametrize("compiled_port", ["authorised", "recipe-declared"])
def test_profile_scope_reconciles_idle_member_and_retains_reusable_installation(
    compiled_port: str,
) -> None:
    sessions = _database()
    _recipe_id, dual_revision_id = _seed(sessions)
    revisions = CatalogDocumentRevision.__table__
    assert isinstance(revisions, Table)
    dual_document = _recipe_document()
    dual_topology = _nested_object(dual_document, "topology")
    role_template = dict(_nested_object(dual_document, "topology", "roles", 0))
    dual_topology.update(
        {
            "name": "pair",
            "node_count": 2,
            "roles": [
                {"name": "entrypoint", "count": 1, "endpoint_owner": True},
                {"name": "worker", "count": 1, "endpoint_owner": False},
            ],
            "parallelism": {
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "nccl",
            },
            "start_order": ["worker", "entrypoint"],
        }
    )
    dual_topology["roles"] = [
        {**role_template, "name": "entrypoint", "endpoint_owner": True},
        {**role_template, "name": "worker", "endpoint_owner": False},
    ]
    _nested_object(dual_document, "models", 0, "files", 0)["roles"] = [
        "entrypoint",
        "worker",
    ]
    RecipeDefinition.model_validate(dual_document)
    solo_revision_id = _uuid(5)
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
        session.execute(
            revisions.update()
            .where(CatalogDocumentRevision.id == dual_revision_id)
            .values(
                document=dual_document,
                content_digest=document_sha256(dual_document),
            )
        )
        solo_document = _recipe_document()
        _nested_object(solo_document, "identity")["slug"] = "synthetic-tiny-solo"
        solo_recipe = RecipeDefinition.model_validate(solo_document)
        solo_root_id = _uuid(4)
        session.add(
            CatalogDocument(
                id=solo_root_id,
                kind="recipe",
                publisher=solo_recipe.identity.publisher,
                slug=solo_recipe.identity.slug,
                title="Synthetic Tiny Solo",
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=solo_revision_id,
                document_id=solo_root_id,
                kind="recipe",
                publisher=solo_recipe.identity.publisher,
                slug=solo_recipe.identity.slug,
                revision_number=1,
                state="active",
                schema_version=2,
                document=solo_document,
                content_digest=document_sha256(solo_document),
                execution_key="c" * 64,
                created_by="admin",
                created_at=NOW,
            )
        )
        session.add(
            ClusterMapping(
                id=_uuid(10),
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
                    id=_uuid(11),
                    mapping_id=_uuid(10),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=NOW,
                ),
                ClusterMappingNode(
                    id=_uuid(12),
                    mapping_id=_uuid(10),
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
                id=_uuid(13),
                recipe_revision_id=dual_revision_id,
                builder_node_id=_node_id(1),
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=f"sha256:{'d' * 64}",
                oci_layout_sha256="e" * 64,
                image_bytes=1024,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            RecipeInstallation(
                id=_uuid(14),
                recipe_revision_id=dual_revision_id,
                mapping_id=_uuid(10),
                mapping_generation=1,
                recipe_build_id=_uuid(13),
                image_digest=f"sha256:{'d' * 64}",
                plan_digest="f" * 64,
                plan=_installed_pair_plan(
                    mapping_id=_uuid(10),
                    build_id=_uuid(13),
                    recipe_revision_id=dual_revision_id,
                    recipe_digest=document_sha256(dual_document),
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
                    id=_uuid(15),
                    installation_id=_uuid(14),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=1,
                    installed_bytes=1,
                    updated_at=NOW,
                ),
                InstallationNode(
                    id=_uuid(16),
                    installation_id=_uuid(14),
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
                id=_uuid(17),
                installation_id=_uuid(14),
                mapping_id=_uuid(10),
                mapping_generation=1,
                alias="dual-chat",
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
                    id=_uuid(18),
                    run_id=_uuid(17),
                    node_id=_node_id(1),
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8000,
                    reserved_memory_bytes=1,
                    updated_at=NOW,
                ),
                RunNode(
                    id=_uuid(19),
                    run_id=_uuid(17),
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

    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile_a = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Dual",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [_node_id(1), _node_id(2)],
                        "desired_state": "running",
                        "assignment_name": "dual-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    profile_b = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Solo and idle",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
                        "spark_ids": [_node_id(1)],
                        "desired_state": "running",
                        "assignment_name": "solo-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    if compiled_port == "recipe-declared":
        # Wrong implementation this catches: the load reused an installation
        # compiled for a port the firewall refuses, so the start failed again
        # and the recipe could never be loaded without removing it by hand.
        with sessions.begin() as session:
            stale = session.get(RecipeInstallation, _uuid(14))
            assert stale is not None
            plan = deepcopy(stale.plan)
            for compiled in _nested_object(plan, "compiled_execution_plans").values():
                _nested_object(compiled, "endpoint")["port"] = 8000
            stale.plan = plan
        reload = service.preview(profile_a.id)
        assert reload.summary.installs == 1
        return
    no_op = service.preview(profile_a.id)
    assert no_op.steps == []
    retained = service.apply(
        profile_a.id,
        request_key=_uuid(802),
        actor="admin",
    )
    assert retained.state == "succeeded"
    with sessions() as session:
        assert [
            node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode).order_by(AgentNode.node_id))
        ] == [0, 0]
    switch_to_b = service.preview(profile_b.id)
    assert switch_to_b.scope.node_ids == [_node_id(1), _node_id(2)]
    assert switch_to_b.scope.idle_node_ids == [_node_id(2)]
    assert [step.kind for step in switch_to_b.steps] == ["switch"]
    assert switch_to_b.scope.idle_node_ids == [_node_id(2)]
    assert switch_to_b.summary.uninstalls == 0

    with sessions.begin() as session:
        run = session.get(RecipeRun, _uuid(17))
        assert run is not None
        run.state = "stopped"
        run.route_state = "withdrawn"
    back_to_a = service.preview(profile_a.id)
    assert [step.kind for step in back_to_a.steps] == ["switch"]
    assert back_to_a.summary.installs == 0


@pytest.mark.parametrize(
    "damage", ["numeric-variant", "missing-spark-ids", "extra", "invalid-root"]
)
def test_profile_round_trip_rejects_corrupt_stored_assignment(damage):
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = service.create(_input(revision_id), actor="admin")
    assert service.get(created.id).definition == created.definition
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        assignments = deepcopy(row.assignments)
        if damage == "invalid-root":
            assignments = {}
        elif damage == "numeric-variant":
            assignments[0]["model_variant"] = 123
        elif damage == "missing-spark-ids":
            del assignments[0]["spark_ids"]
        else:
            assignments[0]["undeclared"] = None
        # The stored document can be any JSON value, so persist the damaged
        # shape directly instead of through the typed ORM attribute.
        session.execute(
            update(FleetProfile)
            .where(FleetProfile.id == created.id)
            .values(assignments=assignments)
        )
    from vonk_control.fleet_profile_contract.definitions import (
        UnavailableFleetProfileView,
    )

    # Keep the exact saved identity observable. Dropping a malformed choice
    # would let an ordinary edit silently delete accepted authoring intent.
    observed = service.read_number(created.number)
    if damage == "extra":
        # The column owner adopts retired fields on reads. Known intent stays
        # visible, and observing it never rewrites the stored document.
        assert isinstance(observed, FleetProfileView)
        assert observed.definition == created.definition
        with sessions() as session:
            row = session.get(FleetProfile, created.id)
            assert row is not None and row.assignments == assignments
        return
    assert isinstance(observed, UnavailableFleetProfileView)
    assert observed.id == created.id and observed.revision == created.revision
    assert observed.definition is None
    with sessions() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None and row.assignments == assignments


def test_profile_preview_blocks_when_required_preparation_cannot_be_attested() -> None:
    """A plan that must prepare assets is blocked when they cannot be attested.

    ``AGENTS.md`` requires missing model or recipe-image assets to be actionable
    blockers, so a profile that still has to place its exact assets must not be
    admitted while the Controller cannot project that evidence.
    """

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)

    def _unavailable(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError(
            "The Run/Switch authority cannot attest exact model and OCI "
            "preparation evidence (run-switch.install-preparation-unavailable)."
        )

    service = FleetProfileService(
        sessions, clock=lambda: NOW, assessment_provider=_unavailable
    )
    profile = service.create(_input(revision_id), actor="admin")

    preview = service.preview(profile.id)

    assert preview.allowed is False
    assert preview.preparations == []
    reason = next(
        reason
        for reason in preview.reasons
        if reason.code == "profile.preparation_unavailable"
    )
    assert reason.severity == "error"
    application = service.apply(
        profile.id,
        request_key=_uuid(41),
        actor="admin",
    )
    assert application.state == "queued"


def test_profile_load_with_missing_preparation_enqueues_it_and_waits() -> None:
    """A load asks for the model and image it lacks instead of stopping there."""

    from vonk_control.operations.blockers import make_blocker

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)

    def _missing(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError("no successful immutable runtime image build is available")

    requested: list[str] = []

    def _starter(
        recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ):
        requested.append(recipe_revision_id)
        return [
            make_blocker(
                "recipe_image.preparing",
                "Preparing the model and runtime image (prepare).",
                severity="info",
            )
        ]

    service = FleetProfileService(
        sessions, clock=lambda: NOW, assessment_provider=_missing
    )
    service.bind_preparation_starter(_starter)
    profile = service.create(_input(revision_id), actor="admin")

    # The review lists what the Controller will prepare.
    preview = service.preview(profile.id)
    assert preview.allowed is False
    assert [step.kind for step in preview.preparation_steps] == ["prepare", "prepare"]

    application = service.apply(profile.id, request_key=_uuid(42), actor="admin")

    assert requested == [revision_id]
    assert application.state == "queued"  # waiting for the preparation, not blocked
    assert application.next_attempt_at is not None


def test_load_waiting_on_a_working_preparation_outlives_its_wait_window() -> None:
    """Catches the 30-minute acceptance budget ending a load whose model and
    image are still being prepared; it ends only when its preparation stalls,
    with that evidence."""

    from datetime import timedelta

    from vonk_agent_protocol.reason_codes.recipes import RecipeImageCode
    from vonk_control.fleet_profile_contract.vocabulary import (
        FLEET_PROFILE_ENDED_STATES,
    )
    from vonk_control.operations.blockers import make_blocker

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    working = [True]

    def _missing(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError("no successful immutable runtime image build is available")

    def _starter(
        recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ):
        if not working[0]:
            # The preparation's own stall window ended it.
            return [
                make_blocker(
                    RecipeImageCode.PREPARATION_EXHAUSTED,
                    "Preparation made no progress for its stall window.",
                    severity="error",
                )
            ]
        return [
            make_blocker(
                "recipe_image.preparing",
                "Preparing the model and runtime image (download).",
                severity="info",
            )
        ]

    def _progressed(recipe_revision_id: str, *, application_id: str | None = None):
        return now[0] if working[0] else None

    service = FleetProfileService(
        sessions, clock=lambda: now[0], assessment_provider=_missing
    )
    service.bind_preparation_starter(_starter, progress=_progressed)
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(profile.id, request_key=_uuid(43), actor="admin")
    window = timedelta(minutes=30)
    for _ in range(10):  # three and a third old windows of a slow download
        now[0] += window / 3
        service._observe_pending_admissions(now[0])
    assert service.application(application.id).state not in FLEET_PROFILE_ENDED_STATES
    working[0] = False  # the preparation stalled
    for _ in range(4):
        now[0] += window / 3
        service._observe_pending_admissions(now[0])
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        assert row.state in FLEET_PROFILE_ENDED_STATES
        assert "no progress" in (row.status_reason or "")
