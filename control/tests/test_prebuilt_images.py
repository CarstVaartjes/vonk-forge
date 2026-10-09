"""Catalog prebuilt images retain their accepted content through retries.

These run the real producer and consumer seams: the CI planner keys a real
recipe package, the signed index carries the pushed digest, catalog sync
records it, build planning prefers it without admitting any Spark resources,
the build job is executed by the Controller importer, and the result is the
same reusable build receipt a Spark upload leaves.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control.auth import TokenCodec
from vonk_control.availability_production import build_recipe_image_availability
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Base,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    ResourceReservation,
)
from vonk_control.oci_image_store import (
    STORE_BUSY,
    OciImageStore,
    StoredImage,
    StoreUnknown,
)
from vonk_control.prebuilt_images import (
    PrebuiltImageImporter,
    write_library_image_plan,
)
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_library_types import RecipeLibrarySnapshot
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.run_switch_contract import SparkGroup, SparkGroupNode, StopImpact
from vonk_control.run_switch_operations import (
    ArtifactInspection,
    RunSwitchOperationService,
)
from vonk_control.runtime_image_preparation import (
    IMAGE_CACHE_DIRECTORY,
    FilesystemRuntimeImageStorage,
)
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from tests.recipe_library_source import recipe_library_root
from tests.runtime_image_fixtures import place_test_image
from tests.signed_recipe_release import SignedRecipeRelease, signed_recipe_releases
from tests.test_recipe_builds import RecordingQueue

from .stored_documents_support import valid_policy_report

pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)
ROOT = recipe_library_root()
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
NODE = "spk_" + "7" * 32
REFERENCE = "ghcr.io/example/vonk-forge-recipe-example@sha256:" + "e" * 64
ADDRESS = "e" * 64
IMAGE_BYTES = 4096


class _Reader:
    def __init__(self, snapshot: RecipeLibrarySnapshot) -> None:
        self.snapshot = snapshot

    def list(self) -> RecipeLibrarySnapshot:
        return self.snapshot

    def fetch(self, uri: str):
        return next(item for item in self.snapshot.items if item.uri == uri)


def _published_library(
    tmp_path: Path, *, build_key: str | None = None
) -> tuple[sessionmaker, str]:
    """Sync a one-recipe signed library whose index pins a prebuilt image.

    The key comes from the CI planner run over the real package, exactly as
    the recipe repository's image workflow computes it, unless a test
    supplies a different one.
    """

    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    index["recipes"] = index["recipes"][:1]
    library = tmp_path / "library"
    library.mkdir()
    (library / "catalog-index.json").write_text(json.dumps(index), encoding="utf-8")
    (library / "packages").symlink_to(ROOT / "packages")
    plan = write_library_image_plan(library, tmp_path / "plan")
    assert plan["skipped"] == []
    (image,) = require_sequence(plan["images"], "planned images")
    image = require_mapping(image, "planned image")
    index["recipes"][0]["prebuilt_image"] = {
        "reference": REFERENCE,
        "build_key": build_key or image["build_key"],
    }
    client = SignedRecipeRelease.from_library(index, ROOT).client(tmp_path / "packages")
    listed = client.list()
    item = client.fetch(listed.items[0].uri)
    recipe = RecipeDefinition.model_validate(item.document)
    selected = {
        (selection.model.publisher, selection.model.slug) for selection in recipe.models
    }
    snapshot = RecipeLibrarySnapshot(
        listed.commit,
        (item,),
        listed.repository,
        tuple(
            entity
            for entity in listed.catalog_entities
            if (
                (model := ModelDefinition.model_validate(entity)).identity.publisher,
                model.identity.slug,
            )
            in selected
        ),
        version=listed.version,
        updated_at=listed.updated_at,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions,
        clock=lambda: NOW,
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )
    ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=_Reader(snapshot), clock=lambda: NOW
    ).automatic()
    with sessions.begin() as session:
        # A Spark with no inventory at all: any Spark build admission fails.
        session.add(
            AgentNode(
                node_id=NODE,
                state="active",
                architecture="linux-arm64",
                semantic_version="1.2.3",
                build_digest="sha256:" + "a" * 64,
                binary_digest="1" * 64,
                last_seen_at=NOW,
            )
        )
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        pinned = require_mapping(revision.projected["prebuilt_image"], "prebuilt")
        assert pinned["reference"] == REFERENCE
        return sessions, revision.id


def _services(sessions: sessionmaker, tmp_path: Path):
    builds = RecipeBuildService(
        sessions,
        bundles=SourceBundleStore(tmp_path / "bundles"),
        build_archive_available=FilesystemRuntimeImageStorage(
            tmp_path
        ).build_archive_available,
    )
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: NOW,
        builds=builds,
    )
    return builds, operations


class _Registry(OciImageStore):
    """GHCR as the importer sees it: a pinned copy lands in the layout."""

    def __init__(
        self,
        artifact_root: Path,
        *,
        failure: StoreUnknown | None = None,
        during_copy=None,
    ) -> None:
        super().__init__(artifact_root)
        self._artifact_root = artifact_root
        self._failure = failure
        self._during_copy = during_copy
        self.copied: list[str] = []

    def import_reference(self, reference: str) -> StoredImage | StoreUnknown:
        self.copied.append(reference)
        if self._during_copy is not None:
            self._during_copy()
        if self._failure is not None:
            return self._failure
        address = reference.rsplit("@", 1)[1].removeprefix("sha256:")
        place_test_image(
            FilesystemRuntimeImageStorage(self._artifact_root), address, IMAGE_BYTES
        )
        image = self.read(f"sha256:{address}")
        assert isinstance(image, StoredImage)
        return image

    def import_archive(self, archive: Path) -> StoredImage | StoreUnknown:
        # A Spark upload converts to the image its bytes describe.
        place_test_image(
            FilesystemRuntimeImageStorage(self._artifact_root), ADDRESS, IMAGE_BYTES
        )
        image = self.read(f"sha256:{ADDRESS}")
        assert isinstance(image, StoredImage)
        return image


def _start(
    builds,
    operations,
    revision_id: str,
    *,
    request_id: str = "00000000-0000-4000-8000-00000000b001",
):
    with builds._sessions.begin() as session:
        plan = builds.persist_plan_in_session(
            session, builds.prepare_plan(revision_id, NODE, now=NOW), now=NOW
        )
    job = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="test",
        request_id=request_id,
    )
    return plan, job


def test_a_recipe_the_controller_refuses_gets_no_image_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vonk_control.recipe_builds import common as recipe_builds
    from vonk_control.source_policy import (
        SourcePolicyFinding,
        SourcePolicyReport,
    )

    def refuse(document, bundle, *, source_sha256=None):
        finding = SourcePolicyFinding(
            "dockerfile.heredoc_forbidden", "Dockerfile", 23, "no heredocs"
        )
        return SourcePolicyReport(False, bundle.sha256, "Dockerfile", (finding,))

    monkeypatch.setattr(recipe_builds, "inspect_package_source_policy", refuse)
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    index["recipes"] = index["recipes"][:1]
    library = tmp_path / "library"
    library.mkdir()
    (library / "catalog-index.json").write_text(json.dumps(index), encoding="utf-8")
    (library / "packages").symlink_to(ROOT / "packages")

    plan = write_library_image_plan(library, tmp_path / "plan")

    assert plan["images"] == []
    (skipped,) = require_sequence(plan["skipped"], "skipped recipes")
    assert "dockerfile.heredoc_forbidden Dockerfile:23" in str(skipped)


def test_catalog_prebuilt_image_is_pulled_instead_of_built_on_a_spark(
    tmp_path: Path,
) -> None:
    sessions, revision_id = _published_library(tmp_path)
    builds, operations = _services(sessions, tmp_path)

    # The Spark has no build inventory, so a Spark build could not be
    # admitted; the prebuilt plan needs none.
    plan, job = _start(builds, operations, revision_id)
    assert plan.policy_report is not None
    assert plan.policy_report["prebuilt_image"] == REFERENCE
    with sessions() as session:
        assert session.scalars(select(AgentOperation)).all() == []
        assert session.scalars(select(ResourceReservation)).all() == []

    registry = _Registry(tmp_path)
    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=registry
    )
    assert importer.run_pending() == 1
    assert importer.run_pending() == 0

    assert registry.copied == [REFERENCE]
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        stored = session.get(Job, job.id)
        assert build is not None and stored is not None
        # The build is the stored image: addressed by its manifest digest,
        # sized by its layers.
        assert (
            build.state,
            build.image_digest,
            build.oci_layout_sha256,
            build.image_bytes,
        ) == ("succeeded", REFERENCE.rsplit("@", 1)[1], ADDRESS, IMAGE_BYTES)
        assert stored.state == "succeeded"
        assert stored.result["node_evidence"][NODE]["oci_layout_sha256"] == ADDRESS

    # The pulled image is the reusable receipt for these build inputs.
    resolution = builds.resolve(revision_id)
    assert resolution.cached and resolution.build_id == plan.build_id


def test_pinned_prebuilt_content_is_reused_despite_source_key_provenance(
    tmp_path: Path,
) -> None:
    sessions, revision_id = _published_library(tmp_path, build_key="0" * 64)
    builds, _operations = _services(sessions, tmp_path)

    planned = builds.prepare_plan(revision_id, NODE, now=NOW)
    assert planned.policy_report is not None
    assert planned.policy_report["prebuilt_image"] == REFERENCE
    with sessions() as session:
        assert not tuple(session.scalars(select(ResourceReservation)))
        assert not tuple(session.scalars(select(AgentOperation)))
        assert not tuple(session.scalars(select(RecipeBuild)))
    _record_build_capacity(sessions)
    fresh = builds.prepare_plan(revision_id, NODE, now=NOW)
    assert fresh.policy_report is not None
    assert fresh.policy_report["prebuilt_image"] == REFERENCE


def test_failed_pull_ends_and_a_fresh_request_recovers_the_pinned_image(
    tmp_path: Path,
) -> None:
    sessions, revision_id = _published_library(tmp_path)
    builds, operations = _services(sessions, tmp_path)
    plan, job = _start(builds, operations, revision_id)

    importer = PrebuiltImageImporter(
        sessions,
        tmp_path,
        clock=lambda: NOW,
        store=_Registry(
            tmp_path,
            failure=StoreUnknown("image_store.copy_failed", "manifest unknown"),
        ),
    )
    assert importer.run_pending() == 1

    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        stored = session.get(Job, job.id)
        assert build is not None and stored is not None
        assert build.state == "failed"
        assert build.image_digest is None and build.oci_layout_sha256 is None
        assert stored.state == "failed"

    # A new request retries the authority-pinned image immediately. A failed
    # historical pull cannot substitute a Spark-built image or gate admission.
    fresh_plan, fresh_job = _start(
        builds, operations, revision_id, request_id="fresh-pull"
    )
    assert fresh_plan.policy_report is not None
    assert fresh_plan.policy_report["prebuilt_image"] == REFERENCE
    assert fresh_job.id != job.id
    later = NOW + timedelta(minutes=5)
    retried = builds.prepare_plan(revision_id, NODE, now=later)
    assert retried.policy_report is not None
    assert retried.policy_report["prebuilt_image"] == REFERENCE
    recovered = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: later, store=_Registry(tmp_path)
    )
    assert recovered.run_pending() == 1
    assert operations.get(fresh_job.id).state == LifecycleState.SUCCEEDED
    with sessions() as session:
        assert not tuple(session.scalars(select(AgentOperation)))
        assert not tuple(session.scalars(select(ResourceReservation)))


def test_cancelled_prebuilt_build_discards_a_late_pull(tmp_path: Path) -> None:
    sessions, revision_id = _published_library(tmp_path)
    builds, operations = _services(sessions, tmp_path)
    plan, job = _start(builds, operations, revision_id)

    def cancel_during_pull() -> None:
        operations._cancel_build(
            job.id,
            actor="test",
            request_id="00000000-0000-4000-8000-00000000c001",
            reason="operator cancelled",
        )

    importer = PrebuiltImageImporter(
        sessions,
        tmp_path,
        clock=lambda: NOW,
        store=_Registry(tmp_path, during_copy=cancel_during_pull),
    )
    assert importer.run_pending() == 1
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        stored = session.get(Job, job.id)
        assert build is not None and stored is not None
        assert stored.state == "cancelled"
        assert build.state == "failed" and build.image_digest is None

    # Cancelling is not a failed pull: the next plan uses the image again.
    again = builds.prepare_plan(revision_id, NODE, now=NOW)
    assert again.policy_report is not None
    assert again.policy_report["prebuilt_image"] == REFERENCE


def test_off_target_build_runs_once_before_an_early_memory_stop() -> None:
    """A prebuilt pull (or any build outside the group) is planned once.

    It runs first, while the old workload still serves; the stop the memory
    fit needs is not doubled into a second build phase after it.
    """

    group = SparkGroup(
        nodes=[SparkGroupNode(node_id=NODE, rank=0, role="worker", endpoint_owner=True)]
    )
    stop = StopImpact(
        run_id="00000000-0000-4000-8000-00000000d001",
        run_plan_digest="1" * 64,
        alias="glm",
        state="running",
        node_ids=[NODE],
        reserved_bytes=1,
        plan_digest="2" * 64,
    )
    inspection = ArtifactInspection(
        required_bytes=0,
        reused_bytes=0,
        copied_bytes=0,
        missing_nas_bytes=0,
        missing_spark_bytes=0,
        reclaimable_bytes=0,
        nas_coverage="complete",
        spark_coverage="complete",
    )
    phases = RunSwitchOperationService._phases(
        object.__new__(RunSwitchOperationService),
        action="switch",
        group=group,
        installation_id=None,
        installation_state=None,
        stops=[stop],
        inspection=inspection,
        runtime_storage=None,
        retention="retain-cached",
        blockers=[],
        stop_before_transfer=False,
        stop_before_prepare=True,
        build_required=True,
        build_on_target=False,
    )
    kinds = [(phase.kind, phase.subphase) for phase in phases]
    assert kinds.count(("prepare", "container-build")) == 1
    assert kinds.index(("prepare", "container-build")) < kinds.index(("stop", None))


def _availability(sessions, tmp_path: Path, monkeypatch, clock):
    builds, operations = _services(sessions, tmp_path)
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path,
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=clock,
    )
    return production, builds


def test_image_preparation_pulls_the_prebuilt_image_without_a_spark_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, revision_id = _published_library(tmp_path)
    now = [NOW]
    production, _builds = _availability(sessions, tmp_path, monkeypatch, lambda: now[0])
    operation = production.service.start(
        revision_id,
        actor="operator",
        request_id="00000000-0000-4000-8000-00000000a001",
    )
    assert production.service.run_pending() == 1
    waiting = production.service.get(operation.id)
    assert waiting.state != "succeeded"
    # The wait says the Controller pulls the image, not that a Spark builds it.
    blockers = {blocker.code: blocker.detail for blocker in waiting.blockers}
    assert REFERENCE in blockers["prebuilt.used"]
    assert "Controller to pull prebuilt image" in blockers["recipe_image.build_wait"]

    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: now[0], store=_Registry(tmp_path)
    )
    assert importer.run_pending() == 1
    now[0] += timedelta(minutes=5)
    assert production.service.run_pending() == 1

    completed = production.service.get(operation.id)
    assert completed.state == "succeeded", completed.failure
    assert completed.result is not None
    assert completed.result["image_digest"] == REFERENCE.rsplit("@", 1)[1]
    with sessions() as session:
        assert session.scalars(select(AgentOperation)).all() == []
    production.close()


def _record_build_capacity(sessions) -> None:
    InventoryRepository(sessions, clock=lambda: NOW).record(
        InventorySnapshotInput(
            NODE,
            NOW,
            2 * 1024**4,
            1 * 1024**4,
            512 * 1024**3,
            480 * 1024**3,
            512 * 1024**3,
            480 * 1024**3,
            1,
            False,
            (
                "recipe.build.v1",
                "recipe.build.egress-proxy.v1",
                "recipe.image.import.v1",
                "runtime.vonk.v1",
                "recipe.image.pull.v1",
            ),
            memory_pool="separate",
        )
    )


def test_pinned_prebuilt_preparation_does_not_allocate_spark_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Available Spark capacity cannot substitute different content for the pin."""

    sessions, revision_id = _published_library(tmp_path, build_key="0" * 64)
    now = [NOW]
    production, _builds = _availability(sessions, tmp_path, monkeypatch, lambda: now[0])
    # Even with build capacity, preparation pulls the accepted image on the Controller.
    _record_build_capacity(sessions)

    operation = production.service.start(
        revision_id,
        actor="operator",
        request_id="00000000-0000-4000-8000-00000000a003",
    )
    assert production.service.run_pending() == 1

    with sessions() as session:
        (build,) = session.scalars(select(RecipeBuild)).all()
        assert build.policy_report["prebuilt_image"] == REFERENCE
        assert not tuple(session.scalars(select(AgentOperation)))
        assert not tuple(session.scalars(select(ResourceReservation)))

    view = production.service.get(operation.id)
    assert view.state == LifecycleState.QUEUED
    assert view.next_attempt_at is not None
    blockers = {blocker.code: blocker.detail for blocker in view.blockers}
    assert REFERENCE in blockers["prebuilt.used"]
    assert "Controller to pull prebuilt image" in blockers["recipe_image.build_wait"]

    production.close()


def test_failed_prebuilt_pull_retries_the_same_pinned_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, revision_id = _published_library(tmp_path)
    now = [NOW]
    production, _builds = _availability(sessions, tmp_path, monkeypatch, lambda: now[0])
    operation = production.service.start(
        revision_id,
        actor="operator",
        request_id="00000000-0000-4000-8000-00000000a002",
    )
    assert production.service.run_pending() == 1

    importer = PrebuiltImageImporter(
        sessions,
        tmp_path,
        clock=lambda: now[0],
        store=_Registry(
            tmp_path, failure=StoreUnknown("image_store.copy_failed", "denied")
        ),
    )
    assert importer.run_pending() == 1
    for _ in range(3):
        now[0] += timedelta(seconds=90)
        production.service.run_pending()

    view = production.service.get(operation.id)
    assert view.state != "succeeded"
    with sessions() as session:
        builds = session.scalars(select(RecipeBuild)).all()
        prebuilt_jobs = [
            job
            for job in session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
            if job.payload.get("prebuilt_image") is not None
        ]
    # Recovery preserves the accepted image and creates a fresh child identity.
    assert len(prebuilt_jobs) == 2
    assert prebuilt_jobs[0].request_id != prebuilt_jobs[1].request_id
    assert all(build.policy_report["prebuilt_image"] == REFERENCE for build in builds)
    assert view.next_attempt_at is not None
    with sessions() as session:
        assert not tuple(session.scalars(select(AgentOperation)))
        assert not tuple(session.scalars(select(ResourceReservation)))
    recovered = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: now[0], store=_Registry(tmp_path)
    )
    assert recovered.run_pending() == 1
    now[0] += timedelta(minutes=5)
    assert production.service.run_pending() == 1
    assert production.service.get(operation.id).state == LifecycleState.SUCCEEDED
    fresh = production.service.start(
        revision_id,
        actor="operator",
        request_id="00000000-0000-4000-8000-00000000a004",
    )
    assert fresh.id != operation.id
    assert production.service.run_pending() == 1
    assert production.service.get(fresh.id).state == LifecycleState.SUCCEEDED

    production.close()


def test_a_busy_store_hands_the_pull_back_instead_of_failing_it(
    tmp_path: Path,
) -> None:
    sessions, revision_id = _published_library(tmp_path)
    builds, operations = _services(sessions, tmp_path)
    plan, job = _start(builds, operations, revision_id)

    busy = PrebuiltImageImporter(
        sessions,
        tmp_path,
        clock=lambda: NOW,
        store=_Registry(
            tmp_path, failure=StoreUnknown(STORE_BUSY, "storing another image")
        ),
    )
    assert busy.run_pending() == 1
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        stored = session.get(Job, job.id)
        assert build is not None and stored is not None
        assert build.state != "failed" and stored.state == "running"

    # The next tick, on any Controller process, takes the pull up at once.
    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=_Registry(tmp_path)
    )
    assert importer.run_pending() == 1
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None and build.state == "succeeded"


def test_a_spark_build_upload_is_converted_into_the_store_after_success(
    tmp_path: Path,
) -> None:
    sessions, revision_id = _published_library(tmp_path)
    upload_sha256 = "a" * 64
    upload = tmp_path / IMAGE_CACHE_DIRECTORY / upload_sha256
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"docker archive from a Spark build")
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id="00000000-0000-4000-8000-00000000d001",
                recipe_revision_id=revision_id,
                builder_node_id=NODE,
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="succeeded",
                policy_report=valid_policy_report(),
                plan={},
                image_digest="sha256:" + "d" * 64,
                oci_layout_sha256=upload_sha256,
                image_bytes=len(b"docker archive from a Spark build"),
                created_at=NOW,
                updated_at=NOW,
            )
        )

    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=_Registry(tmp_path)
    )
    assert importer.run_pending() == 1
    assert importer.run_pending() == 0

    assert not upload.exists()
    with sessions() as session:
        build = session.get(RecipeBuild, "00000000-0000-4000-8000-00000000d001")
        assert build is not None
        assert (build.image_digest, build.oci_layout_sha256, build.image_bytes) == (
            f"sha256:{ADDRESS}",
            ADDRESS,
            IMAGE_BYTES,
        )
