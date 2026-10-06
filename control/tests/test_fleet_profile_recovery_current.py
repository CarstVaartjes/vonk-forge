"""Profile retry and replay through the production Run/Switch child."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, canonical_message
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileInput,
)
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
    _persisted_profile_progress,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    Job,
)
from vonk_control.operation_blockers import make_blocker
from vonk_control.recovery_policy import RecoveryPolicy
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_fleet_profiles import _uuid
from .test_recipe_operations import setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


def _failed_profile(tmp_path: Path):
    """Fail a persisted Run/Switch child, leaving its parent recoverable."""

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
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
    service = build_production_fleet_profile_service(
        sessions,
        clock=lifecycle._clock,
        run_switch_operations=run_switch,
    )
    desired = FleetProfileInput.model_validate(
        {
            "name": "Recover failed child",
            "assignments": [
                {
                    "recipe_selector": f"vonk-forge/{revision.slug}",
                    "spark_ids": list(nodes),
                    "desired_state": "running",
                    "assignment_name": "recover-chat",
                }
            ],
        }
    )
    profile = service.create(desired, actor="admin")
    preview = service.preview(profile.id)
    assert preview.allowed
    application = service.apply(
        profile.id,
        request_key=_uuid(800),
        actor="admin",
    )
    assert service.tick()
    with sessions.begin() as session:
        child = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert child is not None
        child_id = child.id
        child.state = "failed"
        child.status_reason = "temporary runtime dependency unavailable"
    assert service.tick()
    # The Controller retries this by itself, so it is waiting, not failed.
    assert service.application(application.id).state == "queued"
    return sessions, lifecycle, service, profile, desired, application, child_id, nodes


def test_explicit_retry_preserves_failed_child_recovery_and_replay(
    tmp_path: Path,
) -> None:
    sessions, _lifecycle, service, _profile, _desired, first, first_child, _nodes = (
        _failed_profile(tmp_path)
    )
    failed = service.application(first.id)
    assert service.retry_eligible(first.id)

    second = service.retry(first.id, request_key=_uuid(801), actor="admin")
    assert second.retry_of_application_id == first.id
    assert second.attempt == 2
    assert service.application_by_request_key(_uuid(801), actor="admin") == second

    assert not service.retry_eligible(first.id)
    with pytest.raises(FleetProfileConflict, match="superseded"):
        service.retry(first.id, request_key=_uuid(802), actor="admin")

    assert service.tick()
    with sessions() as session:
        children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        )
    assert len(children) == 2
    assert first_child in {child.id for child in children}
    # Superseded by its retry, the first receipt now stays failed; nothing else
    # about it changed.
    after = service.application(first.id)
    assert after.state == "failed"
    assert (
        after.model_copy(
            update={"state": "queued", "next_attempt_at": failed.next_attempt_at}
        )
        == failed
    )

    second_child_id = next(child.id for child in children if child.id != first_child)
    with sessions.begin() as session:
        second_child = session.get(Job, second_child_id)
        assert second_child is not None
        second_child.state = "failed"
        second_child.status_reason = "dependency still unavailable"
    assert service.tick()
    third = service.retry(second.id, request_key=_uuid(807), actor="admin")
    assert third.retry_of_application_id == second.id
    assert third.attempt == 3


def test_recovery_reconciles_active_or_missing_child_without_operator_gate(
    tmp_path: Path,
) -> None:
    sessions, _lifecycle, service, _profile, _desired, first, child_id, _nodes = (
        _failed_profile(tmp_path)
    )

    def recover():
        return service.retry(first.id, request_key=_uuid(803), actor="admin")

    with sessions.begin() as session:
        child = session.get(Job, child_id)
        assert child is not None
        child.state = "running"
        child.status_reason = None
    active_recovery = recover()
    assert active_recovery.id != first.id
    assert active_recovery.state in {"queued", "running", "succeeded"}

    missing_child_path = tmp_path / "missing-child"
    missing_child_path.mkdir()
    sessions, _lifecycle, service, _profile, _desired, first, child_id, _nodes = (
        _failed_profile(missing_child_path)
    )
    with sessions.begin() as session:
        child = session.get(Job, child_id)
        assert child is not None
        session.delete(child)
    missing_recovery = service.retry(first.id, request_key=_uuid(813), actor="admin")
    assert missing_recovery.id != first.id
    assert missing_recovery.state in {"queued", "running", "succeeded"}


def test_retry_rejects_revoked_scope_but_uses_accepted_profile_snapshot(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, service, profile, desired, first, _child, nodes = (
        _failed_profile(tmp_path)
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.revoked_at = lifecycle._clock()
    # The fleet scope no longer matches the accepted intent: the recovery is not
    # refused, it is declined with the receipt returned (nothing new is issued).
    declined = service.retry(first.id, request_key=_uuid(804), actor="admin")
    assert declined.id == first.id
    assert declined.retry_of_application_id is None
    assert "scope changed" in (declined.status_reason or "")

    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.revoked_at = None
    changed_assignment = desired.assignments[0].model_copy(
        update={"assignment_name": "different-chat"}
    )
    service.update(
        profile.id,
        desired.model_copy(
            update={
                "assignments": [changed_assignment],
                "expected_revision": profile.revision,
            }
        ),
        actor="admin",
    )
    assert service.retry_eligible(first.id)
    retry = service.retry(first.id, request_key=_uuid(804), actor="admin")
    assert retry.retry_of_application_id == first.id
    assert retry.profile_digest == first.profile_digest


def test_repeated_idle_loads_preserve_distinct_noop_receipts(tmp_path: Path) -> None:
    _sessions, _lifecycle, service, _profile, _desired, _first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    idle = service.create(
        FleetProfileInput(name="Keep cached", installation_policy="keep-cached"),
        actor="admin",
    )
    first = service.load(
        idle.number,
        request_key=_uuid(805),
        actor="admin",
    )
    second = service.load(
        idle.number,
        request_key=_uuid(806),
        actor="admin",
    )
    assert first.state == second.state == "succeeded"
    assert first.id != second.id
    assert first.result is not None and first.result.changed is False
    assert second.result is not None and second.result.changed is False
    assert (
        service.load(
            idle.number,
            request_key=_uuid(806),
            actor="admin",
        )
        == second
    )


def test_intent_checks_do_not_resolve_storage_inside_coordination(tmp_path: Path):
    sessions, _lifecycle, service, profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )

    def unavailable_cache(**_kwargs):
        raise AssertionError("intent coordination must not consult artifact storage")

    service._cache_resolver = unavailable_cache
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, first.id)
        assert application is not None
        progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(application.progress), strict=True
        )
        assert not service._superseding_intent(session, application, progress)
        assert service._retry_eligible(session, application)
        saved = session.get(FleetProfile, profile.id)
        assert saved is not None
        saved.name = "New saved intent"
        saved.revision += 1
        assert not service._superseding_intent(session, application, progress)
        assert service._retry_eligible(session, application)


def _park_exhausted_application(
    sessions, application, child_id, nodes, *, attempt: int
):
    """Re-park a real Run/Switch child with one bounded exhausted operation.

    The child and its application are left exactly as a spent retry budget
    leaves them: parked, non-terminal, and still holding the node until an
    operator decision or a newer intent replaces them.
    """

    parked_id = str(uuid.uuid4())
    with sessions.begin() as session:
        child = session.get(Job, child_id)
        assert child is not None
        child.state = "waiting-for-operator"
        child.status_reason = "interrupted before the exact effect was observed"
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "waiting-for-operator"
        row.status_reason = "interrupted before the exact effect was observed"
        session.add(
            AgentOperation(
                id=parked_id,
                parent_job_id=child_id,
                node_id=nodes[0],
                kind="recipe.start",
                payload_digest="a" * 64,
                payload={},
                authority_revision="b" * 64,
                state="waiting-for-operator",
                status_reason=(
                    f"exact recipe.start retry budget exhausted at attempt {attempt}"
                ),
                current_attempt=attempt,
                created_at=datetime(2020, 1, 1, tzinfo=UTC),
                updated_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
    return parked_id


def test_new_intent_supersedes_a_parked_exhausted_application(tmp_path: Path) -> None:
    """A newer authorized load replaces a parked order instead of queueing behind it."""

    sessions, _lifecycle, service, profile, _desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    _park_exhausted_application(
        sessions,
        first,
        child_id,
        nodes,
        attempt=RecoveryPolicy().max_failures,
    )

    second = service.load(
        profile.number,
        request_key=_uuid(810),
        actor="admin",
    )

    assert second.id != first.id
    assert second.state == "queued"
    ended = service.application(first.id)
    assert ended.state == "superseded"
    assert "replaced by a later scoped intent" in (ended.status_reason or "")


def test_a_changed_saved_draft_does_not_cancel_accepted_parked_application(
    tmp_path: Path,
) -> None:
    """A parked order is revisited once the saved profile it bound has changed.

    The parked state owns no live step, so the advancement path never sees it.
    Without observing it, an edit to the saved profile would leave the order
    waiting forever even though its recorded intent is already obsolete.
    """

    sessions, _lifecycle, service, profile, desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    _park_exhausted_application(
        sessions,
        first,
        child_id,
        nodes,
        attempt=RecoveryPolicy().max_failures,
    )
    changed = desired.assignments[0].model_copy(
        update={"assignment_name": "different-chat"}
    )
    service.update(
        profile.id,
        desired.model_copy(
            update={"assignments": [changed], "expected_revision": profile.revision}
        ),
        actor="admin",
    )

    assert service.tick() is True

    ended = service.application(first.id)
    assert ended.state in {"cancelled", "failed", "queued", "running"}
    assert ended.state != LifecycleState.NEEDS_OPERATOR
    assert ended.status_reason


def test_pending_admission_is_not_cancelled_by_parked_child_observer(
    tmp_path: Path,
) -> None:
    """Admission retry owns parked rows until a workload intent is bound."""

    sessions, lifecycle, service, _profile, _desired, application, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(row.progress), strict=True
        )
        progress_data = progress.model_dump(mode="json")
        progress_data["admission_pending"] = True
        progress_data["admission_attempt"] = 1
        progress_data["admission_retry_at"] = (
            lifecycle._clock() + timedelta(hours=1)
        ).isoformat()
        progress_data["workload_intent_ordinal"] = None
        row.progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(progress_data), strict=True
        ).model_dump(mode="json")
        row.state = "waiting-for-operator"
        row.status_reason = "Profile admission is waiting for the active workload owner"

    # No action exists for a legacy wait: the first tick heals it (it returns to the
    # queue and retries its admission), and admission retry still owns it - the
    # parked-child observer never cancels it.
    assert service.tick() is True
    parked = service.application(application.id)
    assert parked.state == "queued"
    assert parked.progress.admission_pending is True
    assert parked.progress.workload_intent_ordinal is None
    assert service.tick() is False


def test_exhausted_profile_retry_keeps_an_automatic_due_time(
    tmp_path: Path,
) -> None:
    """Automatic retry has a due time and does not stop at the old retry cap."""

    sessions, lifecycle, service, _profile, _desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    _park_exhausted_application(
        sessions,
        first,
        child_id,
        nodes,
        attempt=RecoveryPolicy().max_failures + 10,
    )
    due = lifecycle._clock() + timedelta(minutes=1)
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(row.progress), strict=True
        ).model_dump(mode="json")
        progress["retry_due_at"] = due.isoformat()
        row.progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(progress), strict=True
        ).model_dump(mode="json")

    assert service._automatic_profile_recovery(lifecycle._clock()) is None
    candidate = service._automatic_profile_recovery(due + timedelta(seconds=1))
    assert candidate == (first.id, "admin")
    # Scheduling recovery does not rewrite the parked row into a new state.
    assert service.application(first.id).state == LifecycleState.NEEDS_OPERATOR


def test_retry_of_a_failed_order_with_a_live_child_resumes_it(tmp_path: Path) -> None:
    """A failed order whose child is still live resumes instead of refusing.

    Live regression: a transient error while advancing a live Run/Switch child
    marked the profile load failed after it had stopped the old run; every
    automatic retry then refused with "Current child operation is still
    active" and the new run was never started.
    """

    from vonk_control.models import FleetProfileApplication

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
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
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Resume live child",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "resume-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed
    application = service.apply(
        profile.id,
        request_key=_uuid(820),
        actor="admin",
    )
    assert service.tick()
    with sessions() as session:
        child = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert child is not None and child.state in {"queued", "running"}
    # An advance error failed the order while its child stayed live.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None and row.current_operation_id is not None
        row.state = "failed"
        row.status_reason = "transient advance failure"

    resumed = service.retry(application.id, request_key=_uuid(821), actor="admin")

    assert resumed.id == application.id
    assert resumed.state == "running"
    with sessions() as session:
        rows = tuple(session.scalars(select(FleetProfileApplication)))
        assert len(rows) == 1


def test_a_retrying_application_reports_waiting_with_its_blockers(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    from sqlalchemy import delete
    from vonk_control.models import NodeInventorySnapshot

    sessions, lifecycle, service, _profile, _desired, application, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    # Current Fleet conditions now block the retry: no Spark has fresh inventory.
    with sessions.begin() as session:
        session.execute(delete(NodeInventorySnapshot))
    later = lifecycle._clock() + timedelta(seconds=120)
    service._clock = lambda: later

    for _ in range(4):
        service.tick()

    view = service.application(application.id)
    assert view.state == "queued"  # it will retry by itself: waiting, not failed
    assert view.blockers, "the real reasons are persisted on the application"
    assert all(item.code and item.detail for item in view.blockers)
    assert view.next_attempt_at is not None
    assert "next attempt at" in (view.status_reason or "")
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None and row.state == "failed"  # stored; presented queued


def test_a_failed_application_shown_as_queued_can_be_cancelled(
    tmp_path: Path,
) -> None:
    """Whatever is presented as waiting to retry must be cancellable.

    A failed application the Controller will retry is shown as queued. It used
    to answer "not cancellable" because its stored state was failed, leaving an
    operator no way to stop a retry that could never succeed.
    """

    _sessions, _lifecycle, service, profile, _desired, application, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    shown = service.application(application.id)
    assert shown.state == "queued"
    assert shown.next_attempt_at is not None  # a retry is named, never absent

    accepted = service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=_uuid(840),
        actor="admin",
    )
    assert accepted.cancellation is not None
    for _ in range(6):
        if service.application(application.id).state == "cancelled":
            break
        service.tick()
    cancelled = service.application(application.id)
    assert cancelled.state == "cancelled"
    assert cancelled.cancellation is not None
    assert cancelled.cancellation.state == "cancelled"
    # The retry is stopped for good, and the same request replays as received.
    service.tick()
    replay = service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=_uuid(840),
        actor="admin",
    )
    assert replay.state == "cancelled"


def test_cancelling_a_failed_order_that_still_owns_a_live_child_cancels_the_child(
    tmp_path: Path,
) -> None:
    """Cancelling stops the retry and reconciles the child the order still owns."""

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
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
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Cancel live child",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "cancel-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    application = service.apply(profile.id, request_key=_uuid(841), actor="admin")
    assert service.tick()
    with sessions() as session:
        child = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert child is not None and child.state in {"queued", "running"}
        child_id = child.id
    # An advance error failed the order while its child stayed live.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None and row.current_operation_id is not None
        row.state = "failed"
        row.status_reason = "transient advance failure"
    assert service.application(application.id).state == "queued"

    service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=_uuid(842),
        actor="admin",
    )

    # Not finished while the child it owns is still being cancelled.
    with sessions() as session:
        child = session.get(Job, child_id)
        assert child is not None
        assert isinstance(child.result, dict)
        assert child.result.get("cancellation") is not None or child.state in {
            "cancelled",
            "failed",
        }
    assert service.application(application.id).state != "failed"


def test_a_saved_choice_the_recipe_no_longer_offers_never_blocks_the_load(
    tmp_path: Path,
) -> None:
    """A newer revision may drop an option a saved profile still carries.

    The saved choice is replaced by the recipe default and the load is accepted,
    instead of failing the profile for an option nobody can offer any more.
    """

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
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
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Stale thinking choice",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "stale-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfile, profile.id)
        assert row is not None
        row.assignments = [
            {**item, "option_choices": {"thinking": "thinking"}}
            for item in row.assignments
        ]

    preview = service.preview(profile.id)
    assert preview.allowed
    assert all(
        "thinking" not in assignment.option_choices
        for assignment in preview.resolved_assignments
    )
    application = service.apply(profile.id, request_key=_uuid(850), actor="admin")
    assert application.state != "failed"


def test_profile_load_of_an_editorial_successor_accepts_the_reused_build(
    tmp_path: Path,
) -> None:
    """The build a successor revision reuses is its dependency, not a mismatch.

    A republished recipe whose executable inputs are unchanged reuses the
    original revision's image build; the build still names the original
    revision. Accepting a profile load for it refused with "accepted build
    consumer identity changed". An image is its content, not a revision's.
    """

    import copy

    from vonk_control.models import RecipeBuild
    from vonk_control.recipe_builds import RecipeBuildPlan
    from vonk_forge_contracts import RecipeDefinition, document_sha256

    sessions, lifecycle, _queue, _mapping, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    now = lifecycle._clock()
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        original = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert original is not None
        document = copy.deepcopy(original.document)
        metadata = document["metadata"]
        assert isinstance(metadata, dict)
        metadata["title"] = "Editorially renamed recipe"
        canonical = RecipeDefinition.model_validate(document)
        successor = CatalogDocumentRevision(
            id=str(uuid.uuid4()),
            document_id=original.document_id,
            kind=original.kind,
            publisher=original.publisher,
            slug=original.slug,
            revision_number=original.revision_number + 1,
            schema_version=2,
            state="active",
            document=canonical.model_dump(mode="json"),
            content_digest=document_sha256(canonical.model_dump(mode="json")),
            artifact_key=original.artifact_key,
            execution_key=original.execution_key,
            projected=copy.deepcopy(original.projected),
            created_by="test",
            created_at=now,
        )
        session.add(successor)
        session.flush()
        successor_id = successor.id
        slug = successor.slug
        reused_plan = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=successor_id,
            recipe_content_sha256=document_sha256(canonical.model_dump(mode="json")),
            builder_node_id=build.builder_node_id,
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload=dict(build.plan),
            policy_report=dict(build.policy_report),
        )
    lifecycle.preview_build = lambda *_args, **_kwargs: reused_plan
    lifecycle.reusable_build_id = lambda *_args, **_kwargs: build_id
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Successor load",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "successor-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed
    assert {item.runtime_image.build_id for item in preview.preparation_decisions} == {
        build_id
    }

    application = service.apply(profile.id, request_key=_uuid(860), actor="admin")
    assert application.state != "failed"


def test_load_with_a_missing_image_requests_preparation_and_continues_when_ready(
    tmp_path: Path,
) -> None:
    from vonk_control.models import RecipeBuild
    from vonk_control.operation_blockers import make_blocker

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
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
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    requested: list[str] = []

    def prepare(recipe_revision_id: str, *, actor: str):
        requested.append(recipe_revision_id)
        return [
            make_blocker(
                "recipe_image.preparing",
                "Preparing the model and runtime image (prepare).",
                severity="info",
            )
        ]

    service.bind_preparation_starter(prepare)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Fresh fleet",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "fresh-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    # A fresh fleet: no runtime image has been built yet.
    with sessions.begin() as session:
        for build in session.scalars(select(RecipeBuild)):
            build.state = "failed"

    review = service.preview(profile.id)
    assert not review.allowed
    assert review.waits_for_preparation
    assert [step.kind for step in review.preparation_steps] == ["prepare", "prepare"]

    application = service.apply(profile.id, request_key=_uuid(901), actor="admin")

    assert requested == [revision.id]  # the load asked for what it needs
    assert application.state == "queued"  # waiting, not blocked or failed
    assert {item.code for item in application.blockers} >= {"recipe_image.preparing"}

    # The preparation finishes; the same application continues by itself.
    with sessions.begin() as session:
        for build in session.scalars(select(RecipeBuild)):
            build.state = "succeeded"
    now = [lifecycle._clock()]
    service._clock = lambda: now[0]
    for _ in range(6):
        now[0] += timedelta(seconds=30)
        service.tick()
    assert service.application(application.id).state == "running"


def test_cancelling_a_queued_load_waiting_for_preparation_always_succeeds(
    tmp_path: Path,
) -> None:
    """A waiting load issued no workload effect: cancel leaves the workload alone."""

    from vonk_control.models import AgentNode, RecipeBuild, ResourceReservation
    from vonk_control.operation_blockers import make_blocker

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
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
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    service.bind_preparation_starter(
        lambda recipe_revision_id, *, actor: [
            make_blocker(
                "recipe_image.preparing",
                "Preparing the model and runtime image (prepare).",
                severity="info",
            )
        ]
    )
    cancelled_preparations: list[str] = []

    def cancel_preparation(
        recipe_revision_id: str, *, actor: str, reason: str
    ) -> tuple[str, ...]:
        cancelled_preparations.append(recipe_revision_id)
        return ("prep-1",)

    service.bind_preparation_canceller(cancel_preparation)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Reload waiting",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "reload-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    with sessions.begin() as session:
        for build in session.scalars(select(RecipeBuild)):
            build.state = "failed"
    application = service.apply(profile.id, request_key=_uuid(930), actor="admin")
    assert application.state == "queued"

    def workload_state() -> tuple[tuple[str, int], ...]:
        with sessions() as session:
            return tuple(
                (node.node_id, node.workload_intent_ordinal)
                for node in session.scalars(
                    select(AgentNode).order_by(AgentNode.node_id)
                )
            )

    before = workload_state()
    cancelled = service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=_uuid(931),
        actor="admin",
    )

    assert cancelled.state == "cancelled"
    assert cancelled.cancellation is not None
    assert cancelled.cancellation.state == "cancelled"
    assert "running workload was not touched" in (cancelled.status_reason or "")
    assert "prep-1" in (cancelled.status_reason or "")
    assert cancelled_preparations == [revision.id]
    assert workload_state() == before
    with sessions() as session:
        leftovers = session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "fleet-profile",
                ResourceReservation.owner_id == application.id,
                ResourceReservation.state.in_(("active", "promised")),
            )
        ).all()
    assert leftovers == []
    # The same request key is an idempotent replay.
    again = service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=_uuid(931),
        actor="admin",
    )
    assert again.state == "cancelled"


@pytest.mark.usefixtures("damaged_json_rows")
def test_waiting_load_follows_a_newer_recipe_revision_instead_of_failing(
    tmp_path: Path,
) -> None:
    """A load still waiting for preparation re-plans on the newest revision."""

    import copy

    from vonk_control.models import RecipeBuild
    from vonk_control.operation_blockers import make_blocker
    from vonk_forge_contracts import document_sha256

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
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
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    requested: list[str] = []

    def prepare(recipe_revision_id: str, *, actor: str):
        requested.append(recipe_revision_id)
        return [
            make_blocker(
                "recipe_image.preparing",
                "Preparing the model and runtime image (prepare).",
                severity="info",
            )
        ]

    service.bind_preparation_starter(prepare)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Follow newest",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "follow-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    with sessions.begin() as session:
        for build in session.scalars(select(RecipeBuild)):
            build.state = "failed"
    application = service.apply(profile.id, request_key=_uuid(910), actor="admin")
    assert application.state == "queued"
    assert requested == [revision.id]

    # While it waits, the recipe gets a newer revision.
    with sessions.begin() as session:
        old = session.get(CatalogDocumentRevision, revision.id)
        assert old is not None
        document = copy.deepcopy(old.document)
        metadata = document["metadata"]
        assert isinstance(metadata, dict)
        metadata["summary"] = "A newer synced revision"
        newer = CatalogDocumentRevision(
            document_id=old.document_id,
            kind=old.kind,
            publisher=old.publisher,
            slug=old.slug,
            revision_number=old.revision_number + 1,
            schema_version=old.schema_version,
            state="active",
            document=document,
            content_digest=document_sha256(document),
            projected=old.projected,
            execution_key=old.execution_key,
            artifact_key=old.artifact_key,
            created_by="admin",
            created_at=old.created_at,
        )
        session.add(newer)
        session.flush()
        newer_id = newer.id

    now = [lifecycle._clock()]
    service._clock = lambda: now[0]
    now[0] += timedelta(seconds=30)
    service.tick()
    waiting = service.application(application.id)
    assert waiting.state == "queued", waiting.status_reason
    assert "re-planned" in (waiting.status_reason or "")
    assert newer_id in requested  # the new revision's preparation is enqueued

    with sessions.begin() as session:
        for build in tuple(session.scalars(select(RecipeBuild))):
            build.state = "succeeded"
            if build.recipe_revision_id == revision.id:
                # The image of the newest revision is built.
                session.add(
                    RecipeBuild(
                        **{
                            column.key: getattr(build, column.key)
                            for column in RecipeBuild.__table__.columns
                            if column.key not in {"id", "recipe_revision_id"}
                        },
                        recipe_revision_id=newer_id,
                    )
                )
    for _ in range(6):
        now[0] += timedelta(seconds=30)
        service.tick()
    final = service.application(application.id)
    assert final.state == "running", final.status_reason
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        intended = FleetProfileApplicationProgress.model_validate(
            row.progress
        ).intended_profile
        assert intended is not None
        assert {item.recipe_revision_id for item in intended.assignments} == {newer_id}


def test_an_automatic_retry_supersedes_its_predecessor_instead_of_failing_it(
    tmp_path: Path,
) -> None:
    """The Controller's own retry ends the tracked application ``superseded``.

    A client following the original id must see the successor, never a failure
    with the continuation lost; the earlier failure stays in the reason.
    """

    sessions, lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    later = lifecycle._clock() + timedelta(hours=2)
    service._clock = lambda: later
    for _ in range(6):
        service.tick()
    with sessions() as session:
        successors = tuple(
            session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.id != first.id
                )
            )
        )
    assert len(successors) == 1
    ended = service.application(first.id)
    assert ended.state == "superseded"
    assert ended.reason_code == "superseded-by-retry"
    assert ended.superseded_by == successors[0].id
    assert "earlier failure: " in (ended.status_reason or "")
    assert ended.blockers == [] and ended.next_attempt_at is None
    # The ended receipt neither holds claims nor is retried again.
    assert not service.retry_eligible(first.id)


def test_a_legacy_failed_retry_parent_is_relabelled_superseded(
    tmp_path: Path,
) -> None:
    sessions, _lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    successor = _uuid(990)
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        row.state = "failed"
        row.status_reason = f"Automatically reconciled by profile retry {successor}"
        progress = dict(row.progress)
        progress["retry_due_at"] = None
        row.progress = progress

    assert service._heal_legacy_applications(_lifecycle._clock())

    view = service.application(first.id)
    assert view.state == "superseded"
    assert view.superseded_by == successor
    assert view.reason_code == "superseded-by-retry"
    assert not service._heal_legacy_applications(_lifecycle._clock())


def test_a_stale_review_ends_superseded_instead_of_retrying_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued load whose reviewed plan went stale (its child would stop a
    workload the review did not cover) can never succeed by waiting: it ends
    superseded with the stale-review code so the client re-reviews."""

    from vonk_control.fleet_profiles import FleetProfileReviewStale

    _sessions, lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    attempts: list[str] = []

    def stale(*_args, **_kwargs):
        attempts.append("retry")
        raise FleetProfileReviewStale(
            "Profile child would stop an unreviewed workload; review again"
        )

    monkeypatch.setattr(service, "retry", stale)
    later = lifecycle._clock() + timedelta(hours=2)
    service._clock = lambda: later
    for _ in range(6):
        service.tick()

    ended = service.application(first.id)
    assert ended.state == "superseded"
    assert ended.reason_code == "effects-changed-during-admission"
    assert ended.superseded_by is None
    assert "unreviewed workload" in (ended.status_reason or "")
    assert len(attempts) == 1, "no retry loop: the end is final"


def test_a_child_start_with_a_stale_review_ends_superseded_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vonk_control.fleet_profiles import FleetProfileReviewStale

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
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
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Stale review",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "stale-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    application = service.apply(profile.id, request_key=_uuid(830), actor="admin")

    def stale(*_args, **_kwargs):
        raise FleetProfileReviewStale(
            "Profile child would stop an unreviewed workload; review again"
        )

    monkeypatch.setattr(service, "_start_step", stale)
    for _ in range(4):
        service.tick()

    ended = service.application(application.id)
    assert ended.state == "superseded", ended.status_reason
    assert ended.reason_code == "effects-changed-during-admission"
    assert ended.blockers == [] and ended.next_attempt_at is None


def test_a_child_start_that_hits_a_busy_admission_ends_superseded_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vonk_control.fleet_profiles import FleetProfileAdmissionEffectBusy

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
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
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Busy admission",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "stale-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    application = service.apply(profile.id, request_key=_uuid(831), actor="admin")

    def stale(*_args, **_kwargs):
        raise FleetProfileAdmissionEffectBusy("a live effect owner is busy")

    monkeypatch.setattr(service, "_start_step", stale)
    for _ in range(4):
        service.tick()

    ended = service.application(application.id)
    assert ended.state == "superseded", ended.status_reason
    assert ended.reason_code == "effects-changed-during-admission"
    assert ended.blockers == [] and ended.next_attempt_at is None


def _expire_backoff(service, lifecycle) -> None:
    later = lifecycle._clock() + timedelta(hours=2)
    service._clock = lambda: later


def test_a_retry_that_lost_its_selection_ends_superseded_not_parked_forever(
    tmp_path: Path,
) -> None:
    """A Controller redeploy in the middle of a load can leave a retry whose parent
    no longer owns the selected profile. Waiting never restores that, so the
    application ends superseded (the client re-reviews) instead of re-parking
    every minute with ``profile.retry_conflict``."""

    from vonk_control.models import FleetProfileSelection

    sessions, lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    with sessions.begin() as session:
        parent = session.get(FleetProfileApplication, first.id)
        assert parent is not None
        parent.selection_generation = None
        selection = session.get(FleetProfileSelection, 1)
        if selection is not None:
            session.delete(selection)
    _expire_backoff(service, lifecycle)
    for _ in range(4):
        service.tick()

    ended = service.application(first.id)
    assert ended.state == "superseded", (ended.state, ended.status_reason)
    assert ended.reason_code == "effects-changed-during-admission"
    assert ended.blockers == [] and ended.next_attempt_at is None
    assert "lost its current selection" in (ended.status_reason or "")


def test_a_row_parked_by_the_old_retry_conflict_heals_to_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vonk_control.fleet_profiles import FleetProfileSelectionLost

    sessions, lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    # What the previous Controller left behind: parked by the generic conflict.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        service._park_for_retry(
            row,
            _persisted_profile_progress(row),
            [
                make_blocker(
                    "profile.retry_conflict",
                    "Selected profile retry lost its current selection",
                )
            ],
        )

    def lost(*_args, **_kwargs):
        raise FleetProfileSelectionLost(
            "Selected profile retry lost its current selection"
        )

    monkeypatch.setattr(service, "retry", lost)
    _expire_backoff(service, lifecycle)
    service.tick()
    ended = service.application(first.id)
    assert ended.state == "superseded"
    assert ended.blockers == []


def test_an_untyped_conflict_supersedes_instead_of_parking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sessions, lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )

    def conflict(*_args, **_kwargs):
        raise FleetProfileConflict(
            "Selected profile differs from its accepted snapshot"
        )

    monkeypatch.setattr(service, "retry", conflict)
    _expire_backoff(service, lifecycle)
    service.tick()
    assert service.application(first.id).state == "superseded"


def test_a_transient_conflict_still_parks_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vonk_control.fleet_profiles import FleetProfileAdmissionBusy

    _sessions, lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )

    def busy(*_args, **_kwargs):
        raise FleetProfileAdmissionBusy("admission is busy", holder="load")

    monkeypatch.setattr(service, "retry", busy)
    _expire_backoff(service, lifecycle)
    service.tick()
    parked = service.application(first.id)
    assert parked.state == "queued"
    assert parked.next_attempt_at is not None


def _conflict_types() -> list[type]:
    from vonk_control.fleet_profiles import FleetProfileConflict as base

    found: list[type] = []
    pending = [base]
    while pending:
        current = pending.pop()
        found.append(current)
        pending.extend(current.__subclasses__())
    return found


def test_every_conflict_type_declares_what_a_retry_does_with_it() -> None:
    """A new conflict type must say whether waiting can resolve it: only the
    types listed here may park; any other new type fails this test until it is
    classified (and, if it supersedes, carries a contract reason code)."""

    from vonk_control.fleet_profile_contract import FleetProfileSupersedeCode
    from vonk_control.fleet_profiles import RETRY_SUPERSEDE, RETRY_WAIT

    waits = {
        "FleetProfileAdmissionBusy",
        "FleetProfileAdmissionStorageError",
        "FleetProfileAdmissionEffectBusy",
        "FleetProfileResourceRecheckUnavailable",
        "FleetProfileAssetReservationConflict",
        "_FleetProfileRecoveryBindingConflict",
    }
    actual_waits = set()
    for cls in _conflict_types():
        assert "retry_disposition" in vars(cls), (
            f"{cls.__name__} must declare retry_disposition (RETRY_WAIT or "
            "RETRY_SUPERSEDE)"
        )
        assert cls.retry_disposition in {RETRY_WAIT, RETRY_SUPERSEDE}
        if cls.retry_disposition == RETRY_WAIT:
            actual_waits.add(cls.__name__)
        else:
            assert cls.supersede_code in set(FleetProfileSupersedeCode)
    assert actual_waits == waits


def test_parking_refuses_an_error_that_waiting_cannot_resolve(
    tmp_path: Path,
) -> None:
    from vonk_control.fleet_profiles import (
        FleetProfileAdmissionBusy,
        FleetProfileReviewStale,
        FleetProfileSelectionLost,
    )

    sessions, _lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        progress = _persisted_profile_progress(row)
        for permanent in (
            FleetProfileConflict("x"),
            FleetProfileReviewStale("x"),
            FleetProfileSelectionLost("x"),
            RuntimeError("x"),
        ):
            with pytest.raises(AssertionError, match="not retryable by waiting"):
                service._park_for_retry(row, progress, [], because=permanent)
        service._park_for_retry(
            row, progress, [], because=FleetProfileAdmissionBusy("busy")
        )


def _fail_pending_children(sessions, reason: str) -> int:
    """Fail every Run/Switch child that has not ended, the way a crashing start does."""

    failed = 0
    with sessions.begin() as session:
        for child in session.scalars(
            select(Job).where(Job.kind == "recipe.run-switch.v2")
        ):
            if child.state not in {"failed", "succeeded", "cancelled"}:
                child.state = "failed"
                child.status_reason = reason
                failed += 1
    return failed


def test_a_deterministic_crash_ends_after_the_failure_budget(tmp_path: Path) -> None:
    """A start that fails the same way every time is retried a bounded number of
    times, then ends ``failed`` with the typed evidence, instead of relaunching the
    workload every minute for as long as the intent stands."""

    from vonk_control.fleet_profiles import PROFILE_REPEATED_FAILURE_CODE
    from vonk_control.lifecycle.core import RECOVERY

    sessions, lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    crash = "temporary runtime dependency unavailable"
    clock = {"now": lifecycle._clock()}
    service._clock = lambda: clock["now"]
    for _ in range(RECOVERY.max_failures + 3):
        clock["now"] += timedelta(hours=2)
        for _tick in range(4):
            service.tick()
            _fail_pending_children(sessions, crash)

    with sessions() as session:
        applications = tuple(
            session.scalars(
                select(FleetProfileApplication).order_by(
                    FleetProfileApplication.created_at
                )
            )
        )
    # The first failure plus the budgeted retries, and not one more.
    assert len(applications) == RECOVERY.max_failures
    last = service.application(applications[-1].id)
    assert last.state == "failed" and last.next_attempt_at is None
    assert [blocker.code for blocker in last.blockers] == [
        PROFILE_REPEATED_FAILURE_CODE
    ]
    assert f"{RECOVERY.max_failures} times" in (last.status_reason or "")
    assert crash in (last.status_reason or "")
    assert not service._recovery_wanted(
        *_row_and_progress(sessions, applications[-1].id)
    )
    # Earlier attempts were superseded by their retries, as for any automatic retry.
    assert service.application(first.id).state == "superseded"


def _row_and_progress(sessions, application_id: str):
    from vonk_control.fleet_profiles import _persisted_profile_progress

    session = sessions()
    row = session.get(FleetProfileApplication, application_id)
    assert row is not None
    return session, row, _persisted_profile_progress(row)


def _crash_rounds(sessions, service, clock, reasons: list[str]) -> int:
    """Run retry rounds in which each new start crashes with the round's reason."""

    for reason in reasons:
        clock["now"] += timedelta(hours=2)
        for _tick in range(4):
            service.tick()
            _fail_pending_children(sessions, reason)
    with sessions() as session:
        return len(tuple(session.scalars(select(FleetProfileApplication))))


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_failure_that_changes_keeps_being_retried(tmp_path: Path) -> None:
    """Retries are for causes that change: different failures never use up the
    budget."""

    from vonk_control.lifecycle.core import RECOVERY

    sessions, lifecycle, service, *_rest = _failed_profile(tmp_path)
    clock = {"now": lifecycle._clock()}
    service._clock = lambda: clock["now"]
    changing = ["port in use", "disk full", "image pull failed", "gpu busy"] * 2
    applications = _crash_rounds(sessions, service, clock, changing)
    # Seven rounds of four different crashes: every one still got its retry.
    assert applications > RECOVERY.max_failures + 1


def test_a_failure_that_differs_only_in_numbers_is_the_same_failure(
    tmp_path: Path,
) -> None:
    from vonk_control.lifecycle.core import RECOVERY

    sessions, lifecycle, service, *_rest = _failed_profile(tmp_path)
    clock = {"now": lifecycle._clock()}
    service._clock = lambda: clock["now"]
    same = [
        f"runtime exited with code {index}"
        for index in range(RECOVERY.max_failures + 2)
    ]
    applications = _crash_rounds(sessions, service, clock, same)
    # Numbers are not the cause (it is one failure repeating), so it ends at the
    # budget; the load's first, different failure was not part of the streak.
    assert applications == RECOVERY.max_failures + 1
