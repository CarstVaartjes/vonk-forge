from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from vonk_control.source_bundles import (
    BundleLimits,
    SourceBundleError,
    SourceBundleStore,
    generate_source_bundle,
    inspect_source_bundle,
)

LIMITS = BundleLimits(
    max_archive_bytes=16_384,
    max_files=8,
    max_file_bytes=4096,
    max_total_bytes=8192,
)


def archive(files: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name, content in files:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            bundle.addfile(info, io.BytesIO(content))
    return output.getvalue()


@pytest.mark.parametrize("name", ["/etc/passwd", "../escape", "a/../../escape"])
def test_bundle_rejects_paths_outside_context(name: str) -> None:
    with pytest.raises(SourceBundleError) as error:
        inspect_source_bundle(io.BytesIO(archive([(name, b"x")])), LIMITS)

    assert error.value.code == "bundle.path_forbidden"


def test_bundle_digest_is_archive_order_independent() -> None:
    first = inspect_source_bundle(
        io.BytesIO(archive([("Dockerfile", b"FROM scratch\n"), ("x", b"1")])),
        LIMITS,
    )
    second = inspect_source_bundle(
        io.BytesIO(archive([("x", b"1"), ("Dockerfile", b"FROM scratch\n")])),
        LIMITS,
    )

    assert first.sha256 == second.sha256
    assert [item.path for item in first.files] == ["Dockerfile", "x"]


def test_bundle_rejects_links_and_expansion_overflow() -> None:
    linked = io.BytesIO()
    with tarfile.open(fileobj=linked, mode="w") as bundle:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        bundle.addfile(info)
    with pytest.raises(SourceBundleError, match="regular files"):
        inspect_source_bundle(io.BytesIO(linked.getvalue()), LIMITS)

    with pytest.raises(SourceBundleError) as error:
        inspect_source_bundle(io.BytesIO(archive([("large", b"x" * 4097)])), LIMITS)
    assert error.value.code == "bundle.file_too_large"


def test_store_is_content_addressed_and_idempotent(tmp_path) -> None:
    payload = archive([("Dockerfile", b"FROM scratch\n")])
    manifest = inspect_source_bundle(io.BytesIO(payload), LIMITS)
    store = SourceBundleStore(tmp_path, limits=LIMITS)

    first = store.put(manifest.sha256, io.BytesIO(payload))
    second = store.put(manifest.sha256, io.BytesIO(payload))
    loaded = store.get(manifest.sha256)

    assert first == second
    assert first.path.read_bytes() == payload
    assert loaded.archive == payload
    assert loaded.files["Dockerfile"] == b"FROM scratch\n"


def test_store_rejects_expected_digest_mismatch(tmp_path) -> None:
    store = SourceBundleStore(tmp_path, limits=LIMITS)
    with pytest.raises(SourceBundleError) as error:
        store.put("f" * 64, io.BytesIO(archive([("Dockerfile", b"FROM scratch\n")])))

    assert error.value.code == "bundle.digest_mismatch"


@pytest.mark.usefixtures("damaged_json_rows")
def test_postgres_source_bundle_metadata_roundtrip_and_strict_reads(postgres_engine):
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import canonical_message
    from vonk_agent_protocol.source_bundles import SourceBundleManifest
    from vonk_control.models import Base, RecipeSourceBundle
    from vonk_control.source_bundles import (
        DatabaseSourceBundleStore,
        generate_source_bundle,
    )

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n", "empty": b""})
    store = DatabaseSourceBundleStore(sessions)
    first = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    assert (
        store.put(bundle.sha256, io.BytesIO(bundle.archive)).manifest == first.manifest
    )
    assert store.get(bundle.sha256).manifest == first.manifest
    with sessions() as session:
        stored = session.get(RecipeSourceBundle, bundle.sha256)
        assert stored is not None
        parsed = SourceBundleManifest.model_validate_json(
            canonical_message(stored.manifest)
        )
        assert parsed == bundle.manifest
    with sessions.begin() as session:
        stored = session.get(RecipeSourceBundle, bundle.sha256)
        assert stored is not None
        stored.manifest = {**stored.manifest, "total_bytes": None}
    with pytest.raises(SourceBundleError) as error:
        store.get(bundle.sha256)
    assert error.value.code == "bundle.manifest_invalid"
    # A damaged stored manifest is re-derived from the archive verified at this
    # ingress, not a collision and not a recipe fault.
    assert store.put(bundle.sha256, io.BytesIO(bundle.archive)).manifest == (
        first.manifest
    )
    assert store.get(bundle.sha256).manifest == first.manifest


@pytest.mark.parametrize("fault", ["io", "permission"])
def test_existing_verified_bundle_is_not_replaced_when_read_is_unknown(
    tmp_path, monkeypatch, fault
):
    import errno

    from vonk_agent_protocol import SecurityRefusalError, SourceBundleCode

    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    store = SourceBundleStore(tmp_path)
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    before = stored.path.stat().st_ino
    original = Path.read_bytes

    def read(path):
        if path == stored.path:
            if fault == "permission":
                raise PermissionError(errno.EACCES, "NAS denied")
            raise OSError(errno.EIO, "NAS unavailable")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    for action in (
        lambda: store.get(bundle.sha256),
        lambda: store.put(bundle.sha256, io.BytesIO(bundle.archive)),
    ):
        with pytest.raises(SourceBundleError) as caught:
            action()
        if fault == "permission":
            assert isinstance(caught.value, SecurityRefusalError)
            assert caught.value.code == "permission_denied"
        else:
            assert caught.value.code == SourceBundleCode.STORAGE_UNAVAILABLE
        assert stored.path.stat().st_ino == before
    monkeypatch.setattr(Path, "read_bytes", original)
    assert store.get(bundle.sha256).archive == bundle.archive


def test_catalog_source_upload_unknown_recovers_exact_digest(tmp_path, monkeypatch):
    import errno
    from datetime import UTC, datetime

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import UnknownOutcomeError
    from vonk_control.auth import TokenCodec
    from vonk_control.catalog_service import CatalogService
    from vonk_control.models import Base, RecipeSourceBundle

    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    store = SourceBundleStore(tmp_path / "bundles")
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    inode = stored.path.stat().st_ino
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 10, 6, tzinfo=UTC),
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        source_bundles=store,
    )
    original = Path.read_bytes
    fault = [True]

    def read(path):
        if path == stored.path and fault[0]:
            raise OSError(errno.EIO, "NAS read unavailable")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(UnknownOutcomeError) as caught:
        catalog.store_source_bundle(
            bundle.sha256, io.BytesIO(bundle.archive), "operator"
        )
    assert isinstance(caught.value, SourceBundleError)
    assert caught.value.code == "bundle.storage_unavailable"
    assert stored.path.stat().st_ino == inode
    fault[0] = False
    restored = catalog.store_source_bundle(
        bundle.sha256, io.BytesIO(bundle.archive), "operator"
    )
    assert restored.sha256 == bundle.sha256
    assert stored.path.stat().st_ino == inode
    with sessions() as session:
        assert len(tuple(session.scalars(select(RecipeSourceBundle)))) == 1
