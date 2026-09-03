from __future__ import annotations

import hashlib
import io
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import vonk_control.recipe_builds as recipe_builds_module
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    AgentClaim,
    AgentResult,
    canonical_message,
)
from vonk_agent_protocol import (
    AgentOperation as ProtocolOperation,
)
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService, RecipeDraftInput
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Base,
    CatalogEntity,
    CatalogEntityRevision,
    ClusterMapping,
    ClusterMappingNode,
    Job,
    NodeArtifact,
    RecipeBuild,
    RecipeSourceBundle,
    ResourceReservation,
)
from vonk_control.recipe_builds import RecipeBuildError, RecipeBuildService
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
    _record_build_evidence,
)
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle

from .test_catalog_service import _seed_recipe_dependencies


class RecordingQueue:
    def enqueue_in_session(
        self,
        session,
        parent_job_id,
        node_id,
        operation,
        authority_revision,
        payload,
        *,
        operation_id,
    ):
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=parent_job_id,
                node_id=node_id,
                kind=operation,
                payload_digest="f" * 64,
                payload=dict(payload),
                authority_revision=authority_revision,
                state="queued",
                current_attempt=0,
                created_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
                updated_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
            )
        )

    def notify_available(self) -> None:
        pass


def setup(tmp_path: Path, *, network: dict[str, object] | None = None):
    engine = create_engine(f"sqlite:///{tmp_path / 'build.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    node_id = "spk_" + "1" * 32
    bundle = generate_source_bundle(
        {
            "Dockerfile": (
                "FROM ghcr.io/example/vllm@sha256:" + "a" * 64 + "\nUSER 10001:10001\n"
            ).encode()
        }
    )
    bundles = SourceBundleStore(tmp_path / "bundles")
    stored = bundles.put(bundle.sha256, io.BytesIO(bundle.archive))
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    document["build"]["network"] = network or {"mode": "none", "hosts": []}
    document["build"]["context"]["sha256"] = bundle.sha256
    document["build"]["context"]["expected_bytes"] = len(bundle.archive)
    document["identity"]["slug"] = "qwen3-vllm"
    document["build"]["target"] = "runtime"
    document["build"]["security"] = {"capabilities": ["DAC_OVERRIDE"]}
    document["build"]["resources"] = {
        "cpu_cores": 6,
        "download_bytes": 100,
        "temporary_bytes": 200,
        "memory_bytes": 300,
        "processes": 2048,
        "timeout_seconds": 600,
    }
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                architecture="linux-arm64",
                semantic_version="1.2.3",
                build_digest="sha256:" + "a" * 64,
                binary_digest="1" * 64,
                self_test_passed=True,
                capabilities=[
                    "recipe.build.v1",
                    "recipe.build.egress-proxy.v1",
                    "recipe.image.import.v1",
                ],
                last_seen_at=now,
            )
        )
        session.add(
            RecipeSourceBundle(
                sha256=bundle.sha256,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=stored.archive_bytes,
                total_bytes=bundle.manifest.total_bytes,
                file_count=len(bundle.manifest.files),
                storage_key=f"{bundle.sha256[:2]}/{bundle.sha256}.tar",
                manifest={"files": [asdict(item) for item in bundle.manifest.files]},
                verified_at=now,
            )
        )
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            node_id,
            now,
            8 * 1024 * 1024 * 1024,
            7 * 1024 * 1024 * 1024,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            (
                "recipe.build.v1",
                "recipe.build.egress-proxy.v1",
                "recipe.image.import.v1",
            ),
        )
    )
    catalog = CatalogService(
        sessions, clock=lambda: now, cursors=TokenCodec(b"c" * 32).cursor_codec()
    )
    _seed_recipe_dependencies(catalog, document)
    draft = catalog.create_recipe(
        "admin", RecipeDraftInput(slug="qwen3-vllm", document=document)
    )
    revision = catalog.resolve(draft.recipe_id, 1, "admin")
    return sessions, bundles, now, node_id, revision


def test_build_plan_is_typed_sandboxed_and_durable(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)

    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )

    assert plan.agent_payload["kind"] == "recipe.build.v1"
    assert "command" not in plan.agent_payload
    assert plan.agent_payload["target"] == "runtime"
    assert plan.agent_payload["capabilities"] == ["DAC_OVERRIDE"]
    assert plan.agent_payload["limits"]["cpu_cores"] == 6
    assert plan.agent_payload["limits"]["gpu"] == 0
    assert plan.agent_payload["limits"]["processes"] == 2048
    assert plan.agent_payload["base_images"] == [
        {
            "manifest_digest": "sha256:" + "a" * 64,
            "reference": "ghcr.io/example/vllm@sha256:" + "a" * 64,
        }
    ]
    assert plan.agent_payload["base_image_storage_bytes"] == 100
    assert (
        plan.agent_payload["source_bundle_sha256"]
        == revision.document["build"]["context"]["sha256"]
    )
    with sessions() as session:
        stored = session.get(RecipeBuild, plan.build_id)
        assert stored is not None and stored.state == "planned"


def test_build_plan_rejects_missing_exact_recipe_dependency(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        session.execute(delete(CatalogEntityRevision))
        session.execute(delete(CatalogEntity))

    with pytest.raises(RecipeBuildError) as caught:
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=now
        )

    assert caught.value.code == "build.dependencies_stale"


def test_build_identity_changes_when_builder_runtime_changes(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)

    first = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.binary_digest = "2" * 64
    second = service.plan(revision.id, node_id, now=now)

    assert second.build_id != first.build_id
    assert second.build_input_sha256 != first.build_input_sha256


def test_build_identity_changes_when_archive_format_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)

    first = service.plan(revision.id, node_id, now=now)
    monkeypatch.setattr(
        recipe_builds_module, "BUILD_ARTIFACT_FORMAT", "future-archive-v2"
    )
    second = service.plan(revision.id, node_id, now=now)

    assert second.build_id != first.build_id
    assert second.build_input_sha256 != first.build_input_sha256


def test_build_reservation_rejects_changed_builder_runtime(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.binary_digest = "2" * 64

    with (
        sessions.begin() as session,
        pytest.raises(RecipeBuildError, match="runtime identity changed"),
    ):
        service.reserve_in_session(session, plan, now=now)


def test_build_rejects_builder_without_runtime_identity(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.binary_digest = None

    with pytest.raises(RecipeBuildError, match="inactive or incompatible"):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=now
        )


def test_build_plan_passes_the_installed_agent_claim_boundary(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    payload_digest = hashlib.sha256(canonical_message(plan.agent_payload)).hexdigest()

    claim = AgentClaim(
        schema_version=1,
        job_id="00000000-0000-4000-8000-000000000001",
        operation_id="00000000-0000-4000-8000-000000000002",
        attempt=1,
        fence="00000000-0000-4000-8000-000000000003",
        node_id=node_id,
        operation=ProtocolOperation.RECIPE_BUILD,
        authority_revision="a"  * 64,
        payload_digest=payload_digest,
        payload=plan.agent_payload,
        deadline=now,
    )

    assert claim.payload["platform"] == "linux/arm64"


def test_starting_build_atomically_reserves_temporary_disk_and_memory(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=object(),
        run_admission=object(),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )

    operation = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="build-reservation-test",
    )

    with sessions() as session:
        reservations = tuple(
            session.scalars(
                select(ResourceReservation)
                .where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == plan.build_id,
                    ResourceReservation.state == "active",
                )
                .order_by(ResourceReservation.kind)
            )
        )
    assert [(item.kind, item.amount_bytes) for item in reservations] == [
        (
            "disk",
            plan.agent_payload["limits"]["temporary_bytes"]
            + plan.agent_payload["source_bundle_bytes"]
            + plan.agent_payload["limits"]["output_bytes"]
            + plan.agent_payload["base_image_storage_bytes"],
        ),
        ("host-memory", plan.agent_payload["limits"]["memory_bytes"]),
    ]

    operations.record_node_result(
        operation.id,
        node_id,
        succeeded=False,
        evidence={"reason": "expected test failure"},
    )
    with sessions() as session:
        assert (
            session.scalar(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == plan.build_id,
                    ResourceReservation.state == "active",
                )
            )
            is None
        )


@pytest.mark.parametrize(
    ("operation_state", "build_state"),
    (
        ("failed", "failed"),
        ("waiting-for-operator", "building"),
        ("expired", "building"),
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
        install_admission=object(),
        run_admission=object(),
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
        session.get(Job, first.id).state = operation_state  # type: ignore[union-attr]
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert child is not None
        child.state = operation_state
        session.get(RecipeBuild, plan.build_id).state = build_state  # type: ignore[union-attr]

    retried = operations.retry(first.id, actor="admin", request_id="retry-build")
    repeated = operations.retry(first.id, actor="admin", request_id="retry-build")

    assert repeated == retried
    assert retried.id != first.id
    assert retried.owner_id == plan.build_id
    assert retried.state == "running"
    with sessions() as session:
        assert session.get(RecipeBuild, plan.build_id).state == "building"  # type: ignore[union-attr]
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


def test_fresh_build_request_retries_matching_failed_build_idempotently(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=object(),
        run_admission=object(),
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
        session.get(Job, first.id).state = "failed"  # type: ignore[union-attr]
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert child is not None
        child.state = "failed"
        session.get(RecipeBuild, plan.build_id).state = "failed"  # type: ignore[union-attr]

    retried = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
    )
    replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
    )

    assert retried == replay
    assert retried.id != first.id
    assert retried.owner_id == plan.build_id
    assert retried.state == "running"
    with sessions() as session:
        assert session.get(RecipeBuild, plan.build_id).state == "building"  # type: ignore[union-attr]
        assert session.get(Job, retried.id).request_id == "fresh-acceptance-build"  # type: ignore[union-attr]


def test_successful_build_retry_converges_original_and_new_request_keys(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=object(),
        run_admission=object(),
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
        session.get(Job, first.id).state = "waiting-for-operator"  # type: ignore[union-attr]
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert child is not None
        child.state = "waiting-for-operator"

    retried = operations.retry(first.id, actor="admin", request_id="retry-build")
    succeeded = operations.record_node_result(
        retried.id,
        node_id,
        succeeded=True,
        evidence={
            "build_input_sha256": plan.build_input_sha256,
            "image_bytes": 500,
            "image_digest": "sha256:" + "b" * 64,
            "oci_layout_sha256": "c" * 64,
            "policy": {
                "dockerfile": "Dockerfile",
                "findings": [],
                "passed": True,
                "source_bundle_sha256": plan.source_bundle_sha256,
            },
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
        assert session.get(Job, new_replay.id).request_id == "fresh-acceptance-build"  # type: ignore[union-attr]
        assert (
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == new_replay.id
                )
            )
            is None
        )


def test_build_plan_rejects_disk_below_concurrent_oci_export_peak(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    source_bytes = len(
        bundles.get(revision.document["build"]["context"]["sha256"]).archive
    )
    temporary_bytes = revision.document["build"]["resources"]["temporary_bytes"]
    output_bytes = max(
        role["resources"]["disk"]["image_bytes"]
        for role in revision.document["topology"]["roles"]
    )
    peak_bytes = temporary_bytes + source_bytes + output_bytes
    # This inventory has enough capacity for staging + source, but not for the
    # simultaneous OCI export that the builder retains before promotion.
    newer = now + timedelta(seconds=1)
    InventoryRepository(sessions, clock=lambda: newer).record(
        InventorySnapshotInput(
            node_id,
            newer,
            peak_bytes,
            peak_bytes - 1,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            ("recipe.build.v1",),
        )
    )

    with pytest.raises(RecipeBuildError, match="temporary disk capacity"):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=newer
        )


def test_build_plan_accepts_public_network_only_with_egress_boundary_capability(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(
        tmp_path, network={"mode": "public", "hosts": ["pypi.org"]}
    )

    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    assert plan.agent_payload["network"] == {
        "mode": "public",
        "hosts": ["pypi.org"],
    }

    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.capabilities = ["recipe.build.v1", "recipe.image.import.v1"]
    with pytest.raises(RecipeBuildError, match="hostname-aware build egress"):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=now
        )


def test_public_build_rejects_stale_inventory_without_egress_capability(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(
        tmp_path, network={"mode": "public", "hosts": ["pypi.org"]}
    )
    newer = now + timedelta(seconds=1)
    InventoryRepository(sessions, clock=lambda: newer).record(
        InventorySnapshotInput(
            node_id,
            newer,
            8 * 1024 * 1024 * 1024,
            7 * 1024 * 1024 * 1024,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            ("recipe.build.v1", "recipe.image.import.v1"),
        )
    )

    with pytest.raises(RecipeBuildError, match="fresh builder inventory"):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=newer
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


def test_success_does_not_claim_isolated_build_image_is_installed(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)

    completed = service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )

    assert completed.image_digest == "sha256:" + "b" * 64
    with sessions() as session:
        artifact = session.scalar(
            select(NodeArtifact).where(NodeArtifact.node_id == node_id)
        )
        assert artifact is None


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

        _record_build_evidence(
            stale_session,
            stale_build,
            {
                "build_input_sha256": plan.build_input_sha256,
                "image_bytes": 500,
                "image_digest": new_image,
                "oci_layout_sha256": new_layout,
                "policy": {
                    "dockerfile": "Dockerfile",
                    "findings": [],
                    "passed": True,
                },
            },
            now=now,
        )
        assert stale_build.image_digest == new_image
    finally:
        stale_session.rollback()
        stale_session.close()


def test_build_result_accepts_protocol_frozen_empty_findings(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=object(),
        run_admission=object(),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    operation_view = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="frozen-policy-result",
    )
    image_digest = "sha256:" + "b" * 64
    layout_digest = "c" * 64
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.image_digest = image_digest
        build.oci_layout_sha256 = layout_digest
        build.image_bytes = 500
        agent_operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == operation_view.id
            )
        )
        assert agent_operation is not None
        operation_id = agent_operation.id

    message = AgentResult.parse(
        {
            "schema_version": 1,
            "job_id": operation_view.id,
            "operation_id": operation_id,
            "attempt": 1,
            "fence": "33333333-3333-4333-8333-333333333333",
            "node_id": node_id,
            "deadline": "2026-08-11T20:30:00+00:00",
            "state": "succeeded",
            "result": {
                "build_input_sha256": plan.build_input_sha256,
                "image_bytes": 500,
                "image_digest": image_digest,
                "oci_layout_sha256": layout_digest,
                "policy": {
                    "dockerfile": "Dockerfile",
                    "findings": [],
                    "passed": True,
                },
            },
        }
    )
    with sessions.begin() as session:
        agent_operation = session.get(AgentOperation, operation_id)
        assert agent_operation is not None
        agent_operation.state = "succeeded"
        operations.consume_agent_result(session, agent_operation, object(), message)

    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None and build.state == "succeeded"
        job = session.get(Job, operation_view.id)
        assert job is not None and job.state == "succeeded"
        assert job.result["node_evidence"][node_id]["policy"]["findings"] == []
        assert (
            session.scalar(select(NodeArtifact).where(NodeArtifact.node_id == node_id))
            is None
        )


def test_distribution_reimports_one_build_digest_for_every_mapped_node(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, builder, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, builder, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    target = "spk_" + "2" * 32
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=target,
                state="active",
                architecture="linux-arm64",
                capabilities=["recipe.image.import.v1"],
            )
        )
        session.add(
            NodeArtifact(
                node_id=builder,
                kind="image",
                digest="b" * 64,
                source="docker-archive:" + "c" * 64,
                size_bytes=500,
                state="verified",
                ref_count=0,
                verified_at=now,
                updated_at=now,
            )
        )
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            topology_name="synthetic-test",
            generation=1,
            node_count=2,
            state="ready",
            parameters={},
            placement_digest="d" * 64,
            endpoint_owner_node_id=builder,
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        session.add(mapping)
        session.flush()
        session.add_all(
            (
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=builder,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=now,
                ),
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=target,
                    rank=1,
                    role="worker",
                    endpoint_owner=False,
                    created_at=now,
                ),
            )
        )
        mapping_id = mapping.id

    distribution = service.plan_distribution(plan.build_id, mapping_id, generation=1)

    assert [item[0] for item in distribution.targets] == [builder, target]
    assert {item[1]["image_digest"] for item in distribution.targets} == {
        "sha256:" + "b" * 64
    }
    assert distribution.targets[0][1]["kind"] == "recipe.image.import.v1"


def test_image_distribution_requires_the_previewed_plan_digest(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, builder, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    build_plan = builds.plan(revision.id, builder, now=now)
    builds.record_success(
        build_plan.build_id,
        build_input_sha256=build_plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    target = "spk_" + "2" * 32
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=target,
                state="active",
                architecture="linux-arm64",
                capabilities=["recipe.image.import.v1"],
            )
        )
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            topology_name="synthetic-test",
            generation=1,
            node_count=2,
            state="ready",
            parameters={},
            placement_digest="d" * 64,
            endpoint_owner_node_id=builder,
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        session.add(mapping)
        session.flush()
        session.add_all(
            (
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=builder,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=now,
                ),
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=target,
                    rank=1,
                    role="worker",
                    endpoint_owner=False,
                    created_at=now,
                ),
            )
        )
        mapping_id = mapping.id

    operations = RecipeOperationService(
        sessions,
        install_admission=object(),
        run_admission=object(),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    preview = operations.preview_image_distribution(
        build_plan.build_id,
        mapping_id,
        mapping_generation=1,
    )

    assert preview.image_digest == "sha256:" + "b" * 64
    assert preview.node_ids == (builder, target)
    assert len(preview.plan_digest) == 64
    with pytest.raises(
        RecipeOperationConflict,
        match="submitted image distribution plan does not match preview",
    ):
        operations.distribute_image(
            build_plan.build_id,
            mapping_id,
            mapping_generation=1,
            plan_digest="0" * 64,
            actor="admin",
            request_id="stale-distribution",
        )

    operation = operations.distribute_image(
        build_plan.build_id,
        mapping_id,
        mapping_generation=1,
        plan_digest=preview.plan_digest,
        actor="admin",
        request_id="accepted-distribution",
    )
    assert operation.kind == "recipe.image.import.v1"
    assert operation.plan_digest == preview.plan_digest
    assert operation.nodes == (builder, target)
