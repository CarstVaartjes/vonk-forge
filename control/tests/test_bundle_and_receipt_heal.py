"""Damaged stored build data is rebuilt from its evidence, never a recipe fault.

Source and receipts are verified at ingress. What is stored afterwards can be
damaged, and a damaged stored copy is repaired from the evidence the Controller
kept (the recipe package closure, the image cache's receipt) and verified again.
Only a source that is invalid right after a fresh verification is the recipe's.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker
from vonk_control.models import (
    Base,
    RecipeSourceBundle,
    SourceBundleArchive,
)
from vonk_control.recipe_builds import (
    RecipeBuildService,
    RecipeBuildUnknown,
    rederive_source_bundle_from_closure,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationUnknown,
    prepare_runtime_image,
)
from vonk_control.source_bundles import (
    DatabaseSourceBundleStore,
    GeneratedSourceBundle,
    SourceBundleError,
    SourceBundleStore,
    generate_source_bundle,
)

from .runtime_image_fixtures import place_test_image
from .test_recipe_builds import setup
from .test_runtime_image_preparation import (
    ARCHIVE,
    ARCHIVE_DIGEST,
    BUILT_IMAGE_DIGEST,
    TinyTransport,
    _build_receipt,
    _document,
    _runtime,
)

# ------------------------------------------------------------------ stores


def _bundle() -> GeneratedSourceBundle:
    return generate_source_bundle({"Dockerfile": b"FROM scratch\n", "a.txt": b"1"})


def test_a_damaged_stored_file_is_replaced_by_the_verified_archive(
    tmp_path: Path,
) -> None:
    """Catches a damaged stored copy reported as a collision and never repaired."""

    bundle = _bundle()
    store = SourceBundleStore(tmp_path)
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    stored.path.write_bytes(b"not a tar any more")
    with pytest.raises(SourceBundleError):
        store.get(bundle.sha256)

    store.put(bundle.sha256, io.BytesIO(bundle.archive))

    assert store.get(bundle.sha256).files["Dockerfile"] == b"FROM scratch\n"


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_database_rows_are_rewritten_from_the_verified_archive(
    postgres_engine,
) -> None:
    """Catches damaged archive or metadata rows blocking their own re-ingress."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    store = DatabaseSourceBundleStore(sessions)
    bundle = _bundle()
    store.put(bundle.sha256, io.BytesIO(bundle.archive))
    with sessions.begin() as session:
        archive_row = session.get(SourceBundleArchive, bundle.sha256)
        assert archive_row is not None
        archive_row.archive = b"damaged"
        metadata = session.get(RecipeSourceBundle, bundle.sha256)
        assert metadata is not None
        metadata.manifest = {"damaged": True}
    with pytest.raises(SourceBundleError):
        store.get(bundle.sha256)

    store.put(bundle.sha256, io.BytesIO(bundle.archive))

    assert store.get(bundle.sha256).files["Dockerfile"] == b"FROM scratch\n"


# ------------------------------------------------------------ build source


def test_a_changed_bundle_waits_for_background_reingress(tmp_path: Path) -> None:
    """Catches a reader repairing source inline instead of observing a cache miss."""
    sessions, bundles, _now, _node, revision = setup(tmp_path)
    sha = str(revision.projected["source_bundle_sha256"])
    archive = bundles.get(sha).archive
    stored = bundles.put(sha, io.BytesIO(archive))
    stored.path.write_bytes(b"damaged")
    service = RecipeBuildService(
        sessions, bundles=bundles, source_rederiver=lambda *_args: archive
    )
    with pytest.raises(RecipeBuildUnknown):
        service.resolve(revision.id)
    assert service.reconcile_source_bundles()
    assert service.resolve(revision.id) is not None


def test_a_bundle_that_cannot_be_derived_again_waits_instead_of_failing(
    tmp_path: Path,
) -> None:
    """Catches a missing closure permanently poisoning a later fresh source ingress."""
    sessions, bundles, _now, _node, revision = setup(tmp_path)
    sha = str(revision.projected["source_bundle_sha256"])
    archive = bundles.get(sha).archive
    bundles.put(sha, io.BytesIO(archive)).path.unlink()
    service = RecipeBuildService(
        sessions, bundles=bundles, source_rederiver=lambda *_: None
    )
    with pytest.raises(RecipeBuildUnknown):
        service.resolve(revision.id)
    service._source_rederiver = lambda *_: archive
    assert service.reconcile_source_bundles()
    assert service.resolve(revision.id) is not None


def test_incomplete_local_evidence_after_reingress_remains_unknown(
    tmp_path: Path,
) -> None:
    """Catches damage after repair silently passing, or poisoning a restarted worker."""
    sessions, bundles, _now, _node, revision = setup(tmp_path)
    sha = str(revision.projected["source_bundle_sha256"])
    archive = bundles.get(sha).archive
    stored = bundles.put(sha, io.BytesIO(archive))
    service = RecipeBuildService(
        sessions, bundles=bundles, source_rederiver=lambda *_: archive
    )
    for _ in range(2):
        stored.path.write_bytes(b"damage after reingress")
        with pytest.raises(RecipeBuildUnknown):
            service.resolve(revision.id)
        service = RecipeBuildService(
            sessions, bundles=bundles, source_rederiver=lambda *_: archive
        )
        assert service.reconcile_source_bundles()
        assert service.resolve(revision.id) is not None


def test_the_closure_rederiver_returns_only_a_digest_matching_archive(
    tmp_path: Path,
) -> None:
    """Catches a re-derivation that is trusted without matching the digest."""

    files = {"recipes/x/Dockerfile": b"FROM scratch\n", "recipes/x/a.txt": b"1"}
    expected = generate_source_bundle(
        {"Dockerfile": files["recipes/x/Dockerfile"], "a.txt": b"1"}
    )
    closure = tmp_path / "closure"
    for name, data in files.items():
        (closure / name).parent.mkdir(parents=True, exist_ok=True)
        (closure / name).write_bytes(data)
    package_sha = "e" * 64
    (closure / ".complete").write_text(package_sha, encoding="ascii")

    class Handle:
        closure_path = str(closure)
        package_sha256 = package_sha

    class Projected:
        package_handle = Handle()

    archive = rederive_source_bundle_from_closure(
        Projected(),  # type: ignore[arg-type]
        "recipes/x",
        expected.sha256,
    )
    assert archive == expected.archive
    assert (
        rederive_source_bundle_from_closure(
            Projected(),  # type: ignore[arg-type]
            "recipes/x",
            "0" * 64,
        )
        is None
    )
    (closure / ".complete").write_text("f" * 64, encoding="ascii")
    assert (
        rederive_source_bundle_from_closure(
            Projected(),  # type: ignore[arg-type]
            "recipes/x",
            expected.sha256,
        )
        is None
    )


# ----------------------------------------------------------------- receipts


def _prepare(storage, receipt):
    return prepare_runtime_image(
        _document("recipe-source-build.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
        build_receipt=receipt,
    )


def test_a_partly_damaged_build_receipt_is_derived_again_from_the_cached_receipt(
    tmp_path: Path,
) -> None:
    """Catches damaged receipt fields treated as a recipe fault instead of re-derived."""

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    intact = _prepare(storage, _build_receipt())

    damaged = _build_receipt() | {
        "image_digest": "not-a-digest",
        "image_bytes": None,
        "build_id": "",
        "build_input_sha256": "xyz",
    }
    healed = _prepare(storage, damaged)

    assert healed.image_digest == intact.image_digest == BUILT_IMAGE_DIGEST
    assert healed.image_bytes == len(ARCHIVE)
    assert healed.build_id == intact.build_id


def test_a_damaged_receipt_without_evidence_is_unknown_and_retried_not_invalid(
    tmp_path: Path,
) -> None:
    """Catches ``receipt_invalid`` raised as a refusal for data the Controller damaged."""

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))

    with pytest.raises(RuntimeImagePreparationUnknown):
        _prepare(storage, _build_receipt() | {"image_digest": None})

    receipt = _prepare(storage, _build_receipt())
    assert receipt.image_digest == BUILT_IMAGE_DIGEST
    assert storage.read_receipt(receipt.oci_archive_sha256) == receipt


@pytest.mark.parametrize(
    "receipt",
    [
        {"state": "running"},
        {"oci_layout_sha256": "short"},
    ],
)
def test_an_unfinished_or_unlocatable_build_receipt_is_observed_again(
    tmp_path: Path, receipt: dict[str, object]
) -> None:
    """Catches ``build_incomplete`` and an unnamed archive refused instead of observed."""

    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    with pytest.raises(RuntimeImagePreparationUnknown):
        _prepare(storage, _build_receipt() | receipt)

    place_test_image(storage, ARCHIVE_DIGEST, len(ARCHIVE))
    repaired = _prepare(storage, _build_receipt())
    assert storage.read_receipt(repaired.oci_archive_sha256) == repaired
