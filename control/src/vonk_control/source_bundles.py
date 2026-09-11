from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import IO, TYPE_CHECKING, Protocol, runtime_checkable

from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.source_bundles import (
    SourceBundleDigestManifest,
    SourceBundleFile,
    SourceBundleManifest,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


class SourceBundleError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class BundleLimits:
    max_archive_bytes: int = 64 * 1024 * 1024
    max_files: int = 4096
    max_file_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoredBundle:
    path: Path
    manifest: SourceBundleManifest
    archive_bytes: int


@dataclass(frozen=True, slots=True)
class GeneratedSourceBundle:
    files: Mapping[str, bytes]
    archive: bytes
    manifest: SourceBundleManifest

    @property
    def sha256(self) -> str:
        return self.manifest.sha256


def generate_source_bundle(files: Mapping[str, bytes]) -> GeneratedSourceBundle:
    """Create a deterministic canonical tar from regular source files."""

    if not files:
        raise SourceBundleError("bundle.empty", "source bundle has no files")
    stream = io.BytesIO()
    normalized: dict[str, bytes] = {}
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as bundle:
        for raw_path in sorted(files, key=lambda value: value.encode("utf-8")):
            path = _safe_path(raw_path)
            content = files[raw_path]
            if not isinstance(content, bytes):
                raise SourceBundleError(
                    "bundle.file_invalid", "source bundle file is not binary"
                )
            normalized[path] = content
            member = tarfile.TarInfo(path)
            member.size = len(content)
            member.mode = 0o644
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mtime = 0
            bundle.addfile(member, io.BytesIO(content))
    archive = stream.getvalue()
    manifest = inspect_source_bundle(io.BytesIO(archive))
    return GeneratedSourceBundle(
        files=MappingProxyType(normalized), archive=archive, manifest=manifest
    )


def inspect_source_bundle(
    payload: IO[bytes], limits: BundleLimits | None = None
) -> SourceBundleManifest:
    active = limits or BundleLimits()
    return _inspect_archive(_read_archive(payload, active), active)


class SourceBundleStore:
    def __init__(self, root: Path, *, limits: BundleLimits | None = None) -> None:
        self._root = root.resolve()
        self._limits = limits or BundleLimits()

    def put(self, expected_sha256: str, payload: IO[bytes]) -> StoredBundle:
        if (
            len(expected_sha256) != 64
            or expected_sha256.lower() != expected_sha256
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise SourceBundleError(
                "bundle.digest_invalid", "expected digest is invalid"
            )
        archive = _read_archive(payload, self._limits)
        manifest = _inspect_archive(archive, self._limits)
        if manifest.sha256 != expected_sha256:
            raise SourceBundleError(
                "bundle.digest_mismatch", "source bundle digest does not match"
            )
        directory = self._root / expected_sha256[:2]
        destination = directory / f"{expected_sha256}.tar"
        directory.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing = destination.read_bytes()
            if _inspect_archive(existing, self._limits).sha256 != expected_sha256:
                raise SourceBundleError(
                    "bundle.storage_collision", "stored source bundle is inconsistent"
                )
            return StoredBundle(destination, manifest, len(existing))

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{expected_sha256}.", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(archive)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()
        return StoredBundle(destination, manifest, len(archive))

    def get(self, sha256: str) -> GeneratedSourceBundle:
        if len(sha256) != 64 or any(
            character not in "0123456789abcdef" for character in sha256
        ):
            raise SourceBundleError(
                "bundle.digest_invalid", "source bundle digest is invalid"
            )
        path = self._root / sha256[:2] / f"{sha256}.tar"
        try:
            archive = path.read_bytes()
        except OSError as error:
            raise SourceBundleError(
                "bundle.not_found", "source bundle is unavailable"
            ) from error
        manifest = _inspect_archive(archive, self._limits)
        if manifest.sha256 != sha256:
            raise SourceBundleError(
                "bundle.storage_collision", "stored source bundle is inconsistent"
            )
        return _generated_bundle(archive, manifest, self._limits)


@runtime_checkable
class SourceBundleStoreProtocol(Protocol):
    """Structural surface shared by the file and database bundle stores.

    Both :class:`SourceBundleStore` and :class:`DatabaseSourceBundleStore` back
    the same content-addressed contract, and every consumer calls only ``put``
    and ``get``.  Annotating consumers with this protocol keeps the two
    implementations interchangeable without pretending one is a subclass of the
    other.
    """

    def put(self, expected_sha256: str, payload: IO[bytes]) -> StoredBundle: ...

    def get(self, sha256: str) -> GeneratedSourceBundle: ...


class DatabaseSourceBundleStore:
    """Content-addressed source bundle store backed entirely by PostgreSQL."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        limits: BundleLimits | None = None,
    ) -> None:
        self._sessions = sessions
        self._limits = limits or BundleLimits()

    def put(self, expected_sha256: str, payload: IO[bytes]) -> StoredBundle:
        _validate_digest(expected_sha256, "bundle.digest_invalid")
        archive = _read_archive(payload, self._limits)
        manifest = _inspect_archive(archive, self._limits)
        if manifest.sha256 != expected_sha256:
            raise SourceBundleError(
                "bundle.digest_mismatch", "source bundle digest does not match"
            )
        from .models import RecipeSourceBundle, SourceBundleArchive

        with self._sessions.begin() as session:
            metadata = session.get(RecipeSourceBundle, expected_sha256)
            stored = session.get(SourceBundleArchive, expected_sha256)
            if metadata is not None and parse_source_bundle_manifest(metadata.manifest) != manifest:
                raise SourceBundleError("bundle.storage_collision", "stored source manifest is inconsistent")
            if stored is not None and stored.archive != archive:
                raise SourceBundleError(
                    "bundle.storage_collision", "stored source bundle is inconsistent"
                )
            if stored is None:
                if metadata is None:
                    metadata = RecipeSourceBundle(
                        sha256=manifest.sha256,
                        media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                        archive_bytes=len(archive),
                        total_bytes=manifest.total_bytes,
                        file_count=len(manifest.files),
                        storage_key=f"postgres:{manifest.sha256}",
                        manifest=json.loads(canonical_message(manifest)),
                        verified_at=datetime.now(UTC),
                    )
                    session.add(metadata)
                session.add(SourceBundleArchive(sha256=expected_sha256, archive=archive))
        return StoredBundle(Path(f"/postgresql/source-bundles/{expected_sha256}"), manifest, len(archive))

    def get(self, sha256: str) -> GeneratedSourceBundle:
        _validate_digest(sha256, "bundle.digest_invalid")
        from .models import RecipeSourceBundle, SourceBundleArchive

        with self._sessions() as session:
            stored = session.get(SourceBundleArchive, sha256)
            if stored is None:
                raise SourceBundleError(
                    "bundle.not_found", "source bundle is unavailable"
                )
            metadata = session.get(RecipeSourceBundle, sha256)
            if metadata is None:
                raise SourceBundleError("bundle.manifest_invalid", "stored source manifest is unavailable")
            persisted = parse_source_bundle_manifest(metadata.manifest)
            archive = stored.archive
        manifest = _inspect_archive(archive, self._limits)
        if manifest != persisted:
            raise SourceBundleError("bundle.storage_collision", "stored source manifest is inconsistent")
        if manifest.sha256 != sha256:
            raise SourceBundleError(
                "bundle.storage_collision", "stored source bundle is inconsistent"
            )
        return _generated_bundle(archive, manifest, self._limits)


def parse_source_bundle_manifest(value: object) -> SourceBundleManifest:
    try:
        return SourceBundleManifest.model_validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise SourceBundleError("bundle.manifest_invalid", "stored source manifest is invalid") from error


def _validate_digest(value: str, code: str) -> None:
    if len(value) != 64 or value.lower() != value or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise SourceBundleError(code, "source bundle digest is invalid")


def _generated_bundle(
    archive: bytes,
    manifest: SourceBundleManifest,
    limits: BundleLimits,
) -> GeneratedSourceBundle:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
        for member in bundle:
            if not member.isfile():
                continue
            stream = bundle.extractfile(member)
            if stream is None:
                raise SourceBundleError(
                    "bundle.read_failed", "source bundle file cannot be read"
                )
            files[_safe_path(member.name)] = stream.read(limits.max_file_bytes + 1)
    return GeneratedSourceBundle(MappingProxyType(files), archive, manifest)


def _read_archive(payload: IO[bytes], limits: BundleLimits) -> bytes:
    archive = payload.read(limits.max_archive_bytes + 1)
    if not isinstance(archive, bytes):
        raise SourceBundleError("bundle.read_failed", "source bundle is not binary")
    if not archive:
        raise SourceBundleError("bundle.empty", "source bundle is empty")
    if len(archive) > limits.max_archive_bytes:
        raise SourceBundleError(
            "bundle.archive_too_large", "source bundle is too large"
        )
    return archive


def _inspect_archive(archive: bytes, limits: BundleLimits) -> SourceBundleManifest:
    files: list[SourceBundleFile] = []
    seen: set[str] = set()
    total = 0
    try:
        bundle = tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")  # noqa: SIM115
    except (tarfile.TarError, OSError) as error:
        raise SourceBundleError(
            "bundle.invalid_archive", "source bundle is invalid"
        ) from error
    with bundle:
        for member in bundle:
            path = _safe_path(member.name)
            if path in seen:
                raise SourceBundleError(
                    "bundle.duplicate_path", "source bundle contains a duplicate path"
                )
            seen.add(path)
            if member.isdir():
                continue
            if not member.isfile():
                raise SourceBundleError(
                    "bundle.entry_forbidden",
                    "source bundle may contain only directories and regular files",
                )
            if len(files) >= limits.max_files:
                raise SourceBundleError(
                    "bundle.too_many_files", "source bundle contains too many files"
                )
            if member.size < 0 or member.size > limits.max_file_bytes:
                raise SourceBundleError(
                    "bundle.file_too_large", "source bundle file is too large"
                )
            total += member.size
            if total > limits.max_total_bytes:
                raise SourceBundleError(
                    "bundle.expanded_too_large", "expanded source bundle is too large"
                )
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise SourceBundleError(
                    "bundle.read_failed", "source bundle file cannot be read"
                )
            content = extracted.read(limits.max_file_bytes + 1)
            if len(content) != member.size:
                raise SourceBundleError(
                    "bundle.size_mismatch", "source bundle file size is inconsistent"
                )
            files.append(
                SourceBundleFile(
                    path=path,
                    mode=0o755 if member.mode & 0o111 else 0o644,
                    size=member.size,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
    files.sort(key=lambda item: item.path.encode("utf-8"))
    identity = SourceBundleDigestManifest(
        schema_version=1, files=tuple(files), total_bytes=total,
    )
    return SourceBundleManifest.model_validate_json(canonical_message(
        identity.model_dump(mode="json") | {"sha256": identity.digest()}
    ))


def _safe_path(value: str) -> str:
    if not value or "\x00" in value or value.startswith("/"):
        raise SourceBundleError(
            "bundle.path_forbidden", "source bundle path is forbidden"
        )
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SourceBundleError(
            "bundle.path_forbidden", "source bundle path is forbidden"
        )
    normalized = path.as_posix()
    if len(normalized.encode("utf-8")) > 512:
        raise SourceBundleError(
            "bundle.path_too_long", "source bundle path is too long"
        )
    return normalized
