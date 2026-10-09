"""Fleet recovery follows content and current effects, never receipt taxonomy."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    LifecycleState,
    ReservationState,
    UnknownOutcomeError,
    canonical_message,
)
from vonk_control.fleet_profile_contract import FleetProfilePreview
from vonk_control.models import (
    FleetProfileApplication,
    RecipeBuild,
    ResourceReservation,
)
from vonk_control.profile_capacity import (
    accepted_profile_runtime_image,
    inherited_profile_memory,
    profile_build_memory_claims,
)
from vonk_control.settings import STORAGE_ADMISSION_WAIT_SECONDS
from vonk_control.strict_json import read_stored_model

from .test_fleet_profile_lifecycle import _World
from .test_fleet_profiles import _uuid
from .test_profile_build_memory import _accepted_build_profile, _parent_build_request


def test_another_build_receipt_reuses_and_repairs_the_exact_memory_promise(
    tmp_path, postgres_engine
):
    """Catches provenance gating and a missing promise poisoning build admission."""
    sessions, profiles, _planner, node_id, application_id, selected = (
        _accepted_build_profile(tmp_path, postgres_engine)
    )
    _parent_id, request_id = _parent_build_request(sessions, profiles)
    assert application_id is not None
    with sessions.begin() as session:
        original = session.get(RecipeBuild, selected.build_id)
        assert original is not None
        image = accepted_profile_runtime_image(
            session, application_id, original.recipe_revision_id, (node_id,)
        )
        sibling = RecipeBuild(
            id=str(uuid4()),
            recipe_revision_id=original.recipe_revision_id,
            builder_node_id=original.builder_node_id,
            source_bundle_sha256=original.source_bundle_sha256,
            build_input_sha256=original.build_input_sha256,
            image_digest=image.image_digest,
            oci_layout_sha256=image.oci_layout_sha256,
            image_bytes=image.image_bytes,
            architecture=image.architecture,
        )
        claims = profile_build_memory_claims(
            session, sibling, memory_pool="shared", request_id=request_id, lock=True
        )
        assert len(claims) == 1
        original_claim_id = claims[0].id
        session.delete(claims[0])
        session.flush()
        repaired = profile_build_memory_claims(
            session, sibling, memory_pool="shared", request_id=request_id, lock=True
        )
        assert len(repaired) == 1 and repaired[0].id == original_claim_id
        assert repaired[0].owner_id == application_id
        assert repaired[0].state == ReservationState.PROMISED
        # Different output bytes cannot inherit even with identical source inputs.
        sibling.image_digest = "sha256:" + "f" * 64
        accepted_claims = None
        try:
            accepted_claims = profile_build_memory_claims(
                session, sibling, memory_pool="shared", request_id=request_id, lock=True
            )
        except UnknownOutcomeError:
            pass
        assert accepted_claims is None
        assert repaired[0].owner_id == application_id
        sibling.image_digest = image.image_digest
        assert (
            profile_build_memory_claims(
                session, sibling, memory_pool="shared", request_id=request_id, lock=True
            )[0].id
            == original_claim_id
        )
        # A different executable input has no parent to borrow from.
        sibling.build_input_sha256 = "f" * 64
        assert not profile_build_memory_claims(
            session, sibling, memory_pool="shared", request_id=request_id, lock=True
        )
    newer = profiles.apply(
        profiles.application(application_id).profile_id,
        request_key=str(uuid4()),
        actor="admin",
    )
    assert newer.id != application_id


def test_a_missing_runtime_claim_reconstructs_only_the_accepted_requirement(
    tmp_path, postgres_engine
):
    """Catches missing bookkeeping forcing replacement of an exact accepted image."""
    sessions, profiles, _planner, node_id, application_id, selected = (
        _accepted_build_profile(tmp_path, postgres_engine)
    )
    assert application_id is not None
    receipt = profiles.application(application_id)
    with sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        accepted = read_stored_model(
            FleetProfilePreview,
            canonical_message(row.plan),
            strict=True,
            from_json=True,
        )
    requirement = accepted.admission_decisions[0].requirements[0]
    assignment = accepted.resolved_assignments[0]
    assert assignment.alias is not None
    assert requirement.memory_kind is not None
    assert requirement.memory_required_bytes is not None
    with sessions.begin() as session:
        for claim in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == application_id,
                ResourceReservation.resource_key == assignment.id,
            )
        ):
            session.delete(claim)
        session.flush()
        claims = inherited_profile_memory(
            session,
            application_id,
            selected.recipe_revision_id,
            assignment.alias,
            {
                node_id: (
                    requirement.memory_kind,
                    requirement.memory_required_bytes,
                    requirement.memory_pool,
                )
            },
            workload_intent_ordinal=receipt.progress.workload_intent_ordinal,
        )
        assert claims[node_id].amount_bytes == requirement.memory_required_bytes
        assert claims[node_id].owner_id == application_id
        session.flush()
        assert len(claims) == 1
    fresh = profiles.apply(receipt.profile_id, request_key=str(uuid4()), actor="admin")
    assert fresh.id != receipt.id


@pytest.mark.parametrize("during_start", [False, True])
def test_child_unknown_reobserves_same_effect_then_continues(
    tmp_path, monkeypatch, during_start
):
    """Catches cancelling or superseding an unknown without a newer request."""
    world = _World(tmp_path)
    child_id = world.child_id()
    receipt = world.service.application(world.id)
    if during_start:
        # Loss of the aggregate pointer is repaired by the deterministic step
        # request; the durable switch document still owns its exact child.
        world.edit(current_operation_id=None)
        method_name = "start"
    else:
        method_name = "advance"

    def unavailable(*_args, **_kwargs):
        raise UnknownOutcomeError("peer response was unreadable")

    monkeypatch.setattr(world.adapter, method_name, unavailable)
    assert world.service.tick()
    observed = world.service.application(world.id)
    assert observed.cancellation is None
    assert observed.progress.retry_due_at is not None
    assert (
        observed.progress.workload_intent_ordinal
        == receipt.progress.workload_intent_ordinal
    )
    assert world.child_id() == child_id
    # Restart retains the observation schedule and the accepted effect.
    due = observed.progress.retry_due_at
    world.restart()
    world.now[0] = due + timedelta(seconds=1)
    assert world.service.tick()
    assert world.child_id() == child_id
    assert world.service.application(world.id).cancellation is None
    world.cancel(19005)
    for _ in range(60):
        world.now[0] += timedelta(seconds=30)
        world.service.tick()
        if world.service.application(world.id).state == LifecycleState.CANCELLED:
            break
    ended = world.service.application(world.id)
    assert ended.state == LifecycleState.CANCELLED
    assert ended.current_operation_id is None
    fresh = world.service.apply(
        world.profile.id, request_key=_uuid(19001), actor="admin"
    )
    assert fresh.id != world.id


def test_expired_child_observation_settles_before_releasing_parent_claims(
    tmp_path, monkeypatch
):
    """Catches endless exact-step backoff and releasing capacity on first unknown."""
    world = _World(tmp_path)
    assert world.adapter is not None
    original_advance = world.adapter.advance

    def unavailable(*_args, **_kwargs):
        raise UnknownOutcomeError("peer response was lost")

    monkeypatch.setattr(world.adapter, "advance", unavailable)
    assert world.service.tick()
    with world.sessions() as session:
        row = session.get(FleetProfileApplication, world.id)
        assert row is not None
        accepted_at = row.created_at
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == world.id
                )
            )
        )
        assert claims and all(
            claim.state != ReservationState.RELEASED for claim in claims
        )
    world.now[0] = accepted_at + timedelta(seconds=STORAGE_ADMISSION_WAIT_SECONDS + 1)
    assert world.service.tick()
    stopping = world.service.application(world.id)
    assert stopping.cancellation is not None
    assert stopping.current_operation_id is not None
    assert stopping.progress.cancellation is not None
    assert stopping.progress.cancellation.pending_operation_ids
    monkeypatch.setattr(world.adapter, "advance", original_advance)
    for _ in range(60):
        world.now[0] += timedelta(seconds=30)
        world.service.tick()
        if world.service.application(world.id).state == LifecycleState.CANCELLED:
            break
    ended = world.service.application(world.id)
    assert ended.state == LifecycleState.CANCELLED
    assert ended.current_operation_id is None
    with world.sessions() as session:
        assert not tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == world.id,
                    ResourceReservation.state.in_(
                        (ReservationState.ACTIVE, ReservationState.PROMISED)
                    ),
                )
            )
        )
    fresh = world.service.apply(
        world.profile.id, request_key=_uuid(19002), actor="admin"
    )
    assert fresh.id != ended.id
    assert fresh.progress.cancellation is None


@pytest.mark.parametrize(
    "sibling_state", [LifecycleState.QUEUED, LifecycleState.CANCELLED]
)
def test_older_or_historical_retry_sibling_cannot_gate_current_repair(
    tmp_path, sibling_state
):
    """Catches same-profile active history and ended retry lineage vetoing repair."""
    from vonk_control.fleet_profile_contract import FleetProfileApplicationProgress

    from .test_fleet_profile_recovery_current import _failed_profile

    sessions, _lifecycle, service, profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    with sessions.begin() as session:
        current = session.get(FleetProfileApplication, first.id)
        assert current is not None
        plan = read_stored_model(
            FleetProfilePreview,
            canonical_message(current.plan),
            strict=True,
            from_json=True,
        )
        progress = read_stored_model(
            FleetProfileApplicationProgress,
            canonical_message(current.progress),
            strict=True,
            from_json=True,
        )
        sibling_id = str(uuid4())
        sibling_digest = "8" * 64
        sibling = FleetProfileApplication(
            id=sibling_id,
            request_key=str(uuid4()),
            profile_id=profile.id,
            profile_digest=current.profile_digest,
            plan_digest=sibling_digest,
            plan=plan.model_copy(update={"plan_digest": sibling_digest}).model_dump(
                mode="json"
            ),
            progress=progress.model_copy(
                update={"retry_of_application_id": first.id}
            ).model_dump(mode="json"),
            state=sibling_state,
            actor=current.actor,
            created_at=current.created_at - timedelta(seconds=1),
            updated_at=current.updated_at,
        )
        session.add(sibling)
    assert service.retry_eligible(first.id)
    repaired = service.retry(first.id, request_key=_uuid(19003), actor="admin")
    assert repaired.id != first.id
    assert repaired.progress.intended_profile == first.progress.intended_profile
    fresh = service.apply(profile.id, request_key=_uuid(19004), actor="admin")
    assert fresh.id not in (first.id, sibling_id, repaired.id)
