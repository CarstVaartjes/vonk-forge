from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    RecipeBuild,
    RuntimeImageAuthorization,
)
from vonk_control.models import RuntimeImageReceipt as RuntimeImageReceiptRow
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    SkopeoOCIImageTransport,
    persist_runtime_image_receipt,
    prepare_runtime_image,
    resolve_persisted_runtime_image_receipt,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

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


def _add_revision(
    session: Session,
    revision_id: str,
    recipe: RecipeDefinition,
    *,
    number: int = 1,
    state: str = "active",
) -> None:
    session.add(
        CatalogDocumentRevision(
            id=revision_id,
            document_id="document-" + revision_id,
            kind="recipe",
            publisher=recipe.identity.publisher,
            slug=recipe.identity.slug,
            revision_number=number,
            schema_version=2,
            state=state,
            document=recipe.model_dump(mode="json"),
            content_digest=content_sha256(recipe),
            artifact_key="b" * 64,
            execution_key="a" * 64,
            projected={},
            created_by="test",
            created_at=datetime.now(UTC),
        )
    )


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
            local_reference="docker-archive:" + str(archive),
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
    written = []

    def writer(value):
        assert Path(value.archive_path).is_file()
        assert storage.read_receipt(value.oci_archive_sha256) == value
        written.append(value)

    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=transport,
        receipt_writer=writer,
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
    assert storage.root == tmp_path / "objects" / "image-cache"
    assert Path(receipt.archive_path).parent == storage.root
    assert Path(receipt.archive_path).read_bytes() == ARCHIVE
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt
    assert transport.calls[0][0].endswith(IMAGE_DIGEST)
    assert written == [receipt]

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
        receipt_writer=writer,
    )
    assert reused == receipt
    assert written == [receipt, receipt]
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


def test_non_schema_two_receipt_is_rejected_by_all_read_paths(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
    )
    receipt_path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    value["schema_version"] = 1
    receipt_path.write_text(json.dumps(value), encoding="utf-8")
    for read in (
        lambda: storage.find_published(
            IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        ),
        lambda: storage.find_verified(
            IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        ),
        lambda: storage.read_receipt(receipt.oci_archive_sha256),
    ):
        with pytest.raises(RuntimeImagePreparationError, match="schema version"):
            read()


def test_packaged_skopeo_transport_observes_config_label_and_exports_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "export.docker.tar"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        calls.append(command)
        if "--format" in command:
            digest = PLATFORM_IMAGE_DIGEST if command[-1].startswith("docker-archive:") else IMAGE_DIGEST
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
    assert any(command[-1] == f"docker-archive:{destination}" and command[1] == "copy" for command in calls)
    assert any(command[-1] == f"docker-archive:{destination}" and command[1] == "inspect" for command in calls)
    assert not any("--raw" in command and command[-1].startswith("docker://") for command in calls)
    assert any(command[1:3] == ["copy", "--override-os"] for command in calls)
    assert all(
        command[1:5] in (
            ["inspect", "--override-os", "linux", "--override-arch"],
            ["copy", "--override-os", "linux", "--override-arch"],
        )
        for command in calls
    )


def test_docker_export_keeps_build_provenance_separate_from_reconstructed_manifest(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    (storage.root / ARCHIVE_DIGEST).write_bytes(ARCHIVE)

    class DifferentArchive(TinyTransport):
        def inspect_archive(self, archive: Path, **kwargs: object) -> PulledImageEvidence:
            return replace(super().inspect_archive(archive, **kwargs), manifest_digest=IMAGE_DIGEST)

    receipt = prepare_runtime_image(
        _recipe("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=DifferentArchive(),
        build_receipt={
            "state": "succeeded", "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST, "image_bytes": len(ARCHIVE),
        },
    )
    assert receipt.platform_manifest_digest == BUILT_IMAGE_DIGEST
    assert receipt.image_digest == BUILT_IMAGE_DIGEST
    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert receipt.local_image_config_id == "sha256:" + "d" * 64
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt


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

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _add_revision(session, "revision-source", _recipe("recipe-source-build.json"))
        session.add(
            RecipeBuild(
                id="build-7",
                recipe_revision_id="revision-source",
                builder_node_id="builder",
                source_bundle_sha256="c" * 64,
                build_input_sha256="d" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=BUILT_IMAGE_DIGEST,
                oci_layout_sha256=ARCHIVE_DIGEST,
                image_bytes=len(ARCHIVE),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        row = persist_runtime_image_receipt(
            session,
            recipe_revision_id="revision-source",
            original_content_digest=receipt.distribution_content_sha256,
            effective_execution_key="e" * 64,
            receipt=receipt,
            verified_at=datetime.now(UTC),
        )
        session.commit()
        assert row.source == "controller-build"
        assert row.build_id == "build-7"
        assert row.registry_manifest_digest is None


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


def test_published_receipt_persists_idempotently_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    revision_id = "revision-direct"
    original_digest = receipt.distribution_content_sha256
    execution_key = "f" * 64
    first_at = datetime.now(UTC)
    with Session(engine) as session:
        _add_revision(session, revision_id, _recipe("recipe-image.json"))
        row = persist_runtime_image_receipt(
            session,
            recipe_revision_id=revision_id,
            original_content_digest=original_digest,
            effective_execution_key=execution_key,
            receipt=receipt,
            verified_at=first_at,
        )
        session.commit()
        assert row.source == "published"
        assert row.registry_manifest_digest == IMAGE_DIGEST
        assert row.platform_manifest_digest == PLATFORM_IMAGE_DIGEST
        assert row.local_image_config_id == "sha256:" + "c" * 64
        assert row.oci_archive_sha256 == ARCHIVE_DIGEST
        assert row.image_bytes == len(ARCHIVE)
    second_at = first_at + timedelta(seconds=1)
    with Session(engine) as session:
        same = persist_runtime_image_receipt(
            session,
            recipe_revision_id=revision_id,
            original_content_digest=original_digest,
            effective_execution_key=execution_key,
            receipt=receipt,
            verified_at=second_at,
        )
        session.commit()
        assert same.id == row.id
        assert same.verified_at.replace(tzinfo=UTC) == second_at
    conflicting = replace(
        receipt,
        platform_manifest_digest=BUILT_IMAGE_DIGEST,
        image_digest=BUILT_IMAGE_DIGEST,
    )
    with Session(engine) as session, pytest.raises(
        RuntimeImagePreparationError, match="identity changed"
    ):
        persist_runtime_image_receipt(
            session,
            recipe_revision_id=revision_id,
            original_content_digest=original_digest,
            effective_execution_key=execution_key,
            receipt=conflicting,
            verified_at=second_at,
        )


def test_persisted_receipt_resolver_requires_the_exact_filesystem_identity(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    persist_kwargs = {
        "recipe_revision_id": "revision-direct",
        "original_content_digest": receipt.distribution_content_sha256,
        "effective_execution_key": "f" * 64,
        "receipt": receipt,
    }
    resolve_kwargs = {
        "recipe_revision_id": "revision-direct",
        "current_content_digest": receipt.distribution_content_sha256,
        "effective_execution_key": "f" * 64,
        "receipt": receipt,
    }
    session = Session(engine)
    _add_revision(session, "revision-direct", _recipe("recipe-image.json"))
    session.flush()
    with pytest.raises(ValueError, match="does not match"):
        resolve_persisted_runtime_image_receipt(session, **resolve_kwargs)
    persist_runtime_image_receipt(
        session,
        **persist_kwargs,
        verified_at=datetime.now(UTC),
    )
    session.commit()
    session.close()
    with Session(engine) as session:
        assert resolve_persisted_runtime_image_receipt(session, **resolve_kwargs).source == "published"
        session.query(RuntimeImageReceiptRow).update({"local_image_config_id": "sha256:" + "d" * 64})
        session.commit()
    session = Session(engine)
    with pytest.raises(ValueError, match="does not match"):
        resolve_persisted_runtime_image_receipt(session, **resolve_kwargs)
    session.close()


def test_notes_revision_reuses_original_receipt_with_separate_authorization(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    original = _recipe("recipe-image.json")
    revised_raw = original.model_dump(mode="json")
    revised_raw["metadata"]["description"] = "Editorial notes only"
    revised = RecipeDefinition.model_validate(revised_raw)
    receipt = prepare_runtime_image(original, runtime=_runtime(), storage=storage, transport=TinyTransport())
    old_digest = content_sha256(original)
    new_digest = content_sha256(revised)
    old_id, new_id, document_id = "old-revision", "new-revision", "recipe-document"
    now = datetime.now(UTC)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            CatalogDocument(
                id=document_id,
                kind="recipe",
                publisher=original.identity.publisher,
                slug=original.identity.slug,
                title=original.metadata.title,
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add_all(
            [
                CatalogDocumentRevision(
                    id=old_id,
                    document_id=document_id,
                    kind="recipe",
                    publisher=original.identity.publisher,
                    slug=original.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=original.model_dump(mode="json"),
                    content_digest=old_digest,
                    projected={
                        "source_bundle_sha256": "c" * 64,
                        "package_handle": {"sha256": "1" * 64},
                    },
                    artifact_key="b" * 64,
                    execution_key="a" * 64,
                    created_by="test",
                    created_at=now,
                ),
                CatalogDocumentRevision(
                    id=new_id,
                    document_id=document_id,
                    kind="recipe",
                    publisher=revised.identity.publisher,
                    slug=revised.identity.slug,
                    revision_number=2,
                    schema_version=2,
                    state="active",
                    document=revised.model_dump(mode="json"),
                    content_digest=new_digest,
                    projected={
                        "source_bundle_sha256": "c" * 64,
                        "package_handle": {"sha256": "2" * 64},
                    },
                    artifact_key="b" * 64,
                    execution_key="a" * 64,
                    created_by="test",
                    created_at=now,
                ),
            ]
        )
        session.flush()
        persist_runtime_image_receipt(
            session,
            recipe_revision_id=old_id,
            original_content_digest=old_digest,
            effective_execution_key="a" * 64,
            receipt=receipt,
            verified_at=now,
        )
        persist_runtime_image_receipt(
            session,
            recipe_revision_id=new_id,
            original_content_digest=old_digest,
            effective_execution_key="a" * 64,
            receipt=receipt,
            verified_at=now,
        )
        session.commit()
        assert session.query(RuntimeImageReceiptRow).count() == 1
        assert session.query(RuntimeImageAuthorization).count() == 2
        assert (
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=new_id,
                current_content_digest=new_digest,
                effective_execution_key="a" * 64,
                receipt=receipt,
            ).original_content_digest
            == old_digest
        )
        with pytest.raises(ValueError, match="current recipe revision digest"):
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=new_id,
                current_content_digest=old_digest,
                effective_execution_key="a" * 64,
                receipt=receipt,
            )
        authorization = session.scalar(
            select(RuntimeImageAuthorization).where(
                RuntimeImageAuthorization.recipe_revision_id == new_id
            )
        )
        assert authorization is not None
        authorization.state = "revoked"
        with pytest.raises(ValueError, match="not authorized"):
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=new_id,
                current_content_digest=new_digest,
                effective_execution_key="a" * 64,
                receipt=receipt,
            )
        with pytest.raises(RuntimeImagePreparationError, match="execution identity"):
            persist_runtime_image_receipt(
                session,
                recipe_revision_id=new_id,
                original_content_digest=old_digest,
                effective_execution_key="b" * 64,
                receipt=receipt,
                verified_at=now,
            )


def test_runtime_image_authority_fails_closed_for_missing_or_revoked_bindings(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    digest = content_sha256(recipe)
    revision_id = "authority-revision"
    now = datetime.now(UTC)
    receipt = prepare_runtime_image(
        recipe,
        runtime=_runtime(),
        storage=FilesystemRuntimeImageStorage(tmp_path / "authority-receipt"),
        transport=TinyTransport(),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _add_revision(session, revision_id, recipe)
        with pytest.raises(RuntimeImagePreparationError, match="unavailable or inactive"):
            persist_runtime_image_receipt(
                session,
                recipe_revision_id="missing-revision",
                original_content_digest=digest,
                effective_execution_key="a" * 64,
                receipt=receipt,
                verified_at=now,
            )
        row = persist_runtime_image_receipt(
            session,
            recipe_revision_id=revision_id,
            original_content_digest=digest,
            effective_execution_key="a" * 64,
            receipt=receipt,
            verified_at=now,
        )
        authorization = session.scalar(
            select(RuntimeImageAuthorization).where(
                RuntimeImageAuthorization.recipe_revision_id == revision_id
            )
        )
        assert authorization is not None
        authorization.state = "revoked"
        with pytest.raises(RuntimeImagePreparationError, match="not active"):
            persist_runtime_image_receipt(
                session,
                recipe_revision_id=revision_id,
                original_content_digest=digest,
                effective_execution_key="a" * 64,
                receipt=receipt,
                verified_at=now,
            )
        authorization.state = "authorized"
        row.state = "revoked"
        with pytest.raises(RuntimeImagePreparationError, match="not verified"):
            persist_runtime_image_receipt(
                session,
                recipe_revision_id=revision_id,
                original_content_digest=digest,
                effective_execution_key="a" * 64,
                receipt=receipt,
                verified_at=now,
            )


def test_runtime_image_authority_rejects_changed_current_execution_identity(
    tmp_path: Path,
) -> None:
    original = _recipe("recipe-image.json")
    revised_raw = original.model_dump(mode="json")
    revised_raw["metadata"]["description"] = "Changed execution"
    revised = RecipeDefinition.model_validate(revised_raw)
    now = datetime.now(UTC)
    receipt = prepare_runtime_image(
        original,
        runtime=_runtime(),
        storage=FilesystemRuntimeImageStorage(tmp_path / "authority-execution"),
        transport=TinyTransport(),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _add_revision(session, "original-execution", original)
        _add_revision(
            session,
            "changed-execution",
            revised,
            number=2,
            state="candidate",
        )
        session.flush()
        current = session.get(CatalogDocumentRevision, "changed-execution")
        assert current is not None
        current.execution_key = "c" * 64
        current.state = "active"
        session.flush()
        with pytest.raises(RuntimeImagePreparationError, match="execution or artifact identity changed"):
            persist_runtime_image_receipt(
                session,
                recipe_revision_id="changed-execution",
                original_content_digest=content_sha256(original),
                effective_execution_key="a" * 64,
                receipt=receipt,
                verified_at=now,
            )
def test_receipt_persistence_failure_is_retryable_from_verified_filesystem_state(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()
    attempts = 0

    def fail_once(_receipt: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeImagePreparationError, match="could not be persisted"):
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=_runtime(),
            storage=storage,
            transport=transport,
            receipt_writer=fail_once,
        )
    assert len(transport.calls) == 1
    retry = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=transport,
        receipt_writer=fail_once,
    )
    assert retry.registry_manifest_digest == IMAGE_DIGEST
    assert attempts == 2
    assert len(transport.calls) == 1
