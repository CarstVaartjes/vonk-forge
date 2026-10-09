from __future__ import annotations

import fcntl
import hashlib
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from vonk_control.catalog_entities import build_policy_projection
from vonk_control.execution_plan_service import _runtime_image_receipt
from vonk_control.models import (
    Base,
    CatalogDocumentRevision,
    RecipeBuild,
)
from vonk_control.revision_images import revision_images
from vonk_control.runtime_adapters import resolve_runtime_adapter
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    OciLayoutImageTransport,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    RuntimeImageReceipt,
    prepare_runtime_image,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .runtime_image_fixtures import place_test_image, remove_test_image

BUILT_IMAGE_DIGEST = "sha256:" + "f" * 64
ARCHIVE = b"tiny verified OCI archive fixture"
ARCHIVE_DIGEST = hashlib.sha256(ARCHIVE).hexdigest()
BUILD_ID = "5b0a8f6e-3c1d-4e2a-9b7c-1d2e3f4a5b6c"


def _recipe(name: str) -> RecipeDefinition:
    raw = json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text()
    )
    return RecipeDefinition.model_validate(raw)


def _document(name: str) -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text()
    )


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
    value.update(build_policy_projection(recipe))
    return value


def _add_build(
    session: Session,
    revision_id: str,
    *,
    build_id: str = BUILD_ID,
    builder_node_id: str = "builder",
) -> None:
    session.add(
        RecipeBuild(
            id=build_id,
            recipe_revision_id=revision_id,
            builder_node_id=builder_node_id,
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


def _add_revision(
    session: Session,
    revision_id: str,
    recipe: RecipeDefinition,
    *,
    number: int = 1,
    state: str = "active",
) -> None:
    projected = _projection(recipe)
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
            content_digest=document_sha256(recipe.model_dump(mode="json")),
            artifact_key="b" * 64,
            execution_key="a" * 64,
            projected=projected,
            created_by="test",
            created_at=datetime.now(UTC),
        )
    )


class TinyTransport:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def inspect_archive(
        self,
        archive: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str,
        expected_archive_bytes: int,
    ) -> PulledImageEvidence:
        self.calls.append(archive)
        assert archive.name == expected_archive_sha256
        return PulledImageEvidence(
            manifest_digest=BUILT_IMAGE_DIGEST,
            config_id="sha256:" + "d" * 64,
            local_reference="oci-layout:" + archive.name,
            architecture=expected_architecture,
            runtime_interface=expected_runtime_interface,
            archive_sha256=expected_archive_sha256,
            archive_bytes=expected_archive_bytes,
        )


def _build_receipt(build_id: str = BUILD_ID) -> dict[str, object]:
    return {
        "state": "succeeded",
        "build_id": build_id,
        "image_digest": BUILT_IMAGE_DIGEST,
        "oci_layout_sha256": ARCHIVE_DIGEST,
        "image_bytes": len(ARCHIVE),
    }


def _prepare(
    *,
    storage: FilesystemRuntimeImageStorage,
    transport: object | None = None,
    recipe: RecipeDefinition | None = None,
    runtime: object | None = None,
    **kwargs: object,
) -> RuntimeImageReceipt:
    """Prepare the stored build archive the way a finished build leaves it."""

    if storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE)) is False:
        place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    return prepare_runtime_image(
        (recipe or _recipe("recipe-source-build.json")).model_dump(mode="json"),
        runtime=runtime or _runtime(),
        storage=storage,
        transport=transport or TinyTransport(),  # type: ignore[arg-type]
        build_receipt=_build_receipt(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_build_archive_receipt_is_verified_and_immediately_readable(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()
    written = []

    def writer(value):
        assert Path(value.archive_path).is_file()
        assert storage.read_receipt(value.oci_archive_sha256) == value
        written.append(value)

    receipt = _prepare(storage=storage, transport=transport, receipt_writer=writer)

    assert receipt.build_id == BUILD_ID
    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert receipt.image_digest == BUILT_IMAGE_DIGEST
    assert receipt.local_image_config_id == "sha256:" + "d" * 64
    assert receipt.runtime_interface == "vonk.runtime.v1"
    assert receipt.runtime_interface_label == "v1"
    assert storage.root == tmp_path / "objects" / "image-cache"
    assert Path(receipt.archive_path) == storage.existing_archive(
        ARCHIVE_DIGEST, len(ARCHIVE)
    )
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt
    assert written == [receipt]

    resolved = storage.find_verified(
        BUILT_IMAGE_DIGEST,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )
    assert resolved == receipt
    assert len(transport.calls) == 1

    reused = _prepare(storage=storage, transport=transport, receipt_writer=writer)
    assert reused == receipt
    assert written == [receipt, receipt]
    assert len(transport.calls) == 1


def test_unparseable_receipt_is_replaced_from_verified_bytes(
    tmp_path: Path,
) -> None:
    """A receipt the contract cannot parse is stale metadata.

    The archive is content-addressed and the transport re-verifies its bytes,
    so the receipt beside it is replaced from that verified evidence instead of
    pinning the recipe forever.
    """

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()
    receipt = _prepare(
        storage=storage,
        transport=transport,
    )
    receipt_path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    value["image_digest"] = "not a digest"
    receipt_path.write_text(json.dumps(value), encoding="utf-8")
    repaired = _prepare(
        storage=storage,
        transport=transport,
    )
    assert storage.read_receipt(receipt.oci_archive_sha256) == repaired
    assert repaired.image_digest == BUILT_IMAGE_DIGEST


def test_non_schema_two_receipt_is_discarded_by_scans_and_re_derived(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = _prepare(
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
        storage.find_verified(
            BUILT_IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    # The scan discarded our own unusable receipt; preparation re-derives it.
    assert not receipt_path.exists()
    restored = _prepare(
        storage=storage,
        transport=TinyTransport(),
    )
    assert storage.read_receipt(receipt.oci_archive_sha256) == restored


@pytest.mark.parametrize("stale", ["retired-fields", "missing-adapter"])
def test_stale_receipt_that_cannot_be_discarded_is_reported_once_with_its_cause(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stale: str
) -> None:
    """Both old receipt shapes are ours; a failed discard names its cause once."""

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = _prepare(storage=storage, transport=TinyTransport())
    receipt_path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    if stale == "retired-fields":
        value |= {
            "platform_manifest_digest": receipt.image_digest,
            "local_image_reference": None,
        }
    else:
        del value["runtime_adapter"], value["runtime_adapter_sha256"]
    receipt_path.write_text(json.dumps(value), encoding="utf-8")

    def denied(_archive: str):
        raise RuntimeImagePreparationError(
            "runtime_image.lock_unavailable",
            "managed image publication lock file is unavailable",
        ) from PermissionError(13, "Permission denied", "lock")

    original_lock = storage.publication_lock
    storage.publication_lock = denied  # type: ignore[method-assign]

    def scan() -> None:
        assert (
            storage.find_verified(
                BUILT_IMAGE_DIGEST,
                expected_architecture="linux/arm64",
                expected_runtime_interface="vonk.runtime.v1",
            )
            is None
        )

    with caplog.at_level("WARNING"):
        scan()
        scan()
    reports = [
        record.getMessage()
        for record in caplog.records
        if receipt.oci_archive_sha256 in record.getMessage()
    ]
    assert len(reports) == 1, reports
    assert reports[0].startswith("could not discard stale runtime image receipt")
    assert "runtime_image.lock_unavailable: [Errno 13] Permission denied" in reports[0]
    assert receipt_path.exists()

    # Once the lock is usable again the next scan removes it.
    storage.publication_lock = original_lock  # type: ignore[method-assign]
    scan()
    assert not receipt_path.exists()


def test_receipt_with_retired_schema_two_fields_is_own_stale_and_replaced(
    tmp_path: Path,
) -> None:
    """Schema 2 dropped these fields without a version bump; they are ours."""

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = _prepare(storage=storage, transport=TinyTransport())
    receipt_path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    value = json.loads(receipt_path.read_text(encoding="utf-8")) | {
        "platform_manifest_digest": receipt.image_digest,
        "local_image_reference": None,
    }
    receipt_path.write_text(json.dumps(value), encoding="utf-8")
    assert (
        storage.find_verified(
            BUILT_IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    # A scan discards it once instead of warning on every lookup.
    assert not receipt_path.exists()
    receipt_path.write_text(json.dumps(value), encoding="utf-8")
    # Preparation replaces it in place instead of refusing a "newer" receipt.
    restored = _prepare(storage=storage, transport=TinyTransport())
    assert storage.read_receipt(receipt.oci_archive_sha256) == restored


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 3},
        {"field_from_a_newer_contract": "value"},
    ],
    ids=["newer-schema-version", "unknown-field"],
)
def test_unknown_receipt_fields_cannot_veto_verified_republication(
    tmp_path: Path, change: dict[str, object]
) -> None:
    """Damaged derived metadata cannot veto current verified preparation."""

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = _prepare(
        storage=storage,
        transport=TinyTransport(),
    )
    receipt_path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    value = json.loads(receipt_path.read_text(encoding="utf-8")) | change
    newer = json.dumps(value)
    receipt_path.write_text(newer, encoding="utf-8")

    assert (
        storage.find_verified(
            BUILT_IMAGE_DIGEST,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    restored = _prepare(storage=storage, transport=TinyTransport())
    assert storage.read_receipt(receipt.oci_archive_sha256) == restored
    assert restored.image_digest == receipt.image_digest
    assert _prepare(storage=storage, transport=TinyTransport()) == restored


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("runtime_interface_label"),
        lambda value: value.update(unexpected_field="rejected"),
        lambda value: value.update(image_bytes=True),
    ],
    ids=["missing-interface-label", "unknown-field", "boolean-image-bytes"],
)
def test_current_receipt_parser_rejects_noncanonical_shape(
    tmp_path: Path, mutation
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = _prepare(
        storage=storage,
        transport=TinyTransport(),
    )
    path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutation(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    assert (
        storage.find_verified(
            receipt.image_digest,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    restored = _prepare(storage=storage, transport=TinyTransport())
    assert restored.image_digest == receipt.image_digest
    assert storage.read_receipt(receipt.oci_archive_sha256) == restored
    assert _prepare(storage=storage, transport=TinyTransport()) == restored


def test_current_producer_parser_and_compiled_plan_consumer_preserve_archive_identity(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    produced = _prepare(storage=storage)
    parsed = storage.read_receipt(produced.oci_archive_sha256)
    compiled = _runtime_image_receipt(parsed)
    assert parsed.oci_archive_sha256 == produced.oci_archive_sha256
    assert compiled.oci_layout_sha256 == produced.oci_archive_sha256
    assert compiled.runtime_interface_label == produced.runtime_interface_label


@pytest.mark.parametrize("field", RuntimeImageReceipt.model_json_schema()["required"])
def test_receipt_reader_requires_every_declared_field(
    tmp_path: Path, field: str
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = _prepare(
        storage=storage,
        transport=TinyTransport(),
    )
    path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document[field]
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RuntimeImagePreparationError):
        storage.read_receipt(receipt.oci_archive_sha256)


def test_layout_transport_reads_platform_and_interface_from_the_stored_config(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    manifest = storage.existing_archive(ARCHIVE_DIGEST, len(ARCHIVE))

    evidence = OciLayoutImageTransport().inspect_archive(
        manifest,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
        expected_archive_sha256=ARCHIVE_DIGEST,
        expected_archive_bytes=len(ARCHIVE),
    )

    assert evidence.manifest_digest == f"sha256:{ARCHIVE_DIGEST}"
    assert evidence.architecture == "linux/arm64"
    assert evidence.runtime_interface == "v1"
    assert evidence.archive_sha256 == ARCHIVE_DIGEST
    assert evidence.archive_bytes == len(ARCHIVE)


@pytest.mark.parametrize(
    ("config", "code"),
    [
        (
            {
                "architecture": "arm64",
                "os": "linux",
                "config": {"Labels": {}},
            },
            "runtime_image.interface_missing",
        ),
        (
            {
                "architecture": "amd64",
                "os": "linux",
                "config": {"Labels": {"ai.vonkforge.runtime-interface": "v1"}},
            },
            "runtime_image.architecture_mismatch",
        ),
    ],
    ids=["unlabeled", "wrong-architecture"],
)
def test_layout_transport_rejects_an_image_that_is_not_the_runtime_platform(
    tmp_path: Path, config: dict[str, object], code: str
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE), config=config)

    with pytest.raises(RuntimeImagePreparationError) as raised:
        OciLayoutImageTransport().inspect_archive(
            storage.existing_archive(ARCHIVE_DIGEST, len(ARCHIVE)),
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
            expected_archive_sha256=ARCHIVE_DIGEST,
            expected_archive_bytes=len(ARCHIVE),
        )
    assert raised.value.code == code


def test_source_build_uses_same_normalized_receipt_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    receipt = prepare_runtime_image(
        _document("recipe-source-build.json"),
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

    assert receipt.build_id == "build-7"
    assert receipt.image_digest == BUILT_IMAGE_DIGEST
    assert receipt.oci_archive_sha256 == ARCHIVE_DIGEST
    assert receipt.local_image_config_id == "sha256:" + "d" * 64
    assert receipt.runtime_interface == "vonk.runtime.v1"
    assert receipt.runtime_interface_label == "v1"
    assert storage.read_receipt(ARCHIVE_DIGEST) == receipt
    assert Path(receipt.archive_path).is_file()

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
        session.commit()
        # The image the recipe can run is derived from its build row; no
        # per-revision record of the receipt exists.
        (image,) = revision_images(session, ["revision-source"])["revision-source"]
        assert image.build_id == "build-7"
        assert image.image_digest == BUILT_IMAGE_DIGEST
        assert image.archive_sha256 == ARCHIVE_DIGEST


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
    receipt = _prepare(
        storage=storage,
        transport=TinyTransport(),
        before_publish=callback_while_locked,
    )

    assert len(callbacks) == 1
    assert callbacks[0].oci_archive_sha256 == receipt.oci_archive_sha256
    assert callbacks[0].image_digest == receipt.image_digest
    assert callbacks[0].build_id == receipt.build_id
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


def test_build_receipt_requires_the_exact_stored_archive(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))

    with pytest.raises(RuntimeImagePreparationError, match="not present"):
        prepare_runtime_image(
            _document("recipe-source-build.json"),
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


def test_build_archive_presence_checks_the_whole_image_by_name_and_size(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")

    assert storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE)) is False
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    assert storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE)) is True

    # A recorded size that is not the stored size is no proof the recorded build is
    # there: the answer is "not available" (the caller builds again), not a refusal.
    assert storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE) + 1) is False

    # A layer blob that vanished makes the whole image absent.
    layers = [
        blob
        for blob in (storage.layout.root / "blobs" / "sha256").iterdir()
        if blob.stat().st_size == len(ARCHIVE) and blob.name != ARCHIVE_DIGEST
    ]
    assert len(layers) == 1
    layers[0].unlink()
    assert storage.build_archive_available(ARCHIVE_DIGEST, len(ARCHIVE)) is False


def test_find_build_matches_the_recorded_input_identity_only(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    build_input = "a" * 64
    receipt = prepare_runtime_image(
        _document("recipe-source-build.json"),
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

    remove_test_image(storage, ARCHIVE_DIGEST)
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
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    receipt = prepare_runtime_image(
        _document("recipe-source-build.json"),
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
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    build_input = "a" * 64
    legacy = prepare_runtime_image(
        _document("recipe-source-build.json"),
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
        _document("recipe-source-build.json"),
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

    # Another build of the same verified bytes is the same image: it succeeds,
    # answers with its own provenance and leaves the stored receipt alone.
    sibling = prepare_runtime_image(
        _document("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        build_receipt={
            "state": "succeeded",
            "build_id": "build-2",
            "build_input_sha256": "b" * 64,
            "image_digest": BUILT_IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_DIGEST,
            "image_bytes": len(ARCHIVE),
        },
    )
    assert (sibling.build_id, sibling.build_input_sha256) == ("build-2", "b" * 64)
    assert sibling.oci_archive_sha256 == repaired.oci_archive_sha256
    assert storage.read_receipt(ARCHIVE_DIGEST).build_input_sha256 == build_input


def test_verified_lookup_treats_a_vanished_archive_as_a_miss(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = _prepare(
        storage=storage,
        transport=TinyTransport(),
    )
    remove_test_image(storage, receipt.oci_archive_sha256)

    assert (
        storage.find_verified(
            BUILT_IMAGE_DIGEST,
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
            build_receipt=_build_receipt(),
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
        _prepare(storage=storage, transport=transport, receipt_writer=fail_once)
    assert len(transport.calls) == 1
    retry = _prepare(
        storage=storage,
        transport=transport,
        receipt_writer=fail_once,
    )
    assert retry.build_id == BUILD_ID
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
        _prepare(
            storage=FilesystemRuntimeImageStorage(tmp_path / "objects"),
            transport=transport,
            runtime=runtime,
        )
    assert transport.calls == []


def test_controller_build_receipt_requires_its_adapter_identity() -> None:
    adapter = resolve_runtime_adapter("vllm", {"node_count": 1})
    shared = {
        "schema_version": 2,
        "distribution_publisher": "vonk",
        "distribution_slug": "cached",
        "distribution_content_sha256": "a" * 64,
        "image_digest": BUILT_IMAGE_DIGEST,
        "oci_archive_sha256": "b" * 64,
        "image_bytes": 1,
        "local_image_config_id": "sha256:" + "c" * 64,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "runtime_interface_label": "v1",
        "archive_path": "/state/runtime-images/" + "b" * 64,
        "recorded_at": "2026-09-15T00:00:00Z",
        "build_id": "build",
    }
    # A receipt that records no adapter cannot prove which reviewed adaptation
    # produced the bytes, so the identity binding is unverifiable.
    with pytest.raises(ValueError, match="runtime_adapter"):
        RuntimeImageReceipt(**shared, runtime_adapter=adapter.adapter_id)
    accepted = RuntimeImageReceipt(
        **shared,
        runtime_adapter=adapter.adapter_id,
        runtime_adapter_sha256=adapter.digest,
    )
    assert accepted.runtime_adapter_sha256 == adapter.digest


def test_damaged_stored_receipt_is_reconstructed_then_reused(tmp_path: Path) -> None:
    """A local parser failure must repair from verified bytes without transfer."""
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()
    receipt = _prepare(storage=storage, transport=transport)
    path = storage.root / f"{receipt.oci_archive_sha256}.receipt.json"
    path.write_text("{}")
    restored = _prepare(storage=storage, transport=transport)
    assert restored.image_digest == receipt.image_digest
    assert storage.read_receipt(receipt.oci_archive_sha256) == restored
    assert _prepare(storage=storage, transport=transport) == restored
    assert len(transport.calls) == 2  # original inspection and reconstruction


def test_stale_receipt_is_discarded_once_by_scan(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One archive's stale receipt must not poison, or spam, every scan."""

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    published = _prepare(
        storage=storage,
        transport=TinyTransport(),
    )
    legacy_archive = b"legacy controller build archive"
    legacy_digest = hashlib.sha256(legacy_archive).hexdigest()
    place_test_image(storage, legacy_digest, len(legacy_archive))
    (storage.root / f"{legacy_digest}.receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "distribution_publisher": "vonk",
                "distribution_slug": "cached",
                "distribution_content_sha256": "a" * 64,
                "image_digest": "sha256:" + "9" * 64,
                "oci_archive_sha256": legacy_digest,
                "image_bytes": len(legacy_archive),
                "local_image_config_id": "sha256:" + "c" * 64,
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
        found = storage.find_verified(
            BUILT_IMAGE_DIGEST,
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
    # The stale receipt is removed once, so later scans stay quiet.
    assert not (storage.root / f"{legacy_digest}.receipt.json").exists()
    caplog.clear()
    with caplog.at_level("WARNING", logger="vonk_control.runtime_image_preparation"):
        storage.find_build(
            "b" * 64,
            expected_architecture="linux-arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
    assert not caplog.records

    with pytest.raises(RuntimeImagePreparationError) as raised:
        storage.read_receipt(legacy_digest)
    assert raised.value.code == "runtime_image.receipt_unavailable"


def test_receipt_content_is_the_identity_and_provenance_never_conflicts(
    tmp_path: Path,
) -> None:

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    adapter = resolve_runtime_adapter("vllm", {"node_count": 1})
    archive = b"content addressed controller build archive"
    digest = hashlib.sha256(archive).hexdigest()
    existing = RuntimeImageReceipt(
        schema_version=2,
        distribution_publisher="vonk",
        distribution_slug="cached",
        distribution_content_sha256="a" * 64,
        image_digest=BUILT_IMAGE_DIGEST,
        oci_archive_sha256=digest,
        image_bytes=len(archive),
        local_image_config_id="sha256:" + "c" * 64,
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
    place_test_image(storage, digest, len(archive))
    manifest = storage.existing_archive(digest, len(archive))
    storage.commit(manifest, receipt=existing)

    # The same bytes asked for by another build are the same image: the
    # answer carries the asking build, the stored receipt keeps its content.
    sibling = existing.model_copy(update={"build_id": "build-two"})
    assert storage.commit(manifest, receipt=sibling).build_id == "build-two"
    assert storage.read_receipt(digest).build_id == existing.build_id
    assert storage.read_receipt(digest).build_input_sha256 == "b" * 64

    # A receipt whose content disagrees with what was just observed in the
    # stored bytes is stale metadata: the observation replaces it.
    observed = existing.model_copy(
        update={"local_image_config_id": "sha256:" + "9" * 64}
    )
    storage.commit(manifest, receipt=observed)
    assert storage.read_receipt(digest).local_image_config_id == ("sha256:" + "9" * 64)


@pytest.mark.parametrize("lookup", ["image", "build", "exact-build"])
def test_temporary_unreadable_receipt_does_not_block_verified_reuse(
    tmp_path: Path, monkeypatch, lookup: str
) -> None:
    """A transient sibling I/O fault must not rebuild or wedge an eligible image."""
    import errno

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()
    published = _prepare(storage=storage, transport=transport)
    published = published.model_copy(update={"build_input_sha256": "d" * 64})
    (storage.root / f"{published.oci_archive_sha256}.receipt.json").write_text(
        published.model_dump_json(), encoding="utf-8"
    )
    other_digest = "0" * 64
    place_test_image(storage, other_digest, published.image_bytes)
    other = published.model_copy(
        update={
            "oci_archive_sha256": other_digest,
            "image_digest": "sha256:" + "9" * 64,
            "archive_path": str(storage.root / other_digest),
            "build_input_sha256": "e" * 64,
        }
    )
    receipt_path = storage.root / f"{other_digest}.receipt.json"
    receipt_path.write_text(other.model_dump_json(), encoding="utf-8")
    original = Path.read_text
    faulty = [True]

    def read(path, *args, **kwargs):
        if path == receipt_path and faulty[0]:
            raise OSError(errno.EIO, "temporary receipt I/O failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    arguments = {
        "expected_architecture": "linux/arm64",
        "expected_runtime_interface": "vonk.runtime.v1",
    }
    assert published.build_input_sha256 is not None
    found = (
        storage.find_verified(published.image_digest, **arguments)
        if lookup == "image"
        else storage.find_build(
            published.build_input_sha256,
            expected_archive_sha256=(
                published.oci_archive_sha256 if lookup == "exact-build" else None
            ),
            **arguments,
        )
    )
    assert found == published
    assert len(transport.calls) == 1

    def find_other():
        assert other.build_input_sha256 is not None
        return (
            storage.find_verified(other.image_digest, **arguments)
            if lookup == "image"
            else storage.find_build(
                other.build_input_sha256,
                expected_archive_sha256=(
                    other.oci_archive_sha256 if lookup == "exact-build" else None
                ),
                **arguments,
            )
        )

    assert find_other() is None
    assert receipt_path.exists()
    faulty[0] = False
    assert find_other() == other
    assert len(transport.calls) == 1


def test_local_receipt_permission_failure_is_a_miss_and_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    """Local I/O cannot become authority or admit unconfirmed cached content."""
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    transport = TinyTransport()
    published = _prepare(storage=storage, transport=transport)
    receipt_path = storage.root / f"{published.oci_archive_sha256}.receipt.json"
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == receipt_path:
            raise PermissionError(13, "receipt access denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert (
        storage.find_verified(
            published.image_digest,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )
    assert receipt_path.exists()
    monkeypatch.setattr(Path, "read_text", original)
    assert _prepare(storage=storage, transport=transport) == published
    assert len(transport.calls) == 1
