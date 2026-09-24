"""Published image recovery keeps accepted output intent separate from bytes."""

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import (
    CatalogDocumentRevision,
    Job,
    RecipeInstallation,
    RuntimeImageAuthorization,
    User,
)
from vonk_control.preparation_contract import RuntimeImageIdentity
from vonk_control.profile_capacity import accepted_profile_runtime_image
from vonk_control.run_switch_contract import RunSwitchApplyRequest
from vonk_control.run_switch_operations import (
    _validate_artifact_execution,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    make_runtime_image_receipt_preparer,
)

from .test_direct_run_switch_production_path import (
    NOW,
    REGISTRY_DIGEST,
    _direct_request,
    _make_service,
    _seed_runtime_image_owner,
    _Transport,
)


def test_profile_identity_survives_loss_of_published_receipt_and_archive(
    tmp_path,
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, _events = (
        _make_service(tmp_path, availability_key="a" * 64)
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    with sessions() as session:
        authorization = session.scalar(select(RuntimeImageAuthorization))
        assert authorization is not None
        approved = RuntimeImageIdentity(
            image_digest=authorization.platform_manifest_digest,
            oci_layout_sha256=authorization.oci_archive_sha256,
            image_bytes=authorization.image_bytes,
            architecture="linux-arm64",
            runtime_interface="vonk.runtime.v1",
            build_id=authorization.build_id,
        )
    receipt = storage.read_receipt(approved.oci_layout_sha256)

    # Model complete loss: neither the managed receipt/archive nor SQL
    # authorization remains. The accepted identity above is still the plan's
    # review intent and must not be replaced by the registry index digest.
    Path(receipt.archive_path).unlink()
    (storage.root / f"{approved.oci_layout_sha256}.receipt.json").unlink()
    with sessions.begin() as session:
        session.execute(delete(RuntimeImageAuthorization))

    plan = service._preview_run(
        _direct_request(revision_id),
        actor="test",
        reviewed_runtime_image=approved,
    )

    assert plan.runtime_storage.image_digest == approved.image_digest
    assert plan.runtime_storage.oci_layout_sha256 == approved.oci_layout_sha256
    assert plan.runtime_storage.image_bytes == approved.image_bytes
    assert plan.runtime_storage.registry_manifest_digest == REGISTRY_DIGEST
    assert plan.runtime_storage.preparation_required is True
    assert plan.runtime_storage.nas_coverage == "unknown"
    assert plan.preparation is not None
    controller = plan.preparation.runtime_image.controller
    assert controller.state == "unknown"
    assert controller.verified_bytes == 0
    assert controller.verified_sha256 is None
    assert controller.verified_at is None
    assert plan.preparation.ready is False

    # Exercise the same callback wired into both production_app() and worker
    # composition. It must restore and persist the original complete receipt
    # before the next Run/Switch phase can execute.
    executor._runtime_image_preparer = make_runtime_image_receipt_preparer(
        sessions,
        storage,
        _Transport(),
        clock=lambda: NOW,
    )
    phase = next(
        phase
        for phase in plan.phases
        if phase.kind == "prepare" and phase.subphase == "runtime-image"
    )
    _operation_id, request_key, progress = _seed_runtime_image_owner(
        sessions, plan, phase
    )
    recovered = executor._prepare_runtime_image(
        plan,
        phase,
        item_index=0,
        actor="test",
        request_key=request_key,
        progress=progress,
    )
    assert recovered is not None
    assert recovered["runtime_image"] == receipt.to_mapping()
    _validate_artifact_execution(plan, phase, recovered, expected_image=approved)
    with sessions() as session:
        authorizations = tuple(
            session.scalars(
                select(RuntimeImageAuthorization).where(
                    RuntimeImageAuthorization.recipe_revision_id == revision_id,
                    RuntimeImageAuthorization.registry_manifest_digest
                    == REGISTRY_DIGEST,
                )
            )
        )
    assert authorizations
    assert {
        (
            item.platform_manifest_digest,
            item.oci_archive_sha256,
            item.image_bytes,
        )
        for item in authorizations
    } == {
        (
            approved.image_digest,
            approved.oci_layout_sha256,
            approved.image_bytes,
        )
    }

    class ChangedTransport(_Transport):
        def pull_and_export(
            self,
            reference: str,
            destination: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            progress=None,
        ) -> PulledImageEvidence:
            del reference, expected_runtime_interface, progress
            changed_archive = b"different output for the same registry reference"
            destination.write_bytes(changed_archive)
            changed_digest = "sha256:" + "b" * 64
            return PulledImageEvidence(
                manifest_digest=changed_digest,
                requested_manifest_digest=REGISTRY_DIGEST,
                config_id="sha256:" + "c" * 64,
                local_reference="localhost/vonk/direct@" + changed_digest,
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=hashlib.sha256(changed_archive).hexdigest(),
                archive_bytes=len(changed_archive),
            )

    Path(receipt.archive_path).unlink()
    (storage.root / f"{approved.oci_layout_sha256}.receipt.json").unlink()
    with sessions.begin() as session:
        session.execute(delete(RuntimeImageAuthorization))
    executor._runtime_image_preparer = make_runtime_image_receipt_preparer(
        sessions,
        storage,
        ChangedTransport(),
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeImagePreparationError) as rejected:
        executor._prepare_runtime_image(
            plan,
            phase,
            item_index=0,
            actor="test",
            request_key=request_key,
            progress=progress,
        )
    assert rejected.value.code == "run-switch.runtime-image-reference-identity-mismatch"
    assert not (storage.root / approved.oci_layout_sha256).exists()
    assert "target-copy" not in _events
    assert "runtime-install" not in _events


def test_changed_published_output_fails_profile_run_before_target_dispatch(
    tmp_path,
    postgres_engine,
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, events = _make_service(
        tmp_path, availability_key="a" * 64, engine=postgres_engine
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, revision_id)
        authorization = session.scalar(select(RuntimeImageAuthorization))
        assert revision is not None and authorization is not None
        selector = f"{revision.publisher}/{revision.slug}"
    with sessions.begin() as session:
        session.add(User(subject="test", role="administrator"))

    profiles = build_production_fleet_profile_service(
        sessions, clock=lambda: NOW, run_switch_operations=service
    )
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Published image recovery",
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": ["spk_" + "1" * 32],
                        "desired_state": "running",
                    }
                ],
            }
        ),
        actor="test",
    )
    review = profiles.preview(profile.id)
    assert review.allowed, review.reasons
    approved = review.preparation_decisions[0].runtime_image
    assert approved.image_digest == authorization.platform_manifest_digest
    application = profiles.apply(
        profile.id,
        plan_digest=review.plan_digest,
        request_key="2a167580-2a76-4fd6-8f4a-c67767a35a0f",
        actor="test",
    )
    assert application.progress.workload_intent_ordinal is not None
    with sessions() as session:
        assert (
            accepted_profile_runtime_image(
                session,
                application.id,
                revision_id,
                ("spk_" + "1" * 32,),
            )
            == approved
        )

    receipt = storage.read_receipt(authorization.oci_archive_sha256)
    Path(receipt.archive_path).unlink()
    (storage.root / f"{receipt.oci_archive_sha256}.receipt.json").unlink()
    with sessions.begin() as session:
        session.execute(delete(RuntimeImageAuthorization))

    class ChangedTransport(_Transport):
        def pull_and_export(
            self,
            reference: str,
            destination: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            progress=None,
        ) -> PulledImageEvidence:
            del reference, expected_runtime_interface, progress
            changed_archive = b"profile-approved published output changed"
            destination.write_bytes(changed_archive)
            changed_digest = "sha256:" + "b" * 64
            return PulledImageEvidence(
                manifest_digest=changed_digest,
                requested_manifest_digest=REGISTRY_DIGEST,
                config_id="sha256:" + "c" * 64,
                local_reference="localhost/vonk/direct@" + changed_digest,
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=hashlib.sha256(changed_archive).hexdigest(),
                archive_bytes=len(changed_archive),
            )

    executor._runtime_image_preparer = make_runtime_image_receipt_preparer(
        sessions,
        storage,
        ChangedTransport(),
        clock=lambda: NOW,
    )
    request = RunSwitchApplyRequest(
        **_direct_request(revision_id).model_dump(mode="json"),
        request_key="3073b225-12fd-4f39-a4ce-f1ea97346519",
    )
    operation = service.apply(
        request,
        actor="test",
        profile_application_id=application.id,
        workload_intent_ordinal=application.progress.workload_intent_ordinal,
    )
    with sessions() as session:
        stored_operation = session.get(Job, operation.operation_id)
        assert stored_operation is not None
        assert (stored_operation.result or {}).get("profile_application_id") == (
            application.id
        )
    for _ in range(16):
        service._advance(operation.operation_id)
        with sessions() as session:
            row = session.get(Job, operation.operation_id)
            assert row is not None
            if row.state in {"failed", "succeeded"}:
                break

    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and row.state == "failed"
        assert (row.result or {}).get("failure_code") == (
            "run-switch.runtime-image-reference-identity-mismatch"
        )
        assert session.scalar(select(RecipeInstallation.id)) is None
        assert not tuple(
            session.scalars(select(Job.id).where(Job.kind == "recipe.install"))
        )
    assert "runtime-image" in events
    assert "target-copy" not in events
    assert "runtime-install" not in events
