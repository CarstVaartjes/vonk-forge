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
            "expected_plan_digest": review["plan_digest"],
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
            "expected_plan_digest": review["plan_digest"],
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
                "expected_plan_digest": review["plan_digest"],
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
