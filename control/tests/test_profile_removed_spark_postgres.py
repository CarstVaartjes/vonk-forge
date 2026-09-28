"""FleetProfile treats removed Sparks as idle unless a model needs the rank."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignmentInput,
    FleetProfileInput,
)
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.memory_reservations import memory_reservations
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentPresence,
    CatalogDocumentRevision,
    ClusterMapping,
    Job,
    RecipeBuild,
    RecipeRun,
    ResourceReservation,
    RunNode,
)

from .test_profile_capacity_admission import _capacity_profile
from .test_recipe_operations import (
    RECEIPT_SIGNER,
    installed_recipe,
    started_recipe,
)


def test_preview_reviews_partial_cleanup_when_a_dual_model_spark_is_revoked(
    tmp_path, postgres_engine
):
    sessions, profiles, planner, profile, _api, _headers, _initial, nodes = (
        _capacity_profile(tmp_path, postgres_engine, node_count=2)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        assert mapping_id is not None and build_id is not None
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="dual-model",
    )
    removed_node = nodes[-1]
    with sessions.begin() as session:
        node = session.get(AgentNode, removed_node)
        assert node is not None
        node.revoked_at = lifecycle._clock()

    review = profiles.preview(profile.id)

    assert review.allowed, (
        [(reason.code, reason.detail) for reason in review.reasons],
        [
            (
                item.assignment_id,
                [reason.code for reason in item.assessment.blockers],
            )
            for item in review.assessments
        ],
    )
    assert any(
        reason.code == "profile.incomplete_multi_spark_model"
        and reason.severity == "warning"
        for reason in review.reasons
    )
    effect = next(
        item
        for item in review.effects.runs
        if item.alias == "dual-model" and item.action == "stop"
    )
    assert effect.node_ids == sorted(nodes)
    assert effect.profile_stop_scope is not None
    assert effect.profile_stop_scope.target_node_ids == [nodes[0]]
    assert effect.profile_stop_scope.missing_node_ids == [removed_node]


def test_profile_apply_stops_only_reachable_rank_and_retains_missing_claim(
    tmp_path, postgres_engine
):
    sessions, profiles, planner, profile, _api, _headers, _initial, nodes = (
        _capacity_profile(tmp_path, postgres_engine, node_count=2)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert mapping_id is not None and build_id is not None
        assert revision is not None
        selector = f"{revision.publisher}/{revision.slug}"

    unrelated_nodes = tuple("spk_" + f"{index:032x}" for index in (3, 4))
    now = lifecycle._clock()
    for index, unrelated_node in enumerate(unrelated_nodes):
        with sessions.begin() as session:
            template = session.get(AgentNode, nodes[0])
            assert template is not None
            capabilities = (
                "runtime.vonk.v1",
                "recipe.operations.v1",
                "fabric.connected.mbps.1000",
            )
            session.add(
                AgentNode(
                    node_id=unrelated_node,
                    state="active",
                    protocol_version=1,
                    architecture=template.architecture,
                    observation_receipt_public_key=RECEIPT_SIGNER.public_key()
                    .public_bytes_raw()
                    .hex(),
                    last_seen_at=now,
                )
            )
            session.flush()
            serial = f"serial-unrelated-{index}"
            fingerprint = f"fingerprint-unrelated-{index}"
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=unrelated_node,
                    fingerprint=fingerprint,
                    not_before=now,
                    not_after=now + timedelta(days=365),
                )
            )
            session.add(
                AgentPresence(
                    node_id=unrelated_node,
                    certificate_serial=serial,
                    certificate_fingerprint=fingerprint,
                    management_address=f"192.168.1.{213 + index}",
                    observed_at=now,
                )
            )
        InventoryRepository(sessions, clock=lifecycle._clock).record(
            InventorySnapshotInput(
                node_id=unrelated_node,
                observed_at=now,
                disk_total_bytes=10_000,
                disk_free_bytes=8_000,
                host_memory_total_bytes=10_000,
                host_memory_free_bytes=8_000,
                gpu_memory_total_bytes=10_000,
                gpu_memory_free_bytes=8_000,
                gpu_count=1,
                artifact_store_read_only=False,
                capabilities=capabilities,
                memory_pool="shared",
                fabric_address=f"192.168.100.{4 + index}",
                fabric_bandwidth_mbps=1000,
            )
        )
    profiles.update(
        profile.id,
        FleetProfileInput(
            expected_revision=profile.revision,
            name=profile.name,
            installation_policy=profile.installation_policy,
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector=selector,
                    spark_ids=list(nodes),
                    assignment_name="dual-model",
                    desired_state="running",
                ),
                FleetProfileAssignmentInput(
                    recipe_selector=selector,
                    spark_ids=list(unrelated_nodes),
                    assignment_name="unrelated-install",
                    desired_state="installed",
                ),
            ],
        ),
        actor="admin",
    )
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="dual-model",
    )
    removed_node = nodes[-1]
    with sessions.begin() as session:
        node = session.get(AgentNode, removed_node)
        assert node is not None
        node.revoked_at = lifecycle._clock()

    review = profiles.preview(profile.id)
    assert review.allowed, (
        [(reason.code, reason.detail) for reason in review.reasons],
        [
            (item.assignment_id, [reason.code for reason in item.assessment.blockers])
            for item in review.assessments
        ],
    )
    effect = next(item for item in review.effects.runs if item.run_id == run.owner_id)
    assert effect.profile_stop_scope is not None
    application = profiles.apply(
        profile.id,
        request_key=str(uuid4()),
        actor="admin",
    )

    stop_job = None
    for _ in range(12):
        planner.tick()
        profiles.tick()
        with sessions() as session:
            stop_job = session.scalar(
                select(Job)
                .where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == run.owner_id,
                )
                .order_by(Job.created_at.desc(), Job.id.desc())
            )
        if stop_job is not None:
            break
    assert stop_job is not None, (
        profiles.application(application.id).state,
        profiles.application(application.id).status_reason,
        profiles.application(application.id).progress,
    )
    assert stop_job.targets == [nodes[0]]
    with sessions() as session:
        agent_operations = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == stop_job.id
                )
            )
        )
    assert [item.node_id for item in agent_operations] == [nodes[0]]

    lifecycle.record_node_result(stop_job.id, nodes[0], succeeded=True, evidence={})
    for _ in range(40):
        planner.tick()
        profiles.tick()
        current = profiles.application(application.id)
        adapter = current.progress.switch_adapter
        if (
            adapter is not None
            and adapter.position == 1
            and adapter.active_kind == "install"
            and adapter.active_operation_id is not None
        ):
            break
    current = profiles.application(application.id)
    adapter = current.progress.switch_adapter
    active_child = (
        planner.get(adapter.active_operation_id)
        if adapter is not None and adapter.active_operation_id is not None
        else None
    )
    child_status = (
        (
            active_child.kind,
            active_child.action,
            active_child.state,
            active_child.current_phase,
            active_child.progress.phase,
            active_child.status_reason,
        )
        if active_child is not None
        else None
    )
    assert (
        current.state == "running"
        and adapter is not None
        and adapter.position == 1
        and adapter.active_kind == "install"
        and active_child is not None
        and active_child.action == "install"
        and active_child.state in {"queued", "running"}
        and active_child.node_ids == list(unrelated_nodes)
    ), (
        current.state,
        current.status_reason,
        adapter.position if adapter is not None else None,
        adapter.active_kind if adapter is not None else None,
        child_status,
    )
    with sessions() as session:
        stored_run = session.get(RecipeRun, run.owner_id)
        assert stored_run is not None
        assert stored_run.state == "lost"
        assert stored_run.route_state == "withdrawn"
        ranks = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == run.owner_id)
                .order_by(RunNode.rank)
            )
        )
        assert [(item.node_id, item.state) for item in ranks] == [
            (nodes[0], "stopped"),
            (removed_node, "running"),
        ]
        claims = tuple(
            session.scalars(
                select(ResourceReservation)
                .where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run.owner_id,
                )
                .order_by(ResourceReservation.node_id)
            )
        )
        assert {item.node_id for item in claims if item.state == "active"} == {
            removed_node
        }
        assert {item.node_id for item in claims if item.state == "released"} == {
            nodes[0]
        }
        assert all(item.state in {"active", "released"} for item in claims)
        live_capacity = memory_reservations(session, nodes[0], memory_pool="shared")
        # The live Spark's capacity projection is node-scoped; retained claims
        # for the revoked rank do not enter the surviving Spark's ledger.
        live_memory_claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.node_id == nodes[0],
                    ResourceReservation.kind == "unified-memory",
                    ResourceReservation.state == "active",
                )
            )
        )
        assert live_capacity.committed_bytes_by_kind.get("unified-memory", 0) == sum(
            item.amount_bytes for item in live_memory_claims
        )
        stop_operation = session.get(Job, stop_job.id)
        assert stop_operation is not None and stop_operation.state == "succeeded"
        run_switch = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop.v2",
                Job.payload["progress"]["profile_application_id"].as_string()
                == application.id,
            )
        )
        assert run_switch is not None and run_switch.state == "failed"
        progress = run_switch.result
        assert progress is not None
        assert (
            progress["failure_code"]
            == "run-switch.profile.incomplete_multi_spark_model"
        )
    with sessions.begin() as session:
        rejoined = session.get(AgentNode, removed_node)
        assert rejoined is not None
        rejoined.revoked_at = None
    rejoin_plan = lifecycle.preview_run(installation.owner_id, "same-id-rejoin")
    missing_rank = next(
        item for item in rejoin_plan.nodes if item.node_id == removed_node
    )
    assert not rejoin_plan.allowed
    assert any(
        reason.code == "run.unreconciled_lost_rank" for reason in missing_rank.blockers
    )
