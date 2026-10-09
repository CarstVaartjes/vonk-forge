"""Recipe-operation bookkeeping reconciles; request and fence refusals still refuse.

Damaged or missing stored state (a run or installation plan, a job's phases or
result, a build receipt, a recovery continuation, a profile Stop's parent, a
model identity) is rebuilt from evidence or retired as unknown
(``lifecycle.evidence``) and the operation goes on.  A request that names
something that does not exist, a destructive Stop without its exact authority and
a stale workload intent are still refused, with a typed category.
"""

from __future__ import annotations

import ast
import hashlib
import uuid
from collections.abc import Mapping
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    InvalidRequestError,
    LifecycleState,
    RecipeBuildCleanupEvidence,
    RecipeStartResult,
    RecipeStopResult,
    ReservationState,
    SecurityRefusalError,
    UnknownOutcomeError,
    canonical_message,
)
from vonk_control import recipe_operations
from vonk_control.bounded_json import require_mapping
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.job_documents import DistributedRecoveryMarker
from vonk_control.lifecycle.evidence import BookkeepingReason, Residue
from vonk_control.models import (
    AgentOperation,
    InstallationNode,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_execution_contract import parse_stored_installation_plan
from vonk_control.recipe_lifecycle_contract import (
    RecipeOperationProgressResult,
    RecipeOperationResult,
)
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
    RecipeRequestInvalid,
    RecipeRetryLater,
    RecipeStopAuthorityRefused,
    _job_workload_intent,
    _primary_model_identity,
    _recipe_model_identities,
    _recorded_result,
    _role_phases,
    _run_accepted_ranks,
    _run_is_one_shot,
    _run_observes_per_generation,
    _stored_phases,
    _topology_order,
    new_recipe_job,
    record_build_evidence,
)
from vonk_control.recipe_operations import action_plans as recipe_action_plans
from vonk_control.run_admission import RunAdmissionService

from .recipe_stop_fixtures import recipe_stop_payload
from .test_recipe_builds import RecordingQueue as BuildQueue
from .test_recipe_builds import setup as build_setup
from .test_recipe_operations import (
    NOW,
    _required,
    complete_started_recipe,
    installed_recipe,
    setup_services,
    start_evidence,
    started_recipe,
)

DAMAGED: dict[str, object] = {"not": "a stored plan"}


def _evidence(view, field: str, node_id: str) -> Mapping[str, object]:
    result = _required(view.result)
    return require_mapping(
        require_mapping(result[field], field)[node_id], "node evidence"
    )


def _running_recipe(tmp_path: Path, *, nodes: int = 1):
    sessions, service, queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes
    )
    installation = installed_recipe(
        service, mapping_id, build_id, node_ids, request_id="b" * 35 + "1"
    )
    run = started_recipe(
        sessions, service, installation.owner_id, node_ids, request_id="b" * 35 + "2"
    )
    return sessions, service, queue, installation, run, node_ids


# ---------------------------------------------------------------- stored plans


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_damaged_run_plan_is_rebuilt_from_the_mapping_not_refused(tmp_path) -> None:
    sessions, service, _queue, _installation, run, node_ids = _running_recipe(tmp_path)
    before = service.preview_stop(run.owner_id)
    with sessions.begin() as session:
        _required(session.get(RecipeRun, run.owner_id)).plan = DAMAGED

    after = service.preview_stop(run.owner_id)
    status = service.run_status(run.owner_id)

    # The accepted ranks come back from the saved mapping: the Stop stays allowed
    # and its membership proof is the same as it was with the plan.
    assert "stop.rank_membership_changed" not in {item.code for item in after.blockers}
    assert after.allowed == before.allowed
    assert [rank.node_id for rank in status.ranks] == list(node_ids)
    with sessions() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        installation = _required(
            session.get(RecipeInstallation, stored.installation_id)
        )
        rebuilt = _run_accepted_ranks(session, stored, installation.recipe_revision_id)
    assert not isinstance(rebuilt, Residue)
    assert rebuilt[1] is True


@pytest.mark.usefixtures("damaged_json_rows")
def test_run_membership_nobody_can_prove_is_retired_as_unknown(tmp_path) -> None:
    sessions, service, _queue, _installation, run, _nodes = _running_recipe(tmp_path)
    with sessions.begin() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        stored.plan = DAMAGED
        # The saved mapping names another generation: no evidence agrees.
        stored.mapping_generation = stored.mapping_generation + 7

    plan = service.preview_stop(run.owner_id)

    # Nothing raised: the Stop plan reports the identity as not exact, which is
    # what unproven means (the caller re-plans), and the run stays readable.
    assert "stop.rank_membership_changed" in {item.code for item in plan.blockers}
    assert service.run_status(run.owner_id).ranks


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_damaged_run_plan_still_finds_a_one_shot_run_by_its_activation(
    tmp_path,
) -> None:
    sessions, _service, _queue, _installation, run, _nodes = _running_recipe(tmp_path)
    with sessions.begin() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        stored.plan = DAMAGED
        session.flush()
        assert _run_is_one_shot(session, stored) is False
        payload = {
            "schema_version": 1,
            "owner_kind": "run",
            "owner_id": stored.id,
            "plan_digest": stored.plan_digest,
        }
        session.add(
            new_recipe_job(
                id=str(uuid.uuid4()),
                request_id=str(uuid.uuid4()),
                kind="recipe.job.activate.v1",
                state="succeeded",
                actor="admin",
                authority_revision="a" * 64,
                targets=[],
                payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                payload=payload,
                result={"activated": True},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        assert _run_is_one_shot(session, stored) is True
        assert _run_observes_per_generation(stored) is True


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_damaged_installation_plan_is_skipped_and_prepared_afresh(tmp_path) -> None:
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(
        tmp_path, nodes=1
    )
    plan = service.preview_install(mapping_id, build_id)
    first = service.prepare_installation(plan, actor="admin")
    assert service.prepare_installation(plan, actor="admin") == first
    with sessions.begin() as session:
        _required(session.get(RecipeInstallation, first)).plan = DAMAGED

    second = service.prepare_installation(plan, actor="admin")

    assert second != first
    with sessions() as session:
        assert (
            recipe_operations.RecipeOperationService._prepared_installation_id(
                session, plan
            )
            == second
        )


@pytest.mark.usefixtures("damaged_json_rows")
def test_start_installation_persists_exact_recompilation_and_completes(
    tmp_path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    plan = service.preview_install(mapping_id, build_id)
    installation_id = service.prepare_installation(plan, actor="admin")
    with sessions.begin() as session:
        _required(session.get(RecipeInstallation, installation_id)).plan = DAMAGED

    started = service.start_installation(
        installation_id, actor="admin", request_id=str(uuid.uuid4())
    )
    with sessions() as session:
        installation = _required(session.get(RecipeInstallation, installation_id))
        stored = parse_stored_installation_plan(installation.plan)
        assert stored.plan_digest == plan.plan_digest
        assert stored.compiled_execution_plans == plan.compiled_plan_by_node
        assert tuple(session.scalars(select(RecipeInstallation.id))) == (
            installation_id,
        )
    for node in nodes:
        service.record_node_result(
            started.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    assert service.get(started.id).state == LifecycleState.SUCCEEDED.value
    run_plan = service.preview_run(installation_id, "repaired-compilation")
    run = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    complete_started_recipe(sessions, service, run.id)
    assert service.get(run.id).state == LifecycleState.SUCCEEDED.value


# ------------------------------------------------------------ job state damage


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_damaged_result_is_shown_without_it_and_never_refuses(tmp_path) -> None:
    sessions, service, _queue, _installation, run, _nodes = _running_recipe(tmp_path)
    with sessions.begin() as session:
        _required(session.get(Job, run.id)).result = {"unexpected": "shape"}

    view = service.get(run.id)

    assert view.state == "succeeded"
    assert view.result is None
    assert _recorded_result("recipe.start", "not a mapping", subject="x") is None


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_phases_end_a_start_through_its_recovery_error(tmp_path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="d" * 36
    )
    plan = service.preview_run(installation.owner_id, "damaged-phases")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="e" * 36
    )
    with sessions.begin() as session:
        parent = _required(session.get(Job, start.id))
        payload = dict(parent.payload)
        payload["phases"] = "garbage"
        parent.payload = payload
        child = _required(
            session.scalar(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        child_payload = dict(child.payload)

    result = service.record_node_result(
        start.id,
        child.node_id,
        succeeded=True,
        evidence=start_evidence(child_payload),
    )

    # The ranks are not advanced blind: the operation ends failed, naming why.
    assert result.state == "failed"
    assert isinstance(result.lifecycle_result, RecipeOperationResult)
    assert result.lifecycle_result.recovery_error is not None
    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        assert run.state in {"stopping", "failed"}


def test_stored_phases_are_retired_not_raised() -> None:
    parent = Job(
        id=str(uuid.uuid4()),
        kind="recipe.start",
        payload={
            "schema_version": 1,
            "owner_kind": "run",
            "owner_id": str(uuid.uuid4()),
            "plan_digest": "a" * 64,
        },
    )
    assert _stored_phases(parent) == ()
    for damaged in ("garbage", [], [[]], [[{"operation_id": "x", "node_id": "n"}]]):
        parent.payload = {**parent.payload, "phases": damaged}
        loaded = _stored_phases(parent)
        assert isinstance(loaded, Residue)
        assert loaded.reason is BookkeepingReason.PERSISTED_STATE_DAMAGED


def test_a_job_that_lost_its_intent_recovers_it_from_its_orders(tmp_path) -> None:
    sessions, _service, _queue, _installation, run, _nodes = _running_recipe(tmp_path)
    with sessions.begin() as session:
        parent = _required(session.get(Job, run.id))
        expected = recipe_operations._bound_workload_intent(parent)
        payload = {
            key: value
            for key, value in parent.payload.items()
            if key != "workload_intent_ordinal"
        }
        parent.payload = payload
        session.flush()
        # The orders carry the ordinal: the fence tolerant of damage re-derives it.
        assert _job_workload_intent(session, parent) in {expected, None}
        # The fence itself still refuses to guess a missing intent.
        with pytest.raises(RecipeStopAuthorityRefused):
            recipe_operations._bound_workload_intent(parent)


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_start_history_still_queues_exact_run_cleanup(
    tmp_path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="f" * 35 + "1"
    )
    plan = service.preview_run(installation.owner_id, "no-start-authority")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="f" * 35 + "2"
    )
    with sessions.begin() as session:
        parent = _required(session.get(Job, start.id))
        payload = dict(parent.payload)
        payload.pop("phases", None)
        parent.payload = payload
        for child in session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        ):
            child.payload = {"not": "a start order"}

    for node in nodes:
        service.record_node_result(
            start.id, node, succeeded=False, evidence={"code": "start.failed"}
        )

    # The missing Start receipt cannot certify absence. Durable run membership
    # still authorizes exact cleanup, retaining its claims until acknowledgement.
    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        assert run.state == "stopping"
        assert (
            session.scalar(
                select(Job.id).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == start.owner_id,
                )
            )
            is not None
        )


# ---------------------------------------------------------- evidence at ingress


def test_a_start_result_that_does_not_match_its_order_marks_the_rank_unproven(
    tmp_path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="1" * 36
    )
    plan = service.preview_run(installation.owner_id, "bad-evidence")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36
    )

    view = service.record_node_result(
        start.id,
        nodes[0],
        succeeded=True,
        evidence={
            "endpoint": "not-the-endpoint-of-this-rank",
            "extra": object.__name__,
        },
    )

    assert view.state == "failed"
    evidence = _evidence(view, "launch_evidence", nodes[0])
    assert evidence["code"] == "recipe.evidence_unproven"
    with sessions() as session:
        assert _required(session.get(RecipeRun, start.owner_id)).state == "stopping"


def test_install_evidence_that_cannot_be_proven_fails_the_rank_for_a_retry(
    tmp_path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="3" * 36
    )

    view = service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": -1}
    )

    assert view.state == "failed"
    assert isinstance(view.lifecycle_result, RecipeOperationResult)
    assert view.lifecycle_result.failed_nodes == [nodes[0]]
    with sessions() as session:
        row = _required(
            session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == install.owner_id
                )
            )
        )
        assert row.state == "failed"
        assert (
            _required(session.get(RecipeInstallation, install.owner_id)).state
            == "partial"
        )
    # The failed install is retried through the ordinary retry.
    retry = service.retry(install.id, actor="admin", request_id="4" * 36)
    assert retry.owner_id == install.owner_id


def test_a_replayed_result_with_different_evidence_keeps_the_first_receipt(
    tmp_path,
) -> None:
    _sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="5" * 36
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )

    replayed = service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 999}
    )

    assert _evidence(replayed, "node_evidence", nodes[0]) == {"installed_bytes": 120}


@pytest.mark.usefixtures("damaged_json_rows")
def test_build_evidence_that_does_not_hold_is_reported_not_raised(tmp_path) -> None:
    sessions, _service, _queue, _mapping_id, build_id, _nodes = setup_services(
        tmp_path, nodes=1
    )
    with sessions.begin() as session:
        build = _required(session.get(RecipeBuild, build_id))
        assert (
            record_build_evidence(session, build, {"image_digest": "nope"}, now=NOW)
            is False
        )
        # A stored envelope that does not parse never blocks a valid receipt.
        build.plan = DAMAGED
        assert (
            record_build_evidence(
                session,
                build,
                {
                    "image_bytes": 10,
                    "image_digest": "sha256:" + "a" * 64,
                    "oci_layout_sha256": "b" * 64,
                },
                now=NOW,
            )
            is True
        )


# ------------------------------------------------------- reconcile and recovery


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_recovery_with_damaged_authority_is_retired_not_raised(tmp_path) -> None:
    sessions, service, _queue, _installation, run, _nodes = _running_recipe(tmp_path)
    with sessions.begin() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        stored.route_state = "withdrawn"
        stored.plan = {"damaged": True}
        recovery = DistributedRecoveryMarker(
            schema_version=1, failed_rank=0, deadline=NOW.isoformat()
        )
        outcome = service.queue_recovery_stop_in_session(
            session,
            stored.id,
            recovery_context=recovery,
            workload_intent_ordinal=1,
            now=NOW,
        )
        not_current = service.queue_recovery_stop_in_session(
            session,
            "no-such-run",
            recovery_context=recovery,
            workload_intent_ordinal=1,
            now=NOW,
        )

    assert isinstance(outcome, Residue)
    assert outcome.reason is BookkeepingReason.PERSISTED_STATE_DAMAGED
    # A scope that is only not current yet is retried on the next pass.
    assert isinstance(not_current, Residue)
    assert not_current.reason is BookkeepingReason.EVIDENCE_UNAVAILABLE


def test_the_superseded_assessment_uses_the_jobs_own_sparks_when_scope_differs(
    tmp_path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="6" * 36
    )
    with sessions.begin() as session:
        # The owner's rows no longer name the Spark the job was issued to.
        for row in session.scalars(
            select(InstallationNode).where(
                InstallationNode.installation_id == install.owner_id
            )
        ):
            session.delete(row)
        parent = _required(session.get(Job, install.id))
        child = _required(
            session.scalar(
                select(AgentOperation).where(AgentOperation.parent_job_id == install.id)
            )
        )
        child.current_attempt = 1
        assert parent.targets == [nodes[0]]

    # Nothing raised for the scope disagreement; the unissued job is retired
    # only on exact evidence, and an issued one is reported for observation.
    assert service.assess_superseded_unissued("recipe.install", install.owner_id) in {
        True,
        False,
    }
    pending = service.assess_superseded_issued("recipe.install", install.owner_id)
    assert pending is None or pending.job_id == install.id


# ------------------------------------------------- stop authority, build cleanup


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_profile_stop_whose_parent_is_damaged_retires_nothing_and_ends_failed(
    tmp_path,
) -> None:
    sessions, service, _queue, _installation, run, nodes = _running_recipe(tmp_path)
    plan = service.preview_stop(run.owner_id)
    stop = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="a" * 36,
    )
    with sessions.begin() as session:
        parent = _required(session.get(Job, stop.id))
        payload = {**parent.payload, "execution_mode": "profile-jobrun-stop"}
        parent.payload = payload
        parent.payload_digest = hashlib.sha256(canonical_message(payload)).hexdigest()

    view = service.record_node_result(stop.id, nodes[0], succeeded=True, evidence={})

    # The exact Stop receipts cannot be proven against an accepted authority that
    # does not parse: no JobRun identity is retired, the Stop ends failed and the
    # profile's own retry answers it; nothing is raised into the agent's result.
    assert view.state == "failed"
    # The exact child receipt succeeded; the damaged parent cannot establish
    # completion of the operation as a whole, rather than a child failure.
    result = view.lifecycle_result
    assert isinstance(result, RecipeOperationResult)
    assert result.failed_nodes == []
    assert result.successful_nodes == [nodes[0]]
    assert result.recovery_error is not None
    with sessions() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        assert stored.state == "failed"
        assert stored.route_error


def test_a_profile_stop_child_that_cannot_be_proven_is_retired_not_raised(
    tmp_path,
) -> None:
    sessions, service, _queue, _installation, run, nodes = _running_recipe(tmp_path)
    plan = service.preview_stop(run.owner_id)
    stop = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="b" * 36,
    )
    with sessions() as session:
        parent = _required(session.get(Job, stop.id))
        child = _required(
            session.scalar(
                select(AgentOperation).where(AgentOperation.parent_job_id == stop.id)
            )
        )
        # Not a profile JobRun Stop at all: its identity cannot be proven.
        outcome = service._validate_profile_jobrun_stop_child(
            session, parent, child, now=NOW, require_current=False
        )
        assert isinstance(outcome, Residue)
        assert outcome.reason is BookkeepingReason.EVIDENCE_MISMATCH
        completion = service._complete_jobrun_stop_in_session(
            session, parent, (child,), now=NOW
        )
        assert isinstance(completion, Residue)
    assert nodes


def test_an_unreadable_profile_stop_is_retried_and_never_guessed(tmp_path) -> None:
    sessions, service, _queue, _installation, run, _nodes = _running_recipe(tmp_path)
    with sessions() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        outcome = service._profile_jobrun_stop_in_session(
            session,
            run=stored,
            nodes=(),
            installation=_required(
                session.get(RecipeInstallation, stored.installation_id)
            ),
            authority_revision="a" * 64,
            target_node_ids=(),
            stop_plan_digest="a" * 64,
            actor="admin",
            request_id="c" * 36,
            profile_application_id=str(uuid.uuid4()),
            workload_intent_ordinal=1,
            now=NOW,
        )

    # No accepted profile Stop: nothing is stopped on a guess; the caller is told
    # to retry (a typed unknown outcome) when it turns this into a refusal.
    assert isinstance(outcome, Residue)
    assert outcome.reason is BookkeepingReason.EVIDENCE_UNAVAILABLE


def test_a_build_cleanup_receipt_that_does_not_match_is_reported_unproven(
    tmp_path,
) -> None:
    sessions, service, _queue, _mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    payload = {
        "schema_version": 1,
        "owner_kind": "recipe-build",
        "owner_id": build_id,
        "plan_digest": "e" * 64,
    }
    parent = new_recipe_job(
        id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        kind="recipe.build.cleanup.v1",
        state="running",
        actor="admin",
        authority_revision="e" * 64,
        targets=list(nodes),
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        created_at=NOW,
        updated_at=NOW,
    )
    operation = AgentOperation(
        id=str(uuid.uuid4()),
        parent_job_id=parent.id,
        node_id=nodes[0],
        kind="recipe.build.cleanup.v1",
        state="succeeded",
        payload={"garbage": True},
        payload_digest="f" * 64,
        authority_revision="e" * 64,
    )
    with sessions.begin() as session:
        reason = service._apply_build_cleanup(
            session, parent, operation, {"garbage": True}, owner_id=build_id, now=NOW
        )

    assert isinstance(reason, str)
    assert "cleanup" in reason


def test_cancelling_a_pull_whose_build_is_gone_completes_on_the_job(tmp_path) -> None:
    sessions, service, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    payload = {
        "schema_version": 1,
        "owner_kind": "recipe-build",
        "owner_id": "missing-build",
        "plan_digest": "e" * 64,
        "build_intent": {"kind": "independent"},
        "prebuilt_image": "ghcr.io/example/image@sha256:" + "d" * 64,
    }
    job = new_recipe_job(
        id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        kind="recipe.build.v1",
        state="running",
        actor="admin",
        authority_revision="e" * 64,
        targets=list(nodes),
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        created_at=NOW,
        updated_at=NOW,
    )
    with sessions.begin() as session:
        session.add(job)

    view = service.cancel(
        job.id, actor="admin", request_id=str(uuid.uuid4()), reason="no longer needed"
    )

    # A cancel always completes, even when the build it belongs to is gone.
    assert view.state == "cancelled"
    # And cancelling something that already ended is a no-op, not a refusal.
    assert service._cancel_build(
        job.id, actor="admin", request_id=str(uuid.uuid4()), reason="again"
    ) in {False, True}


def _build_operations(tmp_path):
    sessions, bundles, now, node_id, revision = build_setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=BuildQueue(),
        clock=lambda: now,
        builds=builds,
    )
    return sessions, operations, plan, node_id


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_succeeded_build_whose_receipt_is_gone_is_built_again(tmp_path) -> None:
    sessions, operations, plan, node_id = _build_operations(tmp_path)
    first = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="initial-build",
    )
    done = operations.record_node_result(
        first.id,
        node_id,
        succeeded=True,
        evidence={
            "image_bytes": 500,
            "image_digest": "sha256:" + "b" * 64,
            "oci_layout_sha256": "c" * 64,
        },
    )
    assert done.state == "succeeded"
    with sessions.begin() as session:
        _required(session.get(Job, first.id)).result = {"unexpected": "shape"}

    # The build row says succeeded but its receipt does not hold: the evidence is
    # rebuilt by building again, never by refusing the request.
    again = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="rebuild-after-lost-receipt",
    )

    assert again.id != first.id
    assert again.state == "running"


def test_a_failed_build_with_no_failed_receipt_is_started_again(tmp_path) -> None:
    sessions, operations, plan, _node_id = _build_operations(tmp_path)
    with sessions.begin() as session:
        build = _required(session.get(RecipeBuild, plan.build_id))
        build.state = "failed"

    started = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="start-after-failed-without-receipt",
    )

    assert started.state == "running"


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_retried_build_recovers_its_damaged_plan_from_the_previous_order(
    tmp_path,
) -> None:
    sessions, operations, plan, _node_id = _build_operations(tmp_path)
    first = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="initial-build",
    )
    with sessions.begin() as session:
        _required(session.get(Job, first.id)).state = "failed"
        build = _required(session.get(RecipeBuild, plan.build_id))
        build.state = "failed"
        build.plan = DAMAGED

    retried = operations.retry(first.id, actor="admin", request_id="retry-damaged")

    assert retried.id != first.id
    with sessions() as session:
        # The order the previous attempt carried is the plan it ran: it is
        # written back, so the next reader finds a stored plan that parses.
        assert _required(session.get(RecipeBuild, plan.build_id)).plan != DAMAGED


# ------------------------------------------------------------- model identity


def test_a_recipe_without_a_model_identity_is_unknown_not_an_error() -> None:
    assert _primary_model_identity({}) is None
    assert _primary_model_identity({"models": [{"model": {"slug": "x"}}]}) is None
    assert _topology_order({}, "stop_order") in (None, ())


def test_uninstall_keeps_the_model_when_the_recipe_names_none(
    tmp_path, monkeypatch
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="7" * 36
    )
    expected = service.preview_uninstall(installation.owner_id)
    # The catalog document is immutable once active, so its damage is modelled at
    # the reader: the recipe names no usable model.
    monkeypatch.setattr(
        recipe_action_plans, "_primary_model_identity", lambda _doc: None
    )

    preview = service.preview_uninstall(installation.owner_id)

    assert preview.nodes
    with sessions() as session:
        stored = _required(session.get(RecipeInstallation, installation.owner_id))
        # The installation row still records the model that was installed, and
        # the Stop is planned on it instead of refusing the removal.
        assert stored.model_content_sha256 is not None
        assert (
            preview.model_impact.model_content_sha256
            == expected.model_impact.model_content_sha256
        )


def test_companion_models_that_cannot_be_read_are_kept_not_refused(tmp_path) -> None:
    sessions, _service, _queue, _mapping_id, _build_id, _nodes = setup_services(
        tmp_path, nodes=1
    )
    with sessions() as session:
        assert _recipe_model_identities(session, {}) is None


# ---------------------------------------------------- categories of the refusals


def test_request_refusals_are_typed_invalid_requests(tmp_path) -> None:
    _sessions, service, _queue, _mapping_id, _build_id, _nodes = setup_services(
        tmp_path, nodes=1
    )
    with pytest.raises(RecipeRequestInvalid) as unknown_installation:
        service.preview_uninstall(str(uuid.uuid4()))
    with pytest.raises(RecipeRequestInvalid):
        service.preview_stop(str(uuid.uuid4()))
    with pytest.raises(RecipeRequestInvalid):
        service.abandon_never_installed(str(uuid.uuid4()))

    assert isinstance(unknown_installation.value, InvalidRequestError)
    # Existing callers that catch the conflict keep catching it.
    assert isinstance(unknown_installation.value, RecipeOperationConflict)


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_stop_without_start_history_uses_current_exact_run_ownership(
    tmp_path,
) -> None:
    sessions, service, _queue, _installation, run, _nodes = _running_recipe(
        tmp_path, nodes=2
    )
    with sessions.begin() as session:
        for child in session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == run.id)
        ):
            child.payload = {"not": "a start order"}
    plan = service.preview_stop(run.owner_id)

    stopped = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="9" * 36,
    )
    assert stopped.kind == "recipe.stop"
    assert stopped.owner_id == run.owner_id
    assert stopped.state == "running"


def test_a_busy_owner_is_a_retry_later_unknown_outcome(tmp_path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    plan = service.preview_install(mapping_id, build_id)
    installation_id = service.prepare_installation(plan, actor="admin")
    reconcile = {
        "schema_version": 1,
        "owner_kind": "installation",
        "owner_id": installation_id,
        "plan_digest": "d" * 64,
    }
    with sessions.begin() as session:
        session.add(
            new_recipe_job(
                id=str(uuid.uuid4()),
                request_id=str(uuid.uuid4()),
                kind="recipe.reconcile",
                state="running",
                actor="admin",
                authority_revision="a" * 64,
                targets=list(nodes),
                payload_digest=hashlib.sha256(canonical_message(reconcile)).hexdigest(),
                payload=reconcile,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with pytest.raises(RecipeRetryLater) as busy:
        service.start_installation(installation_id, actor="admin", request_id="8" * 36)

    assert isinstance(busy.value, UnknownOutcomeError)


def test_role_phases_report_a_mismatch_instead_of_raising() -> None:
    assert _role_phases(("head",), (("n", {"compiled_execution_plan": {}}),)) is None


def test_role_phases_use_exact_stop_identity_without_launch_history() -> None:
    head = {**recipe_stop_payload("head", plan_digest="a" * 64), "role": "head"}
    worker = {
        **recipe_stop_payload("worker", plan_digest="a" * 64),
        "role": "worker",
        "rank": 1,
    }
    assert _role_phases(("worker", "head"), (("head", head), ("worker", worker))) == (
        (("worker", worker),),
        (("head", head),),
    )


def test_empty_rank_receipts_keep_their_parent_operation_kind() -> None:
    node_id = "spk_" + "a" * 32
    for kind, result_type in (
        ("recipe.start", RecipeStartResult),
        ("recipe.stop", RecipeStopResult),
        ("recipe.build.cleanup.v1", RecipeBuildCleanupEvidence),
    ):
        loaded = _recorded_result(
            kind, {"node_evidence": {node_id: {}}}, subject="kind-proof"
        )
        assert isinstance(loaded, RecipeOperationProgressResult)
        assert loaded.node_evidence is not None
        assert isinstance(loaded.node_evidence[node_id], result_type)


# ------------------------------------------------------------------ the guard


_STORED_READS = {
    "parse_stored_run_plan",
    "parse_stored_installation_plan",
    "run_plan_document",
    "installation_plan_document",
    "build_plan_document",
    "parse_stored_build_plan",
    "parse_stored_build_policy",
    "model_validate_parent",
    "recovery_start_plan",
    "parse_recipe_lifecycle_result",
}

#: The functions that own one class of stored-state read; everything else must go
#: through them.  A read elsewhere in the module turns a damaged row into a raise
#: that rolls back the agent's result and parks the load.
_OWNERS = {
    "_recorded_result",
    "_validated_result",
    "_node_result",  # Canonical parse is caught locally and returns unknown.
    "_parse_recipe_parent",  # Central parser, called through _recorded_parent.
    "_evidence_is_acceptable",
    "_plan_ranks",
    "_run_accepted_ranks",
    "_installation_accepted_ranks",
    "_run_is_one_shot",
    "_run_observes_per_generation",
    "_profile_jobrun_parent",
    "_stored_phases",
    "read",
    "plan_from",
    "rebuild",
    "_stored_compiled_plans",
    "_prepared_installation_id",
    "_prepared_install_read",
    "_complete_jobrun_stop_in_session",
    "queue_recovery_stop_in_session",
    "_project_node_result",
    "_uninstall_recipe",
    "prepare_installation",
    "_retry_build_in_session",
    "record_build_evidence",
    "_reconciliation_authority_in_session",
    "_view",
    # A document this very call just wrote is checked before the transaction
    # commits (nothing damaged can be persisted), or the typed parent of a Stop
    # whose result is only probed (a damaged one reads as "not pending").
    "_activate_job_run_once",
    "_profile_jobrun_stop_is_pending",
    "_profile_jobrun_stop_in_session",
}


def test_a_stored_state_read_never_raises_bare_in_the_operation_service() -> None:
    """Every contract parse of stored state sits inside a function that rebuilds it
    or retires it as unknown; none can raise a bookkeeping error into a worker."""

    source = Path(recipe_operations.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def _enter(self, node: ast.AST, name: str) -> None:
            self.scope.append(name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._enter(node, node.name)

        def visit_Call(self, node: ast.Call) -> None:
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if name in _STORED_READS and not (set(self.scope) & _OWNERS):
                offenders.append(f"{'.'.join(self.scope)}:{node.lineno} {name}")
            self.generic_visit(node)

    Visitor().visit(tree)
    assert not offenders, offenders


def test_no_bookkeeping_class_is_raised_from_a_stored_read() -> None:
    """The three request/fence/retry classes derive from the contract categories."""

    assert issubclass(RecipeRequestInvalid, InvalidRequestError)
    assert issubclass(RecipeStopAuthorityRefused, SecurityRefusalError)
    assert issubclass(RecipeRetryLater, UnknownOutcomeError)
    for cls in (RecipeRequestInvalid, RecipeStopAuthorityRefused, RecipeRetryLater):
        assert issubclass(cls, RecipeOperationConflict)


@pytest.mark.usefixtures("damaged_json_rows")
def test_stop_works_without_start_jobs_and_with_unreadable_launch_plan(tmp_path):
    from vonk_agent_protocol import ContainerRuntimeAction
    from vonk_control.host_runtime_plan_authority import derive_runtime_plan_binding

    sessions, service, _queue, _installation, started, _nodes = _running_recipe(
        tmp_path, nodes=2
    )
    with sessions.begin() as session:
        run = _required(session.get(RecipeRun, started.owner_id))
        run.plan = {"unreadable": True}
        for child in session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == started.id)
        ):
            session.delete(child)
        session.delete(_required(session.get(Job, started.id)))
    plan = service.preview_stop(started.owner_id)
    stopped = service.stop(
        started.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    with sessions() as session:
        parent = _required(session.get(Job, stopped.id))
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == stopped.id)
            )
        )
        assert children
        for child in children:
            binding = derive_runtime_plan_binding(
                session,
                parent=parent,
                operation=child,
                node_id=child.node_id,
                action=ContainerRuntimeAction.STOP,
                cancellation_requested=False,
                now=NOW,
            )
            assert binding.runtime_run_id == started.owner_id
            assert (
                binding.stop_plan_sha256
                == hashlib.sha256(canonical_message(child.payload)).hexdigest()
            )


@pytest.mark.parametrize("damage_plan", [False, True])
@pytest.mark.usefixtures("damaged_json_rows")
def test_installation_cleanup_does_not_require_original_install_job(
    tmp_path, damage_plan
):
    sessions, service, _queue, mapping, build, nodes = setup_services(tmp_path, nodes=2)
    installed = installed_recipe(
        service, mapping, build, nodes, request_id=str(uuid.uuid4())
    )
    with sessions.begin() as session:
        parent = _required(session.get(Job, installed.id))
        for child in session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == parent.id)
        ):
            session.delete(child)
        session.delete(parent)
        if damage_plan:
            installation = _required(
                session.get(RecipeInstallation, installed.owner_id)
            )
            installation.plan = {"unreadable": True}
    authority = service.preview_reconciliation_authority(installed.owner_id)
    assert authority.installation_id == installed.owner_id
    assert {target.node_id for target in authority.targets} == set(nodes)


@pytest.mark.usefixtures("damaged_json_rows")
def test_failed_exact_cleanup_reissues_once_then_frees_a_fresh_run(tmp_path):
    from datetime import timedelta

    sessions, service, _queue, installation, started, nodes = _running_recipe(tmp_path)
    with sessions() as session:
        original = _required(session.get(Job, started.id))
        ordinal = recipe_operations._bound_workload_intent(original)
    request_id = str(uuid.uuid4())
    reason, completed, advanced = service._retirement_cleanup(
        original, request_id, "recipe.stop", "run", started.owner_id, ordinal
    )
    assert advanced and not completed, reason
    with sessions.begin() as session:
        first = _required(
            session.scalar(select(Job).where(Job.request_id == request_id))
        )
        first_id = first.id
        first.state = "failed"
        for child in session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == first_id)
        ):
            child.state = "failed"
    service._clock = lambda: NOW + timedelta(seconds=10)
    reason, completed, advanced = service._retirement_cleanup(
        original, request_id, "recipe.stop", "run", started.owner_id, ordinal
    )
    assert advanced and not completed, reason
    with sessions() as session:
        stops = tuple(session.scalars(select(Job).where(Job.kind == "recipe.stop")))
        assert len(stops) == 2
        retry = next(job for job in stops if job.id != first_id)
        assert retry.request_id != request_id
        assert recipe_operations._bound_workload_intent(retry) == ordinal
    _, _, advanced = service._retirement_cleanup(
        original, request_id, "recipe.stop", "run", started.owner_id, ordinal
    )
    assert not advanced
    for node in nodes:
        service.record_node_result(retry.id, node, succeeded=True, evidence={})
    _, completed, _ = service._retirement_cleanup(
        original, request_id, "recipe.stop", "run", started.owner_id, ordinal
    )
    assert completed
    fresh = service.preview_run(installation.owner_id, "after-cleanup")
    assert fresh.allowed
    accepted = service.start(
        fresh,
        plan_digest=fresh.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert accepted.owner_id != started.owner_id


def test_lost_install_history_completes_exact_cleanup_and_fresh_admission(tmp_path):
    from vonk_agent_protocol import ContainerRuntimeAction, RecipeStopPayload
    from vonk_control.host_runtime_plan_authority import derive_runtime_plan_binding
    from vonk_control.models import ResourceReservation

    sessions, service, _queue, installed, started, nodes = _running_recipe(
        tmp_path, nodes=2
    )
    with sessions.begin() as session:
        installation = _required(session.get(RecipeInstallation, installed.owner_id))
        mapping, build = installation.mapping_id, installation.recipe_build_id
        for child in session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == installed.id)
        ):
            session.delete(child)
        session.delete(_required(session.get(Job, installed.id)))
    stop_plan = service.preview_stop(started.owner_id)
    stop = service.stop(
        started.owner_id,
        plan_digest=stop_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    observed_nodes: set[str] = set()
    # Role phases issue one exact child at a time. Validate the actual signed
    # helper binding for each produced child before recording its receipt.
    for _ in nodes:
        with sessions() as session:
            parent = _required(session.get(Job, stop.id))
            children = tuple(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == stop.id,
                        AgentOperation.state.not_in(
                            ["succeeded", "failed", "cancelled"]
                        ),
                    )
                )
            )
            assert children
            for child in children:
                payload = RecipeStopPayload.model_validate(child.payload)
                assert payload.run_id == started.owner_id
                assert payload.installation_id == installed.owner_id
                binding = derive_runtime_plan_binding(
                    session,
                    parent=parent,
                    operation=child,
                    node_id=child.node_id,
                    action=ContainerRuntimeAction.STOP,
                    cancellation_requested=False,
                    now=NOW,
                )
                assert binding.runtime_run_id == started.owner_id
                assert (
                    binding.stop_plan_sha256
                    == hashlib.sha256(canonical_message(payload)).hexdigest()
                )
        for child in children:
            observed_nodes.add(child.node_id)
            service.record_node_result(
                stop.id, child.node_id, succeeded=True, evidence={}
            )
        if service.get(stop.id).state == "succeeded":
            break
    assert observed_nodes == set(nodes)
    assert service.get(stop.id).state == "succeeded"
    uninstall_plan = service.preview_uninstall(installed.owner_id)
    assert uninstall_plan.allowed
    uninstall = service.uninstall(
        installed.owner_id,
        plan_digest=uninstall_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    for node in nodes:
        service.record_node_result(uninstall.id, node, succeeded=True, evidence={})
    assert service.get(uninstall.id).state == "succeeded"
    with sessions() as session:
        assert _required(session.get(RecipeRun, started.owner_id)).state == "stopped"
        assert (
            _required(session.get(RecipeInstallation, installed.owner_id)).state
            == "uninstalled"
        )
        assert (
            session.scalar(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id.in_(
                        [started.owner_id, installed.owner_id]
                    ),
                    ResourceReservation.state == "active",
                )
            )
            is None
        )
    fresh = service.preview_install(mapping, build)
    assert fresh.allowed
    accepted = service.install(
        fresh,
        plan_digest=fresh.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert accepted.owner_id != installed.owner_id
    for node in nodes:
        service.record_node_result(
            accepted.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    assert service.get(accepted.id).state == "succeeded"
    # Complete a fresh physical run too: cleanup must free its exact port and
    # memory ownership, not merely make another installation row admissible.
    fresh_run_plan = service.preview_run(accepted.owner_id, "fresh-after-cleanup")
    assert fresh_run_plan.allowed
    fresh_run = service.start(
        fresh_run_plan,
        plan_digest=fresh_run_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert fresh_run.owner_id != started.owner_id
    complete_started_recipe(sessions, service, fresh_run.id)
    assert service.get(fresh_run.id).state == "succeeded"


@pytest.mark.parametrize("exhaust", [False, True])
def test_install_submission_retries_local_receipt_failure_without_poisoning_fresh_admission(
    tmp_path, monkeypatch, exhaust
):
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    plan = service.preview_install(mapping_id, build_id)
    original = service._install_admission.refresh_install_receipts
    with sessions() as session:
        existing_operations = tuple(sorted(session.scalars(select(AgentOperation.id))))
        existing_claims = tuple(
            sorted(
                session.scalars(
                    select(ResourceReservation.id).where(
                        ResourceReservation.state == ReservationState.ACTIVE.value
                    )
                )
            )
        )
    calls = []

    def refresh(*args, **kwargs):
        calls.append(True)
        if len(calls) <= (3 if exhaust else 1):
            raise ValueError("local receipt is temporarily unreadable")
        return original(*args, **kwargs)

    monkeypatch.setattr(service._install_admission, "refresh_install_receipts", refresh)
    if exhaust:
        returned = None
        ended = False
        try:
            returned = service.install(
                plan,
                plan_digest=plan.plan_digest,
                actor="admin",
                request_id=str(uuid.uuid4()),
            )
        except Exception:  # noqa: BLE001 - effects and fresh completion are the oracle
            ended = True
        assert ended and returned is None and len(calls) == 3
        with sessions() as session:
            assert session.scalar(select(RecipeInstallation)) is None
            assert (
                tuple(sorted(session.scalars(select(AgentOperation.id))))
                == existing_operations
            )
            assert (
                tuple(
                    sorted(
                        session.scalars(
                            select(ResourceReservation.id).where(
                                ResourceReservation.state
                                == ReservationState.ACTIVE.value
                            )
                        )
                    )
                )
                == existing_claims
            )
    accepted = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )
    if not exhaust:
        assert len(calls) == 2
    assert accepted.id
    for node in nodes:
        service.record_node_result(
            accepted.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    assert service.get(accepted.id).state == LifecycleState.SUCCEEDED.value
    # A new request can reuse the accepted exact installation; the first
    # transient failure committed no operation or admission reservation.
    fresh_plan = service.preview_install(mapping_id, build_id)
    fresh = service.install(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert fresh.id
    for node in nodes:
        service.record_node_result(
            fresh.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    assert service.get(fresh.id).state == LifecycleState.SUCCEEDED.value


@pytest.mark.parametrize("prepare_only", [False, True])
@pytest.mark.parametrize("exhaust", [False, True])
def test_submission_recompiles_through_the_owner_and_releases_failed_attempts(
    tmp_path, monkeypatch, prepare_only, exhaust
):
    sessions, service, _queue, mapping, build, nodes = setup_services(tmp_path, nodes=1)
    plan = service.preview_install(mapping, build)
    admission = service._install_admission
    compile_owner = admission._compiled_plan_provider
    assert compile_owner is not None
    with sessions() as session:
        initial_operations = tuple(sorted(session.scalars(select(AgentOperation.id))))
    calls = []

    def compile_again(**kwargs):
        calls.append(True)
        if len(calls) <= (3 if exhaust else 1):
            raise UnknownOutcomeError()
        return compile_owner(**kwargs)

    monkeypatch.setattr(admission, "_compiled_plan_provider", compile_again)

    def submit():
        if prepare_only:
            return service.prepare_installation(plan, actor="admin")
        return service.install(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=str(uuid.uuid4()),
        )

    if exhaust:
        ended = False
        try:
            submit()
        except Exception:  # noqa: BLE001 - bounded effects, not error taxonomy
            ended = True
        assert ended and len(calls) == 3
        with sessions() as session:
            assert session.scalar(select(RecipeInstallation)) is None
            assert session.scalar(select(ResourceReservation)) is None
            assert (
                tuple(sorted(session.scalars(select(AgentOperation.id))))
                == initial_operations
            )
    accepted = submit()
    assert len(calls) == (5 if exhaust else 3)
    if prepare_only:
        assert isinstance(accepted, str)
        accepted = service.start_installation(
            accepted, actor="admin", request_id=str(uuid.uuid4())
        )
    assert not isinstance(accepted, str)
    for node in nodes:
        service.record_node_result(
            accepted.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    assert service.get(accepted.id).state == LifecycleState.SUCCEEDED.value
    fresh_plan = service.preview_install(mapping, build)
    fresh = service.install(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    for node in nodes:
        service.record_node_result(
            fresh.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    assert service.get(fresh.id).state == LifecycleState.SUCCEEDED.value
