"""A persisted maintenance request must not poison every subsequent tick."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, ProfileReasonCode
from vonk_agent_protocol.agent_words import ProfileOperationKind, ProfileReasonSeverity
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.fleet_profile_contract import FleetProfileInput, FleetProfileReason
from vonk_control.fleet_profiles.contracts import _SelectedProfileSnapshot
from vonk_control.fleet_profiles.projection_support import _digest, _roster_digest
from vonk_control.models import (
    AgentNode,
    AgentNodeContact,
    Base,
    FleetProfileApplication,
    FleetProfileSelection,
)

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture
from .test_fleet_profiles import _input, _node_id, _seed, _uuid
from .test_selected_profile_postgres import NOW, _service, _tick_selected_when_due


@pytest.mark.parametrize("pending", [True, False])
@pytest.mark.parametrize("changed_observation", [True, False])
def test_roster_request_survives_repreview_and_worker_restart(
    postgres_engine: Engine, pending: bool, changed_observation: bool
) -> None:
    """Catch replay conflicts, retry-clock resets, duplicate loads and terminal replay."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    service = _service(sessions)
    profile = service.create(
        FleetProfileInput(name="Idle fleet", assignments=[]), actor="admin"
    )
    accepted = service.apply(profile.id, request_key=_uuid(988), actor="admin")
    assert accepted.state == LifecycleState.SUCCEEDED
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state=session.get_one(AgentNode, _node_id(1)).state,
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

    # Inject the key produced by the failing worker, with a valid older review
    # of the same accepted topology. Its observation differs, not its effects.
    with sessions() as session:
        selected = service._selected_profile_snapshot(session)

    assert isinstance(selected, _SelectedProfileSnapshot)
    preview = service.preview(
        profile.id,
        execution_assignments=tuple(selected.intended.assignments),
        profile_name=selected.plan.profile_name,
        profile_digest=selected.intended.profile_digest,
        accepted_intent=selected.intended,
        accepted_profile_revision=selected.profile_revision,
        accepted_profile_definition=selected.plan.profile_definition,
    )
    old_preview = (
        preview.model_copy(
            update={
                "reasons": [
                    *preview.reasons,
                    FleetProfileReason(
                        code=ProfileReasonCode.ADMISSION_BUSY,
                        severity=ProfileReasonSeverity.WARNING.value,
                        detail="Previous observation of a busy capacity writer",
                    ),
                ]
            }
        )
        if changed_observation
        else preview
    )
    old_preview = old_preview.model_copy(
        update={"plan_digest": _digest(old_preview.reviewed_decision())}
    )
    key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "vonk-forge:fleet-profile-reconcile:"
            f"{selected.application_id}:{selected.generation}:"
            f"{_roster_digest(preview.scope.node_ids)}:"
            f"{hashlib.sha256(repr(()).encode()).hexdigest()}",
        )
    )
    receipt = service._create_pending_application(
        old_preview,
        request_key=key,
        actor=selected.actor,
        operation_kind=ProfileOperationKind.APPLY.value,
        select_profile=True,
        selection_precondition=selected,
    )
    if pending:
        receipt = service._defer_pending_application(
            receipt.id,
            "Injected capacity writer wait",
            retry_delay=timedelta(seconds=30),
        )
    else:
        service._finish_pending_admission(
            receipt.id,
            state=LifecycleState.FAILED,
            reason="Injected admission failure before selection advanced",
        )

        receipt = service.application(receipt.id)

    # Lose the submitting worker. Several ordinary ticks must neither compare a
    # new observation against this key nor rearm its durable retry indefinitely.
    restarted = _service(sessions)
    for _ in range(3):
        restarted.tick()
    if not pending and not changed_observation:
        # An identical deterministic refusal remains visible without resubmission.
        with sessions() as session:
            assert (
                session.get_one(FleetProfileSelection, 1).application_id == accepted.id
            )
            assert len(session.scalars(select(FleetProfileApplication)).all()) == 2
        assert restarted.application(receipt.id) == receipt
        return
    if pending:
        held = restarted.application(receipt.id)
        assert held.next_attempt_at == receipt.next_attempt_at
        assert held.progress.admission_attempt == receipt.progress.admission_attempt
        with sessions() as session:
            assert len(session.scalars(select(FleetProfileApplication)).all()) == 2
        assert receipt.next_attempt_at is not None
        due = receipt.next_attempt_at
        restarted._clock = lambda: due
    for _ in range(4):
        restarted.tick()
    with sessions() as session:
        selection = session.get_one(FleetProfileSelection, 1)
        assert selection.generation == selected.generation + 1
        reconciled = restarted.application(selection.application_id)
        assert len(session.scalars(select(FleetProfileApplication)).all()) == (
            2 if pending else 3
        )
    assert reconciled.state == LifecycleState.SUCCEEDED
    assert reconciled.progress.intended_profile is not None
    assert reconciled.progress.intended_profile.scope.node_ids == sorted(
        (_node_id(1), _node_id(2))
    )
    if pending:
        assert reconciled.id == receipt.id
        assert reconciled.request_key == key
    else:
        assert reconciled.id != receipt.id
        assert reconciled.request_key != key
        assert restarted.application(receipt.id) == receipt


def test_pending_drift_request_resumes_execution_after_restart(
    postgres_engine: Engine,
) -> None:
    """Adopting a pending maintenance load must reach its actual switch steps."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _, revision_id = _seed(sessions)
    service = _service(sessions)
    profile = service.create(_input(revision_id), actor="admin")
    accepted = service.apply(profile.id, request_key=_uuid(989), actor="admin")
    for _ in range(8):
        if service.application(accepted.id).state == LifecycleState.SUCCEEDED:
            break
        _tick_selected_when_due(service, accepted.id)
    assert service.application(accepted.id).state == LifecycleState.SUCCEEDED
    with sessions() as session:
        selected = service._selected_profile_snapshot(session)
    assert isinstance(selected, _SelectedProfileSnapshot)
    preview = service.preview(
        profile.id,
        execution_assignments=tuple(selected.intended.assignments),
        profile_name=selected.plan.profile_name,
        profile_digest=selected.intended.profile_digest,
        accepted_intent=selected.intended,
        accepted_profile_revision=selected.profile_revision,
        accepted_profile_definition=selected.plan.profile_definition,
    )
    assert preview.steps
    receipt = service._create_pending_application(
        preview,
        request_key=_uuid(990),
        actor=selected.actor,
        operation_kind=ProfileOperationKind.APPLY.value,
        select_profile=True,
        selection_precondition=selected,
    )
    receipt = service._defer_pending_application(
        receipt.id, "Injected capacity writer wait", retry_delay=timedelta(seconds=30)
    )
    restarted = _service(sessions)
    assert receipt.next_attempt_at is not None
    due = receipt.next_attempt_at
    restarted._clock = lambda: due
    for _ in range(8):
        if restarted.application(receipt.id).state == LifecycleState.SUCCEEDED:
            break
        _tick_selected_when_due(restarted, receipt.id)
    assert restarted.application(receipt.id).state == LifecycleState.SUCCEEDED
    with sessions() as session:
        assert session.get_one(FleetProfileSelection, 1).application_id == receipt.id
        assert len(session.scalars(select(FleetProfileApplication)).all()) == 2
