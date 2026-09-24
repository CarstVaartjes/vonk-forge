"""Accepted installation capacity survives handoff and parent restart."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_control.models import (
    ClusterMapping,
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    ResourceReservation,
)

from .test_profile_capacity_admission import _capacity_profile
from .test_profile_installed_execution import _drive_to_job


@pytest.mark.parametrize("node_count", [1, 2])
def test_profile_disk_claim_blocks_competing_install_and_is_inherited(
    tmp_path, postgres_engine, node_count: int
):
    sessions, profiles, planner, profile, api, headers, review, nodes = (
        _capacity_profile(tmp_path, postgres_engine, node_count=node_count)
    )
    required = review["assessments"][0]["assessment"]["fit_current"]["nodes"][0][
        "disk_required_bytes"
    ]
    with sessions.begin() as session:
        inventory = session.scalar(select(NodeInventorySnapshot))
        assert inventory is not None
        inventory.disk_free_bytes = required
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        assert mapping_id is not None and build_id is not None
    review = api.post(f"/api/profile/{profile.number}/preview", headers=headers).json()
    assert review["allowed"], review
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={
            "plan_digest": review["plan_digest"],
            "request_key": str(uuid4()),
        },
    )
    assert response.status_code == 202, response.text
    application_id = response.json()["id"]
    with sessions() as session:
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "fleet-profile",
                ResourceReservation.owner_id == application_id,
                ResourceReservation.kind == "disk",
                ResourceReservation.state == "active",
            )
        )
        assert claim is not None, "accepted profile has no durable disk claim"
        claim_id = claim.id
        assert claim.amount_bytes == required
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    competing = lifecycle.preview_install(mapping_id, build_id)
    assert not competing.allowed
    assert "install.insufficient_disk" in {
        reason.code for node in competing.nodes for reason in node.blockers
    }
    job_id = _drive_to_job(profiles, planner, sessions, "recipe.install")
    child = lifecycle.get(job_id)
    with sessions() as session:
        claim = session.get(ResourceReservation, claim_id)
        assert claim is not None
        assert (claim.owner_kind, claim.owner_id, claim.state) == (
            "installation",
            child.owner_id,
            "active",
        )
        assert claim.amount_bytes <= required
        assert not tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "fleet-profile",
                    ResourceReservation.owner_id == application_id,
                    ResourceReservation.state == "active",
                )
            )
        )

    assert set(child.nodes) == set(nodes)
    # A parent failure must not free disk already handed to its installation.
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None
        application.progress = {"completed_steps": -1}
    assert profiles.tick()
    with sessions() as session:
        application = session.get(FleetProfileApplication, application_id)
        claim = session.get(ResourceReservation, claim_id)
        assert application is not None and application.state == "failed"
        assert claim is not None and claim.state == "active"
        assert claim.owner_id == child.owner_id


def test_busy_disk_handoff_releases_transaction_and_resumes_original_claim(
    tmp_path, postgres_engine
):
    sessions, profiles, planner, profile, api, headers, review, _ = _capacity_profile(
        tmp_path, postgres_engine
    )
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={
            "plan_digest": review["plan_digest"],
            "request_key": str(uuid4()),
        },
    )
    assert response.status_code == 202, response.text
    application_id = response.json()["id"]
    assert profiles.tick()
    with sessions() as session:
        operation_id = session.scalar(
            select(Job.id).where(Job.kind == "recipe.run-switch.v2")
        )
        assert operation_id is not None
    with sessions.begin() as holder:
        claim = holder.scalar(
            select(ResourceReservation)
            .where(
                ResourceReservation.owner_kind == "fleet-profile",
                ResourceReservation.owner_id == application_id,
            )
            .with_for_update()
        )
        assert claim is not None
        claim_id = claim.id
        for _ in range(12):
            planner.tick()
            profiles.tick()
            operation = planner.get(operation_id)
            if (
                operation.result is not None
                and operation.result.retry_reason == "install.capacity_busy"
            ):
                break
        assert operation.state == "running"
        assert operation.result is not None
        assert operation.result.retry_reason == "install.capacity_busy"
        assert operation.result.observation_due_at is not None
        due = operation.result.observation_due_at
        assert not planner.tick(), (
            "busy capacity was retried before its recorded due time"
        )
        with sessions() as observer:
            assert not tuple(observer.scalars(select(RecipeInstallation)))
            visible = observer.get(ResourceReservation, claim_id)
            assert visible is not None and visible.owner_id == application_id
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    after_due = (
        due + timedelta(seconds=1)
        if isinstance(due, datetime)
        else datetime.fromisoformat(due) + timedelta(seconds=1)
    )
    planner._clock = profiles._clock = lifecycle._clock = lambda: after_due
    child_id = _drive_to_job(profiles, planner, sessions, "recipe.install")
    child = lifecycle.get(child_id)
    with sessions() as session:
        claim = session.get(ResourceReservation, claim_id)
        assert claim is not None and claim.owner_id == child.owner_id
        assert claim.state == "active"
    resumed = planner.get(operation_id)
    assert (
        resumed.result is not None
        and resumed.result.retry_reason != "install.capacity_busy"
    )


def test_supersession_and_failed_dispatch_release_only_unassigned_claims(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, profiles, _, profile, api, headers, review, _ = _capacity_profile(
        tmp_path, postgres_engine
    )

    def load(review):
        response = api.post(
            f"/api/profile/{profile.number}/load",
            headers=headers,
            json={
                "plan_digest": review["plan_digest"],
                "request_key": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        return response.json()["id"]

    first = load(review)
    next_review = api.post(
        f"/api/profile/{profile.number}/preview", headers=headers
    ).json()
    assert next_review["allowed"], next_review
    second = load(next_review)
    with sessions() as session:
        prior = session.get(FleetProfileApplication, first)
        assert prior is not None and prior.state == "cancelled"
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "fleet-profile"
                )
            )
        )
        assert {row.owner_id for row in claims if row.state == "active"} == {second}
        assert {row.owner_id for row in claims if row.state == "released"} == {first}

    def failed_dispatch(*args, **kwargs):
        raise RuntimeError("child dispatch unavailable")

    monkeypatch.setattr(profiles, "_start_step", failed_dispatch)
    assert profiles.tick()
    with sessions() as session:
        current = session.get(FleetProfileApplication, second)
        assert current is not None and current.state == "failed"
        assert not tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "fleet-profile",
                    ResourceReservation.state == "active",
                )
            )
        )


def test_profile_disk_handoff_preserves_materialized_install_headroom(
    tmp_path, postgres_engine
):
    """A replacement inherits its claim without charging old bytes twice.

    The old installation remains cached. Its observed bytes are physical disk
    usage, its staging headroom remains promised, and the new profile's claim
    is excluded only for its own child. A competing install must still fail.
    """
    from vonk_control.inventory_repository import MAX_INVENTORY_FUTURE_SKEW
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    from .test_disk_reservations import _record_disk
    from .test_fleet_profile_recovery_identity import _complete_rebuild
    from .test_recipe_operations import NOW, installed_recipe

    sessions, profiles, planner, profile, _api, _headers, _, nodes = _capacity_profile(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        assert mapping_id is not None and build_id is not None
    original = lifecycle.preview_install(mapping_id, build_id)
    old = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    old_requirement = original.nodes[0]
    retained_headroom = (
        old_requirement.required_bytes - old_requirement.required_download_bytes
    )
    assert retained_headroom > 0
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
    _complete_rebuild(
        sessions,
        storage,
        archive=b"replacement OCI image with an exact new identity",
        image_digest="sha256:" + "1" * 64,
    )
    later = NOW + MAX_INVENTORY_FUTURE_SKEW + timedelta(seconds=1)
    profiles._clock = planner._clock = lifecycle._clock = lambda: later
    _record_disk(sessions, nodes[0], at=later, free=8_000)
    review = profiles.preview(profile.id)
    assert review.allowed
    required = review.assessments[0].assessment.fit_current.nodes[0].disk_required_bytes
    assert required is not None
    later += timedelta(seconds=1)
    _record_disk(sessions, nodes[0], at=later, free=retained_headroom + required)
    review = profiles.preview(profile.id)
    assert review.allowed
    application = profiles.apply(
        profile.id,
        plan_digest=review.plan_digest,
        request_key=str(uuid4()),
        actor="admin",
    )
    with sessions() as session:
        parent_claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "fleet-profile",
                ResourceReservation.owner_id == application.id,
                ResourceReservation.kind == "disk",
                ResourceReservation.state == "active",
            )
        )
        assert parent_claim is not None
        claim_id = parent_claim.id
    assert not lifecycle.preview_install(mapping_id, build_id).allowed
    install_plan = lifecycle.preview_install(
        mapping_id, build_id, profile_application_id=application.id
    )
    assert install_plan.allowed
    assert install_plan.nodes[0].active_reserved_bytes == retained_headroom
    replacement_id = lifecycle.prepare_installation(
        install_plan,
        actor="admin",
        profile_application_id=application.id,
        workload_intent_ordinal=application.progress.workload_intent_ordinal,
    )
    with sessions() as session:
        claim = session.get(ResourceReservation, claim_id)
        assert claim is not None
        assert (claim.owner_kind, claim.owner_id, claim.state) == (
            "installation",
            replacement_id,
            "active",
        )
        assert replacement_id != old.owner_id
        replacement = session.get(RecipeInstallation, replacement_id)
        assert replacement is not None
        assert replacement.plan_digest == install_plan.plan_digest
        old_installation = session.get(RecipeInstallation, old.owner_id)
        assert old_installation is not None and old_installation.state == "installed"


@pytest.mark.parametrize("held_owner", ["mapping", "node", "build"])
def test_install_admission_reschedules_contended_dependencies_without_partial_claims(
    tmp_path, postgres_engine, held_owner
):
    """Contention before the reservation query is still a retryable admission wait."""
    from sqlalchemy import text
    from vonk_control.install_admission import InstallAdmissionBusy
    from vonk_control.models import AgentNode

    sessions, _profiles, planner, _profile, _api, _headers, _, nodes = (
        _capacity_profile(tmp_path, postgres_engine)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        assert mapping_id is not None and build_id is not None
    plan = lifecycle.preview_install(mapping_id, build_id)
    assert plan.allowed
    owner, identity = {
        "mapping": (ClusterMapping, mapping_id),
        "node": (AgentNode, nodes[0]),
        "build": (RecipeBuild, build_id),
    }[held_owner]
    with sessions.begin() as holder:
        assert holder.get(owner, identity, with_for_update=True) is not None
        with pytest.raises(InstallAdmissionBusy), sessions.begin() as admission:
            # The wrong implementation waits and raises a raw SQL error. Bound
            # the regression itself; production configures this centrally.
            admission.execute(text("SET LOCAL lock_timeout = '100ms'"))
            lifecycle._install_admission.accept_install_in_session(
                admission, plan, actor="admin", now=lifecycle._clock()
            )
        with sessions() as observer:
            assert not tuple(observer.scalars(select(RecipeInstallation)))
            assert not tuple(observer.scalars(select(ResourceReservation)))
    # The same reviewed decision can be admitted once its dependency releases.
    with sessions.begin() as admission:
        installation_id = lifecycle._install_admission.accept_install_in_session(
            admission, plan, actor="admin", now=lifecycle._clock()
        )
    with sessions() as observer:
        assert observer.get(RecipeInstallation, installation_id) is not None
