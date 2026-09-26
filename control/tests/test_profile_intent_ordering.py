"""Accepted profile intent order survives delayed admission and retries."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
)
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_profile_installed_execution import _profile_service
from .test_recipe_operations import setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


def _profile_order_services(tmp_path, engine, *, start: datetime):
    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, engine=engine, nodes=2
    )
    profiles, _planner = _profile_service(sessions, lifecycle)
    clock = [start]
    profiles._clock = lambda: clock[0]
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
    assert revision is not None
    selector = f"{revision.publisher}/{revision.slug}"
    return sessions, profiles, tuple(nodes), selector, clock


def _profile_and_review(
    profiles, selector: str, nodes, name: str, *, desired_state: str = "running"
):
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": name,
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": list(nodes),
                        "desired_state": desired_state,
                    }
                ],
            }
        ),
        actor="admin",
    )
    review = profiles.preview(profile.id)
    assert review.allowed, review.reasons
    assert set(review.scope.node_ids) == set(nodes)
    assert {node for step in review.steps for node in step.node_ids} == set(nodes)
    return profile, review


def _pending(profiles, review, request_key: str):
    return profiles._create_pending_application(
        review,
        request_key=request_key,
        actor="admin",
        operation_kind="fleet-profile.apply",
    )


def _application(sessions, application_id: str) -> FleetProfileApplication:
    with sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        return row


def test_malformed_sibling_retry_root_does_not_block_new_pending_admission(
    tmp_path, postgres_engine
) -> None:
    """Bad retry lineage stays local; a valid newer receipt can be admitted."""
    start = datetime(2026, 9, 4, tzinfo=UTC)
    sessions, profiles, nodes, selector, clock = _profile_order_services(
        tmp_path, postgres_engine, start=start
    )
    _older_profile, older_review = _profile_and_review(
        profiles, selector, nodes, "Damaged older receipt"
    )
    older = _pending(profiles, older_review, str(uuid4()))
    profiles._defer_pending_application(
        older.id, "Waiting for admission", retry_delay=timedelta(0)
    )
    damaged_progress = deepcopy(_application(sessions, older.id).progress)
    intended = damaged_progress["intended_profile"]
    assert isinstance(intended, dict)
    # Keep the progress contract valid while making the referenced root
    # unavailable, as can happen after partial/corrupt historical persistence.
    intended["reviewed_application_id"] = str(uuid4())
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, older.id)
        assert row is not None
        row.progress = damaged_progress

    clock[0] = start + timedelta(seconds=1)
    _newer_profile, newer_review = _profile_and_review(
        profiles, selector, nodes, "Valid newer receipt"
    )
    newer = _pending(profiles, newer_review, str(uuid4()))
    profiles._defer_pending_application(
        newer.id, "Waiting for admission", retry_delay=timedelta(0)
    )

    assert profiles._observe_pending_admissions(clock[0])

    older_after = _application(sessions, older.id)
    newer_after = _application(sessions, newer.id)
    with sessions() as session:
        node = session.get(AgentNode, nodes[0])
    assert node is not None and node.workload_intent_ordinal == 1
    assert older_after.state == "failed"
    assert "review source is unavailable" in (older_after.status_reason or "")
    assert newer_after.state == "queued"
    assert newer_after.progress["admission_pending"] is False
    assert newer_after.progress["workload_intent_ordinal"] == 1


def test_older_unbound_admission_cannot_overtake_newer_bound_deferred_intent(
    tmp_path, postgres_engine
) -> None:
    """A late retry must not turn ordinal=None into authority over newer work."""
    start = datetime(2026, 9, 1, tzinfo=UTC)
    sessions, profiles, nodes, selector, clock = _profile_order_services(
        tmp_path, postgres_engine, start=start
    )
    _older_profile, older_review = _profile_and_review(
        profiles, selector, nodes, "Older parked intent"
    )
    older = _pending(profiles, older_review, str(uuid4()))
    profiles._defer_pending_application(
        older.id, "Waiting for later admission", retry_delay=timedelta(seconds=3)
    )

    clock[0] = start + timedelta(seconds=1)
    newer_profile, newer_review = _profile_and_review(
        profiles, selector, nodes, "Newer accepted intent"
    )
    newer = _pending(profiles, newer_review, str(uuid4()))
    profiles._defer_pending_application(
        newer.id, "Waiting for later admission", retry_delay=timedelta(seconds=1)
    )

    # Preparing the two-node workload can bind its ordinal, but the subsequent
    # admission transaction can still lose a profile-row NOWAIT lock. This
    # mirrors accepted pending work whose effects are fenced before full queue
    # admission succeeds.
    blocker = sessions()
    try:
        blocker.execute(
            text("SELECT id FROM fleet_profiles WHERE id = :profile_id FOR UPDATE"),
            {"profile_id": newer_profile.id},
        )
        clock[0] = start + timedelta(seconds=2)
        assert profiles._observe_pending_admissions(clock[0])
    finally:
        blocker.rollback()
        blocker.close()

    newer_after_contention = _application(sessions, newer.id)
    assert newer_after_contention.progress["admission_pending"] is True
    assert newer_after_contention.progress["workload_intent_ordinal"] == 1
    assert newer_after_contention.state == "waiting-for-operator"
    with sessions() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None and node.workload_intent_ordinal == 1

    # The first observer deferred the newer receipt on the queue lock and may
    # already have retired the older unbound row while binding its ordinal.
    # Make the older deadline pass before the newer retry, and verify the
    # observer has no eligible older receipt left to overtake it.
    profiles._defer_pending_application(
        older.id, "Older intent remains pending", retry_delay=timedelta(seconds=1)
    )
    clock[0] = start + timedelta(seconds=3)
    assert not profiles._observe_pending_admissions(clock[0])

    older_after_retry = _application(sessions, older.id)
    newer_after_retry = _application(sessions, newer.id)
    with sessions() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        assert node.workload_intent_ordinal == 1
    assert newer_after_retry.state != "cancelled"
    assert newer_after_retry.progress["workload_intent_ordinal"] == 1
    if newer_after_retry.progress["admission_pending"]:
        assert newer_after_retry.state == "waiting-for-operator"
    else:
        assert newer_after_retry.state == "queued"
    assert older_after_retry.progress["workload_intent_ordinal"] is None
    assert older_after_retry.state == "cancelled"


def test_equal_acceptance_time_uses_deterministic_uuid4_order(
    tmp_path, postgres_engine, monkeypatch
) -> None:
    """For tied acceptance clocks, the higher UUID4 receipt owns the order."""
    import vonk_control.fleet_profiles as fleet_profiles_module

    start = datetime(2026, 9, 2, tzinfo=UTC)
    sessions, profiles, nodes, selector, clock = _profile_order_services(
        tmp_path, postgres_engine, start=start
    )
    _older_profile, older_review = _profile_and_review(
        profiles, selector, nodes, "Lower UUID accepted first"
    )
    newer_profile, newer_review = _profile_and_review(
        profiles, selector, nodes, "Higher UUID accepted second"
    )
    lower_id = UUID("00000000-0000-4000-8000-000000000010")
    higher_id = UUID("00000000-0000-4000-8000-000000000020")
    generated_ids = iter((lower_id, higher_id))
    monkeypatch.setattr(
        fleet_profiles_module.uuid, "uuid4", lambda: next(generated_ids)
    )
    lower = _pending(profiles, older_review, str(uuid4()))
    higher = _pending(profiles, newer_review, str(uuid4()))
    assert lower.id == str(lower_id)
    assert higher.id == str(higher_id)
    assert lower.created_at == higher.created_at

    profiles._defer_pending_application(
        lower.id, "Waiting for later admission", retry_delay=timedelta(seconds=3)
    )
    profiles._defer_pending_application(
        higher.id, "Waiting for later admission", retry_delay=timedelta(seconds=1)
    )
    blocker = sessions()
    try:
        blocker.execute(
            text("SELECT id FROM fleet_profiles WHERE id = :profile_id FOR UPDATE"),
            {"profile_id": newer_profile.id},
        )
        clock[0] = start + timedelta(seconds=2)
        assert profiles._observe_pending_admissions(clock[0])
    finally:
        blocker.rollback()
        blocker.close()

    bound = _application(sessions, higher.id)
    assert bound.progress["admission_pending"] is True
    assert bound.progress["workload_intent_ordinal"] == 1
    profiles._defer_pending_application(
        lower.id, "Lower UUID remains pending", retry_delay=timedelta(seconds=1)
    )
    clock[0] = start + timedelta(seconds=3)
    assert not profiles._observe_pending_admissions(clock[0])

    lower_after_retry = _application(sessions, lower.id)
    higher_after_retry = _application(sessions, higher.id)
    with sessions() as session:
        node = session.get(AgentNode, nodes[0])
    assert node is not None and node.workload_intent_ordinal == 1
    assert lower_after_retry.progress["workload_intent_ordinal"] is None
    assert lower_after_retry.state == "cancelled"
    assert higher_after_retry.state != "cancelled"
    assert higher_after_retry.progress["workload_intent_ordinal"] == 1
    if higher_after_retry.progress["admission_pending"]:
        assert higher_after_retry.state == "waiting-for-operator"
    else:
        assert higher_after_retry.state == "queued"


def test_direct_queue_of_older_pending_receipt_cannot_replace_terminal_newer_intent(
    tmp_path, postgres_engine
) -> None:
    """A later accepted receipt remains authoritative after its terminal state."""
    start = datetime(2026, 9, 3, tzinfo=UTC)
    sessions, profiles, nodes, selector, clock = _profile_order_services(
        tmp_path, postgres_engine, start=start
    )
    clock[0] = start + timedelta(seconds=1)
    _newer_profile, newer_review = _profile_and_review(
        profiles, selector, nodes, "Later accepted terminal receipt"
    )
    newer = _pending(profiles, newer_review, str(uuid4()))
    profiles._prepare_pending_admission(newer.id, newer_review)
    bound_newer = _application(sessions, newer.id)
    assert bound_newer.progress["workload_intent_ordinal"] == 1
    profiles._finish_pending_admission(
        newer.id, state="failed", reason="A later terminal receipt still owns order"
    )

    # Simulate an older request whose submitter captured its acceptance time
    # before its insert committed, while the newer request already reached a
    # terminal receipt. The old transaction must still reconcile by accepted
    # order rather than row insertion time.
    clock[0] = start
    _older_profile, older_review = _profile_and_review(
        profiles, selector, nodes, "Older delayed insert"
    )
    older = _pending(profiles, older_review, str(uuid4()))

    # Deliberately use the normal queue boundary on the older persisted review.
    # The owner must compare accepted order before it can bump the node ordinal
    # or issue cancellation for the newer receipt.
    try:
        profiles._queue_application(
            older_review,
            request_key=older.request_key,
            actor="admin",
            operation_kind="fleet-profile.apply",
            pending_application_id=older.id,
        )
    except FleetProfileConflict:
        pass

    newer_after = _application(sessions, newer.id)
    older_after = _application(sessions, older.id)
    with sessions() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None and node.workload_intent_ordinal == 1
    assert newer_after.state == "failed"
    assert newer_after.progress["workload_intent_ordinal"] == 1
    assert older_after.progress["workload_intent_ordinal"] is None
    assert older_after.progress["admission_pending"] is True or older_after.state in {
        "cancelled",
        "failed",
    }


def test_retry_lineage_cannot_make_an_older_root_newer_than_pending_intent(
    tmp_path, postgres_engine
) -> None:
    """A retry keeps its root's order even when its new receipt is created later."""
    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, engine=postgres_engine, nodes=2
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
    profiles = build_production_fleet_profile_service(
        sessions,
        clock=lifecycle._clock,
        run_switch_operations=run_switch,
    )
    root_profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Older root to retry",
                "assignments": [
                    {
                        "recipe_selector": f"{revision.publisher}/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                    }
                ],
            }
        ),
        actor="admin",
    )
    root_review = profiles.preview(root_profile.id)
    assert root_review.allowed, root_review.reasons
    root = profiles.apply(
        root_profile.id,
        plan_digest=root_review.plan_digest,
        request_key=str(uuid4()),
        actor="admin",
    )
    assert root.progress.workload_intent_ordinal == 1
    assert profiles.tick()
    with sessions.begin() as session:
        child = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert child is not None
        child.state = "failed"
        child.status_reason = "temporary runtime dependency unavailable"
    assert profiles.tick()
    root = profiles.application(root.id)
    assert root.state == "failed"
    # A pending later receipt on another profile still supersedes overlapping
    # workload effects, despite not changing the root's node ordinal yet.
    clock = [root.created_at + timedelta(seconds=1)]
    profiles._clock = lambda: clock[0]
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
    assert revision is not None
    selector = f"{revision.publisher}/{revision.slug}"
    _later_profile, later_review = _profile_and_review(
        profiles,
        selector,
        nodes,
        "Later accepted pending intent",
        desired_state="installed",
    )
    later = _pending(profiles, later_review, str(uuid4()))

    assert not profiles.retry_eligible(root.id)
    retry: object | None = None
    try:
        retry = profiles.retry(root.id, request_key=str(uuid4()), actor="admin")
    except FleetProfileConflict as error:
        assert "supersed" in str(error).lower(), str(error)

    later_after = _application(sessions, later.id)
    with sessions() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        assert node.workload_intent_ordinal == root.progress.workload_intent_ordinal
        retry_rows = tuple(
            session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.id != root.id,
                    FleetProfileApplication.progress[
                        "retry_of_application_id"
                    ].as_string()
                    == root.id,
                )
            )
        )
    assert later_after.progress["admission_pending"] is True
    assert later_after.progress["workload_intent_ordinal"] is None
    assert retry is None
    assert not retry_rows
    assert later_after.state == "queued"
