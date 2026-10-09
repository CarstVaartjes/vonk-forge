"""Accepted kit content survives unrelated builder/provenance damage."""

import io

import pytest
from sqlalchemy import select, update
from vonk_agent_protocol import LifecycleState
from vonk_control.models import AgentNode, CatalogDocumentRevision, RecipeBuild
from vonk_control.prebuilt_images import PREBUILT_PULL_FAILED, executable_build_key
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.source_bundles import generate_source_bundle

from tests.signed_recipe_release import signed_recipe_releases

from .non_blocking import assert_no_orphaned_holds
from .test_recipe_builds import setup


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damage", ["missing", "revoked", "identity"])
def test_prebuilt_preparation_has_no_nominal_spark_admission(tmp_path, damage):
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    reference = "ghcr.io/example/runtime@sha256:" + "b" * 64
    with sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == row.id)
            .values(
                projected=dict(row.projected)
                | {"prebuilt_image": {"reference": reference, "build_key": "0" * 64}}
            )
        )
        node = session.get(AgentNode, node_id)
        assert node is not None
        if damage == "missing":
            node_id = "spk_" + "9" * 32
        elif damage == "revoked":
            node.revoked_at = now
        else:
            node.binary_digest = None
    service = RecipeBuildService(sessions, bundles=bundles)
    service._inventory.latest = lambda *_args, **_kwargs: pytest.fail(
        "Controller prebuilt preparation must not request Spark inventory"
    )
    prepared = service.prepare_plan(revision.id, node_id, now=now)
    assert prepared.policy_report is not None
    assert prepared.policy_report["prebuilt_image"] == reference
    fresh = service.prepare_plan(revision.id, node_id, now=now)
    assert fresh.policy_report is not None
    assert fresh.policy_report["prebuilt_image"] == reference
    planned = service.plan(revision.id, node_id, now=now)
    with sessions() as session:
        assert session.get(RecipeBuild, planned.build_id) is not None
        assert_no_orphaned_holds(session)


@pytest.mark.usefixtures("damaged_json_rows")
def test_past_failed_pull_and_source_key_do_not_change_accepted_content(tmp_path):
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    accepted_key = executable_build_key(service.resolve(revision.id).input_intent)
    reference = "ghcr.io/example/runtime@sha256:" + "b" * 64
    with sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == row.id)
            .values(
                projected=dict(row.projected)
                | {
                    "prebuilt_image": {
                        "reference": reference,
                        "build_key": accepted_key,
                    }
                }
            )
        )
    service = RecipeBuildService(sessions, bundles=bundles)
    first = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, first.build_id)
        assert build is not None
        build.state = LifecycleState.FAILED.value
        build.error = PREBUILT_PULL_FAILED + ": past pull was unavailable"
    fresh = service.plan(revision.id, node_id, now=now)
    assert fresh.policy_report is not None
    assert fresh.policy_report["prebuilt_image"] == reference
    with sessions() as session:
        build = session.get(RecipeBuild, fresh.build_id)
        assert build is not None and build.error is None
        assert_no_orphaned_holds(session)


@pytest.mark.usefixtures("damaged_json_rows")
def test_accepted_source_is_not_readmitted_by_declarative_policy(tmp_path):
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    bundle = generate_source_bundle(
        {
            "Dockerfile": (
                "FROM ghcr.io/example/base@sha256:"
                + "a" * 64
                + "\nRUN <<EOF\necho accepted\nEOF\nUSER 0\n"
            ).encode()
        }
    )
    bundles.put(bundle.sha256, io.BytesIO(bundle.archive))
    with sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == row.id)
            .values(
                projected=dict(row.projected) | {"source_bundle_sha256": bundle.sha256}
            )
        )
    service = RecipeBuildService(sessions, bundles=bundles)
    # Diagnostic findings are visible, but do not become another authority.
    assert service.check_source(revision.id).findings
    resolved = service.resolve(revision.id)
    planned = service.plan(revision.id, node_id, now=now, resolution=resolved)
    assert planned.source_bundle_sha256 == bundle.sha256
    assert bundles.get(bundle.sha256).archive == bundle.archive
    assert service.plan(revision.id, node_id, now=now).build_id == planned.build_id
    with sessions() as session:
        assert len(tuple(session.scalars(select(RecipeBuild)))) == 1
        assert_no_orphaned_holds(session)


@pytest.mark.usefixtures(signed_recipe_releases.__name__)
def test_failed_controller_pull_ends_and_fresh_request_publishes_exact_image(tmp_path):
    """A spent pull never dispatches a Spark build or poisons the pinned image."""
    from vonk_agent_protocol import ImageStoreCode
    from vonk_control.models import AgentOperation, Job, ResourceReservation
    from vonk_control.oci_image_store import StoreUnknown
    from vonk_control.prebuilt_images import PrebuiltImageImporter

    from .test_prebuilt_images import (
        ADDRESS,
        IMAGE_BYTES,
        NODE,
        NOW,
        REFERENCE,
        _published_library,
        _Registry,
        _services,
    )

    sessions, revision_id = _published_library(tmp_path)
    builds, operations = _services(sessions, tmp_path)
    # No usable Spark is necessary even for the connected pull producer.
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE)
        assert node is not None
        node.binary_digest = None
    first = builds.plan(revision_id, NODE, now=NOW)
    job = operations.build(
        first,
        build_input_sha256=first.build_input_sha256,
        actor="operator",
        request_id="unavailable-pull",
    )
    unavailable = PrebuiltImageImporter(
        sessions,
        tmp_path,
        clock=lambda: NOW,
        store=_Registry(
            tmp_path, failure=StoreUnknown(ImageStoreCode.COPY_FAILED, "unavailable")
        ),
    )
    assert unavailable.run_pending() == 1
    with sessions() as session:
        ended = session.get(Job, job.id)
        assert ended is not None and ended.state not in (
            LifecycleState.RUNNING.value,
            LifecycleState.QUEUED.value,
        )
        assert tuple(session.scalars(select(AgentOperation))) == ()
        assert tuple(session.scalars(select(ResourceReservation))) == ()
        assert_no_orphaned_holds(session)
    fresh = builds.plan(revision_id, NODE, now=NOW)
    accepted = operations.build(
        fresh,
        build_input_sha256=fresh.build_input_sha256,
        actor="operator",
        request_id="fresh-pull",
    )
    registry = _Registry(tmp_path)
    recovered = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=registry
    )
    assert recovered.run_pending() == 1
    assert registry.copied == [REFERENCE]
    with sessions() as session:
        completed = session.get(Job, accepted.id)
        build = session.get(RecipeBuild, fresh.build_id)
        assert (
            completed is not None and completed.state == LifecycleState.SUCCEEDED.value
        )
        assert build is not None
        assert build.image_digest == REFERENCE.rsplit("@", 1)[1]
        assert build.oci_layout_sha256 == ADDRESS
        assert build.image_bytes == IMAGE_BYTES
        assert_no_orphaned_holds(session)


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_plan_repairs_without_discarding_last_verified_image(tmp_path):
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    from .runtime_image_fixtures import place_test_image

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    place_test_image(storage, "c" * 64, 500)
    service = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
    )
    plan = service.plan(revision.id, node_id, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    manifest = storage.existing_archive("c" * 64, 500)
    before = manifest.read_bytes()
    with sessions.begin() as session:
        row = session.get(RecipeBuild, plan.build_id)
        assert row is not None
        row.plan = dict(row.plan) | {"removal_fence": "damaged"}
    fresh = service.plan(revision.id, node_id, now=now)
    assert fresh.build_id == plan.build_id
    assert fresh.agent_payload == plan.agent_payload
    with sessions() as session:
        row = session.get(RecipeBuild, plan.build_id)
        assert row is not None and row.state == LifecycleState.SUCCEEDED.value
        assert row.image_digest == "sha256:" + "b" * 64
        assert row.oci_layout_sha256 == "c" * 64
        assert_no_orphaned_holds(session)
    assert manifest.read_bytes() == before
