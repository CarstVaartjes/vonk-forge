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
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import PrebuiltImage, RecipeDefinition

from tests.recipe_builds_support import RecordingQueue
from tests.recipe_library_source import recipe_library_root
from tests.runtime_image_fixtures import place_test_image
from tests.signed_recipe_release import SignedRecipeRelease, signed_recipe_releases
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.reason_codes.images import ImageStoreCode, PrebuiltImageCode
from vonk_agent_protocol.reason_codes.recipes import RecipeBuildCode
from vonk_control import prebuilt_images
from vonk_control.auth import TokenCodec
from vonk_control.availability_production.production import (
    build_recipe_image_availability,
)
from vonk_control.bounded_json import require_mapping
from vonk_control.catalog.service import CatalogService
from vonk_control.catalog.sync import ManagedRecipeCatalogSyncService
from vonk_control.image_store.oci import (
    IMAGE_CACHE_DIRECTORY,
    STORE_BUSY,
    OciImageStore,
    StoredImage,
    StoreUnknown,
)
from vonk_control.install_admission.service import InstallAdmissionService
from vonk_control.model_primitives import Base
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.execution import ResourceReservation
from vonk_control.models.fleet import AgentNode, AgentNodeContact
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.prebuilt_images import (
    PrebuiltImageImporter,
)
from vonk_control.recipe_builds.cancellation import request_build_cancellation
from vonk_control.recipe_builds.common import RecipeBuildUnknown
from vonk_control.recipe_builds.service import RecipeBuildService
from vonk_control.recipe_operations.service import RecipeOperationService
from vonk_control.recipe_packages.library_types import RecipeLibrarySnapshot
from vonk_control.run_switch_contract.evidence import StopImpact
from vonk_control.run_switch_contract.requests import SparkGroup, SparkGroupNode
from vonk_control.run_switch_operations.interfaces import ArtifactInspection
from vonk_control.run_switch_operations.service import RunSwitchOperationService
from vonk_control.runs.admission import RunAdmissionService
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_control.source_bundles import SourceBundleStore
from vonk_control.source_policy import (
    SourcePolicyFinding,
    SourcePolicyReport,
)

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture
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

    def prepare(self, snapshot) -> None:
        return None

    def fetch(self, uri: str):
        return next(item for item in self.snapshot.items if item.uri == uri)


def _one_recipe_index() -> dict[str, Any]:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    index["recipes"] = index["recipes"][:1]
    return index


def _library_snapshot(
    tmp_path: Path,
    pin: dict[str, str] | None,
    *,
    commit: str | None = None,
    cache: str = "packages",
) -> RecipeLibrarySnapshot:
    """A one-recipe signed library as the Controller reads it."""

    index = _one_recipe_index()
    if commit is not None:
        index["source_commit"] = commit
    if pin is not None:
        index["recipes"][0]["prebuilt_image"] = pin
    client = SignedRecipeRelease.from_library(index, ROOT).client(tmp_path / cache)
    listed = client.list()
    item = client.fetch(listed.items[0].uri)
    recipe = RecipeDefinition.model_validate(item.document)
    selected = {
        (selection.model.publisher, selection.model.slug) for selection in recipe.models
    }
    return RecipeLibrarySnapshot(
        listed.commit,
        (item,),
        listed.repository,
        tuple(
            entity
            for entity in listed.catalog_entities
            if (
                entity.entry.document.identity.publisher,
                entity.entry.document.identity.slug,
            )
            in selected
        ),
        version=listed.version,
        updated_at=listed.updated_at,
    )


def _catalog(sessions: sessionmaker[Session], tmp_path: Path) -> CatalogService:
    return CatalogService(
        sessions,
        clock=lambda: NOW,
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )


def _sync(
    sessions: sessionmaker[Session], tmp_path: Path, snapshot: RecipeLibrarySnapshot
) -> None:
    """One automatic catalog sync by a freshly started Controller."""

    ManagedRecipeCatalogSyncService(
        sessions,
        catalog=_catalog(sessions, tmp_path),
        reader=_Reader(snapshot),
        clock=lambda: NOW,
    ).automatic()


def _published_library(
    tmp_path: Path, *, build_key: str | None = None, pinned: bool = True
) -> tuple[sessionmaker[Session], str]:
    """Sync a one-recipe signed library whose index pins a prebuilt image.

    The key comes from the CI planner run over the real package, exactly as
    the recipe repository's image workflow computes it, unless a test
    supplies a different one.
    """

    pin = (
        {"reference": REFERENCE, "build_key": build_key or "a" * 64} if pinned else None
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    _sync(sessions, tmp_path, _library_snapshot(tmp_path, pin))
    with sessions.begin() as session:
        # A Spark with no inventory at all: any Spark build admission fails.
        session.add(
            AgentNode(
                node_id=NODE,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    NODE,
                    observed_at=NOW,
                    identity=AgentRuntimeIdentity.model_validate(
                        {
                            **PACKAGED_RUNTIME_IDENTITY,
                            "architecture": "linux-arm64",
                            "semantic_version": "1.2.3",
                            "build_digest": "sha256:" + "a" * 64,
                            "binary_digest": "1" * 64,
                        }
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=NOW),
            )
        )
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        if pinned:
            stored = require_mapping(revision.projected["prebuilt_image"], "prebuilt")
            assert stored["reference"] == REFERENCE
        return sessions, revision.id


def _stored_pin(sessions: sessionmaker[Session], revision_id: str) -> object:
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, revision_id)
        assert revision is not None
        return revision.projected.get("prebuilt_image")


def _services(sessions: sessionmaker[Session], tmp_path: Path):
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


def test_the_controller_derives_no_prebuilt_build_key() -> None:
    # Wrong implementation: forge keeps a planner or key derivation that
    # recipes CI (which now plans its own builds) could drift from.
    assert not hasattr(prebuilt_images, "executable_build_key")
    assert not hasattr(prebuilt_images, "write_library_image_plan")


def test_a_recipe_the_controller_refuses_is_reported_with_the_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    def refuse(document, bundle, *, source_sha256=None):
        finding = SourcePolicyFinding(
            "dockerfile.heredoc_forbidden", "Dockerfile", 23, "no heredocs"
        )
        return SourcePolicyReport(False, bundle.sha256, "Dockerfile", (finding,))

    monkeypatch.setattr(prebuilt_images, "inspect_package_source_policy", refuse)
    library = tmp_path / "library"
    library.mkdir()
    (library / "catalog-index.json").write_text(
        json.dumps(_one_recipe_index()), encoding="utf-8"
    )
    (library / "packages").symlink_to(ROOT / "packages")

    failures = prebuilt_images.library_source_policy_failures(library)

    (reason,) = failures.values()
    assert "dockerfile.heredoc_forbidden Dockerfile:23" in reason


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
        assert build is not None
        assert stored is not None
        # The build is the stored image: addressed by its manifest digest,
        # sized by its layers.
        assert (
            build.state,
            build.image_digest,
            build.oci_layout_sha256,
            build.image_bytes,
        ) == ("succeeded", REFERENCE.rsplit("@", 1)[1], ADDRESS, IMAGE_BYTES)
        assert stored.state == "succeeded"
        result = cast(dict[str, Any], stored.result)
        assert result["node_evidence"][NODE]["oci_layout_sha256"] == ADDRESS

    # The pulled image is the reusable receipt for these build inputs.
    resolution = builds.resolve(revision_id)
    assert resolution.cached
    assert resolution.build_id == plan.build_id


def test_a_pin_whose_key_forge_never_computed_is_pulled(tmp_path: Path) -> None:
    # Wrong implementation: the Controller re-derives the key and falls back to
    # a Spark build (here: refused for missing inventory) whenever recipes CI
    # keyed the image differently, so an older Controller never finds the
    # image a newer catalog pins.
    sessions, revision_id = _published_library(tmp_path, build_key="0" * 64)
    builds, operations = _services(sessions, tmp_path)

    plan, _job = _start(builds, operations, revision_id)

    assert plan.policy_report is not None
    assert plan.policy_report["prebuilt_image"] == REFERENCE
    decision = require_mapping(plan.policy_report["prebuilt_decision"], "decision")
    assert decision["code"] == PrebuiltImageCode.USED
    assert plan.policy_report["builder_binary_digest"] == ADDRESS
    with sessions() as session:
        assert session.scalars(select(AgentOperation)).all() == []
        assert session.scalars(select(ResourceReservation)).all() == []


def test_a_pin_recorded_for_other_content_is_never_used(tmp_path: Path) -> None:
    # Wrong implementation: a pin follows the slug rather than the exact
    # content digest, so a changed recipe runs the image built for its
    # previous bytes.
    sessions, revision_id = _published_library(tmp_path, pinned=False)
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, revision_id)
        assert revision is not None
        publisher, slug = revision.publisher, revision.slug
    _catalog(sessions, tmp_path).record_published_facts(
        {
            (publisher, slug, "f" * 64): PrebuiltImage(
                reference=REFERENCE, build_key="0" * 64
            )
        },
        {},
    )
    builds, _operations = _services(sessions, tmp_path)

    with pytest.raises(RecipeBuildUnknown) as refused:
        builds.prepare_plan(revision_id, NODE, now=NOW)

    assert _stored_pin(sessions, revision_id) is None
    assert refused.value.code == RecipeBuildCode.INVENTORY_MISSING
    assert refused.value.prebuilt_unused is not None
    assert refused.value.prebuilt_unused.code == PrebuiltImageCode.NOT_PINNED


def test_a_republished_key_keeps_the_pulled_image_across_a_restart(
    tmp_path: Path,
) -> None:
    # Wrong implementation: a new build key for the same digest re-plans the
    # revision (a fallback Spark build, a new build input identity, a re-pull)
    # under a workload that already runs the image.
    sessions, revision_id = _published_library(tmp_path)
    builds, operations = _services(sessions, tmp_path)
    plan, _job = _start(builds, operations, revision_id)
    registry = _Registry(tmp_path)
    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=registry
    )
    assert importer.run_pending() == 1

    # Recipes CI republishes the same image under its new key; a restarted
    # Controller ingests that catalog.
    republished = {"reference": REFERENCE, "build_key": "f" * 64}
    _sync(
        sessions,
        tmp_path,
        _library_snapshot(tmp_path, republished, commit="f" * 40, cache="packages-2"),
    )
    assert _stored_pin(sessions, revision_id) == republished

    restarted, _restarted_operations = _services(sessions, tmp_path)
    resolution = restarted.resolve(revision_id)
    assert resolution.cached
    assert resolution.build_id == plan.build_id
    replanned = restarted.prepare_plan(revision_id, NODE, now=NOW)
    assert replanned.build_input_sha256 == plan.build_input_sha256
    assert replanned.policy_report is not None
    decision = require_mapping(replanned.policy_report["prebuilt_decision"], "decision")
    assert decision["code"] == PrebuiltImageCode.USED
    assert importer.run_pending() == 0
    assert registry.copied == [REFERENCE]
    with sessions() as session:
        assert len(session.scalars(select(Job)).all()) == 1
        assert session.scalars(select(AgentOperation)).all() == []


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
        assert build is not None
        assert stored is not None
        assert build.state == "failed"
        assert build.image_digest is None
        assert build.oci_layout_sha256 is None
        assert stored.state == "failed"

    # A new request retries the authority-pinned image immediately. A failed
    # historical pull cannot substitute a Spark-built image or gate admission.
    fresh_plan, fresh_job = _start(
        builds, operations, revision_id, request_id="fresh-pull"
    )
    assert fresh_plan.policy_report is not None
    assert fresh_plan.policy_report["prebuilt_image"] == REFERENCE
    assert fresh_job.id != job.id
    recovered = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=_Registry(tmp_path)
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
        assert build is not None
        assert stored is not None
        assert stored.state == "cancelled"
        assert build.state == "failed"
        assert build.image_digest is None

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
        storage=FilesystemRuntimeImageStorage(tmp_path),
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
    blockers = {str(blocker.code): blocker.detail for blocker in waiting.blockers}
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
    with sessions() as session:

        def state(job_id: str) -> str:
            row = session.get(Job, job_id)
            assert row is not None
            return row.state

        assert any(state(job.id) == LifecycleState.SUCCEEDED for job in prebuilt_jobs)

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
        assert build is not None
        assert stored is not None
        assert build.state != "failed"
        assert stored.state == "running"

    # The next tick, on any Controller process, takes the pull up at once.
    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=_Registry(tmp_path)
    )
    assert importer.run_pending() == 1
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        assert build.state == "succeeded"


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


class _FailingArchive(_Registry):
    """A registry whose copy of one particular archive always fails."""

    def __init__(self, artifact_root: Path, bad_archive: str) -> None:
        super().__init__(artifact_root)
        self._bad: str | None = bad_archive
        self.attempted: list[str] = []

    def heal(self) -> None:
        self._bad = None

    def import_archive(self, archive: Path) -> StoredImage | StoreUnknown:
        self.attempted.append(archive.name)
        if archive.name == self._bad:
            return StoreUnknown(ImageStoreCode.COPY_FAILED, "unsupported archive")
        return super().import_archive(archive)


def _succeeded_upload(sessions, revision_id: str, tmp_path: Path, n: int, at) -> str:
    sha = f"{n:x}" * 64
    upload = tmp_path / IMAGE_CACHE_DIRECTORY / sha
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_bytes(b"docker archive")
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=f"00000000-0000-4000-8000-00000000e00{n}",
                recipe_revision_id=revision_id,
                builder_node_id=NODE,
                source_bundle_sha256="b" * 64,
                build_input_sha256=f"{n:x}" * 64,
                state="succeeded",
                policy_report=valid_policy_report(),
                plan={},
                image_digest="sha256:" + "d" * 64,
                oci_layout_sha256=sha,
                image_bytes=14,
                created_at=at,
                updated_at=at,
            )
        )
    return sha


_FAILING_BUILD = "00000000-0000-4000-8000-00000000e001"


def _error_of(sessions, build_id: str) -> str | None:
    with sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        return build.error


def test_one_archive_that_keeps_failing_does_not_block_the_next_one(
    tmp_path: Path,
) -> None:
    sessions, revision_id = _published_library(tmp_path)
    now = [NOW]
    bad = _succeeded_upload(
        sessions, revision_id, tmp_path, 1, NOW - timedelta(hours=2)
    )
    good = _succeeded_upload(
        sessions, revision_id, tmp_path, 2, NOW - timedelta(hours=1)
    )
    registry = _FailingArchive(tmp_path, bad)
    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: now[0], store=registry
    )

    assert importer.run_pending() == 1  # the older archive fails ...
    assert importer.run_pending() == 1  # ... and the next one gets its turn
    assert importer.run_pending() == 0
    assert registry.attempted == [bad, good]
    assert not (tmp_path / IMAGE_CACHE_DIRECTORY / good).exists()
    error = _error_of(sessions, _FAILING_BUILD)
    assert error is not None
    assert error.startswith(
        f"conversion failed: {ImageStoreCode.COPY_FAILED}; retried until the upload"
    )
    assert "expires at" in error

    # It is retried with a growing backoff, never in a tight loop.
    for _ in range(4):
        now[0] += timedelta(hours=3)
        assert importer.run_pending() == 1
    assert registry.attempted.count(bad) == 5
    assert _error_of(sessions, _FAILING_BUILD) == error  # still true, no attempt count


def test_a_restarted_importer_still_lets_the_next_archive_convert(
    tmp_path: Path,
) -> None:
    sessions, revision_id = _published_library(tmp_path)
    bad = _succeeded_upload(
        sessions, revision_id, tmp_path, 1, NOW - timedelta(hours=2)
    )
    _succeeded_upload(sessions, revision_id, tmp_path, 2, NOW - timedelta(hours=1))
    registry = _FailingArchive(tmp_path, bad)
    first = PrebuiltImageImporter(sessions, tmp_path, clock=lambda: NOW, store=registry)
    assert first.run_pending() == 1

    restarted = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=registry
    )
    assert restarted.run_pending() == 1  # the failing archive is tried afresh
    assert restarted.run_pending() == 1  # and does not hold the next one back
    assert restarted.run_pending() == 0
    assert not (tmp_path / IMAGE_CACHE_DIRECTORY / ("2" * 64)).exists()


def test_a_fault_that_heals_converts_on_the_next_due_attempt(tmp_path: Path) -> None:
    sessions, revision_id = _published_library(tmp_path)
    now = [NOW]
    bad = _succeeded_upload(
        sessions, revision_id, tmp_path, 1, NOW - timedelta(hours=2)
    )
    registry = _FailingArchive(tmp_path, bad)
    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: now[0], store=registry
    )
    assert importer.run_pending() == 1
    assert importer.run_pending() == 0  # not due yet

    registry.heal()
    now[0] += timedelta(hours=3)
    assert importer.run_pending() == 1
    assert not (tmp_path / IMAGE_CACHE_DIRECTORY / bad).exists()
    assert _error_of(sessions, _FAILING_BUILD) is None


def test_a_reopened_build_with_a_new_archive_converts(tmp_path: Path) -> None:
    sessions, revision_id = _published_library(tmp_path)
    bad = _succeeded_upload(
        sessions, revision_id, tmp_path, 1, NOW - timedelta(hours=2)
    )
    registry = _FailingArchive(tmp_path, bad)
    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=registry
    )
    assert importer.run_pending() == 1  # fails, now backing off

    # The build row is reopened and rebuilt: same row, a new archive.
    fresh = "9" * 64
    (tmp_path / IMAGE_CACHE_DIRECTORY / bad).unlink()
    (tmp_path / IMAGE_CACHE_DIRECTORY / fresh).write_bytes(b"docker archive")
    with sessions.begin() as session:
        build = session.get(RecipeBuild, _FAILING_BUILD)
        assert build is not None
        build.oci_layout_sha256 = fresh
    assert importer.run_pending() == 1
    assert not (tmp_path / IMAGE_CACHE_DIRECTORY / fresh).exists()
    assert importer._conversion_failures == {}


def test_a_removed_archive_prunes_its_retry_entry(tmp_path: Path) -> None:
    sessions, revision_id = _published_library(tmp_path)
    bad = _succeeded_upload(
        sessions, revision_id, tmp_path, 1, NOW - timedelta(hours=2)
    )
    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=_FailingArchive(tmp_path, bad)
    )
    assert importer.run_pending() == 1
    assert set(importer._conversion_failures) == {bad}

    (tmp_path / IMAGE_CACHE_DIRECTORY / bad).unlink()  # the collection dropped it
    assert importer.run_pending() == 0
    assert importer._conversion_failures == {}


def test_prebuilt_import_observes_cancellation_between_claim_and_io(
    tmp_path: Path,
) -> None:
    """Catches a restarted importer pulling after a newer durable cancel won."""

    sessions, revision_id = _published_library(tmp_path)
    builds, operations = _services(sessions, tmp_path)
    _plan, job = _start(builds, operations, revision_id)
    registry = _Registry(tmp_path)
    importer = PrebuiltImageImporter(
        sessions, tmp_path, clock=lambda: NOW, store=registry
    )
    assert importer._claim() == job.id
    with sessions.begin() as session:
        current = session.get(Job, job.id, with_for_update=True)
        assert current is not None
        request_build_cancellation(
            current,
            actor=current.actor,
            request_id=str(uuid4()),
            reason="Operator selected a newer request",
            now=NOW,
        )
    importer.run(job.id)
    assert not registry.copied
    importer._release(job.id)
    operations.reconcile_cancelled_builds()
    _fresh_plan, fresh = _start(
        builds, operations, revision_id, request_id=str(uuid4())
    )
    assert fresh.id != job.id
    assert importer.run_pending() == 1
    assert registry.copied == [REFERENCE]
