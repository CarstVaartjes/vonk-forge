from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest
import vonk_control.recipe_builds as recipe_builds_module
from sqlalchemy import Table, select
from vonk_agent_protocol import (
    canonical_message,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.availability_production.production import (
    build_recipe_image_availability,
)
from vonk_control.bounded_json import require_integer
from vonk_control.catalog.entities import CatalogEntityService
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models import (
    AgentCertificate,
    AgentOperation,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    ResourceReservation,
)
from vonk_control.recipe_builds import RecipeBuildError, RecipeBuildService
from vonk_control.recipe_image_availability import (
    BuildUnsettled,
)
from vonk_control.recipe_image_availability_contract import AvailabilityBuildReceipt
from vonk_control.recipe_operations import (
    RecipeOperationService,
    record_build_evidence,
)
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
)
from vonk_forge_contracts import RecipeDefinition

from .recipe_builds_support import (
    RecordingQueue,
    _json_object,
    _restore_builder_inventory,
    setup,
)
from .recipe_image_availability_support import _typed_availability_payload


def _json_array(value: object) -> list[object]:
    """Narrow decoded JSON to a mutable array; a wrong shape fails the test."""

    assert isinstance(value, list)
    return value


@pytest.mark.parametrize(
    ("operation_state", "build_state"),
    (
        ("failed", "failed"),
        ("needs-operator", "building"),
        ("failed", "building"),
    ),
)
def test_terminal_build_can_be_retried_once_with_fresh_fencing_and_capacity(
    tmp_path: Path, operation_state: str, build_state: str
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    first = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="initial-build",
    )
    with sessions.begin() as session:
        job = session.get(Job, first.id)
        assert job is not None
        job.state = operation_state
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert child is not None
        child.state = operation_state
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.state = build_state

    retried = operations.retry(first.id, actor="admin", request_id="retry-build")
    repeated = operations.retry(first.id, actor="admin", request_id="retry-build")

    assert repeated == retried
    assert retried.id != first.id
    assert retried.owner_id == plan.build_id
    assert retried.state == "running"
    with sessions() as session:
        stored_build = session.get(RecipeBuild, plan.build_id)
        assert stored_build is not None
        assert stored_build.state == "building"
        children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id.in_((first.id, retried.id))
                )
            )
        )
        assert len(children) == 2
        assert children[0].id != children[1].id
        reservations = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == plan.build_id,
                )
            )
        )
        assert sum(item.state == "active" for item in reservations) == 2
        assert sum(item.state == "released" for item in reservations) == 2


@pytest.mark.parametrize("force", [False, True])
def test_fresh_build_request_retries_matching_failed_build_idempotently(
    tmp_path: Path,
    force: bool,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    first = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="failed-acceptance-build",
    )
    with sessions.begin() as session:
        job = session.get(Job, first.id)
        assert job is not None
        job.state = "failed"
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert child is not None
        child.state = "failed"
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.state = "failed"

    retried = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
        force=force,
    )
    replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
        force=force,
    )

    assert retried == replay
    assert retried.id != first.id
    assert retried.owner_id == plan.build_id
    assert retried.state == "running"
    with sessions() as session:
        stored_build = session.get(RecipeBuild, plan.build_id)
        assert stored_build is not None
        assert stored_build.state == "building"
        stored_job = session.get(Job, retried.id)
        assert stored_job is not None
        assert stored_job.request_id == "fresh-acceptance-build"


def test_successful_build_retry_converges_original_and_new_request_keys(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    first = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="initial-build",
    )
    with sessions.begin() as session:
        job = session.get(Job, first.id)
        assert job is not None
        job.state = "needs-operator"
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert child is not None
        child.state = "needs-operator"

    retried = operations.retry(first.id, actor="admin", request_id="retry-build")
    succeeded = operations.record_node_result(
        retried.id,
        node_id,
        succeeded=True,
        evidence={
            "image_bytes": 500,
            "image_digest": "sha256:" + "b" * 64,
            "oci_layout_sha256": "c" * 64,
        },
    )
    assert succeeded.state == "succeeded"

    original_replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="initial-build",
    )
    new_replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
    )
    repeated_replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
    )

    assert original_replay == succeeded
    assert new_replay == repeated_replay
    assert new_replay.id != succeeded.id
    assert new_replay.state == "succeeded"
    assert new_replay.result == succeeded.result
    with sessions() as session:
        stored_job = session.get(Job, new_replay.id)
        assert stored_job is not None
        assert stored_job.request_id == "fresh-acceptance-build"
        assert (
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == new_replay.id
                )
            )
            is None
        )


@pytest.mark.parametrize("completed_before_restart", [False, True])
@pytest.mark.usefixtures("damaged_json_rows")
def test_forced_image_build_resumes_after_worker_restart(
    tmp_path: Path,
    completed_before_restart: bool,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    runtime = {"recipe_revision_id": revision.id, "builder_node_id": node_id}

    def add_parent(parent_id: str):
        with sessions.begin() as session:
            session.add(
                Job(
                    id=parent_id,
                    request_id=parent_id,
                    kind="recipe.image.availability.v2",
                    state="running",
                    actor="operator",
                    authority_revision=revision.id,
                    targets=[revision.id],
                    payload_digest="a" * 64,
                    payload=_typed_availability_payload(
                        {
                            "runtime": runtime,
                            "recipe_revision_id": revision.id,
                            "build_input_sha256": plan.build_input_sha256,
                            # The worker that held this claim died with its lease.
                            "claim_until": (now - timedelta(seconds=1)).isoformat(),
                        },
                        RecipeDefinition.model_validate_json(
                            json.dumps(revision.document)
                        ),
                    ),
                    current_attempt=1,
                    created_at=now,
                    updated_at=now,
                )
            )

    parent_id = "00000000-0000-4000-8000-000000000731"
    add_parent(parent_id)

    def production():
        return build_recipe_image_availability(
            sessions,
            artifact_root=tmp_path / "artifacts",
            storage=FilesystemRuntimeImageStorage(tmp_path / "artifacts"),
            recipe_builds=builds,
            recipe_operations=operations,
            clock=lambda: now,
        )

    def complete_build(_progress=None):
        with sessions() as session:
            job = session.scalar(
                select(Job).where(
                    Job.kind == "recipe.build.v1",
                    Job.state == "running",
                )
            )
            assert job is not None
            job_id = job.id
        operations.record_node_result(
            job_id,
            node_id,
            succeeded=True,
            evidence={
                "image_bytes": 500,
                "image_digest": "sha256:" + "b" * 64,
                "oci_layout_sha256": "c" * 64,
            },
        )

    # An explicit rebuild must not reuse this older successful receipt when
    # its own running child is replayed after the worker stops.
    operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id="original-cached-build",
    )
    complete_build()

    first = production()
    assert first.service._builder is not None
    claim = first.service.claim_pending(limit=1)[0]
    recipe = RecipeDefinition.model_validate_json(json.dumps(revision.document))
    waiting = first.service._builder(
        recipe,
        runtime,
        claim=claim,
        build_input_sha256=plan.build_input_sha256,
        force=True,
        progress=lambda _: None,
    )
    assert isinstance(waiting, BuildUnsettled)

    first.close()
    if completed_before_restart:
        complete_build()

    restarted = production()
    assert restarted.service._builder is not None
    with sessions.begin() as session:
        parent = session.get(Job, parent_id)
        assert parent is not None
        parent.payload = dict(parent.payload) | {
            "claim_until": (now - timedelta(seconds=1)).isoformat()
        }
    claim = restarted.service.claim_pending(limit=1)[0]
    if not completed_before_restart:
        waiting = restarted.service._builder(
            recipe,
            runtime,
            claim=claim,
            build_input_sha256=plan.build_input_sha256,
            force=True,
            progress=lambda _: None,
        )
        assert isinstance(waiting, BuildUnsettled)

        complete_build()
    result = restarted.service._builder(
        recipe,
        runtime,
        claim=claim,
        build_input_sha256=plan.build_input_sha256,
        force=True,
        progress=lambda _: None,
    )
    assert not isinstance(result, BuildUnsettled)
    assert (
        AvailabilityBuildReceipt.model_validate_json(canonical_message(result)).state
        == "succeeded"
    )
    with sessions() as session:
        jobs = tuple(session.scalars(select(Job).where(Job.kind == "recipe.build.v1")))
        assert len(jobs) == 2
        assert all(job.state == "succeeded" for job in jobs)

    with sessions.begin() as session:
        finished = session.get(Job, parent_id)
        assert finished is not None
        finished.state = "succeeded"

    # A separate explicit download still requests a fresh build.
    new_parent_id = "00000000-0000-4000-8000-000000000732"
    add_parent(new_parent_id)
    new_claim = restarted.service.claim_pending(limit=1)[0]
    waiting = restarted.service._builder(
        recipe,
        runtime,
        claim=new_claim,
        build_input_sha256=plan.build_input_sha256,
        force=True,
        progress=lambda _: None,
    )
    assert isinstance(waiting, BuildUnsettled)

    complete_build()
    restarted.service._builder(
        recipe,
        runtime,
        claim=new_claim,
        build_input_sha256=plan.build_input_sha256,
        force=True,
        progress=lambda _: None,
    )
    with sessions() as session:
        jobs = tuple(session.scalars(select(Job).where(Job.kind == "recipe.build.v1")))
        assert len(jobs) == 3
        assert all(job.state == "succeeded" for job in jobs)
    restarted.close()


def test_build_plan_rejects_disk_below_concurrent_oci_export_peak(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    projected = revision.projected
    build_resources = _json_object(projected["build_resources"])
    source_bytes = len(bundles.get(str(revision.source_bundle_sha256)).archive)
    temporary_bytes = require_integer(
        build_resources["temporary_bytes"], "temporary bytes"
    )
    roles = _json_array(_json_object(revision.document["topology"])["roles"])
    output_bytes = max(
        require_integer(
            _json_object(_json_object(_json_object(role)["resources"])["disk"])[
                "image_bytes"
            ],
            "role image bytes",
        )
        for role in roles
    )
    base_image_bytes = require_integer(
        build_resources["download_bytes"], "download bytes"
    )
    peak_bytes = max(temporary_bytes, base_image_bytes + source_bytes + output_bytes)
    disk_total_bytes = 2 * 1024**4
    required_bytes = peak_bytes + recipe_builds_module._build_disk_reserve(
        disk_total_bytes
    )
    # The envelope itself fits, but accepting it would violate the filesystem
    # reserve retained for the Spark host.
    newer = now + timedelta(seconds=1)
    InventoryRepository(sessions, clock=lambda: newer).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": newer,
                    "disk_total_bytes": disk_total_bytes,
                    "disk_free_bytes": required_bytes - 1,
                    "host_memory_total_bytes": 100_000,
                    "host_memory_free_bytes": 80_000,
                    "gpu_memory_total_bytes": 100_000,
                    "gpu_memory_free_bytes": 80_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ("recipe.build.v1",),
                    "memory_pool": "separate",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )

    with pytest.raises(RecipeBuildError):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=newer
        )

    fresh_time = newer + timedelta(seconds=1)
    _restore_builder_inventory(sessions, node_id, fresh_time)
    assert (
        RecipeBuildService(sessions, bundles=bundles)
        .plan(revision.id, node_id, now=fresh_time)
        .recipe_content_sha256
        == revision.content_digest
    )


def test_build_plan_accepts_public_network_only_with_egress_boundary_capability(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(
        tmp_path, network={"hosts": ["pypi.org"]}
    )

    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    assert plan.agent_payload["network"] == {"hosts": ["pypi.org"]}

    # Job claims report executable operations; probed host capabilities are
    # recorded separately by inventory. Exercise the contact write that happens
    # every poll before planning another public-network build.
    with sessions.begin() as session:
        session.add(
            AgentCertificate(
                serial="builder-serial",
                node_id=node_id,
                not_before=now - timedelta(seconds=1),
                not_after=now + timedelta(hours=1),
                fingerprint="builder-fingerprint",
            )
        )
    assert (
        AgentJobService(sessions, clock=lambda: now).claim(
            node_id,
            "builder-serial",
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "1.2.3",
                "build_digest": "sha256:" + "a" * 64,
                "binary_digest": "1" * 64,
            },
        )
        is None
    )
    after_claim = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    assert after_claim.build_input_sha256 == plan.build_input_sha256


def test_public_build_rejects_stale_inventory_without_egress_capability(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(
        tmp_path, network={"hosts": ["pypi.org"]}
    )
    newer = now + timedelta(seconds=1)
    InventoryRepository(sessions, clock=lambda: newer).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": newer,
                    "disk_total_bytes": 2 * 1024**4,
                    "disk_free_bytes": 1 * 1024**4,
                    "host_memory_total_bytes": 100_000,
                    "host_memory_free_bytes": 80_000,
                    "gpu_memory_total_bytes": 100_000,
                    "gpu_memory_free_bytes": 80_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ("recipe.build.v1", "recipe.image.import.v1"),
                    "memory_pool": "separate",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )

    with pytest.raises(RecipeBuildError):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=newer
        )

    fresh_time = newer + timedelta(seconds=1)
    _restore_builder_inventory(sessions, node_id, fresh_time)
    assert (
        RecipeBuildService(sessions, bundles=bundles)
        .plan(revision.id, node_id, now=fresh_time)
        .recipe_content_sha256
        == revision.content_digest
    )


def test_source_check_returns_the_structured_pre_dispatch_policy_report(
    tmp_path: Path,
) -> None:
    sessions, bundles, _now, _node_id, revision = setup(tmp_path)
    # The check is independent of builder capacity and exposes every finding to the UI.
    report = RecipeBuildService(sessions, bundles=bundles).check_source(revision.id)

    assert report.passed is True
    assert report.findings == ()
    assert report.source_bundle_sha256


def test_build_result_refreshes_upload_evidence_after_a_retried_attempt(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    old_image = "sha256:" + "a" * 64
    new_image = "sha256:" + "b" * 64
    old_layout = "c" * 64
    new_layout = "d" * 64
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.image_digest = old_image
        build.oci_layout_sha256 = old_layout
        build.image_bytes = 400

    stale_session = sessions()
    try:
        stale_build = stale_session.get(RecipeBuild, plan.build_id)
        assert stale_build is not None and stale_build.image_digest == old_image
        with sessions.begin() as upload_session:
            uploaded = upload_session.get(RecipeBuild, plan.build_id)
            assert uploaded is not None
            uploaded.image_digest = new_image
            uploaded.oci_layout_sha256 = new_layout
            uploaded.image_bytes = 500

        record_build_evidence(
            stale_session,
            stale_build,
            {
                "image_bytes": 500,
                "image_digest": new_image,
                "oci_layout_sha256": new_layout,
            },
            now=now,
        )
        assert stale_build.image_digest == new_image
    finally:
        stale_session.rollback()
        stale_session.close()


@pytest.mark.parametrize(
    "settings",
    [
        {
            "kind": "generation",
            "context_tokens": 4096,
            "change_effects": {"context_tokens": "rebuild"},
        },
        None,
    ],
)
def test_build_readers_reject_retired_or_null_persisted_settings(
    tmp_path, settings
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        document = copy.deepcopy(row.document)
        document["settings"] = settings
        # Corrupt stored JSON directly so the read boundary is exercised;
        # ordinary catalog writes independently enforce immutability.
        table = CatalogDocumentRevision.__table__
        assert isinstance(table, Table)
        session.execute(
            table.update()
            .where(CatalogDocumentRevision.id == revision.id)
            .values(document=document)
        )
    service = RecipeBuildService(sessions, bundles=bundles)
    for action in (
        lambda: service.resolve(revision.id),
        lambda: service.plan(revision.id, node_id, now=now),
    ):
        with pytest.raises(RecipeBuildError):
            action()

    with sessions() as session:
        assert session.scalar(select(RecipeBuild)) is None

    with sessions.begin() as session:
        session.execute(
            table.update()
            .where(CatalogDocumentRevision.id == revision.id)
            .values(document=revision.document)
        )
    assert service.resolve(revision.id).recipe_content_sha256 == revision.content_digest
    assert (
        service.plan(revision.id, node_id, now=now).recipe_content_sha256
        == revision.content_digest
    )


def test_build_readers_require_persisted_settings(tmp_path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        document = copy.deepcopy(row.document)
        document.pop("settings")
        # Corrupt stored JSON directly so the read boundary is exercised;
        # ordinary catalog writes independently enforce immutability.
        table = CatalogDocumentRevision.__table__
        assert isinstance(table, Table)
        session.execute(
            table.update()
            .where(CatalogDocumentRevision.id == revision.id)
            .values(document=document)
        )
    service = RecipeBuildService(sessions, bundles=bundles)
    with pytest.raises(RecipeBuildError):
        service.resolve(revision.id)
    with pytest.raises(RecipeBuildError):
        service.plan(revision.id, node_id, now=now)

    with sessions.begin() as session:
        session.execute(
            table.update()
            .where(CatalogDocumentRevision.id == revision.id)
            .values(document=revision.document)
        )
    assert service.resolve(revision.id).recipe_content_sha256 == revision.content_digest
    assert (
        service.plan(revision.id, node_id, now=now).recipe_content_sha256
        == revision.content_digest
    )


def test_persisted_canonical_settings_preserve_build_identity_and_rebuild_changes(
    tmp_path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    catalog = CatalogEntityService(sessions, clock=lambda: now)
    document = copy.deepcopy(revision.document)
    _json_object(document["settings"])["knobs"] = {
        "compiler": {"value": "clang", "change_effect": "rebuild"},
        "enabled": {"value": False, "change_effect": "rebuild"},
        "count": {"value": 0, "change_effect": "rebuild"},
        "label": {"value": "", "change_effect": "rebuild"},
    }
    canonical = RecipeDefinition.model_validate(document)

    def publish(document):
        draft = catalog.revise(revision.document_id, document, actor="admin")
        with sessions.begin() as session:
            stored = session.get(CatalogDocumentRevision, draft.id)
            assert stored is not None
            stored.projected = copy.deepcopy(revision.projected)
            stored.source_bundle_sha256 = revision.source_bundle_sha256
        return catalog.resolve(draft.id, actor="admin")

    selected = publish(canonical.model_dump(mode="json"))
    service = RecipeBuildService(sessions, bundles=bundles)
    first = service.plan(selected.id, node_id, now=now)
    assert recipe_builds_module._build_effective_settings(canonical.settings) == {
        "values": {
            "knobs.compiler": "clang",
            "knobs.enabled": False,
            "knobs.count": 0,
            "knobs.label": "",
        },
        "change_effects": {
            name: "rebuild"
            for name in (
                "knobs.compiler",
                "knobs.enabled",
                "knobs.count",
                "knobs.label",
            )
        },
    }
    assert (
        service.plan(selected.id, node_id, now=now).build_input_sha256
        == first.build_input_sha256
    )
    _json_object(_json_object(_json_object(document["settings"])["knobs"])["compiler"])[
        "value"
    ] = "gcc"
    changed = publish(document)
    assert (
        service.plan(changed.id, node_id, now=now).build_input_sha256
        != first.build_input_sha256
    )


def test_source_read_fault_keeps_exact_parent_and_resumes_one_build(
    tmp_path, monkeypatch
):
    import errno

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    rederived = []
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        source_rederiver=lambda *args: rederived.append(args),
    )
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )

    def production():
        return build_recipe_image_availability(
            sessions,
            artifact_root=tmp_path / "artifacts",
            storage=FilesystemRuntimeImageStorage(tmp_path / "artifacts"),
            recipe_builds=builds,
            recipe_operations=operations,
            clock=lambda: now,
        )

    first = production()
    reviewed = builds.prepare_plan(revision.id, node_id, now=now)
    queued = first.service.start(
        revision.id,
        actor="operator",
        request_id="source-read-recovery",
        force_rebuild=True,
        build_input_sha256=reviewed.build_input_sha256,
    )
    source_path = next((tmp_path / "bundles").rglob("*.tar"))
    verified_bytes = source_path.read_bytes()
    read_bytes = Path.read_bytes
    fault: list[bool | str] = [True]

    def read(path):
        if path == source_path and fault[0]:
            if fault[0] == "permission":
                raise PermissionError(errno.EACCES, "NAS access denied")
            raise OSError(errno.EIO, "temporary NAS read fault")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        builds.reusable_build_id(revision.id)
    assert first.service.run_pending() == 1
    waiting = first.service.get(queued.id)
    assert waiting.state == "queued"
    assert waiting.failure is not None

    assert waiting.failure["retryable"] is True
    assert waiting.failure["retry_time"] is not None
    assert rederived == []
    with sessions() as session:
        assert (
            tuple(session.scalars(select(Job).where(Job.kind == "recipe.build.v1")))
            == ()
        )
        parent = session.get(Job, queued.id)
        assert parent is not None and parent.request_id == "source-read-recovery"
        bound_digest = parent.payload["build_input_sha256"]
    first.close()
    fault[0] = False
    assert source_path.read_bytes() == verified_bytes
    now += timedelta(minutes=1)
    restarted = production()
    assert restarted.service.run_pending() == 1
    waiting = restarted.service.get(queued.id)
    assert waiting.state == "queued"
    assert waiting.failure is not None

    now += timedelta(minutes=1)
    assert restarted.service.run_pending() == 1
    with sessions() as session:
        children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
        assert len(children) == 1
        assert children[0].payload["plan_digest"] == bound_digest
        parent = session.get(Job, queued.id)
        assert parent is not None and parent.request_id == "source-read-recovery"
    assert rederived == []
    fault[0] = "permission"
    from vonk_agent_protocol import Disposition, SecurityRefusalError
    from vonk_control.recipe_image_availability import availability_disposition

    with pytest.raises(SecurityRefusalError) as denied:
        builds.reusable_build_id(revision.id)
    assert availability_disposition(denied.value) is Disposition.SECURITY
    fault[0] = False
    assert (
        builds.prepare_plan(revision.id, node_id, now=now).recipe_content_sha256
        == revision.content_digest
    )
    restarted.close()


def test_matching_late_success_heals_failed_build_without_new_execution(
    tmp_path: Path,
) -> None:
    """A failed label must not defeat authenticated matching content completion."""
    from vonk_agent_protocol import LifecycleState

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.state = LifecycleState.FAILED.value
        build.error = "lost completion observation"
    result = service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        assert build.state == LifecycleState.SUCCEEDED.value
        assert build.error is None
        assert build.image_digest == result.image_digest
    assert service.plan(revision.id, node_id, now=now).build_id == plan.build_id


@pytest.mark.parametrize("damage", ["lost", "succeeded-evidence"])
def test_verified_completion_does_not_let_lost_or_damaged_index_block_new_request(
    tmp_path: Path,
    damage: str,
) -> None:
    from vonk_agent_protocol import LifecycleState

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    accepted = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        row = session.get(RecipeBuild, accepted.build_id)
        assert row is not None
        if damage == "lost":
            session.delete(row)
        else:
            row.state = LifecycleState.SUCCEEDED.value
            row.image_digest = "sha256:" + "0" * 64
            row.oci_layout_sha256 = "0" * 64
            row.image_bytes = 1
    completion = service.record_success(
        accepted.build_id,
        build_input_sha256=accepted.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    fresh = service.plan(revision.id, node_id, now=now)
    assert fresh.build_input_sha256 == accepted.build_input_sha256
    if damage == "lost":
        assert fresh.build_id != accepted.build_id
        service.record_success(
            fresh.build_id,
            build_input_sha256=fresh.build_input_sha256,
            image_digest=completion.image_digest,
            oci_layout_sha256=completion.oci_layout_sha256,
            image_bytes=completion.image_bytes,
            now=now,
        )
        # An obsolete completion cannot write the newer request's row.
        service.record_success(
            accepted.build_id,
            build_input_sha256=accepted.build_input_sha256,
            image_digest="sha256:" + "d" * 64,
            oci_layout_sha256="e" * 64,
            image_bytes=999,
            now=now,
        )
    with sessions() as session:
        current = session.get(RecipeBuild, fresh.build_id)
        assert current is not None
        assert current.state == LifecycleState.SUCCEEDED.value
        assert current.image_digest == completion.image_digest
        assert current.oci_layout_sha256 == completion.oci_layout_sha256
        assert current.image_bytes == completion.image_bytes


@pytest.mark.parametrize("field", ["image_digest", "oci_layout_sha256", "image_bytes"])
def test_succeeded_build_with_lost_result_is_reprepared(
    tmp_path: Path, field: str
) -> None:
    """Lost completion fields must not replay an incomplete success forever."""
    from vonk_agent_protocol import LifecycleState

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        setattr(build, field, None)
    repaired = service.plan(revision.id, node_id, now=now)
    assert repaired.build_id == plan.build_id
    assert repaired.build_input_sha256 == plan.build_input_sha256
    with sessions() as session:
        build = session.get(RecipeBuild, repaired.build_id)
        assert build is not None and build.state != LifecycleState.SUCCEEDED
    service.record_success(
        repaired.build_id,
        build_input_sha256=repaired.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    assert service.resolve(revision.id).cached
