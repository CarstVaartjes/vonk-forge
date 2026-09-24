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
    authorize_installation_runtime_images,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    CatalogDocumentRevision,
    ClusterMappingNode,
    RecipeBuild,
    RecipeInstallation,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_builds import RecipeBuildResolution, RecipeBuildService
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
)
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    persist_runtime_image_receipt,
    prepare_runtime_image,
    resolve_persisted_runtime_image_receipt,
)
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .preflight_fixtures import record_passing_preflight
from .test_recipe_builds import (
    RecordingQueue,
    _json_array,
    _json_object,
    _write_controller_build_receipt,
    setup,
)


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
        _json_array(
            _json_object(_json_object(document["execution"])["build"])["arguments"]
        ).append({"name": "executable_change", "value": "new"})
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

        def pull_and_export(self, *_args, **_kwargs) -> NoReturn:
            raise AssertionError("verified archive must not be pulled again")

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
        node.capabilities = [*node.capabilities, "runtime.vonk.v1"]
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
        model = ModelDefinition.model_validate(model_revision.document)
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
                    "model_content_sha256": content_sha256(model),
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

    def prepare(document, runtime_spec, selected_build):
        def authorize(prepared_receipt):
            with sessions.begin() as session:
                persist_runtime_image_receipt(
                    session,
                    recipe_revision_id=revision.id,
                    original_content_digest=revision.content_digest,
                    effective_execution_key=runtime_spec["identity"][
                        "execution_sha256"
                    ],
                    receipt=prepared_receipt,
                    verified_at=now,
                )

        return prepare_runtime_image(
            document,
            runtime=runtime_spec["runtime"],
            storage=storage,
            transport=NoBuild(),
            build_receipt={
                "state": selected_build.state,
                "build_id": selected_build.id,
                "build_input_sha256": selected_build.build_input_sha256,
                "image_digest": selected_build.image_digest,
                "oci_layout_sha256": selected_build.oci_layout_sha256,
                "image_bytes": selected_build.image_bytes,
            },
            now=now,
            receipt_writer=authorize,
        )

    def resolve(document, image_digest, runtime_spec):
        current = storage.find_verified(
            image_digest,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        if current is None:
            raise ValueError("verified runtime image archive is missing")
        with sessions() as session:
            return resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=revision.id,
                current_content_digest=content_sha256(
                    RecipeDefinition.model_validate(document)
                ),
                effective_execution_key=runtime_spec["identity"]["execution_sha256"],
                receipt=current,
            )

    preparation = ControllerExecutionPlanService(
        ModelReceipts(), runtime_image_preparer=prepare
    )
    with sessions() as session:
        preparation.compile_installation(
            session,
            revision=revision,
            build=build,
            mapping_nodes=tuple(
                session.scalars(
                    select(ClusterMappingNode).where(
                        ClusterMappingNode.mapping_id == mapping_id
                    )
                )
            ),
            parameters={},
        )
    compiler = ControllerExecutionPlanService(
        ModelReceipts(), runtime_image_resolver=resolve
    )

    def compile_without_transaction(**kwargs):
        assert not kwargs["session"].in_transaction()
        return compiler.compile_installation(**kwargs)

    admission = InstallAdmissionService(
        sessions,
        disk_floor_bytes=10,
        compiled_plan_provider=compile_without_transaction,
        runtime_image_authorizer=authorize_installation_runtime_images,
    )
    return sessions, now, successor, receipt, storage, admission, mapping_id, build.id


@pytest.mark.parametrize("change", ["editorial", "runtime"])
def test_prepared_successor_installs_the_original_verified_build(tmp_path, change):
    fixture = _prepared_successor(tmp_path, change=change)
    assert isinstance(fixture, tuple)
    sessions, now, successor, receipt, _storage, admission, mapping, build_id = fixture
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


def test_changed_executable_cannot_reuse_the_retained_build(tmp_path):
    resolution = _prepared_successor(tmp_path, change="build")
    assert isinstance(resolution, RecipeBuildResolution)
    assert not resolution.cached
    assert resolution.build_id is None


def test_install_does_not_substitute_receipt_from_another_build(tmp_path):
    fixture = _prepared_successor(tmp_path)
    assert isinstance(fixture, tuple)
    sessions, now, successor, receipt, _storage, admission, mapping, build_id = fixture
    other_id = str(uuid4())
    with sessions.begin() as session:
        original = session.get(RecipeBuild, build_id)
        assert original is not None
        # Independent builds can have the same executable inputs and image
        # digest. The authorized archive still names its exact producing build.
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
    assert not plan.allowed
    assert any(
        "differs from the selected build" in blocker.detail
        for blocker in plan.nodes[0].blockers
    )


@pytest.mark.parametrize("lost_authority", ["revoked", "missing"])
def test_install_rechecks_reused_receipt_after_preview(tmp_path, lost_authority):
    fixture = _prepared_successor(tmp_path)
    assert isinstance(fixture, tuple)
    sessions, now, successor, receipt, storage, admission, mapping, build_id = fixture
    plan = admission.plan_install(mapping, build_id, now=now)
    assert plan.allowed
    if lost_authority == "missing":
        (storage.root / receipt.oci_archive_sha256).unlink()
    else:
        with sessions.begin() as session:
            for authorization in session.scalars(
                select(RuntimeImageAuthorization).where(
                    RuntimeImageAuthorization.recipe_revision_id == successor.id
                )
            ):
                authorization.state = "revoked"
    with pytest.raises(InstallPlanConflict):
        admission.accept_install(plan, actor="admin", now=now)
    with sessions() as session:
        assert session.scalar(select(RecipeInstallation)) is None


def test_revocation_after_storage_check_is_rejected_inside_acceptance(tmp_path):
    fixture = _prepared_successor(tmp_path)
    assert isinstance(fixture, tuple)
    sessions, now, successor, _receipt, _storage, admission, mapping, build_id = fixture
    plan = admission.plan_install(mapping, build_id, now=now)
    admission.refresh_install_receipts(plan, now=now)
    with sessions.begin() as session:
        for authorization in session.scalars(
            select(RuntimeImageAuthorization).where(
                RuntimeImageAuthorization.recipe_revision_id == successor.id
            )
        ):
            authorization.state = "revoked"
    with (
        pytest.raises(InstallPlanConflict, match="authority_stale"),
        sessions.begin() as session,
    ):
        admission.accept_install_in_session(session, plan, actor="admin", now=now)


def test_installation_replay_adopts_accepted_effect_before_cache_refresh(tmp_path):
    fixture = _prepared_successor(tmp_path)
    assert isinstance(fixture, tuple)
    sessions, now, _successor, receipt, storage, admission, mapping, build_id = fixture
    operations = RecipeOperationService(
        sessions,
        install_admission=admission,
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
    )
    plan = admission.plan_install(mapping, build_id, now=now)
    installation_id = operations.prepare_installation(plan, actor="admin")
    (storage.root / receipt.oci_archive_sha256).unlink()
    assert operations.prepare_installation(plan, actor="admin") == installation_id


@pytest.mark.parametrize("receipt_state", ["ready", "revoked", "missing"])
def test_explicit_distribution_consumes_current_revisions_exact_receipt(
    tmp_path, receipt_state
):
    fixture = _prepared_successor(tmp_path)
    assert isinstance(fixture, tuple)
    sessions, now, successor, receipt, storage, admission, mapping, build_id = fixture
    operations = RecipeOperationService(
        sessions,
        install_admission=admission,
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=RecipeBuildService(
            sessions,
            bundles=SourceBundleStore(tmp_path / "bundles"),
            prepared_builds=storage.find_build,
            build_archive_available=storage.build_archive_available,
        ),
    )
    preview = operations.preview_image_distribution(
        build_id, mapping, mapping_generation=1
    )
    assert preview.image_digest == receipt.image_digest
    if receipt_state == "missing":
        (storage.root / receipt.oci_archive_sha256).unlink()
    elif receipt_state == "revoked":
        with sessions.begin() as session:
            for authorization in session.scalars(
                select(RuntimeImageAuthorization).where(
                    RuntimeImageAuthorization.recipe_revision_id == successor.id
                )
            ):
                authorization.state = "revoked"

    def distribute():
        return operations.distribute_image(
            build_id,
            mapping,
            mapping_generation=1,
            plan_digest=preview.plan_digest,
            actor="admin",
            request_id="successor-distribution",
        )

    if receipt_state == "ready":
        queued = distribute()
        assert queued.kind == "recipe.image.import.v1"
        assert queued.plan_digest == preview.plan_digest
    else:
        with pytest.raises(RecipeOperationConflict):
            distribute()
