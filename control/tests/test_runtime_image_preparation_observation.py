"""Transport/storage uncertainty is bounded and never poisons fresh admission."""

from dataclasses import replace

import pytest
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .test_runtime_image_preparation import (
    ARCHIVE,
    ARCHIVE_DIGEST,
    BUILT_IMAGE_DIGEST,
    TinyTransport,
    _prepare,
)


@pytest.mark.parametrize(
    "fault",
    [
        "unreadable",
        "exception",
        "missing-config",
        "missing-size",
        "different-address",
        "different-size",
    ],
)
@pytest.mark.parametrize("clears", [True, False])
def test_transport_reobserves_then_publishes_or_ends_and_fresh_request_heals(
    tmp_path, fault, clears
):
    storage = FilesystemRuntimeImageStorage(tmp_path)
    transport = TinyTransport()
    inspect = transport.inspect_archive
    calls = []
    broken = True

    def observe(*args, **kwargs):
        calls.append(args[0])
        evidence = inspect(*args, **kwargs)
        if not broken or (clears and len(calls) == 3):
            return evidence
        if fault == "exception":
            raise OSError("inspection reply unavailable")
        if fault == "unreadable":
            return replace(evidence, manifest_digest="unreadable")
        if fault == "missing-config":
            return replace(evidence, config_id="")
        if fault == "different-address":
            return replace(evidence, archive_sha256="9" * 64)
        if fault == "different-size":
            return replace(evidence, archive_bytes=evidence.archive_bytes + 1)
        return replace(evidence, archive_bytes=0)

    transport.inspect_archive = observe
    if clears:
        receipt = _prepare(storage=storage, transport=transport)
        assert receipt.image_digest == BUILT_IMAGE_DIGEST
    else:
        # Any bounded ending is acceptable; no invalid observation may publish.
        with pytest.raises(ValueError):
            _prepare(storage=storage, transport=transport)
        assert not (storage.root / f"{ARCHIVE_DIGEST}.receipt.json").exists()
        assert storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE))
    assert len(calls) == 3
    broken = False
    fresh = _prepare(storage=storage, transport=transport)
    assert fresh.image_digest == BUILT_IMAGE_DIGEST
    assert storage.read_receipt(ARCHIVE_DIGEST) == fresh
    with storage.publication_lock(ARCHIVE_DIGEST):
        pass


def test_transport_metadata_does_not_override_accepted_compatibility(tmp_path):
    storage = FilesystemRuntimeImageStorage(tmp_path)
    transport = TinyTransport()
    inspect = transport.inspect_archive

    def conflicting(*args, **kwargs):
        return replace(
            inspect(*args, **kwargs), architecture="linux/amd64", runtime_interface=""
        )

    transport.inspect_archive = conflicting
    receipt = _prepare(storage=storage, transport=transport)
    assert receipt.image_digest == BUILT_IMAGE_DIGEST
    assert receipt == _prepare(storage=storage, transport=transport)


def test_wrong_stored_build_size_is_reconstructed_from_exact_managed_content(tmp_path):
    from vonk_control.runtime_image_preparation import prepare_runtime_image

    from .runtime_image_fixtures import place_test_image
    from .test_runtime_image_preparation import _build_receipt, _document, _runtime

    storage = FilesystemRuntimeImageStorage(tmp_path)
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    build = _build_receipt() | {"image_bytes": len(ARCHIVE) + 1}
    receipt = prepare_runtime_image(
        _document("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        build_receipt=build,
    )
    assert receipt.image_bytes == len(ARCHIVE)
    assert receipt.image_digest == BUILT_IMAGE_DIGEST
    assert _prepare(storage=storage).image_bytes == len(ARCHIVE)
