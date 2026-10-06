"""Fleet profile bookkeeping: mismatches reconcile, damage rebuilds or retires.

Three families, one per rule of the blocker audit:

* a bookkeeping *mismatch* (a fence that moved, a cache that cannot answer, a recipe
  without an active revision, a scope that changed) reconciles and the load goes on;
* *persisted damage* is rebuilt from evidence (``read_or_rebuild``) or retired as
  unknown, and the caller continues with the next row or element;
* the legitimate refusals (a malformed request, a security blocker, an exact
  identity fence) still refuse.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignmentInput,
    FleetProfileInput,
    FleetProfileReason,
    UnavailableFleetProfileView,
)
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _persisted_profile_plan,
    _persisted_profile_progress,
    _persisted_profile_result,
    _require_recovery_preparation,
    _string_items,
)
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
)

from .test_fleet_profiles import (
    NOW,
    _database,
    _exact_preparation,
    _input,
    _node_id,
    _seed,
    _SwitchAdapter,
    _uuid,
)


def _service(sessions, **options):
    return FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter(), **options
    )


def _no_active_revision(sessions, revision_id: str) -> None:
    with sessions.begin() as session:
        # (Core update: an active revision is immutable through the ORM.)
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision_id)
            .values(state="failed")
        )


def _loaded(service: FleetProfileService, revision_id: str, key: int):
    profile = service.create(_input(revision_id), actor="admin")
    return profile, service.apply(profile.id, request_key=_uuid(key), actor="admin")


# (1) A bookkeeping mismatch reconciles instead of failing -----------------------


def test_a_cancel_completes_when_a_node_fence_moved_to_another_intent() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = _service(sessions)
    profile, application = _loaded(service, revision_id, 1001)
    assert application.progress.workload_intent_ordinal == 1
    with sessions.begin() as session:
        node = session.get(AgentNode, _node_id(1))
        assert node is not None
        # The fence no longer carries this application's intent (it used to refuse
        # the cancel: "workload intent is no longer current").
        node.workload_intent_ordinal = 0

    cancelled = service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=_uuid(1002),
        actor="admin",
    )
    assert cancelled.cancellation is not None
    for _ in range(6):
        service.tick()
    assert service.application(application.id).state == "cancelled"


def test_a_failing_cache_resolver_reads_the_profile_with_unknown_cache() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)

    def unavailable(**_kwargs: object):
        raise OSError("cache service is down")

    service = _service(sessions, cache_resolver=unavailable)
    created = service.create(_input(revision_id), actor="admin")
    # The resolver is evidence about what is prepared, never a gate: the profile
    # reads (and saves) without it and nothing claims the recipe is cached.
    assert service.get(created.id).assignments
    assert all(item.recipe.state != "Cached" for item in created.assignments)


def test_a_saved_recipe_without_an_active_revision_waits_for_the_catalog() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = _service(sessions)
    profile = service.create(_input(revision_id), actor="admin")
    _no_active_revision(sessions, revision_id)

    # Reads and previews do not refuse: the choice needs attention and the plan
    # names a reason that clears when the catalog sync replaces the recipe.
    assert service.get(profile.id).id == profile.id
    preview = service.preview(profile.id)
    assert not preview.allowed
    assert any(
        reason.code == "profile.recipe_unavailable" for reason in preview.reasons
    )

    # A parked, waitable load is accepted (not refused) while the catalog catches up.
    pending = service.apply(profile.id, request_key=_uuid(1003), actor="admin")
    assert pending.state in {"queued", "waiting-for-operator"}
    assert any(
        blocker.code == "profile.recipe_unavailable" for blocker in pending.blockers
    )


def test_a_retry_whose_fleet_scope_changed_is_declined_not_refused() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = _service(sessions)
    _profile, application = _loaded(service, revision_id, 1004)
    assert service.tick() is True
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "failed"
        row.status_reason = "interrupted"
        session.add(
            AgentNode(
                node_id=_node_id(9),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
    declined = service.retry(application.id, request_key=_uuid(1005), actor="admin")
    # The receipt comes back with the reason; nothing was issued and nothing raised.
    assert declined.id == application.id
    assert declined.retry_of_application_id is None


# (2) Persisted damage rebuilds or retires, and the worker continues -------------


def test_damaged_progress_is_rebuilt_from_the_row_receipt() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = _service(sessions)
    _profile, application = _loaded(service, revision_id, 1010)
    with sessions.begin() as session:
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == application.id)
            .values(progress="corrupt")
        )
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        rebuilt = _persisted_profile_progress(row)
    assert rebuilt.intended_profile is None  # no accepted intent can be invented
    assert rebuilt.completed_steps == row.current_step


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_plan_and_result_are_retired_or_rebuilt_never_raised() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = _service(sessions)
    _profile, application = _loaded(service, revision_id, 1011)
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.plan = {"steps": []}
        row.state = "succeeded"
        row.result = None
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        plan = _persisted_profile_plan(row)
        result = _persisted_profile_result(row)
    assert isinstance(plan, Residue)
    assert "profile_id" in plan.note  # names the field, never the stored value
    assert result is not None  # rebuilt from the receipt


def test_a_damaged_order_is_retired_and_the_worker_continues() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    profile = service.create(_input(revision_id), actor="admin")
    first = service.apply(profile.id, request_key=_uuid(1012), actor="admin")
    with sessions.begin() as session:
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == first.id)
            .values(progress="corrupt")
        )
    # The damaged order is retired as unknown (cancelled), not failed for a person...
    assert service.tick() is True
    retired = service.application(first.id)
    assert retired.state == "cancelled"
    assert "effect is unknown" in (retired.status_reason or "")
    # ...and the next load is issued by the same worker without any repair.
    second = service.apply(profile.id, request_key=_uuid(1013), actor="admin")
    for _ in range(8):
        service.tick()
    assert adapter.starts
    assert service.application(second.id).state in {"running", "succeeded"}


def test_damaged_choices_remain_unknown_and_assignment_snapshots_rebuild() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = _service(sessions)
    profile = service.create(_input(revision_id), actor="admin")
    with sessions.begin() as session:
        row = session.get(FleetProfile, profile.id)
        assert row is not None
        good = dict(row.assignments[0])
        session.execute(
            update(FleetProfile)
            .where(FleetProfile.id == profile.id)
            .values(assignments=[{"recipe_selector": "broken"}, good])
        )
    # A damaged choice cannot be dropped without deleting saved authoring intent.
    observed = service.read_number(profile.number)
    assert isinstance(observed, UnavailableFleetProfileView)
    assert observed.id == profile.id and observed.revision == profile.revision
    with sessions() as session:
        row = session.get(FleetProfile, profile.id)
        assert row is not None
        assert row.assignments == [{"recipe_selector": "broken"}, good]
    # Restore the exact saved choice; normal reads and applies converge again.
    with sessions.begin() as session:
        session.execute(
            update(FleetProfile)
            .where(FleetProfile.id == profile.id)
            .values(assignments=[good])
        )
    assert len(service.get(profile.id).assignments) == 1

    # An assignment snapshot that is missing is rebuilt from the accepted intent.
    application = service.apply(profile.id, request_key=_uuid(1014), actor="admin")
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        intent = _persisted_profile_progress(row).intended_profile
        assert intent is not None
        state = {
            "assignment_ids": [item.id for item in intent.assignments],
            "assignments": "damaged",
        }
        rebuilt = RunSwitchFleetProfileAdapter._assignments_from_state(state, row)
    assert [item.id for item in rebuilt] == sorted(
        item.id for item in intent.assignments
    )


def test_a_damaged_string_list_reads_as_its_fallback() -> None:
    assert _string_items(["a", 7, "b"]) == ["a", "b"]
    assert _string_items("not-a-list", fallback=("x",)) == ["x"]
    assert _string_items(None) == []


# (3) The legitimate refusals still refuse ---------------------------------------


def test_a_malformed_retry_request_is_still_refused() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = _service(sessions)
    _profile, application = _loaded(service, revision_id, 1020)
    # A receipt that is not failed or waiting cannot be retried (the request itself
    # is wrong); an unknown receipt is not found.
    with pytest.raises(FleetProfileConflict, match="Only failed or waiting"):
        service.retry(application.id, request_key=_uuid(1021), actor="admin")
    with pytest.raises(KeyError):
        service.retry(_uuid(1099), request_key=_uuid(1022), actor="admin")


def test_a_save_naming_a_recipe_without_an_active_revision_is_refused() -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = _service(sessions)
    _no_active_revision(sessions, revision_id)
    with pytest.raises(FleetProfileConflict, match="no active catalog revision"):
        service.create(
            FleetProfileInput(
                name="Needs a recipe",
                assignments=[
                    FleetProfileAssignmentInput(
                        recipe_selector="vonk-forge/synthetic-tiny-build",
                        spark_ids=[_node_id(1)],
                        model_variant="fp16",
                    )
                ],
            ),
            actor="admin",
        )


def test_a_security_blocker_in_the_plan_is_still_refused(monkeypatch) -> None:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    service = _service(sessions)
    profile = service.create(_input(revision_id), actor="admin")
    real = service.preview(profile.id)
    blocked = real.model_copy(
        update={
            "allowed": False,
            "reasons": [
                FleetProfileReason(
                    code="forbidden", detail="access denied", severity="error"
                )
            ],
        }
    )
    monkeypatch.setattr(service, "preview", lambda *_args, **_kwargs: blocked)
    with pytest.raises(FleetProfileConflict, match="security or contract blocker"):
        service.apply(profile.id, request_key=_uuid(1023), actor="admin")


def test_the_exact_identity_fence_of_a_recovery_still_refuses() -> None:
    accepted = _exact_preparation((_node_id(1),))
    other = accepted.model_copy(
        update={
            "model": accepted.model.model_copy(update={"artifact_set_sha256": "e" * 64})
        }
    )
    with pytest.raises(FleetProfileConflict, match="recovery_artifact_changed"):
        _require_recovery_preparation("assignment", accepted, other)
    # An accepted plan that never bound an identity has none to replace.
    _require_recovery_preparation("assignment", None, accepted)
