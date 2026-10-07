"""Future runtime memory stays owned while a profile prepares and stops work."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_control.fleet_profile_contract import FleetProfileApplicationProgress
from vonk_control.memory_reservations import memory_reservations
from vonk_control.models import (
    AgentNode,
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_operations import RecipeOperationConflict
from vonk_control.resource_planning import UnknownRunMemoryResidual
from vonk_control.run_admission import RunAdmissionBusy

from .test_profile_installed_execution import _drive_to_job
from .test_profile_port_claims import _load, _ready_profile
from .test_recipe_operations import installed_recipe, setup_services, started_recipe


@pytest.mark.parametrize("node_count", [1, 2])
def test_accepted_memory_blocks_competition_and_transfers_same_claim(
    tmp_path, postgres_engine, node_count
):
    sessions, profiles, planner, profile, api, headers, nodes, installation = (
        _ready_profile(tmp_path, postgres_engine, node_count=node_count)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    initial = lifecycle.preview_run(installation, "competing")
    requirements = {node.node_id: node for node in initial.nodes}
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            need = requirements[snapshot.node_id]
            snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = (
                need.required_memory_bytes + need.memory_floor_bytes
            )
    planner._memory_floor = initial.nodes[0].memory_floor_bytes
    before = lifecycle.preview_run(installation, "competing")
    assert before.allowed
    application_id, _ = _load(profile, api, headers)
    with sessions() as session:
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application_id,
                    ResourceReservation.kind == "unified-memory",
                )
            )
        )
        assert {claim.node_id for claim in claims} == set(nodes)
        claim_ids = {claim.id for claim in claims}
        assert all(claim.state == "promised" for claim in claims)
        assert all(
            claim.amount_bytes == requirements[claim.node_id].required_memory_bytes
            for claim in claims
        )
    competing = lifecycle.preview_run(installation, "competing")
    assert any(
        reason.code == "run.insufficient_memory"
        for node in competing.nodes
        for reason in node.blockers
    )
    with pytest.raises(RunAdmissionBusy):  # waits; never double-books memory
        lifecycle.start(
            before,
            plan_digest=before.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )
    try:
        job_id = _drive_to_job(profiles, planner, sessions, "recipe.start")
    except AssertionError:
        application = profiles.application(application_id)
        pytest.fail(
            f"{application.state}: {application.status_reason}; {application.progress}"
        )
    child = lifecycle.get(job_id)
    with sessions() as session:
        handed = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == child.owner_id,
                    ResourceReservation.kind == "unified-memory",
                )
            )
        )
        assert {claim.id for claim in handed} == claim_ids
        assert all(
            claim.owner_kind == "run" and claim.state == "active" for claim in handed
        )


def test_replacement_owns_memory_before_and_after_old_claim_is_released(
    tmp_path, postgres_engine
):
    sessions, profiles, planner, profile, api, headers, nodes, installation = (
        _ready_profile(tmp_path, postgres_engine)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    old = started_recipe(
        sessions,
        lifecycle,
        installation,
        nodes,
        request_id=str(uuid4()),
        alias="previous",
    )
    with sessions() as session:
        old_claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == old.owner_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert old_claim is not None
        demand = old_claim.amount_bytes
        old_claim_id = old_claim.id
        old_totals = memory_reservations(session, nodes[0], memory_pool="shared")
        assert old_totals.committed_bytes_by_kind == {"unified-memory": demand}
        assert old_totals.unmaterialized_bytes_by_kind == {}
    application_id, _ = _load(profile, api, headers)
    with sessions() as session:
        pending = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == application_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert pending is not None and pending.state == "promised"
        totals = memory_reservations(session, nodes[0], memory_pool="shared")
        assert totals.committed_bytes_by_kind == {
            "unified-memory": max(demand, pending.amount_bytes)
        }
        assert totals.unmaterialized_bytes_by_kind == {
            "unified-memory": pending.amount_bytes
        }
    stop_id = _drive_to_job(profiles, planner, sessions, "recipe.stop")
    lifecycle.record_node_result(stop_id, nodes[0], succeeded=True, evidence={})
    with sessions() as session:
        released = session.get(ResourceReservation, old_claim_id)
        assert released is not None and released.state == "released"
        totals = memory_reservations(session, nodes[0], memory_pool="shared")
        assert totals.committed_bytes_by_kind == {"unified-memory": demand}
        assert totals.unmaterialized_bytes_by_kind == {"unified-memory": demand}


def test_starting_and_running_claims_keep_the_full_unknown_residual_range(
    tmp_path, postgres_engine
):
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id, "retained", now=lifecycle._clock()
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan, actor="admin", now=lifecycle._clock()
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run_generation = run.run_generation
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.node_id == nodes[0],
                ResourceReservation.kind == "unified-memory",
                ResourceReservation.state == "active",
            )
        )
        assert claim is not None
        peak_bytes = claim.amount_bytes

    for state in ("starting", "running"):
        with sessions.begin() as session:
            run = session.get(RecipeRun, run_id)
            run_node = session.scalar(
                select(RunNode).where(
                    RunNode.run_id == run_id,
                    RunNode.node_id == nodes[0],
                )
            )
            assert run is not None and run_node is not None
            run.state = state
            run_node.state = state

        with sessions() as session:
            totals = memory_reservations(session, nodes[0], memory_pool="shared")
            assert totals.committed_bytes_by_kind == {"unified-memory": peak_bytes}
            assert totals.unmaterialized_bytes_by_kind == {}
            assert totals.unknown_run_residuals_by_kind == {
                "unified-memory": (
                    UnknownRunMemoryResidual(
                        run_id=run_id,
                        run_generation=run_generation,
                        reservation_kind="unified-memory",
                        maximum_bytes=peak_bytes,
                    ),
                )
            }
            unrelated_exclusion = memory_reservations(
                session,
                nodes[0],
                memory_pool="shared",
                excluded_run_ids=(str(uuid4()),),
            )
            assert unrelated_exclusion.unknown_run_residuals_by_kind == (
                totals.unknown_run_residuals_by_kind
            )
            exact_exclusion = memory_reservations(
                session,
                nodes[0],
                memory_pool="shared",
                excluded_run_ids=(run_id,),
            )
            assert exact_exclusion.committed_bytes_by_kind == {}
            assert exact_exclusion.unknown_run_residuals_by_kind == {}


@pytest.mark.parametrize("change", ["missing", "amount", "pool", "digest", "intent"])
def test_changed_memory_authority_cannot_be_reacquired_by_profile_child(
    tmp_path, postgres_engine, change
):
    sessions, _, planner, profile, api, headers, nodes, installation = _ready_profile(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    application_id, review = _load(profile, api, headers)
    alias = review["admission_decisions"][0]["alias"]
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None
        ordinal = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(application.progress), strict=True
        ).workload_intent_ordinal
        assert ordinal is not None
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == application_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert claim is not None
        if change == "missing":
            session.delete(claim)
        elif change == "amount":
            claim.amount_bytes += 1
        elif change == "digest":
            claim.plan_digest = "f" * 64
        elif change == "pool":
            snapshot = session.scalar(select(NodeInventorySnapshot))
            assert snapshot is not None
            snapshot.memory_pool = "separate"
        else:
            node = session.get(AgentNode, nodes[0])
            assert node is not None
            node.workload_intent_ordinal += 1
    # Even a fresh, independently fitting child preview cannot replace the
    # original claim or change the physical pool that the parent reviewed.
    current = lifecycle.preview_run(
        installation, alias, profile_application_id=application_id
    )
    assert current.allowed
    with pytest.raises(RecipeOperationConflict):
        lifecycle.start(
            current,
            plan_digest=current.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
            profile_application_id=application_id,
            workload_intent_ordinal=ordinal,
        )
    with sessions() as session:
        assert not tuple(session.scalars(select(RecipeRun)))


@pytest.mark.parametrize("change", ["claim-digest", "run-digest", "owner", "intent"])
def test_stale_or_unrelated_work_cannot_supply_replacement_overlap(
    tmp_path, postgres_engine, change
):
    sessions, _, planner, profile, api, headers, nodes, installation = _ready_profile(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    old = started_recipe(
        sessions,
        lifecycle,
        installation,
        nodes,
        request_id=str(uuid4()),
        alias="previous",
    )
    _load(profile, api, headers)
    with sessions.begin() as session:
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == old.owner_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert claim is not None
        demand = claim.amount_bytes
        if change == "claim-digest":
            claim.plan_digest = "f" * 64
        elif change == "run-digest":
            run = session.get(RecipeRun, old.owner_id)
            assert run is not None
            run.plan_digest = "f" * 64
        elif change == "owner":
            claim.owner_id = str(uuid4())
        else:
            node = session.get(AgentNode, nodes[0])
            assert node is not None
            node.workload_intent_ordinal += 1
    with sessions() as session:
        totals = memory_reservations(session, nodes[0], memory_pool="shared")
        assert totals.committed_bytes_by_kind == {"unified-memory": 2 * demand}
        assert totals.unmaterialized_bytes_by_kind == {
            "unified-memory": demand * (2 if change == "owner" else 1)
        }


@pytest.mark.parametrize(
    "changed_field",
    [None, "workload_intent_ordinal", "phase", "request_id", "child_operation_id"],
)
def test_replacement_stop_retains_current_root_after_withdrawal_response_loss(
    tmp_path, postgres_engine, changed_field
):
    from vonk_control.job_documents import RecipeStopParent
    from vonk_control.recipe_operations import RecipeStopAuthorityRefused
    from vonk_control.run_switch_contract import RunSwitchOperationResult
    from vonk_control.run_switch_operations import (
        _phase_request_key,
        _stop_child_request_key,
    )

    sessions, profiles, planner, profile, api, headers, nodes, installation = (
        _ready_profile(tmp_path, postgres_engine)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    old = started_recipe(
        sessions,
        lifecycle,
        installation,
        nodes,
        request_id=str(uuid4()),
        alias="previous",
    )
    application_id, _ = _load(profile, api, headers)

    def unavailable_withdrawal(_run_id: str) -> None:
        raise OSError("lost route withdrawal response")

    lifecycle._route_withdrawer = unavailable_withdrawal
    stop_id = _drive_to_job(profiles, planner, sessions, "recipe.stop")
    with sessions() as session:
        stop = session.get(Job, stop_id)
        assert stop is not None and stop.state == "running"
        document = RecipeStopParent.model_validate_json(
            canonical_message(stop.payload), strict=True
        )
        review = document.service_stop_review
        assert review is not None and review.profile_stop_owner is not None
        owner = review.profile_stop_owner
        assert owner.profile_application_id == application_id
        assert owner.run_id == old.owner_id and owner.run_node_ids == list(nodes)
        assert stop.payload.get("phases") is None
        request_id = stop.request_id
        ordinal = document.workload_intent_ordinal
        assert ordinal is not None
        root = session.get(Job, owner.profile_operation_id)
        assert root is not None and root.kind == "recipe.run-switch.v2"
        current = RunSwitchOperationResult.model_validate_json(
            canonical_message(root.result), strict=True
        )
        assert current.child_operation_id == stop_id
        retry_generation = current.phase_retry_generation
        claims_before = {
            (row.id, row.state)
            for row in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == old.owner_id
                )
            )
        }
    if changed_field is not None:
        with sessions.begin() as session:
            root = session.get(Job, owner.profile_operation_id)
            assert root is not None
            damaged = dict(root.result)
            if changed_field == "request_id":
                root.request_id = str(uuid4())
                damaged["child_operation_id"] = None
                root.result = damaged
                # A forged caller follows the altered Root key exactly. Only
                # the accepted profile queue binding can reject this request.
                phase_key = (
                    _phase_request_key(
                        root.request_id,
                        current.phase_index,
                        current.item_index,
                        current.phase_retry_generation,
                    )
                    if current.phase_retry_generation
                    else root.request_id
                )
                request_id = _stop_child_request_key(
                    phase_key, old.owner_id, application_id
                )
            else:
                damaged[changed_field] = (
                    ordinal + 1
                    if changed_field == "workload_intent_ordinal"
                    else str(uuid4())
                    if changed_field == "child_operation_id"
                    else "start"
                )
                root.result = damaged
        with pytest.raises(RecipeStopAuthorityRefused):
            lifecycle.stop(
                old.owner_id,
                plan_digest=document.plan_digest,
                actor="admin",
                request_id=request_id,
                workload_intent_ordinal=ordinal,
                profile_application_id=application_id,
            )
        with sessions() as session:
            if changed_field == "request_id":
                assert (
                    session.scalar(select(Job.id).where(Job.request_id == request_id))
                    is None
                )
            assert {
                (row.id, row.state)
                for row in session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_id == old.owner_id
                    )
                )
            } == claims_before
        return
    lifecycle._route_withdrawer = lambda _run_id: None
    resumed = lifecycle.stop(
        old.owner_id,
        plan_digest=document.plan_digest,
        actor="admin",
        request_id=request_id,
        workload_intent_ordinal=ordinal,
        profile_application_id=application_id,
    )
    assert resumed.id == stop_id
    with sessions() as session:
        stored = session.get(Job, stop_id)
        assert stored is not None
        assert stored.payload["workload_intent_ordinal"] == ordinal
        assert stored.payload.get("phases")
        root = session.get(Job, owner.profile_operation_id)
        assert root is not None and root.result["child_operation_id"] == stop_id
        assert root.result.get("phase_retry_generation") == retry_generation
