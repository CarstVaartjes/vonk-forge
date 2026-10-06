"""Build resolution treats an incomplete cached receipt as no cache, not as a refusal."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace

from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .test_recipe_builds import _write_controller_build_receipt, setup


def test_an_incomplete_cached_receipt_resolves_as_stale_so_a_fresh_build_replaces_it(
    tmp_path: Path, caplog
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    storage = FilesystemRuntimeImageStorage(tmp_path / "artifacts")
    archive = b"cached source build archive"
    archive_digest = hashlib.sha256(archive).hexdigest()
    image_digest = "sha256:" + "b" * 64
    seeding = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    plan = seeding.plan(revision.id, node_id, now=now)
    seeding.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest=image_digest,
        oci_layout_sha256=archive_digest,
        image_bytes=len(archive),
        now=now,
    )
    _write_controller_build_receipt(
        storage,
        archive=archive,
        image_digest=image_digest,
        build_id=plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        distribution_content_sha256=revision.content_digest,
    )

    def incomplete(*_args: object, **_kwargs: object) -> object:
        # The receipt reader answered, but without the build identity.
        return SimpleNamespace(
            build_id=plan.build_id,
            build_input_sha256=None,
            image_digest=image_digest,
            oci_archive_sha256=archive_digest,
            image_bytes=len(archive),
        )

    service = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=incomplete,  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.DEBUG):
        resolution = service.resolve(revision.id)

    assert resolution.cached is False
    assert resolution.stale_receipt is True
    assert any(
        getattr(record, "residue_kind", "") == "recipe-build.receipt"
        for record in caplog.records
    )
    rebuilt = service.plan(revision.id, node_id, now=now, resolution=resolution)
    assert rebuilt.build_input_sha256 == plan.build_input_sha256
