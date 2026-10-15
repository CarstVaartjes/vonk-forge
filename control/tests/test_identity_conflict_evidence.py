"""An identity conflict keeps both identities as typed evidence, end to end."""

from __future__ import annotations

from pathlib import Path

import pytest
from vonk_agent_protocol import LifecycleState, RecipeImageCode
from vonk_control.failure_evidence import FailureEvidenceService
from vonk_control.install_admission import InstallAdmissionBusy
from vonk_control.models import CatalogDocumentRevision, Job, RecipeBuild
from vonk_control.operation_blockers import IdentityConflict
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityInvalid,
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_image_availability_contract import AvailabilityBuildReceipt
from vonk_control.run_switch_progress import RECOVERY_STALL_WINDOW
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .test_recipe_operations import _CanonicalImageTransport, read_recipe
from .test_run_switch_operations import _cold_compile_switch


def test_identity_conflict_names_both_build_inputs_through_the_evidence_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waiting install's image preparation meets an authority that names
    another build input, and the conflict persists until the load ends.  The
    refusal, the Run/Switch result and the evidence download each carry the
    submitted and the authoritative digest as typed fields; the download's
    prose redaction never touches them.

    Wrong implementation caught: digests only in prose (redacted as opaque
    values in the download) or dropped between the refusal and the download.
    """

    switch = _cold_compile_switch(tmp_path, seconds=1)
    with switch.sessions() as session:
        build = session.get(RecipeBuild, switch.build_id)
        revision = session.get(
            CatalogDocumentRevision, switch.admitted.recipe_revision_id
        )
        assert build is not None and revision is not None
        assert build.build_input_sha256 is not None
        assert build.image_digest is not None
        assert build.oci_layout_sha256 is not None
        assert build.image_bytes is not None
        recipe = read_recipe(revision.document)
        evidence = AvailabilityBuildReceipt(
            state=LifecycleState.SUCCEEDED,
            build_id=build.id,
            build_input_sha256=build.build_input_sha256,
            image_digest=build.image_digest,
            oci_layout_sha256=build.oci_layout_sha256,
            image_bytes=build.image_bytes,
        )
    authoritative = "f" * 64
    assert authoritative != build.build_input_sha256
    expected = IdentityConflict(
        identity="build_input_sha256",
        submitted=build.build_input_sha256,
        authoritative=authoritative,
    )
    preparation = RecipeImageAvailabilityService(
        switch.sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "runtime-images"),
        transport=_CanonicalImageTransport(),
        authority=lambda *_args, **_kwargs: (
            recipe,
            {
                "architecture": "linux/arm64",
                "interface": "vonk.runtime.v1",
                "build_input_sha256": authoritative,
            },
        ),
        builder=lambda *_args, **_kwargs: evidence.model_dump(mode="json"),
        clock=switch.clock,
    )
    refusals: list[RecipeImageAvailabilityInvalid] = []

    def prepare(plan, actor, request_id):
        try:
            preparation.prepare_install(plan, actor, request_id)
        except RecipeImageAvailabilityInvalid as refused:
            refusals.append(refused)
            raise

    switch.lifecycle.bind_install_preparation(prepare)

    # The install's managed-bytes recheck keeps finding capacity busy, so it
    # waits and asks the image preparation to run on every attempt.
    def refresh(*_args, **_kwargs):
        raise InstallAdmissionBusy("install.capacity_busy")

    monkeypatch.setattr(switch.admission, "refresh_install_receipts", refresh)

    for _ in range(30):
        switch.drive()
        view = switch.service.get(switch.operation.operation_id)
        assert view.result is not None
        if view.result.refusal:
            break
    assert refusals, "the image preparation never refused"
    assert refusals[0].code == RecipeImageCode.IDENTITY_CONFLICT
    assert [item.identity_conflict for item in refusals[0].blockers] == [expected]
    assert [item.identity_conflict for item in view.result.refusal] == [expected]

    # Whether the conflict ended the load at once or kept it waiting, once the
    # recovery window has passed the load has ended with the refusal on record.
    switch.clock.advance(2 * int(RECOVERY_STALL_WINDOW.total_seconds()))
    for _ in range(30):
        if view.state not in {LifecycleState.QUEUED, LifecycleState.RUNNING}:
            break
        switch.drive()
        view = switch.service.get(switch.operation.operation_id)
        assert view.result is not None
    assert view.state == LifecycleState.FAILED, view.status_reason
    assert [item.identity_conflict for item in view.result.refusal] == [expected]
    with switch.sessions() as session:
        job = session.get(Job, switch.operation.operation_id)
        assert job is not None
        attempt = max(1, job.current_attempt)
    bundle = FailureEvidenceService(switch.sessions, clock=switch.clock).read(
        switch.operation.operation_id, attempt
    )
    assert [item.identity_conflict for item in bundle.blockers] == [expected]
