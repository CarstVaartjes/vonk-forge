"""Fleet profiles: switch delegation."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.reason_codes.runs import SupersedeCode
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileAssignment,
    FleetProfileInput,
    FleetProfileScope,
)
from vonk_control.fleet_profile_contract.switch_state import (
    FleetProfileVerificationResult,
)
from vonk_control.fleet_profiles.contracts import FleetProfileAdmissionBusy
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.models.fleet import (
    AgentNode,
    AgentNodeContact,
    FleetProfileApplication,
)
from vonk_control.operation_api.contracts import OperationQuery
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
    _follow_profile_retry,
    _input,
    _node_id,
    _seed,
    _seed_dual_solo_without_runtime_state,
    _SwitchAdapter,
    _uuid,
)
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture


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
