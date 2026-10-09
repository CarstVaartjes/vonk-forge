"""Every in-process gate accepts an image by its content, whoever recorded it first.

A runtime image is identified by its content (image digest, OCI archive,
bytes, architecture). The recipe, slug, revision, ``build_id``,
``distribution_*`` and runtime adapter of whoever asked for it first are
provenance. The same verified image bytes are therefore presented through
different provenance and the gates a load passes through must accept each:
review, install plan, install, start, and the profile assignment's check that
an installation uses the selected image.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_control.models import (
    AgentPresence,
    InstallationNode,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
)
from vonk_control.preparation_contract import RuntimeImageIdentity
from vonk_control.recipe_execution_contract import installation_matches_runtime_image
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.run_switch_contract import (
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    RecipeLifecyclePhaseExecutor,
    RunSwitchOperationService,
    _validate_artifact_execution,
)

from .test_build_revision_install import _prepared_successor
from .test_recipe_builds import RecordingQueue
from .test_recipe_operations import complete_started_recipe


def _foreign_receipt(storage, receipt) -> None:
    """Rewrite the stored receipt as another recipe's download left it."""

    path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    document = json.loads(path.read_text())
    document.update(
        distribution_publisher="another-publisher",
        distribution_slug="sibling-recipe",
        distribution_content_sha256="c" * 64,
        build_id=str(uuid4()),
        runtime_adapter="another-adapter",
        runtime_adapter_sha256="d" * 64,
    )
    path.write_text(json.dumps(document))


def _sibling_build(sessions, now, successor, receipt, build_id) -> str:
    """A second succeeded build row for the same image bytes."""

    other_id = str(uuid4())
    with sessions.begin() as session:
        original = session.get(RecipeBuild, build_id)
        assert original is not None
        session.add(
            RecipeBuild(
                id=other_id,
                recipe_revision_id=successor.id,
                builder_node_id=original.builder_node_id,
                source_bundle_sha256=original.source_bundle_sha256,
                build_input_sha256=original.build_input_sha256,
                state="succeeded",
                policy_report=original.policy_report,
                plan={
                    **original.plan,
                    "build_id": other_id,
                    "recipe_revision_id": successor.id,
                    "recipe_content_sha256": successor.content_digest,
                },
                image_digest=receipt.image_digest,
                oci_layout_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                created_at=now,
                updated_at=now,
            )
        )
    return other_id


@pytest.mark.parametrize(
    "provenance", ["recorded-by-this-build", "foreign-receipt", "sibling-build"]
)
def test_the_same_image_loads_through_every_gate_whatever_its_provenance(
    tmp_path, provenance
) -> None:
    fixture = _prepared_successor(tmp_path, change="editorial")
    assert isinstance(fixture, tuple)
    (
        sessions,
        now,
        successor,
        receipt,
        storage,
        admission,
        mapping,
        build_id,
        builds,
    ) = fixture
    if provenance == "foreign-receipt":
        _foreign_receipt(storage, receipt)
    if provenance == "sibling-build":
        build_id = _sibling_build(sessions, now, successor, receipt, build_id)
    service = RecipeOperationService(
        sessions,
        install_admission=admission,
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        builds=builds,
        clock=lambda: now,
    )
    # Review and install plan.
    plan = service.preview_install(mapping, build_id)
    assert plan.allowed, plan.nodes[0].blockers
    # Install.
    install = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
    )
    for node in plan.nodes:
        service.record_node_result(
            install.id,
            node.node_id,
            succeeded=True,
            evidence={"installed_bytes": 120},
        )
    with sessions() as session:
        installed = session.scalar(select(RecipeInstallation))
        assert installed is not None
        assert installed.image_digest == receipt.image_digest
    # The profile assignment's check that the installation uses the image.
    with sessions() as session:
        installed = session.scalar(select(RecipeInstallation))
        build = session.get(RecipeBuild, build_id)
        assert installed is not None and build is not None
        assert installation_matches_runtime_image(
            installed,
            image_digest=build.image_digest,
            oci_layout_sha256=build.oci_layout_sha256,
            image_bytes=build.image_bytes,
        )
    # The Run/Switch plan binds the installation by content: the build that
    # selected it may be another row of the same image.
    with sessions() as session:
        installed = session.scalar(select(RecipeInstallation))
        assert installed is not None
        members = session.scalars(
            select(InstallationNode).where(
                InstallationNode.installation_id == installed.id
            )
        ).all()
        switch_plan = RunSwitchPlan.model_construct(
            None,
            recipe_revision_id=installed.recipe_revision_id,
            model_content_sha256=installed.model_content_sha256,
            mapping=SimpleNamespace(mapping_generation=installed.mapping_generation),
            recipe_build_id=str(uuid4()),
            image_digest=installed.image_digest,
            spark_group=SimpleNamespace(
                nodes=[
                    SimpleNamespace(
                        node_id=node.node_id, rank=node.rank, role=node.role
                    )
                    for node in members
                ]
            ),
        )
        RecipeLifecyclePhaseExecutor._bound_installation(
            session, switch_plan, installed.id, mapping, installed.plan_digest
        )
    # Planning must rediscover the accepted image by content even when its
    # producer row has disappeared. It retains the installed executable plan
    # rather than compiling or distributing an identical image again.
    planner = RunSwitchOperationService(sessions, lifecycle=service, clock=lambda: now)
    reviewed = RuntimeImageIdentity(
        image_digest=receipt.image_digest,
        oci_layout_sha256=receipt.oci_archive_sha256,
        image_bytes=receipt.image_bytes,
        architecture=receipt.architecture,
        runtime_interface=receipt.runtime_interface,
        build_id=str(uuid4()),
    )
    assert installed.model_content_sha256 is not None
    reuse = planner._preview_run(
        RunSwitchPreviewRequest(
            model_content_sha256=installed.model_content_sha256,
            recipe_revision_id=successor.id,
            spark_group=SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        endpoint_owner=node.rank == 0,
                    )
                    for node in members
                ]
            ),
            alias="qwen",
        ),
        actor="admin",
        reviewed_runtime_image=reviewed,
    )
    assert reuse.installation_id == installed.id
    assert reuse.image_digest == receipt.image_digest
    assert reuse.build.oci_layout_sha256 == receipt.oci_archive_sha256
    assert not any(phase.subphase == "container-build" for phase in reuse.phases)
    # Start.
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.host_memory_total_bytes = 10**12
            snapshot.host_memory_free_bytes = 10**12
            snapshot.gpu_memory_total_bytes = 10**12
            snapshot.gpu_memory_free_bytes = 10**12
        session.add(
            AgentPresence(
                node_id=plan.nodes[0].node_id,
                certificate_serial="revision-reuse-preflight",
                certificate_fingerprint="revision-reuse-preflight",
                management_address="192.168.1.211",
                observed_at=now,
            )
        )
    run_plan = service.preview_run(installed.id, "qwen")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    complete_started_recipe(sessions, service, start.id)
    assert service.get(start.id).state == "succeeded"


def test_the_operator_reviewed_image_accepts_a_receipt_recorded_by_another_build() -> (
    None
):
    """The reviewed image of a profile load is content; the receipt's build is not.

    The stored receipt keeps the first requester's ``build_id``. A profile whose
    review accepted this image content must still load it.
    """

    image_digest = "sha256:" + "b" * 64
    archive = "a" * 64
    receipt = {
        "schema_version": 2,
        "distribution_publisher": "another-publisher",
        "distribution_slug": "sibling-recipe",
        "distribution_content_sha256": "c" * 64,
        "image_digest": image_digest,
        "oci_archive_sha256": archive,
        "image_bytes": 31,
        "local_image_config_id": "sha256:" + "4" * 64,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "runtime_interface_label": "v1",
        "archive_path": f"/images/{archive}",
        "recorded_at": "2026-09-15T00:00:00Z",
        "build_id": str(uuid4()),
        "runtime_adapter": "another-adapter",
        "runtime_adapter_sha256": "d" * 64,
    }
    expected = RuntimeImageIdentity(
        image_digest=image_digest,
        oci_layout_sha256=archive,
        image_bytes=31,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        build_id=str(uuid4()),
    )
    plan = RunSwitchPlan.model_construct(
        None,
        image_digest=image_digest,
        build=SimpleNamespace(oci_layout_sha256=archive),
    )
    phase = RunSwitchPhase.model_construct(
        None, kind="prepare", subphase="runtime-image"
    )
    _validate_artifact_execution(
        plan, phase, {"runtime_image": receipt}, expected_image=expected
    )
    # Changed-content observation and bounded fresh admission are exercised
    # through the worker in test_run_switch_observation_lifetime, rather than
    # asserting an internal exception type here.
