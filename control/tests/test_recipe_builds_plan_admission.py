"""Recipe builds: plan admission."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Table, select
from vonk_forge_contracts import RecipeDefinition, document_sha256, read_recipe

from vonk_agent_protocol.agent_words import ProgressPhase
from vonk_agent_protocol.build_import import RecipeBuildRequest
from vonk_agent_protocol.contracts import AgentClaim, canonical_message
from vonk_agent_protocol.contracts import AgentOperation as ProtocolOperation
from vonk_control.agent_jobs.retry import superseded_cancellation_deadline
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.availability_production.production import (
    build_recipe_image_availability,
)
from vonk_control.bounded_json import require_integer
from vonk_control.install_admission.service import InstallAdmissionService
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.execution import ResourceReservation
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.recipe_builds.common import (
    RecipeBuildError,
    _build_disk_reserve,
)
from vonk_control.recipe_builds.service import RecipeBuildService
from vonk_control.recipe_image_availability.availability_contract import (
    AvailabilityBuildReceipt,
)
from vonk_control.recipe_image_availability.contracts import BuildUnsettled
from vonk_control.recipe_image_availability.removal_contract import (
    RecipeCacheRemovalOwner,
)
from vonk_control.recipe_image_availability.service import (
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_operations.execution_contract import parse_stored_build_plan
from vonk_control.recipe_operations.service import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage

from .recipe_builds_support import (
    RecordingQueue,
    _json_array,
    _json_object,
    _restore_builder_inventory,
    setup,
)
from .recipe_image_availability_support import _typed_availability_payload
from .recipe_removal_review_support import remove_after_review
from .runtime_identity_support import (
    set_inventory_identity,
)


def test_build_plan_rejects_a_stale_resolution_but_keeps_live_admission(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    resolution = service.resolve(revision.id)
    with sessions.begin() as session:
        current = session.get(CatalogDocumentRevision, revision.id)
        assert current is not None
        document = copy.deepcopy(current.document)
        _json_object(_json_object(document["execution"])["build"])["network"] = {
            "hosts": ["pypi.org"]
        }
        read_recipe(document)
        content_digest = document_sha256(document)
        newer_revision = CatalogDocumentRevision(
            id=str(uuid.uuid4()),
            document_id=current.document_id,
            kind=current.kind,
            publisher=current.publisher,
            slug=current.slug,
            revision_number=current.revision_number + 1,
            schema_version=2,
            state="active",
            document=document,
            content_digest=content_digest,
            artifact_key="e" * 64,
            execution_key="f" * 64,
            source_bundle_sha256=current.source_bundle_sha256,
            projected=copy.deepcopy(current.projected),
            created_by="test",
            created_at=now,
        )
        session.add(newer_revision)

    with pytest.raises(RecipeBuildError):
        service.plan(newer_revision.id, node_id, now=now, resolution=resolution)
    fresh = service.plan(newer_revision.id, node_id, now=now)
    assert fresh.recipe_content_sha256 == newer_revision.content_digest


def test_build_plan_from_intent_rechecks_selected_builder_capacity(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    resolution = service.resolve(revision.id)
    newer = now + timedelta(seconds=1)
    InventoryRepository(sessions, clock=lambda: newer).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": newer,
                    "disk_total_bytes": 2 * 1024**4,
                    "disk_free_bytes": 1,
                    "host_memory_total_bytes": 100_000,
                    "host_memory_free_bytes": 80_000,
                    "gpu_memory_total_bytes": 100_000,
                    "gpu_memory_free_bytes": 80_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ("recipe.build.v1", "recipe.build.egress-proxy.v1"),
                    "memory_pool": "separate",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )

    with pytest.raises(RecipeBuildError):
        service.plan(revision.id, node_id, now=newer, resolution=resolution)

    fresh_time = newer + timedelta(seconds=1)
    _restore_builder_inventory(sessions, node_id, fresh_time)
    assert (
        RecipeBuildService(sessions, bundles=bundles)
        .plan(revision.id, node_id, now=fresh_time)
        .recipe_content_sha256
        == revision.content_digest
    )


def test_build_identity_changes_when_archive_format_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)

    first = service.plan(revision.id, node_id, now=now)
    monkeypatch.setattr(
        "vonk_control.recipe_builds.planning.BUILD_ARTIFACT_FORMAT", "future-archive-v2"
    )
    monkeypatch.setattr(
        "vonk_control.recipe_builds.source.BUILD_ARTIFACT_FORMAT", "future-archive-v2"
    )
    monkeypatch.setattr(
        "vonk_control.recipe_builds.persistence.BUILD_ARTIFACT_FORMAT",
        "future-archive-v2",
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
        set_inventory_identity(node, binary_digest="2" * 64)

    with (
        sessions.begin() as session,
        pytest.raises(RecipeBuildError),
    ):
        service.reserve_in_session(session, plan, now=now)
    fresh = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        service.reserve_in_session(session, fresh, now=now)


def test_damaged_reservation_projection_leaves_capacity_free_and_repairs(tmp_path):
    """A damaged stored envelope cannot reserve capacity or poison fresh work."""

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        stored = session.get(RecipeBuild, plan.build_id)
        assert stored is not None
        document = copy.deepcopy(stored.plan)
        # Valid JSON that the current model still rejects: the limit must be
        # strictly positive.  Corrupt the stored JSON directly so the real
        # stored-envelope read boundary is what rejects it.
        _json_object(document["limits"])["temporary_bytes"] = 0
        table = RecipeBuild.__table__
        assert isinstance(table, Table)
        session.execute(
            table.update().where(RecipeBuild.id == plan.build_id).values(plan=document)
        )

    with (
        sessions.begin() as session,
        pytest.raises(RecipeBuildError),
    ):
        service.reserve_in_session(session, plan, now=now)

    # Damaged storage never admits an unverified envelope or leaves capacity.
    with sessions() as session:
        assert tuple(session.scalars(select(ResourceReservation))) == ()
    with sessions.begin() as session:
        session.execute(
            table.update()
            .where(RecipeBuild.id == plan.build_id)
            .values(plan=plan.agent_payload)
        )
    with sessions.begin() as session:
        service.reserve_in_session(session, plan, now=now)


def test_removal_does_not_corrupt_the_stored_build_envelope(tmp_path: Path) -> None:
    """Removal preserves another request's build and its canonical envelope.

    This catches implicit build cancellation or removal-only fields written
    into the strict build plan while cache eviction is accepted.
    """

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    planned = builds.plan(revision.id, node_id, now=now)
    recipe = RecipeDefinition.model_validate(revision.document)
    availability = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "cache"),
        authority=lambda *_args, **_kwargs: (
            recipe,
            {
                "architecture": "linux/arm64",
                "interface": "vonk.runtime.v1",
                "image_bytes": 1,
            },
        ),
        clock=lambda: now,
    )

    result = remove_after_review(
        availability,
        recipe.identity.slug,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000024",
    )
    assert result["cancelled_builds"] == []

    with sessions() as session:
        stored = session.get(RecipeBuild, planned.build_id)
        assert stored is not None
        assert stored.state == "planned"
        # The exact stored document still satisfies the canonical contract.
        assert parse_stored_build_plan(stored.plan).build_id == planned.build_id

    # The fence belongs to the exact removal owner.
    with sessions() as session:
        removal = session.scalar(
            select(Job).where(Job.request_id == "00000000-0000-4000-8000-000000000024")
        )
        assert removal is not None
        owner = RecipeCacheRemovalOwner.model_validate_json(
            canonical_message(removal.payload)
        )
        assert owner.plan.intent.request_key == removal.request_id

    # Replanning reconnects to the same valid build request.
    replanned = builds.plan(revision.id, node_id, now=now)
    assert replanned.build_id == planned.build_id
    assert parse_stored_build_plan(replanned.agent_payload).build_id == planned.build_id
    with sessions() as session:
        rebuilt = session.get(RecipeBuild, planned.build_id)
        assert rebuilt is not None
        assert rebuilt.state == "planned"
        assert rebuilt.error is None


def test_planner_repairs_a_build_envelope_an_older_removal_fence_damaged(
    tmp_path: Path,
) -> None:
    """The planner rebuilds a row whose stored envelope no longer parses.

    An older Controller's removal merged ``removal_fence`` and ``cancelled``
    into the stored ``RecipeBuildRequest``.  The row is damaged history, not a
    live request, so the planner must replace its documents with a freshly
    derived envelope instead of rejecting the operator's build with
    ``build.plan_invalid``.  Repair derives new bytes; it never blesses the
    damaged document.
    """

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    planned = service.plan(revision.id, node_id, now=now)
    damaged = copy.deepcopy(planned.agent_payload) | {
        "removal_fence": "00000000-0000-4000-8000-0000000000ff",
        "cancelled": True,
    }
    table = RecipeBuild.__table__
    assert isinstance(table, Table)
    with sessions.begin() as session:
        session.execute(
            table.update()
            .where(RecipeBuild.id == planned.build_id)
            .values(
                plan=damaged,
                state="failed",
                error="recipe Controller cache removal cancelled the build",
            )
        )

    repaired = service.plan(revision.id, node_id, now=now)

    assert parse_stored_build_plan(repaired.agent_payload).build_id == planned.build_id
    with sessions() as session:
        stored = session.get(RecipeBuild, planned.build_id)
        assert stored is not None
        assert stored.state == "planned"
        assert stored.error is None
        repaired_document = _json_object(stored.plan)
        # Repair replaces the damaged bytes; the forbidden engine keys are gone.
        assert "removal_fence" not in repaired_document
        assert "cancelled" not in repaired_document
        assert parse_stored_build_plan(repaired_document).build_id == planned.build_id


@pytest.mark.usefixtures("damaged_json_rows")
def test_planner_repairs_active_projection_without_replacing_execution(tmp_path):
    """Damaged derived metadata cannot cancel or permanently veto exact work."""
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    planned = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        stored = session.get(RecipeBuild, planned.build_id)
        assert stored is not None
        stored.state = ProgressPhase.BUILDING.value
        stored.plan = copy.deepcopy(planned.agent_payload) | {"removal_fence": "x"}
    repaired = service.plan(revision.id, node_id, now=now)
    assert repaired.build_id == planned.build_id
    assert repaired.agent_payload == planned.agent_payload
    with sessions() as session:
        stored = session.get(RecipeBuild, planned.build_id)
        assert stored is not None
        assert stored.state == ProgressPhase.BUILDING.value
        assert stored.plan == planned.agent_payload
    assert service.plan(revision.id, node_id, now=now).build_id == planned.build_id


def test_build_rejects_builder_without_runtime_identity(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        set_inventory_identity(node, binary_digest=None)

    with pytest.raises(RecipeBuildError):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=now
        )

    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        set_inventory_identity(node, architecture="linux-arm64", binary_digest="1" * 64)
    assert (
        RecipeBuildService(sessions, bundles=bundles)
        .plan(revision.id, node_id, now=now)
        .recipe_content_sha256
        == revision.content_digest
    )


def test_build_plan_passes_the_installed_agent_claim_boundary(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    claim = AgentClaim(
        fence="00000000-0000-4000-8000-000000000003",
        operation=ProtocolOperation.RECIPE_BUILD,
        payload=RecipeBuildRequest.model_validate(plan.agent_payload),
        deadline=now,
    )

    assert claim.payload["dockerfile"] == "Dockerfile"


def test_starting_build_atomically_reserves_temporary_disk_and_memory(
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
    limits = _json_object(plan.agent_payload["limits"])
    assert [(item.kind, item.amount_bytes) for item in reservations] == [
        (
            "disk",
            max(
                require_integer(limits["temporary_bytes"], "temporary bytes"),
                require_integer(
                    plan.agent_payload["source_bundle_bytes"], "source bundle bytes"
                )
                + require_integer(limits["output_bytes"], "output bytes")
                + require_integer(
                    plan.agent_payload["base_image_storage_bytes"],
                    "base image storage bytes",
                ),
            ),
        ),
        ("host-memory", require_integer(limits["memory_bytes"], "memory bytes")),
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
    ("source_state", "outcome"),
    [
        ("running", "success"),
        ("needs-operator", "success"),
        ("running", "failed"),
    ],
)
def test_cancelled_build_keeps_capacity_until_cleanup_is_confirmed(
    tmp_path: Path,
    source_state: str,
    outcome: str,
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
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="cancel-build-test",
    )
    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == original.id)
        )
        assert child is not None
        child.state = source_state
        stored_job = session.get(Job, original.id)
        assert stored_job is not None
        stored_job.state = source_state
        child.current_attempt = 1
        child_id = child.id
    operations.cancel(
        original.id,
        actor="admin",
        request_id="d75c1b26-f9f5-48b0-94a3-8190bf7c181f",
        reason="remove recipe cache",
    )
    with sessions() as session:
        original_job = session.get(Job, original.id)
        assert original_job is not None
        assert original_job.state == source_state
        assert original_job.result is not None
        assert original_job.result["cancel_requested"] is True
        # A cancellation without a parseable instant can never authorise the
        # bounded cleanup STOP and would wedge the node forever.
        assert superseded_cancellation_deadline(original_job.result) is not None
        assert (
            session.scalar(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == plan.build_id,
                    ResourceReservation.state == "active",
                )
            )
            is not None
        )
        cleanup = session.scalar(
            select(Job).where(Job.kind == "recipe.build.cleanup.v1")
        )
        assert cleanup is not None
        cleanup_id = cleanup.id
        cleanup_child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == cleanup.id)
        )
        assert cleanup_child is not None
        assert cleanup_child.payload["operation_id"] == child_id
    assert not operations.reconcile_cancelled_builds()
    # A late execution result must not publish the removed image, lose the
    # cancellation metadata, or release the reservation before cleanup.
    operations.record_node_result(
        original.id,
        node_id,
        succeeded=source_state == "running",
        evidence={
            "image_bytes": 500,
            "image_digest": "sha256:" + "b" * 64,
            "oci_layout_sha256": "c" * 64,
        }
        if source_state == "running"
        else {"reason": "execution stopped after cancellation"},
    )
    with sessions.begin() as session:
        AgentJobService(sessions, clock=lambda: now)._aggregate_parent(
            session, original.id
        )
        removed = session.get(RecipeBuild, plan.build_id)
        assert removed is not None and removed.state == "failed"
    if outcome == "failed":
        operations.record_node_result(
            cleanup_id,
            node_id,
            succeeded=False,
            evidence={"reason": "systemd stop failed"},
        )
    else:
        operations.record_node_result(cleanup_id, node_id, succeeded=True, evidence={})
    with sessions() as session:
        remaining = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == plan.build_id,
                ResourceReservation.state == "active",
            )
        )
        original_job = session.get(Job, original.id)
        assert original_job is not None
        assert superseded_cancellation_deadline(original_job.result) is not None
        if outcome == "success":
            assert original_job.state == "cancelled"
            assert remaining is None
        else:
            assert remaining is not None
            assert original_job.state == "observing"


@pytest.mark.parametrize(
    ("operation_state", "build_state"),
    [
        ("failed", "failed"),
        ("needs-operator", "building"),
        ("failed", "building"),
    ],
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
    required_bytes = peak_bytes + _build_disk_reserve(disk_total_bytes)
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
