"""Damaged stored build data is rebuilt from its evidence, never a recipe fault.

Source and receipts are verified at ingress. What is stored afterwards can be
damaged, and a damaged stored copy is repaired from the evidence the Controller
kept (the recipe package closure, the image cache's receipt) and verified again.
Only a source that is invalid right after a fresh verification is the recipe's.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import UnknownOutcomeError
from vonk_control.models import (
    Base,
    CatalogDocumentRevision,
    RecipeSourceBundle,
    SourceBundleArchive,
)
from vonk_control.recipe_builds import (
    RecipeBuildInvalid,
    RecipeBuildService,
    RecipeBuildUnknown,
    RecipeSourcePolicyError,
    rederive_source_bundle_from_closure,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationInvalid,
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
def test_damaged_database_rows_are_rewritten_from_the_verified_archive() -> None:
    """Catches damaged archive or metadata rows blocking their own re-ingress."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
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


@dataclass
class _DamagedOnce:
    """A store whose copy lacks the Dockerfile until it is stored again."""

    real: SourceBundleStore
    damaged: bool = True
    puts: int = 0

    def put(self, expected_sha256, payload):
        self.puts += 1
        stored = self.real.put(expected_sha256, payload)
        self.damaged = False
        return stored

    def get(self, sha256):
        bundle = self.real.get(sha256)
        if not self.damaged:
            return bundle
        files = {
            path: data for path, data in bundle.files.items() if path != "Dockerfile"
        }
        return replace(bundle, files=files)


def test_a_bundle_lacking_the_dockerfile_is_derived_again_not_refused(
    tmp_path: Path,
) -> None:
    """Catches a damaged stored bundle becoming a terminal recipe fault."""

    sessions, bundles, _now, _node, revision = setup(tmp_path)
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        sha = str(row.projected["source_bundle_sha256"])
    archive = bundles.get(sha).archive
    store = _DamagedOnce(bundles)
    service = RecipeBuildService(
        sessions,
        bundles=store,
        source_rederiver=lambda _projected, _context, _sha: archive,
    )

    resolution = service.resolve(revision.id)

    assert resolution is not None
    assert store.puts == 1 and not store.damaged


def test_a_bundle_that_cannot_be_derived_again_waits_instead_of_failing(
    tmp_path: Path,
) -> None:
    """Catches terminal refusal of a source nobody re-verified."""

    sessions, bundles, _now, _node, revision = setup(tmp_path)
    store = _DamagedOnce(bundles)
    service = RecipeBuildService(
        sessions, bundles=store, source_rederiver=lambda *_args: None
    )

    with pytest.raises(RecipeBuildUnknown) as raised:
        service.resolve(revision.id)

    assert isinstance(raised.value, UnknownOutcomeError)
    assert not isinstance(raised.value, (RecipeBuildInvalid, RecipeSourcePolicyError))


def test_incomplete_local_evidence_after_reingress_remains_unknown(
    tmp_path: Path,
) -> None:
    """Catches local damage after re-ingress misclassified as a recipe fault."""

    sessions, bundles, _now, _node, revision = setup(tmp_path)

    @dataclass
    class AlwaysWithout:
        real: SourceBundleStore

        def put(self, expected_sha256, payload):
            return self.real.put(expected_sha256, payload)

        def get(self, sha256):
            bundle = self.real.get(sha256)
            files = {p: d for p, d in bundle.files.items() if p != "Dockerfile"}
            return replace(bundle, files=files)

    with sessions() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        sha = str(row.projected["source_bundle_sha256"])
    archive = bundles.get(sha).archive
    service = RecipeBuildService(
        sessions,
        bundles=AlwaysWithout(bundles),
        source_rederiver=lambda *_args: archive,
    )

    with pytest.raises(RecipeBuildUnknown):
        service.resolve(revision.id)
    service._bundles = bundles
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

    with pytest.raises(RuntimeImagePreparationUnknown) as raised:
        _prepare(storage, _build_receipt() | {"image_digest": None})

    assert raised.value.retryable
    assert not isinstance(raised.value, RuntimeImagePreparationInvalid)
    assert json.dumps(raised.value.recovery_actions) == '["retry"]'


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
    with pytest.raises(RuntimeImagePreparationUnknown) as raised:
        _prepare(storage, _build_receipt() | receipt)
    assert raised.value.retryable
