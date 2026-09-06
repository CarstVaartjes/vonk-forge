from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    SkopeoOCIImageTransport,
    prepare_runtime_image,
)
from vonk_forge_contracts import RecipeDefinition

IMAGE_DIGEST = "sha256:" + "d" * 64
PLATFORM_IMAGE_DIGEST = "sha256:" + "e" * 64
BUILT_IMAGE_DIGEST = "sha256:" + "f" * 64
ARCHIVE = b"tiny verified OCI archive fixture"
ARCHIVE_DIGEST = hashlib.sha256(ARCHIVE).hexdigest()


def _recipe(name: str) -> RecipeDefinition:
    raw = json.loads(files("vonk_forge_contracts").joinpath("examples", name).read_text())
    return RecipeDefinition.model_validate(raw)


def _runtime() -> dict[str, str]:
    return {"architecture": "linux/arm64", "interface": "vonk.runtime.v1"}


class TinyTransport:
    def __init__(self, payload: bytes = ARCHIVE) -> None:
        self.payload = payload
        self.calls: list[tuple[str, Path]] = []

    def pull_and_export(
        self,
        reference: str,
        destination: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> PulledImageEvidence:
        self.calls.append((reference, destination))
        destination.write_bytes(self.payload)
        return PulledImageEvidence(
            manifest_digest=PLATFORM_IMAGE_DIGEST,
            requested_manifest_digest=IMAGE_DIGEST,
            config_id="sha256:" + "c" * 64,
            local_reference="localhost/vonk/tiny@" + PLATFORM_IMAGE_DIGEST,
            architecture="linux/arm64",
            runtime_interface="v1",
            archive_sha256=ARCHIVE_DIGEST,
            archive_bytes=len(ARCHIVE),
        )

    def inspect_archive(
        self,
        archive: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str,
        expected_archive_bytes: int,
    ) -> PulledImageEvidence:
        assert archive.read_bytes() == ARCHIVE
        return PulledImageEvidence(
            manifest_digest=BUILT_IMAGE_DIGEST,
            requested_manifest_digest=None,
            config_id="sha256:" + "d" * 64,
            local_reference="oci-archive:" + str(archive),
            architecture=expected_architecture,
            runtime_interface=expected_runtime_interface,
            archive_sha256=expected_archive_sha256,
            archive_bytes=expected_archive_bytes,
        )


def test_prebuilt_pull_export_is_verified_and_receipt_is_immediately_readable(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()

    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=transport,
    )

    assert receipt.source == "published"
    assert receipt.registry_manifest_digest == IMAGE_DIGEST
    assert receipt.platform_manifest_digest == PLATFORM_IMAGE_DIGEST
    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert receipt.image_digest == PLATFORM_IMAGE_DIGEST
    assert receipt.local_image_config_id == "sha256:" + "c" * 64
    assert receipt.local_image_reference is None
    assert receipt.runtime_interface == "vonk.runtime.v1"
    assert receipt.runtime_interface_label == "v1"
    assert Path(receipt.archive_path).read_bytes() == ARCHIVE
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt
    assert transport.calls[0][0].endswith(IMAGE_DIGEST)

    resolved = storage.find_verified(
        IMAGE_DIGEST,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )
    assert resolved == receipt
    assert len(transport.calls) == 1

    reused = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=transport,
    )
    assert reused == receipt
    assert len(transport.calls) == 1


def test_prebuilt_cache_reuse_ignores_editorial_recipe_digest(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()
    original = _recipe("recipe-image.json")
    first = prepare_runtime_image(
        original,
        runtime=_runtime(),
        storage=storage,
        transport=transport,
    )
    raw = original.model_dump(mode="json")
    raw["metadata"]["description"] = "Editorially revised description"
    revised = RecipeDefinition.model_validate(raw)
    second = prepare_runtime_image(
        revised,
        runtime=_runtime(),
        storage=storage,
        transport=transport,
    )
    assert second == first
    assert len(transport.calls) == 1


def test_corrupt_prebuilt_receipt_fails_before_redownload(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=transport,
    )
    receipt_path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    value["image_digest"] = "sha256:" + "f" * 64
    receipt_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeImagePreparationError, match="receipt identity"):
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=_runtime(),
            storage=storage,
            transport=transport,
        )
    assert len(transport.calls) == 1


def test_packaged_skopeo_transport_observes_config_label_and_exports_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "export.oci.tar"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        calls.append(command)
        if "--format" in command:
            digest = BUILT_IMAGE_DIGEST if command[-1].startswith("oci-archive:") else PLATFORM_IMAGE_DIGEST
            return SimpleNamespace(stdout=digest + "\n")
        if "--raw" in command:
            return SimpleNamespace(stdout=json.dumps({"config": {"digest": "sha256:" + "c" * 64}}))
        if "--config" in command:
            return SimpleNamespace(stdout=json.dumps({
                "os": "linux",
                "architecture": "arm64",
                "config": {"Labels": {"ai.vonkforge.runtime-interface": "v1"}},
            }))
        destination.write_bytes(ARCHIVE)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr("vonk_control.runtime_image_preparation.subprocess.run", fake_run)
    evidence = SkopeoOCIImageTransport().pull_and_export(
        "registry.example/vonk/tiny@" + IMAGE_DIGEST,
        destination,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )

    assert evidence.manifest_digest == PLATFORM_IMAGE_DIGEST
    assert evidence.requested_manifest_digest == IMAGE_DIGEST
    assert evidence.config_id == "sha256:" + "c" * 64
    assert evidence.archive_sha256 == ARCHIVE_DIGEST
    assert any(command[1:3] == ["copy", "--override-os"] for command in calls)
    assert all(
        command[1:5] in (
            ["inspect", "--override-os", "linux", "--override-arch"],
            ["copy", "--override-os", "linux", "--override-arch"],
        )
        for command in calls
    )


def test_packaged_skopeo_transport_rejects_unlabeled_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "export.oci.tar"

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        if "--format" in command:
            return SimpleNamespace(stdout=IMAGE_DIGEST + "\n")
        if "--raw" in command:
            return SimpleNamespace(stdout=json.dumps({"config": {"digest": "sha256:" + "c" * 64}}))
        return SimpleNamespace(stdout=json.dumps({
            "os": "linux",
            "architecture": "arm64",
            "config": {"Labels": {}},
        }))

    monkeypatch.setattr("vonk_control.runtime_image_preparation.subprocess.run", fake_run)
    with pytest.raises(RuntimeImagePreparationError, match="runtime interface label"):
        SkopeoOCIImageTransport().pull_and_export(
            "registry.example/vonk/tiny@" + IMAGE_DIGEST,
            destination,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )


def test_source_build_uses_same_normalized_receipt_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    archive = storage.root / ARCHIVE_DIGEST
    archive.write_bytes(ARCHIVE)
    receipt = prepare_runtime_image(
        _recipe("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        build_receipt={
            "state": "succeeded",
            "build_id": "build-7",
            "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST,
            "image_bytes": len(ARCHIVE),
        },
    )

    assert receipt.source == "controller-build"
    assert receipt.build_id == "build-7"
    assert receipt.registry_manifest_digest is None
    assert receipt.platform_manifest_digest == BUILT_IMAGE_DIGEST
    assert receipt.image_digest == BUILT_IMAGE_DIGEST
    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert receipt.local_image_config_id == "sha256:" + "d" * 64
    assert receipt.local_image_reference is None
    assert receipt.runtime_interface == "vonk.runtime.v1"
    assert receipt.runtime_interface_label == "v1"
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt
    assert archive.read_bytes() == ARCHIVE


def test_transport_digest_mismatch_does_not_publish_archive_or_receipt(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")

    class WrongDigest(TinyTransport):
        def pull_and_export(
            self,
            reference: str,
            destination: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
        ) -> PulledImageEvidence:
            destination.write_bytes(ARCHIVE)
            return PulledImageEvidence(
                manifest_digest="sha256:" + "b" * 64,
                requested_manifest_digest="sha256:" + "a" * 64,
                config_id="sha256:" + "c" * 64,
                local_reference=reference,
                architecture="linux/arm64",
                runtime_interface="v1",
                archive_sha256=ARCHIVE_DIGEST,
                archive_bytes=len(ARCHIVE),
            )

    with pytest.raises(RuntimeImagePreparationError, match="different recipe image digest"):
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=_runtime(),
            storage=storage,
            transport=WrongDigest(),
        )

    assert list(storage.root.iterdir()) == []


def test_build_receipt_requires_the_exact_stored_archive(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    archive = storage.root / ARCHIVE_DIGEST
    archive.write_bytes(ARCHIVE)

    with pytest.raises(RuntimeImagePreparationError, match="not present"):
        prepare_runtime_image(
            _recipe("recipe-source-build.json"),
            runtime=_runtime(),
            storage=storage,
            build_receipt={
                "state": "succeeded",
                "image_digest": BUILT_IMAGE_DIGEST,
                "oci_layout_sha256": "1" * 64,
                "image_bytes": len(ARCHIVE),
                "architecture": "linux/arm64",
                "runtime_interface": "v1",
            },
        )


def test_runtime_distribution_document_is_not_a_recipe_authority(tmp_path: Path) -> None:
    with pytest.raises(RuntimeImagePreparationError, match="canonical RecipeDefinition"):
        prepare_runtime_image(
            {
                "kind": "runtime-distribution",
                "identity": {"publisher": "vonk-forge", "slug": "legacy"},
                "image": "registry.example/legacy@sha256:" + "a" * 64,
                "image_manifest": {"digest": "a" * 64},
                "platform": "linux/arm64",
            },
            runtime=_runtime(),
            storage=FilesystemRuntimeImageStorage(tmp_path / "objects"),
            transport=TinyTransport(),
        )
