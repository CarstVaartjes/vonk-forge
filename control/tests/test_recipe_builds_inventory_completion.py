"""Recipe builds: inventory completion."""

from __future__ import annotations

import copy
import errno
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Table, select
from vonk_forge_contracts import RecipeDefinition

from vonk_agent_protocol.agent_words import Disposition
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.outcome import SecurityRefusalError
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.availability_production.production import (
    build_recipe_image_availability,
)
from vonk_control.catalog.entities import CatalogEntityService
from vonk_control.install_admission.service import InstallAdmissionService
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.jobs import Job
from vonk_control.recipe_builds.common import (
    RecipeBuildError,
    _build_effective_settings,
)
from vonk_control.recipe_builds.service import RecipeBuildService
from vonk_control.recipe_image_availability.contracts import (
    availability_disposition,
)
from vonk_control.recipe_operations.observation_helpers import record_build_evidence
from vonk_control.recipe_operations.service import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage

from .recipe_builds_support import (
    RecordingQueue,
    _json_object,
    _restore_builder_inventory,
    setup,
)


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
        assert stale_build is not None
        assert stale_build.image_digest == old_image
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
    assert _build_effective_settings(canonical.settings) == {
        "values": {
            "knobs.compiler": "clang",
            "knobs.enabled": False,
            "knobs.count": 0,
            "knobs.label": "",
        },
        "change_effects": dict.fromkeys(
            ("knobs.compiler", "knobs.enabled", "knobs.count", "knobs.label"), "rebuild"
        ),
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
        assert parent is not None
        assert parent.request_id == "source-read-recovery"
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
        assert parent is not None
        assert parent.request_id == "source-read-recovery"
    assert rederived == []
    fault[0] = "permission"

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
        assert build is not None
        assert build.state != LifecycleState.SUCCEEDED
    service.record_success(
        repaired.build_id,
        build_input_sha256=repaired.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    assert service.resolve(revision.id).cached
