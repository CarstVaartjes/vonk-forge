from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from vonk_control.catalog_entities import _build_projection
from vonk_control.compiled_execution_plan import CompiledRuntimeImage
from vonk_control.execution_plan_service import _runtime_receipt_mapping
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    RecipeBuild,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_runtime_specs import compile_runtime_spec
from vonk_control.runtime_adapters import resolve_runtime_adapter
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    RuntimeImagePublishedStageCheckpoint,
    RuntimeImageReceipt,
    SkopeoOCIImageTransport,
    _parse_runtime_image_receipt,
    persist_runtime_image_receipt,
    prefixed_image_digest,
    prepare_runtime_image,
    resolve_persisted_runtime_image_receipt,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256
from vonk_forge_contracts.recipe import RecipeImage, RecipeImageExecution

IMAGE_DIGEST = "sha256:" + "d" * 64
PLATFORM_IMAGE_DIGEST = "sha256:" + "e" * 64
BUILT_IMAGE_DIGEST = "sha256:" + "f" * 64
ARCHIVE = b"tiny verified OCI archive fixture"
ARCHIVE_DIGEST = hashlib.sha256(ARCHIVE).hexdigest()


def _recipe(name: str) -> RecipeDefinition:
    raw = json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text()
    )
    return RecipeDefinition.model_validate(raw)


def _runtime() -> dict[str, str]:
    return {"architecture": "linux/arm64", "interface": "vonk.runtime.v1"}


def _projection(recipe: RecipeDefinition) -> dict[str, object]:
    value: dict[str, object] = {
        "title": recipe.metadata.title,
        "description": recipe.metadata.description,
        "tags": list(recipe.metadata.tags),
        "runtime_engine": recipe.runtime.engine,
        "topology": recipe.topology.model_dump(mode="json"),
    }
    value.update(_build_projection(recipe))
    return value


def _add_revision(
    session: Session,
    revision_id: str,
    recipe: RecipeDefinition,
    *,
    number: int = 1,
    state: str = "active",
) -> None:
    projected = _projection(recipe)
    if recipe.execution.mode == "build":
        projected["source_bundle_sha256"] = "c" * 64
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
            projected=projected,
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
        progress=None,
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


def _fake_skopeo_run(
    command: list[str],
    *,
    state: dict[str, int] | None = None,
    counter_path: Path | None = None,
    **_: object,
) -> SimpleNamespace:
    layer_digest = "sha256:" + "1" * 64
    if command[1] == "inspect":
        source = command[-1]
        if "--format" in command:
            digest = (
                PLATFORM_IMAGE_DIGEST
                if source.startswith("docker-archive:")
                else IMAGE_DIGEST
            )
            return SimpleNamespace(stdout=digest + "\n")
        if "--raw" in command:
            return SimpleNamespace(
                stdout=json.dumps({"config": {"digest": "sha256:" + "c" * 64}})
            )
        if "--config" in command:
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "os": "linux",
                        "architecture": "arm64",
                        "config": {"Labels": {"ai.vonkforge.runtime-interface": "v1"}},
                    }
                )
            )
        return SimpleNamespace(
            stdout=json.dumps(
                {"LayersData": [{"Digest": layer_digest, "Size": len(ARCHIVE)}]}
            )
        )
    if "--dest-shared-blob-dir" in command:
        blob_root = Path(command[command.index("--dest-shared-blob-dir") + 1])
        blob = blob_root / layer_digest.replace(":", "/", 1)
        blob.parent.mkdir(parents=True, exist_ok=True)
        if not blob.exists():
            blob.write_bytes(b"completed registry layer")
            if state is not None:
                state["blob_fetches"] = state.get("blob_fetches", 0) + 1
    else:
        destination = Path(command[-1].removeprefix("docker-archive:"))
        destination.write_bytes(ARCHIVE)
        if state is not None:
            state["exports"] = state.get("exports", 0) + 1
        if counter_path is not None:
            previous = (
                int(counter_path.read_text() or "0") if counter_path.exists() else 0
            )
            counter_path.write_text(str(previous + 1), encoding="utf-8")
    return SimpleNamespace(stdout="")


def _die_after_published_checkpoint(root: str, counter: str) -> None:
    """Subprocess target that exits after the storage checkpoint, before commit."""

    from unittest.mock import patch

    with patch(
        "vonk_control.runtime_image_preparation.subprocess.run",
        side_effect=lambda command, **kwargs: _fake_skopeo_run(
            command, counter_path=Path(counter), **kwargs
        ),
    ):
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=_runtime(),
            storage=FilesystemRuntimeImageStorage(Path(root)),
            transport=SkopeoOCIImageTransport(),
            before_publish=lambda _receipt: os._exit(79),
        )


def _die_after_receiptless_final(root: str, counter: str) -> None:
    """Subprocess target that exits after final-link publication, before receipt."""

    from unittest.mock import patch

    from vonk_control import runtime_image_preparation

    atomic_replace = runtime_image_preparation._atomic_json_replace

    def die_before_receipt(path: Path, value: dict[str, object]) -> None:
        if path.name.endswith(".receipt.json"):
            os._exit(80)
        atomic_replace(path, value)

    with (
        patch(
            "vonk_control.runtime_image_preparation.subprocess.run",
            side_effect=lambda command, **kwargs: _fake_skopeo_run(
                command, counter_path=Path(counter), **kwargs
            ),
        ),
        patch(
            "vonk_control.runtime_image_preparation._atomic_json_replace",
            side_effect=die_before_receipt,
        ),
    ):
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=_runtime(),
            storage=FilesystemRuntimeImageStorage(Path(root)),
            transport=SkopeoOCIImageTransport(),
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


def test_unparseable_prebuilt_receipt_is_replaced_from_verified_bytes(
    tmp_path: Path,
) -> None:
    """A published receipt the contract cannot parse is stale metadata.

    The archive is content-addressed and the transport re-verifies its bytes,
    so the receipt beside it is replaced from that verified evidence instead of
    pinning the recipe forever.
    """

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
    repaired = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=transport,
    )
    assert storage.read_receipt(receipt.oci_archive_sha256) == repaired
    assert repaired.image_digest == PLATFORM_IMAGE_DIGEST


def test_non_schema_two_receipt_is_skipped_by_scans_but_refused_by_exact_read(
    tmp_path: Path,
) -> None:
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
    # A scan cannot match a document the current contract rejects, and one
    # archive's unusable receipt must not poison unrelated lookups.
    assert (
        storage.find_published(
            IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    assert (
        storage.find_verified(
            IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    with pytest.raises(RuntimeImagePreparationError, match="schema version"):
        storage.read_receipt(receipt.oci_archive_sha256)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("runtime_interface_label"), "identity"),
        (lambda value: value.update(unexpected_field="rejected"), "identity"),
        (lambda value: value.update(image_bytes=True), "identity"),
    ],
    ids=["missing-interface-label", "unknown-field", "boolean-image-bytes"],
)
def test_current_receipt_parser_rejects_noncanonical_shape(
    tmp_path: Path, mutation, message: str
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
    )
    path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutation(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeImagePreparationError, match=message):
        storage.read_receipt(receipt.oci_archive_sha256)


def test_current_producer_parser_and_compiled_plan_consumer_preserve_archive_identity(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    raw_recipe = _recipe("recipe-image.json").model_dump(mode="json")
    raw_recipe["runtime"]["engine"] = "vllm"
    raw_recipe["runtime"]["entrypoint"] = ["/opt/vonk/bin/vllm", "serve", "/models"]
    recipe = RecipeDefinition.model_validate(raw_recipe)
    model = ModelDefinition.model_validate_json(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_bytes()
    )
    runtime = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)[
        "runtime"
    ]
    produced = prepare_runtime_image(
        recipe,
        runtime=runtime,
        storage=storage,
        transport=TinyTransport(),
    )
    parsed = storage.read_receipt(produced.oci_archive_sha256)
    compiled = CompiledRuntimeImage.model_validate(_runtime_receipt_mapping(parsed))
    assert parsed.oci_archive_sha256 == produced.oci_archive_sha256
    assert compiled.oci_layout_sha256 == produced.oci_archive_sha256
    assert compiled.runtime_interface_label == produced.runtime_interface_label


@pytest.mark.parametrize("field", RuntimeImageReceipt.model_json_schema()["required"])
def test_receipt_reader_requires_every_declared_field(
    tmp_path: Path, field: str
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
    )
    path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document[field]
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RuntimeImagePreparationError):
        storage.read_receipt(receipt.oci_archive_sha256)


def test_packaged_skopeo_transport_observes_config_label_and_exports_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "export.docker.tar"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        calls.append(command)
        if "--format" in command:
            digest = (
                PLATFORM_IMAGE_DIGEST
                if command[-1].startswith("docker-archive:")
                else IMAGE_DIGEST
            )
            return SimpleNamespace(stdout=digest + "\n")
        if "--raw" in command:
            return SimpleNamespace(
                stdout=json.dumps({"config": {"digest": "sha256:" + "c" * 64}})
            )
        if "--config" in command:
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "os": "linux",
                        "architecture": "arm64",
                        "config": {"Labels": {"ai.vonkforge.runtime-interface": "v1"}},
                    }
                )
            )
        if command[1] == "inspect":
            return SimpleNamespace(stdout=json.dumps({"LayersData": []}))
        destination.write_bytes(ARCHIVE)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation.subprocess.run", fake_run
    )
    evidence = SkopeoOCIImageTransport().pull_and_export(
        "registry.example/vonk/tiny@" + IMAGE_DIGEST,
        destination,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )

    registry_copy = next(
        command for command in calls if "--dest-shared-blob-dir" in command
    )
    archive_copy = next(
        command for command in calls if "--src-shared-blob-dir" in command
    )
    assert registry_copy[-1].startswith(f"oci:{tmp_path}/registry-layers/")
    assert registry_copy[registry_copy.index("--retry-times") + 1] == "3"
    assert registry_copy[registry_copy.index("--image-parallel-copies") + 1] == "6"
    assert archive_copy[-2] == registry_copy[-1]
    assert evidence.manifest_digest == PLATFORM_IMAGE_DIGEST
    assert evidence.requested_manifest_digest == IMAGE_DIGEST
    assert evidence.config_id == "sha256:" + "c" * 64
    assert evidence.archive_sha256 == ARCHIVE_DIGEST
    assert any(
        command[-1] == f"docker-archive:{destination}" and command[1] == "copy"
        for command in calls
    )
    assert any(
        command[-1] == f"docker-archive:{destination}" and command[1] == "inspect"
        for command in calls
    )
    assert not any(
        "--raw" in command and command[-1].startswith("docker://") for command in calls
    )
    assert any(command[1:3] == ["copy", "--override-os"] for command in calls)
    assert all(
        command[1:5]
        in (
            ["inspect", "--override-os", "linux", "--override-arch"],
            ["copy", "--override-os", "linux", "--override-arch"],
        )
        for command in calls
    )


def test_docker_export_keeps_build_provenance_separate_from_reconstructed_manifest(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    (storage.root / ARCHIVE_DIGEST).write_bytes(ARCHIVE)

    class DifferentArchive(TinyTransport):
        def inspect_archive(
            self,
            archive: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            evidence = super().inspect_archive(
                archive,
                expected_architecture=expected_architecture,
                expected_runtime_interface=expected_runtime_interface,
                expected_archive_sha256=expected_archive_sha256,
                expected_archive_bytes=expected_archive_bytes,
            )
            return replace(evidence, manifest_digest=IMAGE_DIGEST)

    receipt = prepare_runtime_image(
        _recipe("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=DifferentArchive(),
        build_receipt={
            "state": "succeeded",
            "build_id": "build-archive",
            "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST,
            "image_bytes": len(ARCHIVE),
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
            return SimpleNamespace(
                stdout=json.dumps({"config": {"digest": "sha256:" + "c" * 64}})
            )
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "os": "linux",
                    "architecture": "arm64",
                    "config": {"Labels": {}},
                }
            )
        )

    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation.subprocess.run", fake_run
    )
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
            progress=None,
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

    with pytest.raises(
        RuntimeImagePreparationError, match="different recipe image digest"
    ):
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=_runtime(),
            storage=storage,
            transport=WrongDigest(),
        )
    assert list(storage.root.iterdir()) == []


def test_publication_callback_and_commit_share_the_exact_archive_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    original_commit = storage.commit
    callbacks: list[RuntimeImageReceipt] = []

    def assert_locked() -> None:
        lock_path = storage.root / ".publication-locks" / f"{ARCHIVE_DIGEST}.lock"
        with lock_path.open("a+b") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def commit_while_locked(staged: Path, *, receipt: RuntimeImageReceipt):
        assert_locked()
        return original_commit(staged, receipt=receipt)

    def callback_while_locked(receipt: RuntimeImageReceipt) -> None:
        assert_locked()
        callbacks.append(receipt)

    monkeypatch.setattr(storage, "commit", commit_while_locked)
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        before_publish=callback_while_locked,
    )

    assert len(callbacks) == 1
    assert callbacks[0].oci_archive_sha256 == receipt.oci_archive_sha256
    assert callbacks[0].image_digest == receipt.image_digest
    assert callbacks[0].registry_manifest_digest == receipt.registry_manifest_digest
    assert receipt.registry_manifest_digest == IMAGE_DIGEST
    with storage.publication_lock(receipt.oci_archive_sha256):
        pass


def test_image_publication_lock_refuses_symlinked_lock_directory(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.root / ".publication-locks").symlink_to(outside, target_is_directory=True)

    with (
        pytest.raises(RuntimeImagePreparationError) as error,
        storage.publication_lock(ARCHIVE_DIGEST),
    ):
        pass

    assert error.value.code == "runtime_image.lock_unavailable"
    assert not (outside / f"{ARCHIVE_DIGEST}.lock").exists()


def test_publication_contention_keeps_skopeo_blob_checkpoint_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    state: dict[str, int] = {}
    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation.subprocess.run",
        lambda command, **kwargs: _fake_skopeo_run(command, state=state, **kwargs),
    )
    transport = SkopeoOCIImageTransport()
    with (
        storage.publication_lock(ARCHIVE_DIGEST),
        pytest.raises(RuntimeImagePreparationError) as contended,
    ):
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=_runtime(),
            storage=storage,
            transport=transport,
        )
    assert contended.value.code == "runtime_image.publication_contended"
    assert list(storage.root.glob(".runtime-image-*.part")) == []

    # The verified export and typed checkpoint survive publication contention.
    # The source lock then makes a retry reuse both the tar and completed blobs.
    assert state == {"blob_fetches": 1, "exports": 1}
    checkpoints = list(storage.root.glob(".published-image-*.checkpoint.json"))
    assert len(checkpoints) == 1
    checkpoint = RuntimeImagePublishedStageCheckpoint.model_validate_json(
        checkpoints[0].read_bytes()
    )
    stage = storage.published_stage_path(checkpoint.oci_archive_sha256)
    assert stage.read_bytes() == ARCHIVE
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=transport,
    )
    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert state == {"blob_fetches": 1, "exports": 1}


def test_process_death_after_checkpoint_reuses_export_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "objects"
    counter = tmp_path / "exports.txt"
    test_dir = str(Path(__file__).parent)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (test_dir, environment.get("PYTHONPATH", "")) if value
    )
    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[3])\n"
        "from test_runtime_image_preparation import _die_after_published_checkpoint\n"
        "_die_after_published_checkpoint(sys.argv[1], sys.argv[2])\n"
    )
    killed = subprocess.run(
        [sys.executable, "-c", script, str(root), str(counter), test_dir],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    assert killed.returncode == 79, killed.stderr
    assert counter.read_text(encoding="utf-8") == "1"

    storage = FilesystemRuntimeImageStorage(root)
    checkpoints = list(storage.root.glob(".published-image-*.checkpoint.json"))
    assert len(checkpoints) == 1
    checkpoint = RuntimeImagePublishedStageCheckpoint.model_validate_json(
        checkpoints[0].read_bytes(), strict=True
    )
    stage = storage.published_stage_path(checkpoint.oci_archive_sha256)
    assert stage.read_bytes() == ARCHIVE
    assert not (storage.root / ARCHIVE_DIGEST).exists()
    assert not (storage.root / f"{ARCHIVE_DIGEST}.receipt.json").exists()

    state: dict[str, int] = {}
    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation.subprocess.run",
        lambda command, **kwargs: _fake_skopeo_run(command, state=state, **kwargs),
    )
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=SkopeoOCIImageTransport(),
    )

    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert counter.read_text(encoding="utf-8") == "1"
    assert state == {}
    assert Path(receipt.archive_path).read_bytes() == ARCHIVE
    assert os.path.samefile(stage, Path(receipt.archive_path))


def test_process_death_after_final_link_repairs_receipt_from_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "objects"
    counter = tmp_path / "exports.txt"
    test_dir = str(Path(__file__).parent)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (test_dir, environment.get("PYTHONPATH", "")) if value
    )
    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[3])\n"
        "from test_runtime_image_preparation import _die_after_receiptless_final\n"
        "_die_after_receiptless_final(sys.argv[1], sys.argv[2])\n"
    )
    killed = subprocess.run(
        [sys.executable, "-c", script, str(root), str(counter), test_dir],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    assert killed.returncode == 80, killed.stderr
    assert counter.read_text(encoding="utf-8") == "1"

    storage = FilesystemRuntimeImageStorage(root)
    checkpoints = list(storage.root.glob(".published-image-*.checkpoint.json"))
    assert len(checkpoints) == 1
    checkpoint = RuntimeImagePublishedStageCheckpoint.model_validate_json(
        checkpoints[0].read_bytes(), strict=True
    )
    stage = storage.published_stage_path(checkpoint.oci_archive_sha256)
    final = storage.root / ARCHIVE_DIGEST
    assert os.path.samefile(stage, final)
    assert final.read_bytes() == ARCHIVE
    assert not (storage.root / f"{ARCHIVE_DIGEST}.receipt.json").exists()

    state: dict[str, int] = {}
    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation.subprocess.run",
        lambda command, **kwargs: _fake_skopeo_run(command, state=state, **kwargs),
    )
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=SkopeoOCIImageTransport(),
    )

    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert state == {}
    assert (storage.root / f"{ARCHIVE_DIGEST}.receipt.json").is_file()
    assert Path(receipt.archive_path).read_bytes() == ARCHIVE


def test_cancelled_owner_and_source_lock_loser_preserve_verified_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    state: dict[str, int] = {}
    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation.subprocess.run",
        lambda command, **kwargs: _fake_skopeo_run(command, state=state, **kwargs),
    )
    recipe = _recipe("recipe-image.json")
    assert isinstance(recipe.execution, RecipeImageExecution)
    reference = (
        f"{recipe.execution.image.repository}@sha256:{recipe.execution.image.digest}"
    )
    denied: list[RuntimeImageReceipt] = []

    def cancelled(receipt: RuntimeImageReceipt) -> None:
        denied.append(receipt)
        raise RuntimeImagePreparationError(
            "runtime_image.owner_cancelled",
            "current operation intent no longer authorizes publication",
        )

    with pytest.raises(RuntimeImagePreparationError) as refusal:
        prepare_runtime_image(
            recipe,
            runtime=_runtime(),
            storage=storage,
            transport=SkopeoOCIImageTransport(),
            before_publish=cancelled,
        )
    assert refusal.value.code == "runtime_image.owner_cancelled"
    assert len(denied) == 1
    assert state == {"blob_fetches": 1, "exports": 1}

    checkpoints = list(storage.root.glob(".published-image-*.checkpoint.json"))
    assert len(checkpoints) == 1
    checkpoint = RuntimeImagePublishedStageCheckpoint.model_validate_json(
        checkpoints[0].read_bytes(), strict=True
    )
    stage = storage.published_stage_path(checkpoint.oci_archive_sha256)
    assert stage.read_bytes() == ARCHIVE
    assert not (storage.root / ARCHIVE_DIGEST).exists()

    source_key = hashlib.sha256(f"{reference}\nlinux/arm64".encode()).hexdigest()
    source_lock = storage.root / "registry-layers" / f"{source_key}.lock"
    with source_lock.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeImagePreparationError) as contended:
            prepare_runtime_image(
                recipe,
                runtime=_runtime(),
                storage=storage,
                transport=SkopeoOCIImageTransport(),
            )
        assert contended.value.code == "runtime_image.transfer_contended"
        assert stage.read_bytes() == ARCHIVE
        assert checkpoints[0].is_file()
        assert state == {"blob_fetches": 1, "exports": 1}
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    receipt = prepare_runtime_image(
        recipe,
        runtime=_runtime(),
        storage=storage,
        transport=SkopeoOCIImageTransport(),
    )
    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert state == {"blob_fetches": 1, "exports": 1}
    assert stage.read_bytes() == ARCHIVE


def test_oversized_published_stage_checkpoint_fails_closed_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    recipe = _recipe("recipe-image.json")
    assert isinstance(recipe.execution, RecipeImageExecution)
    reference = (
        f"{recipe.execution.image.repository}@sha256:{recipe.execution.image.digest}"
    )
    checkpoint = storage.published_stage_checkpoint_path(
        reference,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )
    checkpoint.write_bytes(b" " * (16 * 1024))
    state: dict[str, int] = {}
    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation.subprocess.run",
        lambda command, **kwargs: _fake_skopeo_run(command, state=state, **kwargs),
    )

    with pytest.raises(RuntimeImagePreparationError) as rejected:
        prepare_runtime_image(
            recipe,
            runtime=_runtime(),
            storage=storage,
            transport=SkopeoOCIImageTransport(),
        )

    assert rejected.value.code == "runtime_image.stage_checkpoint_invalid"
    assert "16384 bytes" in rejected.value.detail
    assert "4096 bytes" in rejected.value.detail
    assert state == {}


def test_nonregular_published_stage_checkpoint_reports_type_refusal(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    recipe = _recipe("recipe-image.json")
    assert isinstance(recipe.execution, RecipeImageExecution)
    reference = (
        f"{recipe.execution.image.repository}@sha256:{recipe.execution.image.digest}"
    )
    checkpoint_path = storage.published_stage_checkpoint_path(
        reference,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )
    checkpoint_path.mkdir()

    with pytest.raises(RuntimeImagePreparationError) as rejected:
        storage.find_published_stage(
            reference,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )

    assert rejected.value.code == "runtime_image.stage_checkpoint_invalid"
    assert rejected.value.detail == (
        "published runtime image checkpoint is not a regular file"
    )


def test_fifo_published_stage_checkpoint_is_rejected_without_blocking(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    storage = FilesystemRuntimeImageStorage(root)
    recipe = _recipe("recipe-image.json")
    assert isinstance(recipe.execution, RecipeImageExecution)
    reference = (
        f"{recipe.execution.image.repository}@sha256:{recipe.execution.image.digest}"
    )
    checkpoint_path = storage.published_stage_checkpoint_path(
        reference,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )
    os.mkfifo(checkpoint_path)

    test_dir = Path(__file__).resolve().parent
    environment = os.environ.copy()
    source_paths = (
        test_dir,
        test_dir.parent / "src",
        test_dir.parent.parent / "src",
        Path(environment["VONK_RECIPE_LIBRARY_ROOT"]) / "contracts" / "src",
    )
    environment["PYTHONPATH"] = os.pathsep.join(
        [*(str(path) for path in source_paths), environment.get("PYTHONPATH", "")]
    )
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from vonk_control.runtime_image_preparation import "
        "FilesystemRuntimeImageStorage, RuntimeImagePreparationError\n"
        "try:\n"
        "    FilesystemRuntimeImageStorage(Path(sys.argv[1])).find_published_stage(\n"
        "        sys.argv[2], expected_architecture='linux/arm64',\n"
        "        expected_runtime_interface='vonk.runtime.v1'\n"
        "    )\n"
        "except RuntimeImagePreparationError as error:\n"
        "    assert error.code == 'runtime_image.stage_checkpoint_invalid'\n"
        "    assert error.detail == 'published runtime image checkpoint is not a regular file'\n"
        "else:\n"
        "    raise AssertionError('FIFO checkpoint was accepted')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), reference],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_published_checkpoint_accepts_maximum_canonical_recipe_image_reference() -> (
    None
):
    image = RecipeImage(
        repository="a" + "b" * 511,
        digest="a" * 64,
        platform="linux/arm64",
    )
    reference = f"{image.repository}@sha256:{image.digest}"
    checkpoint = RuntimeImagePublishedStageCheckpoint(
        schema_version=2,
        registry_reference=reference,
        registry_manifest_digest="sha256:" + "a" * 64,
        platform_manifest_digest=PLATFORM_IMAGE_DIGEST,
        image_digest=PLATFORM_IMAGE_DIGEST,
        local_image_config_id="sha256:" + "c" * 64,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        runtime_interface_label="v1",
        oci_archive_sha256="d" * 64,
        image_bytes=16 * 1024**4,
        runtime_adapter=None,
        runtime_adapter_sha256=None,
    )

    encoded = checkpoint.model_dump_json().encode("utf-8")
    assert len(reference) == 584
    assert len(encoded) == 1285
    assert len(encoded) <= 4 * 1024


def test_receiptless_final_with_wrong_bytes_is_not_repaired_from_size_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vonk_control import runtime_image_preparation

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    state: dict[str, int] = {}
    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation.subprocess.run",
        lambda command, **kwargs: _fake_skopeo_run(command, state=state, **kwargs),
    )
    atomic_replace = runtime_image_preparation._atomic_json_replace

    def fail_receipt(path: Path, value: dict[str, object]) -> None:
        if path.name.endswith(".receipt.json"):
            raise RuntimeImagePreparationError(
                "runtime_image.receipt_write_failed",
                "injected interruption after archive publication",
            )
        atomic_replace(path, value)

    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation._atomic_json_replace", fail_receipt
    )
    with pytest.raises(RuntimeImagePreparationError) as interrupted:
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=_runtime(),
            storage=storage,
            transport=SkopeoOCIImageTransport(),
        )
    assert interrupted.value.code == "runtime_image.receipt_write_failed"
    final = storage.root / ARCHIVE_DIGEST
    stage = storage.published_stage_path(ARCHIVE_DIGEST)
    assert os.path.samefile(final, stage)
    assert not (storage.root / f"{ARCHIVE_DIGEST}.receipt.json").exists()

    final.write_bytes(b"X" * len(ARCHIVE))
    monkeypatch.setattr(
        "vonk_control.runtime_image_preparation._atomic_json_replace", atomic_replace
    )
    with pytest.raises(RuntimeImagePreparationError) as mismatch:
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=_runtime(),
            storage=storage,
            transport=SkopeoOCIImageTransport(),
        )
    assert mismatch.value.code == "runtime_image.archive_conflict"
    assert final.read_bytes() == b"X" * len(ARCHIVE)
    assert not (storage.root / f"{ARCHIVE_DIGEST}.receipt.json").exists()
    assert state == {"blob_fetches": 1, "exports": 2}


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
                "build_id": "missing-archive",
                "image_digest": BUILT_IMAGE_DIGEST,
                "oci_layout_sha256": "1" * 64,
                "image_bytes": len(ARCHIVE),
                "architecture": "linux/arm64",
                "runtime_interface": "v1",
            },
        )


def test_build_archive_presence_is_cheap_and_requires_a_regular_exact_size_file(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")

    assert storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE)) is False
    archive = storage.root / ARCHIVE_DIGEST
    archive.write_bytes(ARCHIVE)
    assert storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE)) is True

    archive.write_bytes(ARCHIVE + b"corrupt")
    with pytest.raises(RuntimeImagePreparationError, match="length"):
        storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE))

    archive.unlink()
    archive.symlink_to(tmp_path / "outside")
    with pytest.raises(RuntimeImagePreparationError, match="regular archive"):
        storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE))


def test_find_build_matches_the_recorded_input_identity_only(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    (storage.root / ARCHIVE_DIGEST).write_bytes(ARCHIVE)
    build_input = "a" * 64
    receipt = prepare_runtime_image(
        _recipe("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        build_receipt={
            "state": "succeeded",
            "build_id": "build-1",
            "build_input_sha256": build_input,
            "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST,
            "image_bytes": len(ARCHIVE),
        },
    )
    assert receipt.build_input_sha256 == build_input

    assert (
        storage.find_build(
            build_input,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        == receipt
    )
    # A different input identity must not reuse these bytes.
    assert (
        storage.find_build(
            "b" * 64,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    with pytest.raises(RuntimeImagePreparationError, match="identity is invalid"):
        storage.find_build(
            "not-a-digest",
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )

    (storage.root / ARCHIVE_DIGEST).unlink()
    assert (
        storage.find_build(
            build_input,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )


def test_find_build_does_not_reuse_a_receipt_without_an_input_identity(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    (storage.root / ARCHIVE_DIGEST).write_bytes(ARCHIVE)
    receipt = prepare_runtime_image(
        _recipe("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        build_receipt={
            "state": "succeeded",
            "build_id": "build-legacy",
            "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST,
            "image_bytes": len(ARCHIVE),
        },
    )
    assert receipt.build_input_sha256 is None

    # A receipt that cannot prove its executable inputs is not a reuse
    # authority: the absent key must not degrade into a weaker identity match.
    assert (
        storage.find_build(
            "a" * 64,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )


def test_preparation_backfills_a_missing_build_input_identity(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    (storage.root / ARCHIVE_DIGEST).write_bytes(ARCHIVE)
    build_input = "a" * 64
    legacy = prepare_runtime_image(
        _recipe("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        build_receipt={
            "state": "succeeded",
            "build_id": "build-1",
            "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST,
            "image_bytes": len(ARCHIVE),
        },
    )
    assert legacy.build_input_sha256 is None

    repaired = prepare_runtime_image(
        _recipe("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        build_receipt={
            "state": "succeeded",
            "build_id": "build-1",
            "build_input_sha256": build_input,
            "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST,
            "image_bytes": len(ARCHIVE),
        },
    )

    assert repaired.build_input_sha256 == build_input
    assert storage.read_receipt(ARCHIVE_DIGEST).build_input_sha256 == build_input
    assert (
        storage.find_build(
            build_input,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        == repaired
    )

    with pytest.raises(RuntimeImagePreparationError, match="different build input"):
        prepare_runtime_image(
            _recipe("recipe-source-build.json"),
            runtime=_runtime(),
            storage=storage,
            transport=TinyTransport(),
            build_receipt={
                "state": "succeeded",
                "build_id": "build-1",
                "build_input_sha256": "b" * 64,
                "image_digest": BUILT_IMAGE_DIGEST,
                "oci_layout_sha256": ARCHIVE_DIGEST,
                "image_bytes": len(ARCHIVE),
            },
        )


def test_verified_lookup_treats_a_vanished_archive_as_a_miss(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
    )
    Path(receipt.archive_path).unlink()

    assert (
        storage.find_published(
            IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    assert (
        storage.find_verified(
            IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )


def test_runtime_distribution_document_is_not_a_recipe_authority(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        RuntimeImagePreparationError, match="canonical RecipeDefinition"
    ):
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
        assert prefixed_image_digest(row.registry_manifest_digest) == IMAGE_DIGEST
        assert (
            prefixed_image_digest(row.platform_manifest_digest) == PLATFORM_IMAGE_DIGEST
        )
        assert prefixed_image_digest(row.local_image_config_id) == "sha256:" + "c" * 64
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
        assert same.archive_path == row.archive_path
        # Managed storage owns the immutable receipt published when the bytes
        # were verified, so a second authorization does not rewrite it.
        assert same.recorded_at == receipt.recorded_at
    conflicting = receipt.model_copy(
        update={
            "platform_manifest_digest": BUILT_IMAGE_DIGEST,
            "image_digest": BUILT_IMAGE_DIGEST,
        }
    )
    with (
        Session(engine) as session,
        pytest.raises(RuntimeImagePreparationError, match="identity changed"),
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
    with pytest.raises(ValueError, match="not authorized"):
        resolve_persisted_runtime_image_receipt(session, **resolve_kwargs)
    persist_runtime_image_receipt(
        session,
        **persist_kwargs,
        verified_at=datetime.now(UTC),
    )
    session.commit()
    session.close()
    with Session(engine) as session:
        assert (
            resolve_persisted_runtime_image_receipt(session, **resolve_kwargs).source
            == "published"
        )
        session.query(RuntimeImageAuthorization).update(
            {"oci_archive_sha256": "e" * 64}
        )
        session.commit()
    session = Session(engine)
    with pytest.raises(ValueError, match="not authorized"):
        resolve_persisted_runtime_image_receipt(session, **resolve_kwargs)
    session.close()


def test_one_verified_archive_serves_availability_and_launch_identities(
    tmp_path: Path,
) -> None:
    """A prepared recipe records its artifact under more than one identity.

    The availability operation records the recipe-level identity it admitted,
    and the launch then records the compiled identity the Spark agent compares
    against.  Both describe the same verified bytes, so the second must be
    recorded instead of refused; refusing it left every prepared recipe
    unusable at apply time.
    """

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    recipe = _recipe("recipe-image.json")
    receipt = prepare_runtime_image(
        recipe,
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    availability_key = "a" * 64
    launch_key = "b" * 64
    now = datetime.now(UTC)
    with Session(engine) as session:
        _add_revision(session, "revision-direct", recipe)
        session.flush()
        availability_row = persist_runtime_image_receipt(
            session,
            recipe_revision_id="revision-direct",
            original_content_digest=receipt.distribution_content_sha256,
            effective_execution_key=availability_key,
            receipt=receipt,
            verified_at=now,
        )
        launch_row = persist_runtime_image_receipt(
            session,
            recipe_revision_id="revision-direct",
            original_content_digest=receipt.distribution_content_sha256,
            effective_execution_key=launch_key,
            receipt=receipt,
            verified_at=now,
        )
        session.flush()
        availability_archive = availability_row.oci_archive_sha256
        launch_archive = launch_row.oci_archive_sha256
        assert launch_row.oci_archive_sha256 == ARCHIVE_DIGEST
        session.commit()

    assert availability_archive == launch_archive == ARCHIVE_DIGEST
    assert session.query(RuntimeImageAuthorization).count() == 2
    with Session(engine) as session:
        assert (
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id="revision-direct",
                current_content_digest=receipt.distribution_content_sha256,
                effective_execution_key=launch_key,
                receipt=receipt,
            ).oci_archive_sha256
            == launch_archive
        )
        assert (
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id="revision-direct",
                current_content_digest=receipt.distribution_content_sha256,
                effective_execution_key=availability_key,
                receipt=receipt,
            ).oci_archive_sha256
            == availability_archive
        )
        assert (
            session.query(RuntimeImageAuthorization)
            .filter(
                RuntimeImageAuthorization.original_content_digest
                == receipt.distribution_content_sha256
            )
            .count()
            == 2
        )


def test_rebuilt_source_image_registers_new_receipt_without_rebinding_old_plan(
    postgres_engine: Engine,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    recipe_digest = content_sha256(recipe)
    adapter = resolve_runtime_adapter(recipe.runtime.engine, recipe.topology)
    revision_id = "revision-rebuilt-source"
    execution_key = "a" * 64
    old_build_id = "a0a6e2d9-7771-45eb-bacb-c56142a240cf"
    new_build_id = "833ac675-ec54-46b4-b490-81916d9d6b0e"
    old_receipt = RuntimeImageReceipt(
        schema_version=2,
        source="controller-build",
        distribution_publisher=recipe.identity.publisher,
        distribution_slug=recipe.identity.slug,
        distribution_content_sha256=recipe_digest,
        registry_manifest_digest=None,
        platform_manifest_digest=(
            "sha256:a511438d08c3e9761e6f91ce10561cb50dcdd8ff33b898015910c7c6d6bf87cf"
        ),
        image_digest=(
            "sha256:a511438d08c3e9761e6f91ce10561cb50dcdd8ff33b898015910c7c6d6bf87cf"
        ),
        oci_archive_sha256=(
            "92363d7363402c0c4217b2711b786e124c4d173db0301e3c95f695f36d5f1def"
        ),
        image_bytes=21_017_472_512,
        local_image_config_id="sha256:" + "7b37b97a71c439d0" + "0" * 48,
        local_image_reference=None,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        runtime_interface_label="v1",
        archive_path="/state/agent-artifacts/image-cache/old",
        recorded_at="2026-09-15T17:00:00+00:00",
        build_id=old_build_id,
        runtime_adapter=adapter.adapter_id,
        runtime_adapter_sha256=adapter.digest,
    )
    new_receipt = old_receipt.model_copy(
        update={
            "platform_manifest_digest": (
                "sha256:c27772a442473d151a312c46add350a6f35f1cc9713017f2f06a377b8ca01b5e"
            ),
            "image_digest": (
                "sha256:c27772a442473d151a312c46add350a6f35f1cc9713017f2f06a377b8ca01b5e"
            ),
            "oci_archive_sha256": (
                "69b60a20441d70237e7ac1dd375ab5fe22270f40aba94a63b4404fb2161016ff"
            ),
            "local_image_config_id": "sha256:" + "0b81177e49318128" + "0" * 48,
            "archive_path": "/state/agent-artifacts/image-cache/new",
            "recorded_at": "2026-09-15T17:17:02+00:00",
            "build_id": new_build_id,
        }
    )
    engine = postgres_engine
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            CatalogDocument(
                id="document-" + revision_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                title=recipe.metadata.title,
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            [
                AgentNode(node_id="old-builder", state="active"),
                AgentNode(node_id="new-builder", state="active"),
            ]
        )
        session.flush()
        _add_revision(session, revision_id, recipe)
        session.flush()
        session.add_all(
            [
                RecipeBuild(
                    id=old_build_id,
                    recipe_revision_id=revision_id,
                    builder_node_id="old-builder",
                    source_bundle_sha256="c" * 64,
                    build_input_sha256="d" * 64,
                    state="succeeded",
                    policy_report={},
                    plan={},
                    image_digest=old_receipt.image_digest,
                    oci_layout_sha256=old_receipt.oci_archive_sha256,
                    image_bytes=old_receipt.image_bytes,
                    created_at=now,
                    updated_at=now,
                ),
                RecipeBuild(
                    id=new_build_id,
                    recipe_revision_id=revision_id,
                    builder_node_id="new-builder",
                    source_bundle_sha256="c" * 64,
                    build_input_sha256="e" * 64,
                    state="succeeded",
                    policy_report={},
                    plan={},
                    image_digest=new_receipt.image_digest,
                    oci_layout_sha256=new_receipt.oci_archive_sha256,
                    image_bytes=new_receipt.image_bytes,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.flush()
        old_row = persist_runtime_image_receipt(
            session,
            recipe_revision_id=revision_id,
            original_content_digest=recipe_digest,
            effective_execution_key=execution_key,
            receipt=old_receipt,
            verified_at=now,
        )
        new_row = persist_runtime_image_receipt(
            session,
            recipe_revision_id=revision_id,
            original_content_digest=recipe_digest,
            effective_execution_key=execution_key,
            receipt=new_receipt,
            verified_at=now + timedelta(seconds=1),
        )
        changed_provenance = new_receipt.model_copy(
            update={
                "oci_archive_sha256": "1" * 64,
                "build_id": "11111111-1111-4111-8111-111111111111",
            }
        )
        # A changed archive under the same build is rejected by the build
        # authority before any authorization is written.
        with pytest.raises(
            RuntimeImagePreparationError,
            match="not backed by the exact recorded build result",
        ):
            persist_runtime_image_receipt(
                session,
                recipe_revision_id=revision_id,
                original_content_digest=recipe_digest,
                effective_execution_key=execution_key,
                receipt=changed_provenance,
                verified_at=now + timedelta(seconds=2),
            )
        session.commit()

        assert new_row.oci_archive_sha256 != old_row.oci_archive_sha256
        assert session.query(RuntimeImageAuthorization).count() == 2
        assert session.query(RuntimeImageAuthorization).count() == 2
        assert (
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=revision_id,
                current_content_digest=recipe_digest,
                effective_execution_key=execution_key,
                receipt=new_receipt,
            ).oci_archive_sha256
            == new_row.oci_archive_sha256
        )
        assert (
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=revision_id,
                current_content_digest=recipe_digest,
                effective_execution_key=execution_key,
                receipt=old_receipt,
            ).oci_archive_sha256
            == old_row.oci_archive_sha256
        )


def test_notes_revision_reuses_original_receipt_with_separate_authorization(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    original = _recipe("recipe-image.json")
    revised_raw = original.model_dump(mode="json")
    revised_raw["metadata"]["description"] = "Editorial notes only"
    revised = RecipeDefinition.model_validate(revised_raw)
    receipt = prepare_runtime_image(
        original, runtime=_runtime(), storage=storage, transport=TinyTransport()
    )
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
                    projected=_projection(original)
                    | {"source_bundle_sha256": "c" * 64},
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
                    projected=_projection(revised) | {"source_bundle_sha256": "c" * 64},
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
        # One authorization per revision for the same verified archive.
        assert session.query(RuntimeImageAuthorization).count() == 2
        assert (
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=new_id,
                current_content_digest=new_digest,
                effective_execution_key="a" * 64,
                receipt=receipt,
            ).distribution_content_sha256
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
        # The same verified bytes under a second execution identity used to be
        # refused as a conflict.  That is the normal shape of an apply: the
        # availability operation records the recipe-level identity it admitted,
        # and the launch records the compiled identity the Spark agent compares
        # against, so the second identity is recorded instead of refused.
        # Immutability is per identity: the same identity with different bytes
        # still fails above.
        launch_key = "b" * 64
        launch_row = persist_runtime_image_receipt(
            session,
            recipe_revision_id=new_id,
            original_content_digest=old_digest,
            effective_execution_key=launch_key,
            receipt=receipt,
            verified_at=now,
        )
        session.flush()
        assert launch_row.oci_archive_sha256 == ARCHIVE_DIGEST
        # The original revision, the successor's admitted identity, and the
        # successor's compiled launch identity all bind the same archive.
        assert session.query(RuntimeImageAuthorization).count() == 3
        assert (
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=new_id,
                current_content_digest=new_digest,
                effective_execution_key=launch_key,
                receipt=receipt,
            ).oci_archive_sha256
            == launch_row.oci_archive_sha256
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
        with pytest.raises(
            RuntimeImagePreparationError, match="authority is unavailable"
        ):
            persist_runtime_image_receipt(
                session,
                recipe_revision_id="missing-revision",
                original_content_digest=digest,
                effective_execution_key="a" * 64,
                receipt=receipt,
                verified_at=now,
            )
        persist_runtime_image_receipt(
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
        # The storage receipt has no mutable state: revocation is the SQL
        # authorization's decision, and it stays terminal.
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
        with pytest.raises(
            RuntimeImagePreparationError, match="execution or artifact identity changed"
        ):
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


@pytest.mark.parametrize("include_interface", [False, True])
def test_image_preparation_rejects_retired_runtime_interface_before_transport(
    tmp_path, include_interface
) -> None:
    runtime = _runtime()
    runtime["runtime_interface"] = runtime["interface"]
    if not include_interface:
        runtime.pop("interface")
    transport = TinyTransport()
    with pytest.raises(RuntimeImagePreparationError, match="retired runtime_interface"):
        prepare_runtime_image(
            _recipe("recipe-image.json"),
            runtime=runtime,
            storage=FilesystemRuntimeImageStorage(tmp_path / "objects"),
            transport=transport,
        )
    assert transport.calls == []


def test_native_transfer_continues_while_progress_observer_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from vonk_control.runtime_image_preparation import _run_with_progress

    observer_entered = threading.Event()
    transfer_finished = threading.Event()
    destination = tmp_path / "transfer"

    def transfer(command: list[str]) -> str:
        assert observer_entered.wait(5)
        destination.write_bytes(b"completed while observer was busy")
        transfer_finished.set()
        return ""

    reports = []

    def progress(phase: str, received: int, total: int | None) -> None:
        observer_entered.set()
        assert transfer_finished.wait(5)
        reports.append((phase, received, total))

    monkeypatch.setattr("vonk_control.runtime_image_preparation._run_text", transfer)
    _run_with_progress(
        ["skopeo", "copy"],
        lambda: destination.stat().st_size if destination.exists() else 0,
        progress,
        "download",
        None,
    )
    assert reports[-1] == ("download", len(destination.read_bytes()), None)


def test_runtime_image_storage_types_only_clean_absence_as_cache_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    digest = "a" * 64
    with pytest.raises(RuntimeImagePreparationError) as missing:
        storage.verify_existing(digest, 4)
    assert missing.value.code == "runtime_image.cache_missing"
    assert missing.value.retryable is True

    archive = storage.root / digest
    archive.symlink_to(tmp_path / "absent-target")
    with pytest.raises(RuntimeImagePreparationError) as unsafe:
        storage.verify_existing(digest, 4)
    assert unsafe.value.code == "runtime_image.archive_mismatch"
    assert unsafe.value.retryable is False

    original_lstat = Path.lstat

    def denied_lstat(path: Path) -> os.stat_result:
        if path == archive:
            raise PermissionError("injected archive stat denial")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied_lstat)
    with pytest.raises(RuntimeImagePreparationError) as denied:
        storage.verify_existing(digest, 4)
    assert denied.value.code == "runtime_image.archive_unavailable"
    assert denied.value.retryable is False


def test_controller_build_receipt_requires_its_adapter_identity() -> None:
    adapter = resolve_runtime_adapter("vllm", {"mode": "single"})
    shared = {
        "schema_version": 2,
        "distribution_publisher": "vonk",
        "distribution_slug": "cached",
        "distribution_content_sha256": "a" * 64,
        "registry_manifest_digest": None,
        "platform_manifest_digest": PLATFORM_IMAGE_DIGEST,
        "image_digest": PLATFORM_IMAGE_DIGEST,
        "oci_archive_sha256": "b" * 64,
        "image_bytes": 1,
        "local_image_config_id": "sha256:" + "c" * 64,
        "local_image_reference": None,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "runtime_interface_label": "v1",
        "archive_path": "/state/runtime-images/" + "b" * 64,
        "recorded_at": "2026-09-15T00:00:00Z",
    }
    # A receipt that records no adapter cannot prove which reviewed adaptation
    # produced the bytes, so the identity binding is unverifiable.
    with pytest.raises(ValueError, match="lacks its adapter"):
        RuntimeImageReceipt(**shared, source="controller-build", build_id="build")
    with pytest.raises(ValueError, match="incomplete"):
        RuntimeImageReceipt(
            **shared,
            source="controller-build",
            build_id="build",
            runtime_adapter=adapter.adapter_id,
        )
    accepted = RuntimeImageReceipt(
        **shared,
        source="controller-build",
        build_id="build",
        runtime_adapter=adapter.adapter_id,
        runtime_adapter_sha256=adapter.digest,
    )
    assert accepted.runtime_adapter_sha256 == adapter.digest

    with pytest.raises(ValueError, match="carries an adapter"):
        RuntimeImageReceipt(
            **{
                **shared,
                "registry_manifest_digest": "sha256:" + "d" * 64,
                "build_id": None,
            },
            source="published",
            runtime_adapter=adapter.adapter_id,
            runtime_adapter_sha256=adapter.digest,
        )


def test_an_unreadable_stored_receipt_names_the_rule_that_rejected_it() -> None:
    """A stored receipt that will not validate must say which rule rejected it.

    The reader collapsed every validation failure into one sentence, so a live
    ``install.compiled_plan_unavailable`` blocker could report only "runtime
    image receipt identity is unavailable or malformed" and the failing rule
    stayed invisible on every operator surface.
    """

    malformed = {
        "schema_version": 2,
        "source": "controller-build",
        "build_id": "build",
        "distribution_publisher": "vonk",
        "distribution_slug": "cached",
        "distribution_content_sha256": "a" * 64,
        "registry_manifest_digest": None,
        "platform_manifest_digest": PLATFORM_IMAGE_DIGEST,
        "image_digest": PLATFORM_IMAGE_DIGEST,
        "oci_archive_sha256": "b" * 64,
        "image_bytes": 1,
        "local_image_config_id": "sha256:" + "c" * 64,
        "local_image_reference": None,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "runtime_interface_label": "v1",
        "archive_path": "/state/runtime-images/" + "b" * 64,
        "recorded_at": "2026-09-15T00:00:00Z",
        # A Controller build must carry the adapter identity that produced the
        # bytes; this receipt omits it.
    }

    with pytest.raises(RuntimeImagePreparationError) as raised:
        _parse_runtime_image_receipt(malformed)

    message = str(raised.value)
    assert "runtime image receipt identity" in message
    assert "lacks its adapter" in message, message


def test_unreadable_receipt_is_skipped_by_scan_but_refused_by_exact_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One archive's unusable receipt must not poison a scan over all receipts.

    The scan spans unrelated archives and recipes, so a file the current
    contract rejects is skipped with a bounded warning naming its digest.  The
    exact-identity read for that same digest still refuses.
    """

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    published = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
    )
    legacy_archive = b"legacy controller build archive"
    legacy_digest = hashlib.sha256(legacy_archive).hexdigest()
    (storage.root / legacy_digest).write_bytes(legacy_archive)
    (storage.root / f"{legacy_digest}.receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source": "controller-build",
                "distribution_publisher": "vonk",
                "distribution_slug": "cached",
                "distribution_content_sha256": "a" * 64,
                "registry_manifest_digest": None,
                "platform_manifest_digest": BUILT_IMAGE_DIGEST,
                "image_digest": BUILT_IMAGE_DIGEST,
                "oci_archive_sha256": legacy_digest,
                "image_bytes": len(legacy_archive),
                "local_image_config_id": "sha256:" + "c" * 64,
                "local_image_reference": None,
                "architecture": "linux-arm64",
                "runtime_interface": "vonk.runtime.v1",
                "runtime_interface_label": "v1",
                "archive_path": str(storage.root / legacy_digest),
                "recorded_at": "2026-09-15T00:00:00Z",
                "build_id": "legacy-build",
                "build_input_sha256": "b" * 64,
                # No runtime_adapter / runtime_adapter_sha256: the file a
                # Controller wrote before the adapter identity existed.
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="vonk_control.runtime_image_preparation"):
        found = storage.find_published(
            IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        assert (
            storage.find_build(
                "b" * 64,
                expected_architecture="linux-arm64",
                expected_runtime_interface="vonk.runtime.v1",
            )
            is None
        )
    assert found == published
    warnings = [record.getMessage() for record in caplog.records]
    assert any(legacy_digest in message for message in warnings), warnings
    assert any("lacks its adapter" in message for message in warnings), warnings

    with pytest.raises(RuntimeImagePreparationError) as raised:
        storage.read_receipt(legacy_digest)
    assert raised.value.code == "runtime_image.receipt_unavailable"
    assert "lacks its adapter" in str(raised.value)


def test_parseable_receipt_with_a_different_identity_stays_a_conflict(
    tmp_path: Path,
) -> None:
    """Stale metadata is replaceable; a parsed, disagreeing identity is not.

    The replacement rule applies only to a document the current contract
    cannot parse.  A receipt that parses and names a different immutable
    identity for the same content-addressed archive remains an explicit
    conflict.
    """

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    adapter = resolve_runtime_adapter("vllm", {"mode": "single"})
    archive = b"content addressed controller build archive"
    digest = hashlib.sha256(archive).hexdigest()
    existing = RuntimeImageReceipt(
        schema_version=2,
        source="controller-build",
        distribution_publisher="vonk",
        distribution_slug="cached",
        distribution_content_sha256="a" * 64,
        registry_manifest_digest=None,
        platform_manifest_digest=BUILT_IMAGE_DIGEST,
        image_digest=BUILT_IMAGE_DIGEST,
        oci_archive_sha256=digest,
        image_bytes=len(archive),
        local_image_config_id="sha256:" + "c" * 64,
        local_image_reference=None,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        runtime_interface_label="v1",
        archive_path=str(storage.root / digest),
        recorded_at="2026-09-15T00:00:00Z",
        build_id="build-one",
        build_input_sha256="b" * 64,
        runtime_adapter=adapter.adapter_id,
        runtime_adapter_sha256=adapter.digest,
    )
    first = storage.prepare_path()
    first.write_bytes(archive)
    storage.commit(first, receipt=existing)

    disagreeing = existing.model_copy(update={"build_id": "build-two"})
    second = storage.prepare_path()
    second.write_bytes(archive)
    with pytest.raises(RuntimeImagePreparationError) as raised:
        storage.commit(second, receipt=disagreeing)

    assert raised.value.code == "runtime_image.archive_conflict"
    assert storage.read_receipt(digest) == existing
