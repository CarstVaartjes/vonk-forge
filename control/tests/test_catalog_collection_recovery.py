"""Unknown or concurrently accepted references never authorize catalog deletion."""

from __future__ import annotations

import json
import uuid

import pytest
import vonk_control.catalog_revision_collection as collection
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.agent_words import ProfileSwitchChildKind
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.job_documents import RecipeStopParent
from vonk_control.models import (
    Base,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    RecipeRun,
    RecipeSourceBundle,
    SourceBundleArchive,
)

from .test_catalog_revision_collection import (
    NODE,
    NOW,
    OLD,
    Catalog,
    _refresh,
)
from .test_catalog_revision_collection import catalog as catalog_fixture

# Reuse the real database fixture, not a mocked reference scanner.
catalog = catalog_fixture


def _payload(identity: str):
    return RecipeStopParent(
        schema_version=1,
        owner_kind=ProfileSwitchChildKind.RUN.value,
        owner_id=identity,
        plan_digest="0" * 64,
    ).model_dump(mode="json")


def _bundle(catalog: Catalog, sha: str) -> None:
    with catalog.sessions.begin() as session:
        session.add(
            RecipeSourceBundle(
                sha256=sha,
                media_type="application/x-tar",
                archive_bytes=1,
                total_bytes=1,
                file_count=1,
                storage_key=f"postgres:{sha}",
                manifest={},
                verified_at=OLD,
            )
        )
        session.add(SourceBundleArchive(sha256=sha, archive=b"x"))


def _assert_bundle(catalog: Catalog, sha: str, *, exists: bool) -> None:
    with catalog.sessions() as session:
        assert (session.get(RecipeSourceBundle, sha) is not None) is exists
        assert (session.get(SourceBundleArchive, sha) is not None) is exists


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damage", [{}, "truncated", [], {"schema_version": 1}])
def test_damaged_live_payload_defers_collection_and_fresh_requests_still_work(
    catalog: Catalog, damage
) -> None:
    old, _head = _refresh(catalog, "protected")
    sha = "4" * 64
    _bundle(catalog, sha)
    catalog.job(damage, state=LifecycleState.RUNNING.value, updated=OLD)
    collector = catalog.collector()
    collector.collect()
    assert catalog.exists(old)
    _assert_bundle(catalog, sha, exists=True)
    # Ending a conservative scan owns no admission slot against newer work.
    catalog.job(_payload(old), state=LifecycleState.SUCCEEDED.value, updated=OLD)
    with catalog.sessions.begin() as session:
        row = session.scalar(
            select(Job).where(Job.state == LifecycleState.RUNNING.value)
        )
        assert row is not None
        row.payload = _payload(old)
    collector.collect()
    assert catalog.exists(old)  # repaired live reference is observed, not discarded
    with catalog.sessions.begin() as session:
        row = session.scalar(
            select(Job).where(Job.state == LifecycleState.RUNNING.value)
        )
        assert row is not None
        row.state = LifecycleState.SUCCEEDED.value
    collector.collect()
    assert not catalog.exists(old)
    _assert_bundle(catalog, sha, exists=False)
    fresh, _ = _refresh(catalog, "fresh-after-scan")
    collector.collect()
    assert not catalog.exists(fresh)


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_live_run_plan_keeps_json_only_references_until_reobserved(
    catalog: Catalog,
):
    owner, _ = _refresh(catalog, "owner")
    protected, _ = _refresh(catalog, "plan-only-reference")
    sha = "5" * 64
    _bundle(catalog, sha)
    _installation, run_id = catalog.workload(owner)
    with catalog.sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        valid = dict(run.plan)
        session.execute(update(RecipeRun).where(RecipeRun.id == run_id).values(plan={}))
    collector = catalog.collector()
    collector.collect()
    assert catalog.exists(protected)
    _assert_bundle(catalog, sha, exists=True)
    # Re-observe the owner's exact accepted plan rather than inventing defaults.
    with catalog.sessions.begin() as session:
        session.execute(
            update(RecipeRun).where(RecipeRun.id == run_id).values(plan=valid)
        )
    collector.collect()
    assert not catalog.exists(protected)
    _assert_bundle(catalog, sha, exists=False)
    fresh, _ = _refresh(catalog, "fresh-with-live-owner")
    collector.collect()
    assert not catalog.exists(fresh)
    assert catalog.exists(owner)


@pytest.mark.usefixtures("damaged_json_rows")
def test_selected_application_damage_retains_content_then_new_intent_releases_it(
    catalog: Catalog,
):
    old, _head = _refresh(catalog, "selected")
    sha = "6" * 64
    _bundle(catalog, sha)
    profiles = FleetProfileService(catalog.sessions, clock=lambda: NOW)
    profile = profiles.create(
        FleetProfileInput(name="All idle", assignments=[]), actor="test"
    )
    idle = profiles.preview(profile.id)
    with catalog.sessions.begin() as session:
        application = FleetProfileApplication(
            request_key=str(uuid.uuid4()),
            profile_id=profile.id,
            profile_digest=idle.profile_digest,
            plan_digest=idle.plan_digest,
            state=LifecycleState.SUCCEEDED.value,
            plan={},
            actor="test",
            created_at=OLD,
            updated_at=OLD,
        )
        session.add(application)
        session.flush()
        identity = application.id
        session.add(
            FleetProfileSelection(
                singleton_id=1,
                generation=1,
                profile_id=profile.id,
                profile_revision=profile.revision,
                application_id=identity,
                roster_digest="3" * 64,
                updated_at=OLD,
            )
        )
    collector = catalog.collector()
    collector.collect()
    assert catalog.exists(old)
    _assert_bundle(catalog, sha, exists=True)
    fresh_profile = profiles.create(
        FleetProfileInput(name="Fresh idle", assignments=[]), actor="test"
    )
    assert profiles.preview(fresh_profile.id).profile_id == fresh_profile.id
    with catalog.sessions.begin() as session:
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == identity)
            .values(plan=json.loads(idle.model_dump_json()))
        )
    collector.collect()
    assert not catalog.exists(old)
    _assert_bundle(catalog, sha, exists=False)
    fresh_profile = profiles.create(
        FleetProfileInput(name="Fresh after repair", assignments=[]), actor="test"
    )
    assert profiles.preview(fresh_profile.id).profile_id == fresh_profile.id


@pytest.mark.usefixtures("damaged_json_rows")
def test_new_bundle_reference_between_candidate_scan_and_delete_is_retained(
    postgres_engine: Engine, monkeypatch
):
    Base.metadata.create_all(postgres_engine)
    catalog = Catalog(postgres_engine)
    sha = "7" * 64
    _bundle(catalog, sha)
    real_fence = collection._fence_references
    accepted = False

    def accept_before_fence(session, deadline):
        nonlocal accepted
        if not accepted:
            accepted = True
            catalog.job(_payload(sha), state=LifecycleState.RUNNING.value, updated=OLD)
        real_fence(session, deadline)

    monkeypatch.setattr(collection, "_fence_references", accept_before_fence)
    collector = catalog.collector()
    collector.collect()
    assert accepted
    _assert_bundle(catalog, sha, exists=True)
    with catalog.sessions.begin() as session:
        row = session.scalar(select(Job))
        assert row is not None
        row.state = LifecycleState.SUCCEEDED.value
    collector.collect()
    _assert_bundle(catalog, sha, exists=False)
    catalog.job(
        _payload(str(uuid.uuid4())), state=LifecycleState.SUCCEEDED.value, updated=OLD
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_uncommitted_reference_wins_without_waiting_or_leaking_collector_locks(
    postgres_engine: Engine,
):
    Base.metadata.create_all(postgres_engine)
    catalog = Catalog(postgres_engine)
    old, _head = _refresh(catalog, "concurrent")
    sha = "8" * 64
    _bundle(catalog, sha)
    collector = catalog.collector()
    with catalog.sessions.begin() as writer:
        # This transaction has accepted a reference but has not published it yet.
        writer.execute(
            insert(Job).values(
                request_id=str(uuid.uuid4()),
                kind=OperationKind.RECIPE_STOP.value,
                state=LifecycleState.RUNNING.value,
                actor="test",
                authority_revision="r",
                targets=[NODE],
                payload_digest="0" * 64,
                payload=_payload(sha),
                current_attempt=0,
                created_at=OLD,
                updated_at=OLD,
            )
        )
        collector.collect()
        assert catalog.exists(old)
        _assert_bundle(catalog, sha, exists=True)
    # The next scan sees the committed reference and can collect unrelated rows.
    collector.collect()
    assert not catalog.exists(old)
    _assert_bundle(catalog, sha, exists=True)
    with catalog.sessions.begin() as session:
        row = session.scalar(select(Job))
        assert row is not None
        row.state = LifecycleState.SUCCEEDED.value
    collector.collect()
    _assert_bundle(catalog, sha, exists=False)
    fresh, _ = _refresh(catalog, "fresh-after-contention")
    collector.collect()
    assert not catalog.exists(fresh)
