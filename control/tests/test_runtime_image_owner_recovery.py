"""Verified ingress, durable preparation and exact receipt reuse under storage faults."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, WaitReason
from vonk_control.models import Base, Job
from vonk_control.oci_image_store import OciImageStore, StoredImage
from vonk_control.recipe_image_availability import (
    BuildUnsettled,
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_image_availability_contract import AvailabilityBuildReceipt
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    OciLayoutImageTransport,
    prepare_runtime_image,
)

from .test_oci_image_store import _docker_archive, _layer
from .test_recipe_image_availability import _add_revision, _recipe, _runtime


@pytest.mark.needs_skopeo
@pytest.mark.parametrize("damage", ["manifest", "recorded-size"])
def test_verified_ingress_repairs_metadata_and_bounded_storage_failure_allows_fresh_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'owner.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        _add_revision(session, "verified-ingress", recipe)
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    storage.layout = OciImageStore(
        tmp_path / "images", skopeo=shutil.which("skopeo") or ""
    )
    archive = _docker_archive(
        tmp_path / "verified.tar", [_layer("model", b"verified image")]
    )
    now = [datetime.now(UTC)]
    imports = []

    def builder(*_args, **_kwargs):
        cached = storage.find_build(
            "f" * 64,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        if cached is not None:
            return AvailabilityBuildReceipt(
                state=LifecycleState.SUCCEEDED,
                build_id=cached.build_id,
                build_input_sha256=cached.build_input_sha256,
                image_digest=cached.image_digest,
                oci_layout_sha256=cached.oci_archive_sha256,
                image_bytes=cached.image_bytes,
            ).model_dump(mode="json")
        image = storage.layout.import_archive(archive)
        if not isinstance(image, StoredImage):
            return BuildUnsettled(
                image.code,
                image.detail,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                retryable=True,
            )
        imports.append(image)
        return AvailabilityBuildReceipt(
            state=LifecycleState.SUCCEEDED,
            build_id="verified-build",
            build_input_sha256="f" * 64,
            image_digest=image.manifest_digest,
            oci_layout_sha256=image.manifest_digest.removeprefix("sha256:"),
            image_bytes=image.stored_bytes,
        ).model_dump(mode="json")

    def owner():
        return RecipeImageAvailabilityService(
            sessions,
            storage=storage,
            transport=OciLayoutImageTransport(),
            builder=builder,
            authority=lambda *_args, **_kwargs: (recipe, _runtime()),
            clock=lambda: now[0],
        )

    service = owner()
    first = service.start("verified-ingress", actor="operator", request_id="initial")
    service.run_pending()
    assert service.get(first.id).state == LifecycleState.SUCCEEDED, service.get(
        first.id
    ).failure
    image = imports[0]
    manifest = storage.layout.blob_path(image.manifest_digest)
    receipt_path = (
        storage.root / f"{image.manifest_digest.removeprefix('sha256:')}.receipt.json"
    )
    receipt = storage.read_receipt(image.manifest_digest.removeprefix("sha256:"))
    if damage == "manifest":
        manifest.write_text("{")
    else:
        receipt_path.write_text(
            receipt.model_copy(
                update={"image_bytes": receipt.image_bytes + 1}
            ).model_dump_json()
        )
    repaired = service.start("verified-ingress", actor="operator", request_id="repair")
    service.run_pending()
    assert service.get(repaired.id).state == LifecycleState.SUCCEEDED
    assert storage.layout.read(image.manifest_digest) == image
    assert (
        storage.read_receipt(image.manifest_digest.removeprefix("sha256:")).image_bytes
        == image.stored_bytes
    )
    before_reuse = len(imports)
    reuse = service.start("verified-ingress", actor="operator", request_id="reuse")
    service.run_pending()
    assert service.get(reuse.id).state == LifecycleState.SUCCEEDED
    assert len(imports) == before_reuse

    sibling = storage.layout.import_archive(
        _docker_archive(tmp_path / "sibling.tar", [_layer("sibling", b"healthy image")])
    )
    assert isinstance(sibling, StoredImage)
    sibling_receipt = prepare_runtime_image(
        recipe.model_dump(mode="json"),
        runtime=_runtime(),
        storage=storage,
        transport=OciLayoutImageTransport(),
        build_receipt=AvailabilityBuildReceipt(
            state=LifecycleState.SUCCEEDED,
            build_id="healthy-build",
            build_input_sha256="e" * 64,
            image_digest=sibling.manifest_digest,
            oci_layout_sha256=sibling.manifest_digest.removeprefix("sha256:"),
            image_bytes=sibling.stored_bytes,
        ).model_dump(mode="json"),
    )
    original = Path.stat
    blocked_blob = storage.layout.blob_path(image.config_digest)

    def unavailable(path: Path, *args, **kwargs):
        if path == blocked_blob:
            raise PermissionError("temporary local observation failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", unavailable)
    pending = service.start(
        "verified-ingress", actor="operator", request_id="persistent-fault"
    )
    service.run_pending()
    assert service.get(pending.id).result is None
    assert storage.layout.read(sibling.manifest_digest) == sibling
    assert (
        storage.find_verified(
            sibling.manifest_digest,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        == sibling_receipt
    )
    now[0] += timedelta(minutes=16)
    service = owner()
    service.run_pending()
    assert service.get(pending.id).state == LifecycleState.FAILED
    with sessions() as session:
        ended = session.get(Job, pending.id)
        assert ended is not None
        assert ended.payload.get("claim_owner") is None
        assert ended.payload.get("claim_until") is None
    fresh = service.start("verified-ingress", actor="operator", request_id="fresh")
    assert fresh.id != pending.id
    monkeypatch.setattr(Path, "stat", original)
    service.run_pending()
    assert service.get(fresh.id).state == LifecycleState.SUCCEEDED
    assert storage.layout.read(image.manifest_digest) == image
    engine.dispose()
