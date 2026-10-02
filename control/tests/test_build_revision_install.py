"""The same verified build must survive a recipe-only revision through install."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from typing import NoReturn
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import CompiledExecutionPlan
from vonk_control.availability_production import build_recipe_image_availability
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.execution_plan_service import ControllerExecutionPlanService
from vonk_control.install_admission import (
    InstallAdmissionService,
    InstallPlanConflict,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentPresence,
    CatalogDocumentRevision,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_builds import (
    RecipeBuildError,
    RecipeBuildResolution,
    RecipeBuildService,
)
from vonk_control.recipe_operations import (
    RecipeOperationService,
)
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    stored_runtime_image_resolver,
)
from vonk_forge_contracts import read_model

from .preflight_fixtures import record_passing_preflight
from .runtime_image_fixtures import remove_test_image
from .test_recipe_builds import (
    RecordingQueue,
    _json_array,
    _json_object,
    _write_controller_build_receipt,
    setup,
)
from .test_recipe_operations import complete_started_recipe


def _prepared_successor(tmp_path, *, change="runtime"):
    sessions, bundles, now, node_id, original = setup(tmp_path)
    catalog = CatalogEntityService(sessions, clock=lambda: now)
    runnable = deepcopy(original.document)
    _json_array(_json_object(runnable["runtime"])["entrypoint"])[0] = (
        "/opt/vonk/bin/vllm"
    )
    runnable_draft = catalog.revise(original.document_id, runnable, actor="admin")
    with sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, runnable_draft.id)
        assert row is not None
        row.projected = deepcopy(original.projected)
    original = catalog.resolve(runnable_draft.id, actor="admin")
    storage = FilesystemRuntimeImageStorage(tmp_path / "artifacts")
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    build_plan = builds.plan(original.id, node_id, now=now)
    receipt = _write_controller_build_receipt(
        storage,
        archive=b"verified retained build archive",
        image_digest="sha256:" + "b" * 64,
        build_id=build_plan.build_id,
        build_input_sha256=build_plan.build_input_sha256,
        distribution_content_sha256=original.content_digest,
    )
    builds.record_success(
        build_plan.build_id,
        build_input_sha256=build_plan.build_input_sha256,
        image_digest=receipt.image_digest,
        oci_layout_sha256=receipt.oci_archive_sha256,
        image_bytes=receipt.image_bytes,
        now=now,
    )
    document = deepcopy(original.document)
    _json_object(document["metadata"])["description"] = "Current operational notes"
    if change == "runtime":
        _json_object(_json_object(document["settings"])["knobs"])["max_model_len"] = {
            "value": 16384,
            "change_effect": "restart",
        }
        _json_array(_json_object(document["runtime"])["arguments"]).append(
            {"name": "max-model-len", "setting": "max_model_len"}
        )
    elif change == "build":
        _json_object(_json_object(document["execution"])["build"])["network"] = {
            "hosts": ["pypi.org"]
        }
    draft = catalog.revise(original.document_id, document, actor="admin")
    with sessions.begin() as session:
        successor = session.get(CatalogDocumentRevision, draft.id)
        assert successor is not None
        successor.projected = deepcopy(original.projected)
    successor = catalog.resolve(draft.id, actor="admin")
    resolution = builds.resolve(successor.id)
    if change == "build":
        return resolution
    assert resolution.build_id == build_plan.build_id
    assert resolution.build_input_sha256 == build_plan.build_input_sha256

    class NoBuild:
        def build(self, *_args, **_kwargs):
            raise AssertionError("unchanged executable must not be built again")

        def inspect_archive(self, *_args, **_kwargs) -> NoReturn:
            raise AssertionError("verified archive must not be rehashed")

    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "artifacts",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=NoBuild(),
        clock=lambda: now,
    )
    operation = production.service.start(
        successor.id, actor="admin", request_id="reuse-successor"
    )
    production.service.run_pending()
    prepared = production.service.get(operation.id)
    assert prepared.state == "succeeded", prepared.failure
    assert prepared.result is not None
    assert prepared.result["build_id"] == build_plan.build_id

    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        session.add(
            AgentCertificate(
                serial="revision-reuse-preflight",
                node_id=node_id,
                fingerprint="revision-reuse-preflight",
                not_before=now - timedelta(seconds=1),
                not_after=now + timedelta(days=1),
            )
        )
        model_revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model"
            )
        )
        assert model_revision is not None
        model = read_model(model_revision.document)
        model_digest = model_revision.content_digest
        build = session.get(RecipeBuild, build_plan.build_id)
        revision = session.get(CatalogDocumentRevision, successor.id)
        assert build is not None and revision is not None
    mappings = ClusterMappingService(sessions)
    mapping_id = mappings.materialize(
        mappings.preview(successor.id, (node_id,), {}, "admin"),
        actor="admin",
        now=now,
    )
    record_passing_preflight(sessions, now, floor=10)

    class ModelReceipts:
        def resolve_artifact_set(self, **kwargs):
            assert kwargs["recipe_revision_sha256"] == successor.content_digest
            return SimpleNamespace(digest="f" * 64)

        def verified_model_objects_for_set(self, _digest):
            return tuple(
                {
                    "model_content_sha256": model_digest,
                    "file_id": file.id,
                    "path": file.path,
                    "sha256": file.sha256,
                    "bytes": file.size_bytes,
                    "roles": file.roles,
                    "distribution_object": {
                        "name": file.path,
                        "sha256": file.sha256,
                        "bytes": file.size_bytes,
                        "kind": "model",
                    },
                }
                for file in model.files
            )

    compiler = ControllerExecutionPlanService(
        ModelReceipts(), runtime_image_resolver=stored_runtime_image_resolver(storage)
    )

    def compile_without_transaction(**kwargs):
        assert not kwargs["session"].in_transaction()
        return compiler.compile_installation(**kwargs)

    admission = InstallAdmissionService(
        sessions,
        disk_floor_bytes=10,
        compiled_plan_provider=compile_without_transaction,
    )
    return (
        sessions,
        now,
        successor,
        receipt,
        storage,
        admission,
        mapping_id,
        build.id,
        builds,
    )


@pytest.mark.parametrize("change", ["editorial", "runtime"])
def test_prepared_successor_installs_the_original_verified_build(tmp_path, change):
    fixture = _prepared_successor(tmp_path, change=change)
    assert isinstance(fixture, tuple)
    (
        sessions,
        now,
        successor,
        receipt,
        _storage,
        admission,
        mapping,
        build_id,
        _builds,
    ) = fixture
    plan = admission.plan_install(mapping, build_id, now=now)
    assert plan.allowed, plan.nodes[0].blockers
    installed_id = admission.accept_install(plan, actor="admin", now=now)
    with sessions() as session:
        installed = session.get(RecipeInstallation, installed_id)
        assert installed is not None
        assert installed.recipe_revision_id == successor.id
        assert installed.recipe_build_id == receipt.build_id
        assert installed.image_digest == receipt.image_digest
    compiled = CompiledExecutionPlan.model_validate(
        next(iter(plan.compiled_plan_by_node.values()))
    )
    assert compiled.identity.recipe_revision_sha256 == successor.content_digest
    if change == "runtime":
        argv = compiled.runtime.argv
        assert argv[argv.index("--max-model-len") + 1] == "16384"


def test_editorial_successor_installs_and_starts_with_no_image_grant(tmp_path):
    """An image is its content: a successor revision runs its predecessor's build.

    The recipe's image is already on the Sparks, so no phase prepares it for
    the successor, and nothing records any per-revision grant. Review, install
    plan, install and start must still go through.
    """

    fixture = _prepared_successor(tmp_path, change="editorial")
    assert isinstance(fixture, tuple)
    (
        sessions,
        now,
        successor,
        receipt,
        _storage,
        admission,
        mapping,
        build_id,
        builds,
    ) = fixture
    with sessions.begin() as session:
        session.query(RuntimeImageAuthorization).delete()
    queue = RecordingQueue()
    service = RecipeOperationService(
        sessions,
        install_admission=admission,
        run_admission=RunAdmissionService(sessions),
        agent_jobs=queue,
        builds=builds,
        clock=lambda: now,
    )
    # Review: the successor finds the identical build by content.
    plan = service.preview_install(mapping, build_id)
    assert plan.allowed, plan.nodes[0].blockers
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
        assert installed.recipe_revision_id == successor.id
        assert installed.recipe_build_id == receipt.build_id
        assert session.query(RuntimeImageAuthorization).count() == 0
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


def test_review_of_a_successor_finds_its_build_while_the_builder_is_busy(tmp_path):
    """A lookup by content needs the build's identity, not room for a new build.

    The serving workload holds the Spark's memory, so a new build there would
    be refused; the successor's review must still find the build it reuses.
    The acceptance lane failed with "Prepare the exact model and runtime image"
    because the lookup ran the builder's capacity admission.
    """

    fixture = _prepared_successor(tmp_path, change="editorial")
    assert isinstance(fixture, tuple)
    (
        sessions,
        now,
        successor,
        _receipt,
        _storage,
        _admission,
        _mapping,
        build_id,
        builds,
    ) = fixture
    with sessions.begin() as session:
        node_id = session.scalar(select(AgentNode.node_id))
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.host_memory_free_bytes = 1
            snapshot.gpu_memory_free_bytes = 1
    assert node_id is not None
    # Admission for a new build is refused for lack of memory ...
    with pytest.raises(RecipeBuildError):
        builds.prepare_plan(successor.id, node_id, now=now)
    # ... and the identical build is still found by content.
    assert builds.reusable_build_id(successor.id, node_id, now=now) == build_id


def test_changed_executable_cannot_reuse_the_retained_build(tmp_path):
    resolution = _prepared_successor(tmp_path, change="build")
    assert isinstance(resolution, RecipeBuildResolution)
    assert not resolution.cached
    assert resolution.build_id is None


def test_install_accepts_a_sibling_build_of_the_same_image(tmp_path):
    fixture = _prepared_successor(tmp_path)
    assert isinstance(fixture, tuple)
    (
        sessions,
        now,
        successor,
        receipt,
        _storage,
        admission,
        mapping,
        build_id,
        _builds,
    ) = fixture
    other_id = str(uuid4())
    with sessions.begin() as session:
        original = session.get(RecipeBuild, build_id)
        assert original is not None
        # Independent builds can have the same executable inputs and image
        # digest. The image is its content, so either build installs it.
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
    plan = admission.plan_install(mapping, other_id, now=now)
    assert plan.allowed, plan.nodes[0].blockers


def test_install_rechecks_the_stored_image_after_preview(tmp_path):
    fixture = _prepared_successor(tmp_path)
    assert isinstance(fixture, tuple)
    (
        sessions,
        now,
        _successor,
        receipt,
        storage,
        admission,
        mapping,
        build_id,
        _builds,
    ) = fixture
    plan = admission.plan_install(mapping, build_id, now=now)
    assert plan.allowed
    remove_test_image(storage, receipt.oci_archive_sha256)
    with pytest.raises(InstallPlanConflict):
        admission.accept_install(plan, actor="admin", now=now)
    with sessions() as session:
        assert session.scalar(select(RecipeInstallation)) is None


def test_installation_replay_adopts_accepted_effect_before_cache_refresh(tmp_path):
    fixture = _prepared_successor(tmp_path)
    assert isinstance(fixture, tuple)
    (
        sessions,
        now,
        _successor,
        receipt,
        storage,
        admission,
        mapping,
        build_id,
        _builds,
    ) = fixture
    operations = RecipeOperationService(
        sessions,
        install_admission=admission,
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
    )
    plan = admission.plan_install(mapping, build_id, now=now)
    installation_id = operations.prepare_installation(plan, actor="admin")
    remove_test_image(storage, receipt.oci_archive_sha256)
    assert operations.prepare_installation(plan, actor="admin") == installation_id
