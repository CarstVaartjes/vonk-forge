"""Unknown build facts recover within a request, without sleeping on SQL holds."""

from __future__ import annotations

import pytest
from sqlalchemy import update
from vonk_agent_protocol import SecurityRefusalError, SourceBundleCode
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.source_bundles import SourceBundleRefused, SourceBundleUnknown

from .non_blocking import assert_no_orphaned_holds
from .test_recipe_builds import _json_text, setup


@pytest.mark.parametrize("action", ["check_source", "resolve", "prepare_plan"])
def test_storage_recovers_in_same_exact_request(tmp_path, monkeypatch, action):
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    original = bundles.get
    calls = []
    delays = []

    def get(digest):
        calls.append(digest)
        if len(calls) <= 2:
            raise SourceBundleUnknown(SourceBundleCode.STORAGE_UNAVAILABLE, "offline")
        return original(digest)

    def sleep(delay):
        # The real producer/store/consumer path must close each SQL read before
        # retrying. An implementation that sleeps inside its Session fails here.
        assert sessions.kw["bind"].pool.checkedout() == 0
        with sessions() as session:
            assert_no_orphaned_holds(session)
        delays.append(delay)

    monkeypatch.setattr(bundles, "get", get)
    service = RecipeBuildService(sessions, bundles=bundles, sleep=sleep)
    args = (revision.id, node_id) if action == "prepare_plan" else (revision.id,)
    kwargs = {"now": now} if action == "prepare_plan" else {}
    result = getattr(service, action)(*args, **kwargs)
    assert result is not None
    assert len(calls) == 3 and len(set(calls)) == 1
    assert delays == [0.1, 0.2]
    with sessions() as session:
        assert_no_orphaned_holds(session)


@pytest.mark.parametrize("action", ["check_source", "resolve", "prepare_plan"])
def test_exhaustion_releases_resources_and_fresh_request_recovers(
    tmp_path, monkeypatch, action
):
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    original = bundles.get
    error = SourceBundleUnknown(SourceBundleCode.STORAGE_UNAVAILABLE, "offline")
    attempts = []
    delays = []

    def unavailable(digest):
        attempts.append(digest)
        raise error

    monkeypatch.setattr(bundles, "get", unavailable)
    service = RecipeBuildService(sessions, bundles=bundles, sleep=delays.append)
    args = (revision.id, node_id) if action == "prepare_plan" else (revision.id,)
    kwargs = {"now": now} if action == "prepare_plan" else {}
    with pytest.raises(ValueError):
        getattr(service, action)(*args, **kwargs)
    assert len(attempts) == 3
    assert delays == [0.1, 0.2]
    with sessions() as session:
        assert_no_orphaned_holds(session)
    monkeypatch.setattr(bundles, "get", original)
    assert getattr(service, action)(*args, **kwargs) is not None


@pytest.mark.parametrize("action", ["check_source", "resolve", "prepare_plan"])
def test_security_refusal_is_not_retried(tmp_path, monkeypatch, action):
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    attempts = []
    delays = []

    def denied(digest):
        attempts.append(digest)
        raise SourceBundleRefused("denied")

    monkeypatch.setattr(bundles, "get", denied)
    service = RecipeBuildService(sessions, bundles=bundles, sleep=delays.append)
    args = (revision.id, node_id) if action == "prepare_plan" else (revision.id,)
    kwargs = {"now": now} if action == "prepare_plan" else {}
    with pytest.raises(SecurityRefusalError):
        getattr(service, action)(*args, **kwargs)
    assert len(attempts) == 1
    assert delays == []


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("action", ["check_source", "resolve", "prepare_plan"])
def test_damaged_projection_is_reobserved_from_its_typed_column(tmp_path, action):
    from vonk_control.models import CatalogDocumentRevision

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        original = row.projected
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision.id)
            .values(projected={"damaged": True})
        )
    delays = []

    def repair(delay):
        assert sessions.kw["bind"].pool.checkedout() == 0
        with sessions.begin() as session:
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == revision.id)
                .values(projected=original)
            )
        delays.append(delay)

    service = RecipeBuildService(sessions, bundles=bundles, sleep=repair)
    args = (revision.id, node_id) if action == "prepare_plan" else (revision.id,)
    kwargs = {"now": now} if action == "prepare_plan" else {}
    assert getattr(service, action)(*args, **kwargs) is not None
    assert delays == [0.1]
    with sessions() as session:
        assert_no_orphaned_holds(session)


def test_builder_inventory_recovers_before_plan_is_returned(tmp_path, monkeypatch):
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    delays = []
    service = RecipeBuildService(sessions, bundles=bundles, sleep=delays.append)
    original = service._inventory.latest
    calls = []

    def latest(builder_node_id, **kwargs):
        calls.append(builder_node_id)
        if len(calls) < 3:
            raise KeyError(builder_node_id)
        return original(builder_node_id, **kwargs)

    monkeypatch.setattr(service._inventory, "latest", latest)
    prepared = service.prepare_plan(revision.id, node_id, now=now)
    assert prepared.builder_node_id == node_id
    assert calls == [node_id] * 3
    assert delays == [0.1, 0.2]
    with sessions() as session:
        assert_no_orphaned_holds(session)


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "field", ["build_resources", "build_security", "source_bundle_sha256"]
)
@pytest.mark.parametrize("repairs", [True, False])
def test_missing_stored_dependency_reobserves_exact_authority_and_admits_fresh(
    tmp_path, field, repairs
):
    from vonk_control.models import CatalogDocumentRevision

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    original = revision.projected
    with sessions.begin() as session:
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision.id)
            .values(projected=dict(original) | {field: None})
        )
    delays = []

    def repair(delay):
        assert sessions.kw["bind"].pool.checkedout() == 0
        delays.append(delay)
        if repairs:
            with sessions.begin() as session:
                session.execute(
                    update(CatalogDocumentRevision)
                    .where(CatalogDocumentRevision.id == revision.id)
                    .values(projected=original)
                )

    service = RecipeBuildService(sessions, bundles=bundles, sleep=repair)
    if repairs:
        result = service.prepare_plan(revision.id, node_id, now=now)
        assert result.recipe_content_sha256 == revision.content_digest
        assert delays == [0.1]
    else:
        with pytest.raises(ValueError):
            service.prepare_plan(revision.id, node_id, now=now)
        assert delays == [0.1, 0.2]
        with sessions.begin() as session:
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == revision.id)
                .values(projected=original)
            )
    with sessions() as session:
        assert_no_orphaned_holds(session)
    fresh = service.plan(revision.id, node_id, now=now)
    assert fresh.recipe_content_sha256 == revision.content_digest


def test_damaged_completion_index_is_observed_then_fresh_completion_is_admitted(
    tmp_path,
):
    from vonk_control.models import RecipeBuild

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    delays = []
    service = RecipeBuildService(sessions, bundles=bundles, sleep=delays.append)
    plan = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        row = session.get(RecipeBuild, plan.build_id)
        assert row is not None
        row.build_input_sha256 = "9" * 64

    def complete():
        return service.record_success(
            plan.build_id,
            build_input_sha256=plan.build_input_sha256,
            image_digest="sha256:" + "b" * 64,
            oci_layout_sha256="c" * 64,
            image_bytes=500,
            now=now,
        )

    with pytest.raises(ValueError):
        complete()
    assert delays == [0.1, 0.2]
    with sessions.begin() as session:
        row = session.get(RecipeBuild, plan.build_id)
        assert row is not None and row.image_digest is None
        assert_no_orphaned_holds(session)
        row.build_input_sha256 = plan.build_input_sha256
    completed = complete()
    assert completed.image_digest == "sha256:" + "b" * 64


def test_damaged_stored_source_observation_rederives_and_verifies_exact_bytes(
    tmp_path, monkeypatch
):
    from vonk_control.source_bundles import SourceBundleIntegrityRefused

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    digest = _json_text(revision.projected["source_bundle_sha256"])
    original = bundles.get
    bundle = original(digest)
    reads = []

    def damaged(address):
        reads.append(address)
        if len(reads) == 1:
            raise SourceBundleIntegrityRefused("damaged local observation")
        return original(address)

    monkeypatch.setattr(bundles, "get", damaged)
    service = RecipeBuildService(
        sessions,
        bundles=bundles,
        source_rederiver=lambda *_args: bundle.archive,
    )
    plan = service.plan(revision.id, node_id, now=now)
    assert plan.source_bundle_sha256 == digest
    assert original(digest).archive == bundle.archive
    assert reads == [digest, digest]
    assert service.plan(revision.id, node_id, now=now).build_id == plan.build_id
    with sessions() as session:
        assert_no_orphaned_holds(session)
