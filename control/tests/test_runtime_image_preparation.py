from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    prepare_runtime_image,
)

IMAGE_DIGEST = "sha256:" + "a" * 64
BUILT_IMAGE_DIGEST = "sha256:" + "f" * 64
CONTENT_DIGEST = "b" * 64
ARCHIVE = b"tiny verified OCI archive fixture"
ARCHIVE_DIGEST = hashlib.sha256(ARCHIVE).hexdigest()


def _distribution() -> dict[str, object]:
    return {
        "content_sha256": CONTENT_DIGEST,
        "document": {
            "kind": "runtime-distribution",
            "identity": {"publisher": "vllm", "slug": "tiny-arm64"},
            "platform": "linux/arm64",
            "runtime_interface": "vonk.runtime.v1",
            "image": f"registry.example/vonk/tiny@{IMAGE_DIGEST}",
            "image_manifest": {"digest": "a" * 64},
        },
    }


class TinyTransport:
    def __init__(self, payload: bytes = ARCHIVE) -> None:
        self.payload = payload
        self.calls: list[tuple[str, Path]] = []

    def pull_and_export(self, reference: str, destination: Path) -> PulledImageEvidence:
        self.calls.append((reference, destination))
        destination.write_bytes(self.payload)
        return PulledImageEvidence(
            manifest_digest=IMAGE_DIGEST,
            config_id="sha256:" + "c" * 64,
            local_reference="localhost/vonk/tiny@" + IMAGE_DIGEST,
            architecture="linux/arm64",
            runtime_interface="vonk.runtime.v1",
        )


def test_prebuilt_pull_export_is_verified_and_receipt_is_immediately_readable(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()

    receipt = prepare_runtime_image(_distribution(), storage=storage, transport=transport)

    assert receipt.source == "published"
    assert receipt.registry_manifest_digest == IMAGE_DIGEST
    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert receipt.image_digest == IMAGE_DIGEST
    assert receipt.local_image_config_id == "sha256:" + "c" * 64
    assert receipt.local_image_reference.endswith(IMAGE_DIGEST)
    assert Path(receipt.archive_path).read_bytes() == ARCHIVE
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt
    assert transport.calls[0][0].endswith(IMAGE_DIGEST)


def test_source_build_uses_same_normalized_receipt_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    archive = storage.root / ARCHIVE_DIGEST
    archive.write_bytes(ARCHIVE)
    distribution = _distribution()
    document = distribution["document"]
    assert isinstance(document, dict)
    document.pop("runtime_interface")

    receipt = prepare_runtime_image(
        distribution,
        storage=storage,
        recipe={"runtime": {"interface": "vonk.runtime.v1"}},
        build_receipt={
            "state": "succeeded",
            "build_id": "build-7",
            "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST,
            "image_bytes": len(ARCHIVE),
            "architecture": "linux/arm64",
            "runtime_interface": "vonk.runtime.v1",
            "config_id": "sha256:" + "d" * 64,
            "local_reference": "controller/build/build-7@" + IMAGE_DIGEST,
        },
    )

    assert receipt.source == "controller-build"
    assert receipt.build_id == "build-7"
    assert receipt.registry_manifest_digest == IMAGE_DIGEST
    assert receipt.image_digest == BUILT_IMAGE_DIGEST
    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt
    assert archive.read_bytes() == ARCHIVE


def test_transport_digest_mismatch_does_not_publish_archive_or_receipt(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")

    class WrongDigest(TinyTransport):
        def pull_and_export(self, reference: str, destination: Path) -> PulledImageEvidence:
            destination.write_bytes(ARCHIVE)
            return PulledImageEvidence(
                manifest_digest="sha256:" + "e" * 64,
                config_id="config",
                local_reference=reference,
                architecture="linux/arm64",
                runtime_interface="vonk.runtime.v1",
            )

    with pytest.raises(RuntimeImagePreparationError, match="different manifest"):
        prepare_runtime_image(_distribution(), storage=storage, transport=WrongDigest())

    assert list(storage.root.iterdir()) == []


def test_build_receipt_requires_the_exact_stored_archive(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    archive = storage.root / ARCHIVE_DIGEST
    archive.write_bytes(ARCHIVE)

    with pytest.raises(RuntimeImagePreparationError, match="not present"):
        prepare_runtime_image(
            _distribution(),
            storage=storage,
            build_receipt={
                "state": "succeeded",
                "image_digest": BUILT_IMAGE_DIGEST,
                "oci_layout_sha256": "1" * 64,
                "image_bytes": len(ARCHIVE),
                "architecture": "linux/arm64",
                "runtime_interface": "vonk.runtime.v1",
            },
        )
