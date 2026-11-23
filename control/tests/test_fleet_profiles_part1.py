from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    SupersedeCode,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileAssignment,
    FleetProfileInput,
    FleetProfileScope,
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchChildResult,
    FleetProfileSwitchChildState,
    FleetProfileVerificationResult,
)
from vonk_control.fleet_profiles import (
    FleetProfileAdmissionBusy,
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
)
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import (
    AgentNode,
    AgentNodeContact,
    FleetProfile,
    FleetProfileApplication,
)
from vonk_control.operation_api import OperationQuery
from vonk_control.operations.item_contract import operation_item
from vonk_control.preparation_contract import (
    ControllerAssetState,
    ModelArtifactPreparation,
    PreparationReason,
    RolloutPreparation,
    RuntimeImagePreparation,
    TargetAssetState,
)

from .fleet_profiles_support import (
    NOW,
    _assessment,
    _database,
    _exact_preparation,
    _follow_profile_retry,
    _input,
    _node_id,
    _seed,
    _seed_dual_solo_without_runtime_state,
    _SwitchAdapter,
    _uuid,
)
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture


def test_profile_progress_and_results_are_closed_nested_contracts() -> None:
    operation_id = _uuid(700)
    progress = FleetProfileApplicationProgress.model_validate(
        {
            "operation_kind": "fleet-profile.apply",
            "completed_steps": 1,
            "total_steps": 1,
            "step_results": {
                "0": {
                    "kind": "switch",
                    "operation_id": operation_id,
                    "result": {"verified": True},
                }
            },
        }
    )
    step_result = progress.step_results["0"].result
    assert isinstance(step_result, FleetProfileVerificationResult)
    assert step_result.verified is True
    application_result = FleetProfileApplicationResult(changed=True, completed_steps=1)
    assert application_result.changed and application_result.completed_steps == 1
    adapter_result = FleetProfileSwitchAdapterResult(
        children=[
            FleetProfileSwitchChildState(
                queue_index=0,
                operation_id=operation_id,
                kind="run",
                state=LifecycleState.SUCCEEDED,
            )
        ],
        assignment_ids=[],
    )
    assert adapter_result.children[0].operation_id == operation_id
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        FleetProfileApplicationProgress.model_validate(
            {"step_results": {}, "unexpected": True}
        )
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        FleetProfileApplicationResult.model_validate({"completed_steps": 1})
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        FleetProfileSwitchAdapterResult.model_validate({"assignment_ids": []})
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        FleetProfileSwitchChildResult.model_validate(
            {"run_switch_operation_id": operation_id}
        )


def test_profile_switch_state_rebuilds_malformed_persisted_progress() -> None:
    # Damaged progress is rebuilt from the row's own receipt: it names no child
    # document, so the child is found again through its deterministic key.
    assert (
        RunSwitchFleetProfileAdapter._state(FleetProfileApplication(progress="invalid"))
        is None
    )
    assert (
        RunSwitchFleetProfileAdapter._state(
            FleetProfileApplication(progress={"switch_adapter": "invalid"})
        )
        is None
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_profile_application_read_degrades_for_malformed_persisted_plan_and_result() -> (
    None
):
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
        request_key=_uuid(701),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        admitted_plan = deepcopy(row.plan)
        row.plan = {"steps": []}
    # A plan that cannot be read degrades the view (the step count the progress
    # recorded, no cancellation projection); the read is never refused.
    degraded = service.application(application.id)
    assert degraded.id == application.id
    assert degraded.cancellation is None

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = admitted_plan
        # A JSON array is never a valid stored result document; write it as the
        # driver would have persisted it rather than through the typed attribute.
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == application.id)
            .values(result=["malformed"])
        )
    # A damaged result of a load that has not succeeded is retired (no result).
    assert service.application(application.id).result is None


@pytest.mark.usefixtures("damaged_json_rows")
def test_profile_worker_retires_malformed_persisted_plan_as_unknown() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
        request_key=_uuid(702),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = {"steps": []}

    assert service.tick() is True
    with sessions() as session:
        retired = session.get(FleetProfileApplication, application.id)
        assert retired is not None
        # Retired, not failed for a person: its effect is unknown, and the worker
        # went on to the next item.
        assert retired.state == "cancelled"


def test_profile_worker_retires_malformed_persisted_progress_as_unknown() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)

    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
        request_key=_uuid(705),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.current_operation_id = _uuid(706)
        # Persisted JSON can be a string even though the contract requires an
        # object; write what the driver stored rather than the typed attribute.
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == application.id)
            .values(progress="corrupt-json")
        )

    assert service.tick() is True
    with sessions() as session:
        retired = session.get(FleetProfileApplication, application.id)
        assert retired is not None
        assert retired.state == "cancelled"


def test_profile_application_read_does_not_invent_a_missing_success_result() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
        request_key=_uuid(703),
        actor="admin",
    )

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "succeeded"
        row.current_step = application.total_steps
        row.result = None
    # A success label and reached step do not prove completed effects.
    unknown = service.application(application.id)
    assert unknown.result is None
    assert unknown.state == "cancelled"
    assert unknown.projection_issue is not None
    assert unknown.projection_issue.observation == "unknown"


def test_profile_admission_proceeds_when_the_reviewed_build_record_is_missing() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    preparation = _exact_preparation((_node_id(1),))
    preparation = preparation.model_copy(
        update={
            "runtime_image": preparation.runtime_image.model_copy(
                update={"build_id": _uuid(999)}
            )
        }
    )
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda *_args, **_kwargs: _assessment(preparation),
    )
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)
    assert preview.allowed
    # The build record the review named is gone: there is no running build to
    # protect, so the load is admitted (the exact image identity is the plan's own
    # and is verified again when its child starts), not refused.
    application = service.apply(profile.id, request_key=_uuid(40), actor="admin")
    assert application.state == "queued"
    with sessions() as session:
        assert session.scalar(select(FleetProfileApplication)) is not None


def test_profile_operation_projection_uses_bound_scope_and_canonical_phase() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(
        profile.id,
        request_key=_uuid(40),
        actor="admin",
    )

    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        item = service._operation_item(row).model_dump(mode="json")

    assert item["id"] == application.id
    assert item["parent_id"] is None
    assert item["node_ids"] == [_node_id(1)]
    assert item["kind"] == "fleet-profile.apply"
    assert item["progress"]["phase"] == "prepare"


def test_profile_view_projects_the_loaded_assignment_state(monkeypatch) -> None:
    # Live regression: a loaded profile whose run was serving still listed
    # its assignment as "Not loaded" because the view never consulted the
    # loaded application.
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")
    assert service.get(profile.id).assignments[0].observed_state == "Not loaded"

    service.apply(
        profile.id,
        request_key=_uuid(441),
        actor="admin",
    )
    assert service.get(profile.id).assignments[0].observed_state == "Not placed"

    real_state = FleetProfileService._assignment_state.__func__

    def running(cls, session, assignment, **kwargs):
        state = real_state(cls, session, assignment, **kwargs)
        state.current_state = "running"
        return state

    monkeypatch.setattr(FleetProfileService, "_assignment_state", classmethod(running))
    view = service.get(profile.id)
    assert view.status == "loaded"
    assert view.assignments[0].observed_state == "Running"

    # A saved edit the loaded application does not contain is not loaded.
    with sessions.begin() as session:
        saved = session.get(FleetProfile, profile.id)
        assert saved is not None
        saved.assignments = [{**saved.assignments[0], "spark_ids": [_node_id(2)]}]
    assert service.get(profile.id).assignments[0].observed_state == "Not loaded"


def test_an_endpoint_read_parses_a_first_loads_documents_once(monkeypatch) -> None:
    """A load that reviewed itself is its own root: no second parse of its documents."""

    from vonk_control.fleet_profiles import persistence

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")
    service.apply(profile.id, request_key=_uuid(441), actor="admin")
    parsed: list[str] = []
    original = persistence.read_row_column

    def counting(row, column):
        parsed.append(column)
        return original(row, column)

    monkeypatch.setattr(persistence, "read_row_column", counting)
    with sessions() as session:
        service.endpoint_intent(session, profile.number)

    assert (parsed.count("progress"), parsed.count("plan")) == (2, 1)


@pytest.mark.usefixtures("damaged_json_rows")
def test_profile_endpoint_intent_uses_loaded_application_after_saved_edits() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")
    with sessions() as session:
        saved_only = service.endpoint_intent(session, profile.number)
    assert saved_only.assignments == ()

    application = service.apply(
        profile.id,
        request_key=_uuid(440),
        actor="admin",
    )

    with sessions.begin() as session:
        saved = session.get(FleetProfile, profile.id)
        assert saved is not None
        saved.assignments = []

    with sessions() as session:
        intent = service.endpoint_intent(session, profile.number)

    assert intent.application_id == application.id
    assert intent.application_state == "queued"
    assert intent.assignments is not None
    assert len(intent.assignments) == 1
    assert intent.assignments[0].alias == "studio-chat"
    assert intent.assignments[0].state == "not-published-yet"

    # A completed application cannot make a missing run look merely pending.
    # The saved definition is still empty; the immutable assignment remains
    # visible with truthful withdrawal and no usable run identity.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "succeeded"
    with sessions() as session:
        withdrawn = service.endpoint_intent(session, profile.number)
    assert withdrawn.application_id == application.id
    assert withdrawn.assignments is not None
    assert len(withdrawn.assignments) == 1
    assert withdrawn.assignments[0].state == "withdrawn"
    assert withdrawn.assignments[0].expected_run_id is None

    # A required field missing from immutable persisted progress should make
    # endpoint membership explicitly unreadable, while execution keeps using
    # the strict progress validator.
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        valid_progress = deepcopy(row.progress)
    assert isinstance(valid_progress, dict)

    for missing_field in ("reviewed_plan_digest", "reviewed_application_id"):
        corrupted_progress = deepcopy(valid_progress)
        intended_profile = corrupted_progress.get("intended_profile")
        assert isinstance(intended_profile, dict)
        intended_profile.pop(missing_field)
        with sessions.begin() as session:
            row = session.get(FleetProfileApplication, application.id)
            assert row is not None
            row.progress = corrupted_progress

        with sessions() as session:
            unreadable = service.endpoint_intent(session, profile.number)
            row = session.get(FleetProfileApplication, application.id)
            assert row is not None
            # The strict reader names the damaged field and retires the intent
            # as unknown (a Residue); it never raises into a read.
            retired = service._intended_profile(row, session=session)
            assert isinstance(retired, Residue)

        assert unreadable.application_id == application.id
        assert unreadable.assignments is None
        assert unreadable.projection_issue is not None

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.progress = valid_progress

    # Endpoint discovery is a read-only projection. If the immutable reviewed
    # plan is corrupt, expose that membership is unknown without changing the
    # strict validator used by execution and recovery.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = {}

    with sessions() as session:
        unavailable = service.endpoint_intent(session, profile.number)
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        assert isinstance(service._intended_profile(row, session=session), Residue)

    assert unavailable.application_id == application.id
    assert unavailable.application_state == "succeeded"
    assert unavailable.assignments is None
    assert unavailable.projection_issue is not None


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "damage", ["missing-application", "invalid-state", "saved-profile"]
)
def test_profile_observation_does_not_depend_on_damaged_bookkeeping(
    damage: str,
    tmp_path: Path,
) -> None:
    from vonk_control.auth import TokenCodec
    from vonk_control.operation_api import durable_operation_services

    sessions = _database()
    _, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(profile.id, request_key=_uuid(441), actor="admin")
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        if damage == "missing-application":
            session.delete(row)
        elif damage == "invalid-state":
            # Simulate a historical persisted row outside the current
            # vocabulary; current writes must still enforce their constraint.
            session.execute(text("PRAGMA ignore_check_constraints=ON"))
            row.state = "damaged-state"
        else:
            session.execute(
                update(FleetProfile)
                .where(FleetProfile.id == profile.id)
                .values(labels=["invalid-label"])
            )
    if damage == "saved-profile":
        assert service.progress_number(profile.number).id == application.id
        return
    projections = durable_operation_services(
        sessions,
        tmp_path,
        clock=lambda: NOW,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        profile_endpoint_intent=service.endpoint_intent,
    )
    assert projections.profile_endpoint is not None
    view = projections.profile_endpoint(profile.number, None, "https://example.test/v1")
    assert view.application_id == application.id
    assert view.application_state is None
    assert view.assignments is None
    assert view.projection_issue is not None


def test_profile_switch_delegates_non_idle_assignment_and_surfaces_child_progress() -> (
    None
):
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
    now = [NOW]
    adapter = _SwitchAdapter()
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=adapter
    )
    profile_input = _input(revision_id).model_copy(
        update={"scope": FleetProfileScope(node_ids=[_node_id(1), _node_id(2)])}
    )
    profile = service.create(profile_input, actor="admin")

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert [step.kind for step in preview.steps] == ["switch"]
    assert preview.assignments[0].actions == ["switch"]
    assert preview.summary.starts == 1

    application = service.apply(
        profile.id,
        request_key=_uuid(42),
        actor="admin",
    )
    assert service.tick() is True
    progress = service.application(application.id).progress
    assert progress.child_progress is not None
    assert progress.child_progress.phase == "model-download"
    assert adapter.starts[0]["scope_node_ids"] == (_node_id(1),)
    with sessions() as session:
        # The accepted selection covers the whole fleet, including idle Sparks,
        # so a later profile load can fence an older effect on either node.
        assert [
            node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode).order_by(AgentNode.node_id))
        ] == [1, 1]

    observed_phases = [progress.child_progress.phase]
    for _ in range(8):
        _follow_profile_retry(service, application.id, now, assert_no_work=True)
        assert service.tick() is True
        current = service.application(application.id)
        child_progress = current.progress.child_progress
        if child_progress is not None:
            observed_phases.append(child_progress.phase)
        if current.state == "succeeded":
            break

    completed = service.application(application.id)
    assert completed.state == "succeeded"
    assert completed.result is not None
    assert completed.result.changed is True
    assert completed.result.completed_steps == 1
    assert observed_phases[:5] == [
        "model-download",
        "container-download",
        "target-copy",
        "start",
        "final-verify",
    ]
    step_result = service.application(application.id).progress.step_results["0"]
    assert isinstance(step_result.result, FleetProfileVerificationResult)
    assert step_result.result.verified is True


def test_new_profile_load_supersedes_older_queued_scope_by_acceptance_order() -> None:
    """A pending whole-fleet load cannot veto a later authorized profile."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=_SwitchAdapter()
    )
    first_profile = service.create(_input(revision_id), actor="admin")
    second_profile = service.create(
        _input(revision_id).model_copy(update={"name": "Newer choice"}),
        actor="admin",
    )
    first = service.apply(
        first_profile.id,
        request_key=_uuid(971),
        actor="admin",
    )
    second = service.apply(
        second_profile.id,
        request_key=_uuid(972),
        actor="admin",
    )
    assert first.state == second.state == "queued"
    assert first.created_at < second.created_at
    assert first.progress.workload_intent_ordinal == 1
    assert second.progress.workload_intent_ordinal == 2
    # Admission cancels the older logical order even if the worker selects
    # the newer row first under a tied clock.
    replaced = service.application(first.id)
    assert replaced.state == "superseded"
    assert service.application(second.id).state == "queued"


def test_parked_profile_load_fences_workload_before_admission_retry(
    monkeypatch,
) -> None:
    """A parked replacement cancels the older intent before full admission resumes."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    profile = service.create(_input(revision_id), actor="admin")
    original_queue = FleetProfileService._queue_application

    def stay_busy(self, reviewed, **kwargs):
        raise FleetProfileAdmissionBusy("test admission owner")

    monkeypatch.setattr(FleetProfileService, "_queue_application", stay_busy)
    parked = service.apply(
        profile.id,
        request_key=_uuid(973),
        actor="admin",
    )
    # Admission retries itself; the accepted intent stays queued with a due time.
    assert parked.state == "queued"
    assert parked.next_attempt_at is not None
    assert parked.progress.workload_intent_ordinal is None

    monkeypatch.setattr(FleetProfileService, "_queue_application", original_queue)
    assert service.tick() is True
    resumed = service.application(parked.id)
    assert resumed.progress.workload_intent_ordinal == 1
    assert adapter.cancellations == [((_node_id(1),), 1)]


def test_parked_profile_load_rechecks_fencing_after_ordinal_is_bound(
    monkeypatch,
) -> None:
    """A parked admission repeats supersession reconciliation after a retry."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    adapter = _SwitchAdapter()
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=adapter
    )
    profile = service.create(_input(revision_id), actor="admin")

    def stay_busy(self, reviewed, **kwargs):
        raise FleetProfileAdmissionBusy("test admission owner")

    monkeypatch.setattr(FleetProfileService, "_queue_application", stay_busy)
    parked = service.apply(
        profile.id,
        request_key=_uuid(974),
        actor="admin",
    )
    # Admission retries itself; the accepted intent stays queued with a due time.
    assert parked.state == "queued"
    assert parked.next_attempt_at is not None
    assert parked.progress.workload_intent_ordinal is None

    assert service.tick() is True
    first = service.application(parked.id)
    assert first.progress.workload_intent_ordinal == 1
    assert adapter.cancellations == [((_node_id(1),), 1)]

    # The second attempt is due on the lifecycle core's clock (3 to 5 seconds).
    now[0] += timedelta(seconds=6)
    assert service.tick() is True
    assert adapter.cancellations == [((_node_id(1),), 1), ((_node_id(1),), 1)]


def test_newer_parked_profile_load_retires_older_parked_intent(
    monkeypatch,
) -> None:
    """A newer parked intent must stop an older retry loop from starving it."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    preview = service.preview(profile.id)

    def stay_busy(self, reviewed, **kwargs):
        raise FleetProfileAdmissionBusy("test admission owner")

    monkeypatch.setattr(FleetProfileService, "_queue_application", stay_busy)
    first = service.apply(
        profile.id,
        request_key=_uuid(976),
        actor="admin",
    )
    assert first.state == "queued"
    assert service.tick() is True
    first = service.application(first.id)
    assert first.progress.workload_intent_ordinal == 1

    refreshed = service.preview(profile.id)
    assert refreshed.allowed is True
    assert refreshed.plan_digest != preview.plan_digest
    assert any(
        effect.kind == "profile-application" and effect.id == first.id
        for effect in refreshed.effects.superseded
    )
    second = service.apply(
        profile.id,
        request_key=_uuid(977),
        actor="admin",
    )
    assert second.state == "queued"
    assert service.tick() is True
    first = service.application(first.id)
    second = service.application(second.id)
    assert first.state == "superseded"
    assert second.progress.workload_intent_ordinal == 2


def test_newer_parked_profile_load_retires_older_backing_off_load(
    monkeypatch,
) -> None:
    """A load backing off for a retry is live: a newer load on its Sparks retires it.

    Left out of the newer load's view, the older load's retry fires once it is
    due and contends with the newer intent for the same Sparks (the newest
    request steers).
    """

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    first = service.apply(profile.id, request_key=_uuid(978), actor="admin")
    assert first.progress.workload_intent_ordinal == 1
    # Its run failed and the Controller retries it later: it backs off.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        row.state = LifecycleState.BACKOFF.value
        row.next_action_at = NOW + timedelta(hours=1)

    def stay_busy(self, reviewed, **kwargs):
        raise FleetProfileAdmissionBusy("test admission owner")

    monkeypatch.setattr(FleetProfileService, "_queue_application", stay_busy)
    now[0] += timedelta(seconds=1)
    second = service.apply(profile.id, request_key=_uuid(979), actor="admin")
    assert second.state == "queued"
    assert service.tick() is True

    first = service.application(first.id)
    second = service.application(second.id)
    assert first.state == "superseded", first.state
    assert first.progress.supersede_code == SupersedeCode.SUPERSEDED_BY_INTENT
    assert first.next_attempt_at is None
    assert second.progress.workload_intent_ordinal == 2


@pytest.mark.parametrize("old_state", ["failed", "queued", "running"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_new_load_is_independent_of_invalid_historical_progress(old_state: str) -> None:
    """A history parser failure must not veto a fresh authorized workload."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = NOW
    service = FleetProfileService(
        sessions, clock=lambda: now, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    first = service.load(
        profile.number,
        request_key=_uuid(975),
        actor="admin",
    )
    now += timedelta(seconds=1)
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        row.state = old_state
        row.status_reason = "stored attempt cannot continue"
        damaged_progress = {**row.progress, "attempt": "invalid"}
        row.progress = damaged_progress

    second = service.load(
        profile.number,
        request_key=_uuid(976),
        actor="admin",
    )
    assert second.state == "queued"
    assert second.progress.workload_intent_ordinal == 2
    assert second.retry_of_application_id is None
    assert second.progress.intended_profile is not None
    assert (
        service.load(
            profile.number,
            request_key=_uuid(976),
            actor="admin",
        )
        == second
    )
    assert service.progress_number(profile.number).id == second.id
    with sessions() as session:
        old = session.get(FleetProfileApplication, first.id)
        assert old is not None
        assert old.progress == damaged_progress
        assert old.state == old_state
    if old_state != "failed":
        # The damaged order is retired as superseded with its effect unknown by its
        # own worker pass; the fresh load is not held back by it.
        assert service.tick() is True
        with sessions() as session:
            old = session.get(FleetProfileApplication, first.id)
            assert old is not None
            assert old.state == "cancelled"
            assert old.progress == damaged_progress
    # Invalid history is never executed or silently repaired: replaying its key
    # answers with the stored receipt, rebuilt from its own columns.
    replay = service.load(
        profile.number,
        request_key=_uuid(975),
        actor="admin",
    )
    assert replay.id == first.id


@pytest.mark.usefixtures("damaged_json_rows")
def test_preview_isolates_an_unreadable_pending_plan() -> None:
    """A damaged pending step list must not veto a fresh authorized preview."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    assigned = service.create(_input(revision_id), actor="admin")
    pending = service.load(
        assigned.number,
        request_key=_uuid(981),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None
        # The step list is unreadable, but the declared scope written at
        # admission is still the durable boundary.
        row.plan = {**row.plan, "steps": [{"kind": "not-a-step"}]}
    idle = service.create(
        FleetProfileInput(name="All idle", assignments=[]), actor="admin"
    )

    preview = service.preview(idle.id)

    assert preview.allowed is True
    assert [step.node_ids for step in preview.steps] == [[_node_id(1)]]
    # The damaged order is preserved for its own worker to quarantine.
    with sessions() as session:
        damaged = session.get(FleetProfileApplication, pending.id)
        assert damaged is not None
        assert damaged.state == "queued"
        assert damaged.plan["steps"] == [{"kind": "not-a-step"}]


@pytest.mark.parametrize("desired_state", ["installed", "running"])
def test_named_assignment_keeps_authoring_name_without_inventing_an_endpoint(
    desired_state: str,
) -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    document = _input(revision_id).model_dump(mode="json")
    document["assignments"][0]["desired_state"] = desired_state
    saved = service.create(
        FleetProfileInput.model_validate_json(json.dumps(document)), actor="admin"
    )

    # Re-read persisted authoring data before the real preview conversion.
    stored = service.get(saved.id)
    assert stored.definition.assignments[0].assignment_name == "studio-chat"
    preview = service.preview(saved.id)
    assignment = preview.resolved_assignments[0]
    assert assignment.desired_state == desired_state
    assert assignment.alias == ("studio-chat" if desired_state == "running" else None)


@pytest.mark.usefixtures("damaged_json_rows")
def test_preview_skips_an_unreadable_pending_plan_without_a_scope() -> None:
    """A record that cannot be read at all never blocks new profile work."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    assigned = service.create(_input(revision_id), actor="admin")
    pending = service.load(
        assigned.number,
        request_key=_uuid(982),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None
        row.plan = {"steps": [{"kind": "not-a-step"}]}
    idle = service.create(
        FleetProfileInput(name="All idle", assignments=[]), actor="admin"
    )

    assert service.preview(idle.id).allowed
    fresh = service.load(idle.number, request_key=_uuid(983), actor="admin")
    assert fresh.id != pending.id


def test_preview_treats_a_preparation_blocker_as_not_allowed() -> None:
    """A preparation blocker blocks admission instead of only being listed.

    Preparation reasons carry the run-switch severity vocabulary, whose blocking
    value is ``blocker`` rather than this contract's ``error``.  Counting only
    ``error`` reported the profile as allowed while its own preparation said the
    persisted source-build plan is invalid -- and the apply then refused with
    ``profile child plan blocked: run-switch.container-build-plan-invalid`` a
    moment later, which is the silent admission the profile cache contract
    forbids.
    """

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    node_id = _node_id(1)
    digest = "a" * 64
    asset_bytes = 1024

    def blocking_preparation(
        session: Session,
        assignment: FleetProfileAssignment,
        expected_nodes: tuple[str, ...],
    ) -> RolloutPreparation:
        del session, assignment, expected_nodes
        # Shaped like the live GLM receipt: the Controller holds verified bytes,
        # no Spark has the asset yet, and the stored build plan cannot be
        # revalidated under the current executable identity.
        return RolloutPreparation(
            model=ModelArtifactPreparation(
                artifact_set_sha256=digest,
                model_content_sha256="b" * 64,
                recipe_revision_sha256="c" * 64,
                artifact_count=1,
                artifact_set_bytes=asset_bytes,
                completeness="complete",
                controller=ControllerAssetState(
                    state="ready",
                    expected_bytes=asset_bytes,
                    verified_bytes=asset_bytes,
                    missing_bytes=0,
                    verified_sha256=digest,
                    verified_at=NOW,
                    source="nas-cache",
                ),
                targets=[
                    TargetAssetState(
                        node_id=node_id,
                        state="unknown",
                        expected_bytes=asset_bytes,
                        present_bytes=0,
                        missing_bytes=asset_bytes,
                    )
                ],
            ),
            runtime_image=RuntimeImagePreparation(
                image_digest="sha256:" + "d" * 64,
                oci_layout_sha256="e" * 64,
                image_bytes=asset_bytes,
                architecture="linux-arm64",
                runtime_interface="openai",
                build_id=_uuid(1234),
                controller=ControllerAssetState(
                    state="ready",
                    expected_bytes=asset_bytes,
                    verified_bytes=asset_bytes,
                    missing_bytes=0,
                    verified_sha256="e" * 64,
                    verified_at=NOW,
                    source="controller-build",
                ),
                targets=[
                    TargetAssetState(
                        node_id=node_id,
                        state="unknown",
                        expected_bytes=asset_bytes,
                        present_bytes=0,
                        missing_bytes=asset_bytes,
                    )
                ],
            ),
            target_node_ids=[node_id],
            controller_ready=True,
            targets_ready=False,
            ready=False,
            reasons=[
                PreparationReason(
                    code="run-switch.container-build-plan-invalid",
                    detail="The persisted source-build plan is invalid.",
                    severity="blocker",
                )
            ],
        )

    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda *args, **_kwargs: _assessment(
            blocking_preparation(*args)
        ),
    )
    profile = service.create(_input(revision_id), actor="admin")

    preview = service.preview(profile.id)

    # The blocker is carried by the preparation, not by the profile's own
    # reasons, and it must still decide the admission verdict.
    assert preview.summary.blockers == 1
    assert preview.allowed is False


@pytest.mark.usefixtures("damaged_json_rows")
def test_activity_projection_keeps_valid_records_when_one_is_unreadable() -> None:
    """One damaged profile application must not fail the whole Activity page."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    valid = service.load(
        profile.number,
        request_key=_uuid(983),
        actor="admin",
    )
    with sessions() as session:
        valid_row = session.get(FleetProfileApplication, valid.id)
        assert valid_row is not None
        valid_progress = deepcopy(valid_row.progress)
    damaged_id = _uuid(984)
    with sessions.begin() as session:
        session.add(
            FleetProfileApplication(
                id=damaged_id,
                request_key=_uuid(985),
                profile_id=profile.id,
                profile_digest="a" * 64,
                plan_digest="b" * 64,
                state="failed",
                plan={"steps": []},
                current_step=0,
                current_operation_id=None,
                progress=valid_progress,
                result=None,
                status_reason="stored attempt cannot continue",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    page = service.operation_provider().list_operations(
        OperationQuery(after=None, limit=10, state=None, node_id=None)
    )

    items = {
        operation_item(item).id: operation_item(item).model_dump(
            mode="json", exclude_none=False
        )
        for item in page.items
    }
    assert set(items) == {valid.id, damaged_id}
    assert items[valid.id]["failure"] is None
    assert items[valid.id]["node_ids"] == [_node_id(1)]
    unreadable = items[damaged_id]
    assert unreadable.get("failure") is None
    assert unreadable["supported_actions"] == []
    assert unreadable["result"] is None


@pytest.mark.usefixtures("damaged_json_rows")
def test_activity_lists_a_record_with_retired_fields_and_new_work_proceeds() -> None:
    """Fields a newer Controller retired never make stored history unreadable."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    old = service.load(profile.number, request_key=_uuid(991), actor="admin")
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, old.id)
        assert row is not None
        row.progress = {**row.progress, "retired_field": {"nested": 1}}
        row.plan = {**row.plan, "retired_plan_field": True}

    items = {
        operation_item(item).id: operation_item(item).model_dump(mode="json")
        for item in service.operation_provider()
        .list_operations(OperationQuery(after=None, limit=10, state=None, node_id=None))
        .items
    }

    assert items[old.id]["kind"] == "fleet-profile.apply"
    assert items[old.id]["node_ids"] == [_node_id(1)]
    assert items[old.id].get("status_reason") is None
    idle = service.create(
        FleetProfileInput(name="All idle", assignments=[]), actor="admin"
    )
    assert service.preview(idle.id).allowed is True


@pytest.mark.usefixtures("damaged_json_rows")
def test_retry_eligibility_survives_a_damaged_sibling_receipt() -> None:
    """Damaged history must not deny a valid receipt its retry authority."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    first = service.load(
        profile.number,
        request_key=_uuid(986),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        row.state = "failed"
        row.status_reason = "stored attempt cannot continue"
        valid_progress = deepcopy(row.progress)
        session.add(
            FleetProfileApplication(
                id=_uuid(987),
                request_key=_uuid(988),
                profile_id=profile.id,
                profile_digest=row.profile_digest,
                plan_digest="c" * 64,
                state="failed",
                plan=deepcopy(row.plan),
                current_step=0,
                current_operation_id=None,
                progress={**valid_progress, "unexpected": {}},
                result=None,
                status_reason="stored attempt cannot continue",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    assert service.retry_eligible(first.id) is True

    # The authoritative per-node intent still fences the receipt once a later
    # load supersedes it, even though a damaged sibling is present.
    service.load(
        profile.number,
        request_key=_uuid(989),
        actor="admin",
    )
    assert service.retry_eligible(first.id) is False


def test_new_load_replaces_same_profile_while_same_key_replays() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=_SwitchAdapter()
    )
    profile = service.create(_input(revision_id), actor="admin")
    first = service.load(
        profile.number,
        request_key=_uuid(977),
        actor="admin",
    )
    second = service.load(
        profile.number,
        request_key=_uuid(978),
        actor="admin",
    )
    assert second.id != first.id
    assert second.progress.workload_intent_ordinal == 2
    assert service.application(first.id).state == "superseded"
    assert second.progress.intended_profile is not None
    assert (
        service.load(
            profile.number,
            request_key=_uuid(978),
            actor="admin",
        )
        == second
    )


def test_profile_switch_adapter_plans_disjoint_assignments_once_and_resumes() -> None:
    sessions = _database()
    _dual_revision_id, _solo_revision_id = _seed_dual_solo_without_runtime_state(
        sessions
    )
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(3),
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    _node_id(3),
                    observed_at=NOW,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(protocol_version=1, last_seen_at=NOW),
            )
        )
    now = [NOW]
    adapter = _SwitchAdapter()
    service = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=adapter
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Dual plus solo",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [_node_id(1), _node_id(2)],
                        "desired_state": "running",
                        "assignment_name": "dual-chat",
                    },
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
                        "spark_ids": [_node_id(3)],
                        "desired_state": "running",
                        "assignment_name": "solo-chat",
                    },
                ],
            }
        ),
        actor="admin",
    )

    preview = service.preview(profile.id)
    assert [step.kind for step in preview.steps] == ["switch"]
    application = service.apply(
        profile.id,
        request_key=_uuid(43),
        actor="admin",
    )
    assert service.tick() is True
    running = service.application(application.id)
    replayed_child = adapter.start(
        application_id=application.id,
        assignments=service.get(profile.id).assignments,
        scope_node_ids=tuple(preview.scope.node_ids),
        actor="admin",
        request_id=adapter.starts[0]["request_id"],
    )
    assert replayed_child.id == running.current_operation_id
    assert len(adapter.starts) == 1

    resumed = FleetProfileService(
        sessions, clock=lambda: now[0], switch_adapter=adapter
    )
    for _ in range(8):
        if resumed.application(application.id).state == "succeeded":
            break
        _follow_profile_retry(resumed, application.id, now, assert_no_work=True)
        assert resumed.tick() is True

    assert resumed.application(application.id).state == "succeeded"
    assert len(adapter.starts) == 1
    assert adapter.starts[0]["scope_node_ids"] == (
        _node_id(1),
        _node_id(2),
        _node_id(3),
    )
    assignment_scopes = dict(
        zip(
            adapter.starts[0]["assignment_ids"],
            adapter.starts[0]["assignment_scopes"],
            strict=True,
        )
    )
    assert set(assignment_scopes.values()) == {
        (_node_id(1), _node_id(2)),
        (_node_id(3),),
    }
    assert tuple(adapter.starts[0]["assignment_ids"]) == tuple(
        sorted(adapter.starts[0]["assignment_ids"])
    )
