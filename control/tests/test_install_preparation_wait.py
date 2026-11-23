"""A waiting install stays a wait when its image preparation answers a conflict."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, RecipeImageCode
from vonk_control.install_admission import InstallAdmissionBusy
from vonk_control.models import CatalogDocumentRevision, RecipeBuild, RecipeInstallation
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityInvalid,
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_image_availability_contract import AvailabilityBuildReceipt
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .test_recipe_operations import _CanonicalImageTransport, read_recipe
from .test_run_switch_operations import _cold_compile_switch


def _install_waits(switch, monkeypatch: pytest.MonkeyPatch, *, times: int) -> list:
    """The install's managed-bytes recheck finds capacity busy ``times`` times."""

    recheck = switch.admission.refresh_install_receipts
    busy = [InstallAdmissionBusy("install.capacity_busy") for _ in range(times)]

    def refresh(*args, **kwargs):
        if busy:
            raise busy.pop()
        return recheck(*args, **kwargs)

    monkeypatch.setattr(switch.admission, "refresh_install_receipts", refresh)
    return busy


def test_waiting_install_with_conflicting_image_preparation_replans_and_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install waits (its managed-bytes recheck finds capacity busy), so it
    asks the real image preparation to run.  The image owner's authority names
    another build input once, and the preparation answers IDENTITY_CONFLICT.
    The load re-plans; the next plan meets the settled authority and installs.

    Wrong implementation caught: raising the preparation's conflict as the
    install's outcome turns the wait into a definite failure that ends the load.
    """

    switch = _cold_compile_switch(tmp_path, seconds=1)
    with switch.sessions() as session:
        build = session.get(RecipeBuild, switch.build_id)
        revision = session.get(
            CatalogDocumentRevision, switch.admitted.recipe_revision_id
        )
        assert build is not None and revision is not None
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
    # The authority names another build input during the first preparation.
    moved = ["f" * 64]

    def authority(*_args, **_kwargs):
        return recipe, {
            "architecture": "linux/arm64",
            "interface": "vonk.runtime.v1",
            "build_input_sha256": moved[0] if moved else build.build_input_sha256,
        }

    preparation = RecipeImageAvailabilityService(
        switch.sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "runtime-images"),
        transport=_CanonicalImageTransport(),
        authority=authority,
        builder=lambda *_args, **_kwargs: evidence.model_dump(mode="json"),
        clock=switch.clock,
    )
    conflicts: list[str] = []

    def prepare(plan, actor, request_id):
        try:
            preparation.prepare_install(plan, actor, request_id)
        except RecipeImageAvailabilityInvalid as refused:
            conflicts.append(refused.code)
            raise
        finally:
            moved.clear()

    switch.lifecycle.bind_install_preparation(prepare)
    busy = _install_waits(switch, monkeypatch, times=2)

    generations: set[int] = set()
    waits: set[str | None] = set()
    for _ in range(30):
        switch.drive()
        view = switch.service.get(switch.operation.operation_id)
        assert view.state != "failed", view.status_reason
        assert view.result is not None
        generations.add(view.result.phase_retry_generation)
        waits.add(view.result.retry_reason)
        if view.progress.subphase == "runtime-install":
            break
    assert conflicts == [RecipeImageCode.IDENTITY_CONFLICT]
    assert not busy, "the install never waited"
    assert view.progress.subphase == "runtime-install", view.status_reason
    assert max(generations) >= 1, "the conflict did not re-plan the load"
    # The wait keeps the refusal it came from as its evidence.
    assert any(
        wait and RecipeImageAvailabilityInvalid.__name__ in wait for wait in waits
    ), waits
    with switch.sessions() as session:
        installations = list(session.scalars(select(RecipeInstallation)))
    assert len(installations) == 1


def test_waiting_install_with_a_deterministic_preparation_refusal_ends_the_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the conflict a fresh plan answers stays a wait: a preparation that
    refuses the request itself ends the load with that refusal as evidence.

    Wrong implementation caught: keeping every preparation refusal a wait, which
    re-plans a request no plan can satisfy until its budget is spent.
    """

    switch = _cold_compile_switch(tmp_path, seconds=1)

    def prepare(*_args):
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.RECIPE_INVALID, "recipe revision is required"
        )

    switch.lifecycle.bind_install_preparation(prepare)
    _install_waits(switch, monkeypatch, times=1)
    for _ in range(30):
        switch.drive()
        view = switch.service.get(switch.operation.operation_id)
        if view.state != "running":
            break
    assert view.state == "failed"
    assert view.status_reason
