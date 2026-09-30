"""Catalog prebuilt images replace the Spark build, and fall back to it.

These run the real producer and consumer seams: the CI planner keys a real
recipe package, the signed index carries the pushed digest, catalog sync
records it, build planning prefers it without admitting any Spark resources,
the build job is executed by the Controller importer, and the result is the
same reusable build receipt a Spark upload leaves.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import vonk_control.availability_production as availability_production_module
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.availability_production import build_recipe_image_availability
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Base,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    ResourceReservation,
)
from vonk_control.prebuilt_images import (
    PrebuiltImageImporter,
    PrebuiltImagePullError,
    write_library_image_plan,
)
from vonk_control.recipe_builds import RecipeBuildError, RecipeBuildService
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
    PulledImageEvidence,
)
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from tests.recipe_library_source import recipe_library_root
from tests.signed_recipe_release import SignedRecipeRelease, signed_recipe_releases
from tests.test_recipe_builds import RecordingQueue

pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)
ROOT = recipe_library_root()
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
NODE = "spk_" + "7" * 32
REFERENCE = "ghcr.io/example/vonk-forge-recipe-example@sha256:" + "e" * 64
ARCHIVE = b"prebuilt docker archive"


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


def _pull(reference: str, destination: Path, *, local_name: str) -> None:
    assert reference == REFERENCE
    assert local_name.startswith("localhost/vonk/recipe-build-")
    destination.write_bytes(ARCHIVE)


def _start(builds, operations, revision_id: str):
    with builds._sessions.begin() as session:
        plan = builds.persist_plan_in_session(
            session, builds.prepare_plan(revision_id, NODE, now=NOW), now=NOW
        )
    job = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="test",
        request_id="00000000-0000-4000-8000-00000000b001",
    )
    return plan, job


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

    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, puller=_pull
    )
    assert importer.run_pending() == 1
    assert importer.run_pending() == 0

    archive_sha256 = hashlib.sha256(ARCHIVE).hexdigest()
    assert (tmp_path / IMAGE_CACHE_DIRECTORY / archive_sha256).read_bytes() == ARCHIVE
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        stored = session.get(Job, job.id)
        assert build is not None and stored is not None
        assert (build.state, build.image_digest, build.oci_layout_sha256) == (
            "succeeded",
            REFERENCE.rsplit("@", 1)[1],
            archive_sha256,
        )
        assert stored.state == "succeeded"
        assert stored.result["node_evidence"][NODE]["oci_layout_sha256"] == (
            archive_sha256
        )

    # The pulled image is the reusable receipt for these build inputs.
    resolution = builds.resolve(revision_id)
    assert resolution.cached and resolution.build_id == plan.build_id


def test_prebuilt_image_from_other_inputs_falls_back_to_a_spark_build(
    tmp_path: Path,
) -> None:
    sessions, revision_id = _published_library(tmp_path, build_key="0" * 64)
    builds, _operations = _services(sessions, tmp_path)

    with pytest.raises(RecipeBuildError) as refused:
        builds.prepare_plan(revision_id, NODE, now=NOW)
    # A Spark build was planned, and it needs Spark inventory.
    assert refused.value.code == "build.inventory_missing"


def test_failed_pull_is_visible_and_the_next_plan_builds_on_a_spark(
    tmp_path: Path,
) -> None:
    sessions, revision_id = _published_library(tmp_path)
    builds, operations = _services(sessions, tmp_path)
    plan, job = _start(builds, operations, revision_id)

    def unavailable(reference: str, destination: Path, *, local_name: str) -> None:
        raise PrebuiltImagePullError("prebuilt_image_pull_failed", "manifest unknown")

    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, puller=unavailable
    )
    assert importer.run_pending() == 1

    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        stored = session.get(Job, job.id)
        assert build is not None and stored is not None
        assert build.state == "failed"
        assert build.error is not None and "manifest unknown" in build.error
        assert stored.state == "failed"
        failure = stored.result["node_evidence"][NODE]
        assert failure["error_code"] == "prebuilt_image_pull_failed"
        assert failure["failure_kind"] == "temporary-dependency"
    assert not list((tmp_path / IMAGE_CACHE_DIRECTORY).glob(".prebuilt-*"))

    with pytest.raises(RecipeBuildError) as refused:
        builds.prepare_plan(revision_id, NODE, now=NOW)
    assert refused.value.code == "build.inventory_missing"


def test_cancelled_prebuilt_build_discards_a_late_pull(tmp_path: Path) -> None:
    sessions, revision_id = _published_library(tmp_path)
    builds, operations = _services(sessions, tmp_path)
    plan, job = _start(builds, operations, revision_id)

    def cancel_during_pull(reference: str, destination: Path, *, local_name: str):
        operations._cancel_build(
            job.id,
            actor="test",
            request_id="00000000-0000-4000-8000-00000000c001",
            reason="operator cancelled",
        )
        destination.write_bytes(ARCHIVE)

    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, puller=cancel_during_pull
    )
    assert importer.run_pending() == 1
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        stored = session.get(Job, job.id)
        assert build is not None and stored is not None
        assert stored.state == "cancelled"
        assert build.state == "failed" and build.image_digest is None


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


class _Transport:
    """Inspects the pulled archive the way skopeo reports a runtime image."""

    def inspect_archive(
        self,
        archive: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str,
        expected_archive_bytes: int,
    ) -> PulledImageEvidence:
        assert archive.read_bytes() == ARCHIVE
        return PulledImageEvidence(
            manifest_digest=REFERENCE.rsplit("@", 1)[1],
            config_id="sha256:" + "c" * 64,
            local_reference=f"docker-archive:{archive}",
            architecture=expected_architecture,
            runtime_interface=expected_runtime_interface,
            archive_sha256=expected_archive_sha256,
            archive_bytes=expected_archive_bytes,
        )


def _availability(sessions, tmp_path: Path, monkeypatch, clock):
    builds, operations = _services(sessions, tmp_path)
    monkeypatch.setattr(
        availability_production_module, "SkopeoOCIImageTransport", _Transport
    )
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

    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: now[0], puller=_pull
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


def test_failed_prebuilt_pull_falls_back_to_a_spark_build_on_retry(
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

    def unavailable(reference: str, destination: Path, *, local_name: str) -> None:
        raise PrebuiltImagePullError("prebuilt_image_pull_failed", "denied")

    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: now[0], puller=unavailable
    )
    assert importer.run_pending() == 1
    for _ in range(3):
        now[0] += timedelta(minutes=16)
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
    # The pull ran once; the retry planned a Spark build, which waits for
    # this Spark's build inventory instead of pulling the same digest again.
    assert len(prebuilt_jobs) == 1
    assert [build.state for build in builds] == ["failed"]
    assert view.failure is not None
    assert view.failure["code"] == "recipe_image.build_capacity_wait"
    assert "build.inventory_missing" in {blocker.code for blocker in view.blockers}
    production.close()
