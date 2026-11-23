from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vonk_agent_protocol.reason_codes.sources import SourceBundleCode
from vonk_agent_protocol.source_bundles import SourceBundleManifest
from vonk_control import source_bundles
from vonk_control.source_bundles import (
    BundleLimits,
    SourceBundleError,
    SourceBundleStore,
    generate_source_bundle,
    inspect_source_bundle,
    parse_source_bundle,
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


@pytest.mark.parametrize("name", ["/etc/passwd", "../escape", "a/../../escape", "."])
def test_bundle_rejects_paths_outside_context(name: str) -> None:
    with pytest.raises(Exception):  # noqa: B017 -- rejected ingress followed by valid admission
        inspect_source_bundle(io.BytesIO(archive([(name, b"x")])), LIMITS)

    accepted = inspect_source_bundle(io.BytesIO(archive([("safe", b"x")])), LIMITS)
    assert [item.path for item in accepted.files] == ["safe"]


@pytest.mark.parametrize("cut", [513, 520, 1537])
def test_truncated_archive_raises_only_the_typed_error(cut: int) -> None:
    whole = archive([("a", b"abc"), ("b", b"abc")])
    packed = gzip.compress(whole)
    for damaged in (whole[:cut], packed[: len(packed) // 2]):
        with pytest.raises(SourceBundleError) as raised:
            parse_source_bundle(damaged, LIMITS)
        assert raised.value.code == SourceBundleCode.INVALID_ARCHIVE


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
    with pytest.raises(SourceBundleError):
        inspect_source_bundle(io.BytesIO(linked.getvalue()), LIMITS)

    with pytest.raises(Exception):  # noqa: B017 -- rejected ingress followed by valid admission
        inspect_source_bundle(io.BytesIO(archive([("large", b"x" * 4097)])), LIMITS)
    accepted = inspect_source_bundle(io.BytesIO(archive([("safe", b"x")])), LIMITS)
    assert accepted.files[0].size == 1


def test_store_is_content_addressed_and_idempotent(tmp_path, monkeypatch) -> None:
    payload = archive([("Dockerfile", b"FROM scratch\n")])
    manifest = inspect_source_bundle(io.BytesIO(payload), LIMITS)
    store = SourceBundleStore(tmp_path, limits=LIMITS)

    first = store.put(manifest.sha256, io.BytesIO(payload))
    inode = first.path.stat().st_ino
    inspected: list[bytes] = []
    inspect_original = source_bundles._inspect_archive

    def inspect_actual(value: bytes, limits: BundleLimits) -> SourceBundleManifest:
        inspected.append(value)
        return inspect_original(value, limits)

    monkeypatch.setattr(source_bundles, "_inspect_archive", inspect_actual)
    second = store.put(manifest.sha256, io.BytesIO(payload))
    # Validate the actual incoming archive once, reuse its proven exact bytes,
    # and preserve the verified physical object instead of rewriting it.
    assert inspected == [payload]
    assert second.path.stat().st_ino == inode
    loaded = store.get(manifest.sha256)

    assert first == second
    assert first.path.read_bytes() == payload
    assert loaded.archive == payload
    assert loaded.files["Dockerfile"] == b"FROM scratch\n"


@pytest.mark.parametrize("stored_damage", ["different-valid", "corrupt"])
def test_nonidentical_stored_archive_is_strictly_checked_and_recovers(
    tmp_path, monkeypatch, stored_damage: str
) -> None:
    payload = archive([("Dockerfile", b"FROM scratch\n"), ("x", b"1")])
    manifest = inspect_source_bundle(io.BytesIO(payload), LIMITS)
    store = SourceBundleStore(tmp_path, limits=LIMITS)
    stored = store.put(manifest.sha256, io.BytesIO(payload))
    changed = (
        archive([("x", b"1"), ("Dockerfile", b"FROM scratch\n")])
        if stored_damage == "different-valid"
        else b"damaged archive bytes"
    )
    assert changed != payload
    stored.path.write_bytes(changed)
    inspected: list[bytes] = []
    inspect_original = source_bundles._inspect_archive

    def inspect_actual(value: bytes, limits: BundleLimits) -> SourceBundleManifest:
        inspected.append(value)
        return inspect_original(value, limits)

    monkeypatch.setattr(source_bundles, "_inspect_archive", inspect_actual)
    restored = store.put(manifest.sha256, io.BytesIO(payload))
    assert inspected == [payload]
    assert restored.manifest == manifest
    loaded = store.get(manifest.sha256)
    assert loaded.manifest == manifest
    assert loaded.files == {"Dockerfile": b"FROM scratch\n", "x": b"1"}
    # Different tar order is valid under the same content identity. Corruption
    # is repaired atomically from the real, strictly validated ingress.
    assert loaded.archive == payload


def test_equal_unvalidated_archive_is_not_blessed(tmp_path) -> None:
    payload = archive([("Dockerfile", b"FROM scratch\n")])
    manifest = inspect_source_bundle(io.BytesIO(payload), LIMITS)
    store = SourceBundleStore(tmp_path, limits=LIMITS)
    stored = store.put(manifest.sha256, io.BytesIO(payload))
    damaged = b"not an archive"
    stored.path.write_bytes(damaged)
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        store.put(manifest.sha256, io.BytesIO(damaged))
    assert stored.path.read_bytes() == damaged
    # Repair resumes through the same identity and full ordinary ingress.
    repaired = store.put(manifest.sha256, io.BytesIO(payload))
    assert repaired.manifest == manifest
    assert store.get(manifest.sha256).archive == payload


def test_store_rejects_expected_digest_mismatch(tmp_path) -> None:
    store = SourceBundleStore(tmp_path, limits=LIMITS)
    payload = archive([("Dockerfile", b"FROM scratch\n")])
    manifest = inspect_source_bundle(io.BytesIO(payload), LIMITS)
    original = store.put(manifest.sha256, io.BytesIO(payload))
    with pytest.raises(Exception):  # noqa: B017 -- effects and repaired input establish the outcome
        store.put("f" * 64, io.BytesIO(payload))

    assert original.path.read_bytes() == payload
    assert store.get(manifest.sha256).manifest == manifest
    assert not (tmp_path / "ff" / f"{'f' * 64}.tar").exists()


@pytest.mark.usefixtures("damaged_json_rows")
def test_postgres_source_bundle_metadata_roundtrip_and_archive_owned_reads(
    postgres_engine,
):
    from sqlalchemy.orm import sessionmaker

    from vonk_agent_protocol.contracts import canonical_message
    from vonk_agent_protocol.source_bundles import SourceBundleManifest
    from vonk_control.model_primitives import Base
    from vonk_control.models.catalog import RecipeSourceBundle
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
    with pytest.raises(source_bundles.SourceBundleUnknown):
        store.get(bundle.sha256)
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
        with pytest.raises(Exception):  # noqa: B017 -- unavailable local bytes remain intact and recover below
            action()
        assert stored.path.stat().st_ino == before
    monkeypatch.setattr(Path, "read_bytes", original)
    assert store.get(bundle.sha256).archive == bundle.archive


def test_catalog_source_upload_unknown_recovers_exact_digest(tmp_path, monkeypatch):
    import errno
    from datetime import UTC, datetime

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from vonk_control.auth import TokenCodec
    from vonk_control.catalog.service import CatalogService
    from vonk_control.model_primitives import Base
    from vonk_control.models.catalog import RecipeSourceBundle

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
    with pytest.raises(Exception):  # noqa: B017 -- unavailable store preserves bytes then recovers
        catalog.store_source_bundle(
            bundle.sha256, io.BytesIO(bundle.archive), "operator"
        )
    assert stored.path.stat().st_ino == inode
    fault[0] = False
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        catalog.store_source_bundle("f" * 64, io.BytesIO(bundle.archive), "operator")
    restored = catalog.store_source_bundle(
        bundle.sha256, io.BytesIO(bundle.archive), "operator"
    )
    assert restored.sha256 == bundle.sha256
    assert stored.path.stat().st_ino == inode
    with sessions() as session:
        assert len(tuple(session.scalars(select(RecipeSourceBundle)))) == 1


@pytest.mark.parametrize("damage", [False, True])
def test_filesystem_receipt_survives_restart_without_hashing(
    tmp_path, monkeypatch, damage
):
    """Catches process-local proof and same-length corruption silently passing."""
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    stored = SourceBundleStore(tmp_path).put(bundle.sha256, io.BytesIO(bundle.archive))

    def forbidden(*_args):
        pytest.fail("reader rehashed bytes already verified at ingress")

    monkeypatch.setattr(source_bundles, "_inspect_archive", forbidden)
    for _ in range(2):
        assert SourceBundleStore(tmp_path).get(bundle.sha256).files == bundle.files
    if damage:
        stored.path.write_bytes(bundle.archive.replace(b"scratch", b"changed"))
        with pytest.raises(source_bundles.SourceBundleUnknown):
            SourceBundleStore(tmp_path).get(bundle.sha256)


def _stays_inside_root(path: str) -> bool:
    parts = path.split("/")
    return (
        bool(path)
        and not path.startswith("/")
        and "\x00" not in path
        and all(part not in {"", ".", ".."} for part in parts)
        and len(path.encode("utf-8")) <= 512
    )


_PATH_PART = st.sampled_from(["a", "b.txt", "..", ".", "", "\\", "x" * 300])
_ENTRY_PATH = st.lists(_PATH_PART, min_size=1, max_size=4).map("/".join)
_ENTRY_KIND = st.sampled_from(
    [
        tarfile.REGTYPE,
        tarfile.DIRTYPE,
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.FIFOTYPE,
    ]
)


def _typed_archive(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = (
                "/etc/passwd" if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
            )
            data = b"abc" if kind == tarfile.REGTYPE else b""
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.property
@given(
    st.lists(st.tuples(_ENTRY_PATH, _ENTRY_KIND), max_size=4),
    st.one_of(st.none(), st.integers(0, 4096)),
)
@example([("a", tarfile.REGTYPE), ("b", tarfile.REGTYPE)], 513)
@example([("a", tarfile.REGTYPE), ("b", tarfile.REGTYPE)], 1537)
@example([("safe", tarfile.REGTYPE)], None)
def test_parsed_bundle_is_typed_or_confined_to_the_root(
    entries: list[tuple[str, bytes]], cut: int | None
) -> None:
    payload = _typed_archive(entries)
    if cut is not None:
        payload = payload[:cut]
    try:
        bundle = parse_source_bundle(payload, LIMITS)
    except SourceBundleError:
        return
    assert all(_stays_inside_root(path) for path in bundle.files)
    assert {item.path for item in bundle.manifest.files} == set(bundle.files)


@pytest.mark.property
@given(st.lists(_PATH_PART, max_size=5).map("/".join))
@example("./a")
@example("a//b")
@example("a/../b")
@example("/etc/passwd")
@example("a\x00b")
@example(".")
def test_normalised_source_bundle_path_never_leaves_the_root(raw: str) -> None:
    try:
        normalised = source_bundles._safe_path(raw)
    except SourceBundleError as error:
        assert error.code in {
            SourceBundleCode.PATH_FORBIDDEN,
            SourceBundleCode.PATH_TOO_LONG,
        }
        return
    assert _stays_inside_root(normalised)
