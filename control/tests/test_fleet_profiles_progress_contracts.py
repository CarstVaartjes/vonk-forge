"""Fleet profiles: progress contracts."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_control.fleet_profile_contract.applications import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
)
from vonk_control.fleet_profile_contract.switch_state import (
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchChildResult,
    FleetProfileSwitchChildState,
    FleetProfileVerificationResult,
)
from vonk_control.fleet_profiles.run_switch_adapter import RunSwitchFleetProfileAdapter
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models.fleet import (
    FleetProfile,
    FleetProfileApplication,
)

from .fleet_profiles_support import (
    NOW,
    _assessment,
    _database,
    _exact_preparation,
    _input,
    _node_id,
    _seed,
    _SwitchAdapter,
    _uuid,
)


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
    from vonk_control.operation_api.durable import durable_operation_services

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
