from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import IO, TYPE_CHECKING, Protocol, runtime_checkable

from sqlalchemy import literal_column, select
from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    SecurityRefusalError,
    SecurityRefusalReason,
    SourceBundleCode,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.source_bundles import (
    SourceBundleDigestManifest,
    SourceBundleFile,
    SourceBundleManifest,
)

from .source_bundle_contract import SourceBundleVerification

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


class SourceBundleError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class SourceBundleRefused(SecurityRefusalError, SourceBundleError):
    """Explicit managed source storage denial, never damaged or missing data."""

    def __init__(self, detail: str) -> None:
        SourceBundleError.__init__(
            self, SecurityRefusalReason.PERMISSION_DENIED.value, detail
        )
        self.typed_reason = SecurityRefusalReason.PERMISSION_DENIED


class SourceBundleIntegrityRefused(SecurityRefusalError, SourceBundleError):
    """Incoming source does not verify against the accepted content identity."""

    def __init__(self, detail: str) -> None:
        SourceBundleError.__init__(self, SourceBundleCode.DIGEST_MISMATCH, detail)
        self.typed_reason = SecurityRefusalReason.DIGEST_MISMATCH


class SourceBundleUnknown(UnknownOutcomeError, SourceBundleError):
    """The exact source cannot be observed now; keep existing verified data."""

    def __init__(self, code: str, detail: str) -> None:
        SourceBundleError.__init__(self, code, detail)
        self.typed_reason = WaitReason.OBSERVATION_UNAVAILABLE


class SourceBundleInvalid(InvalidRequestError, SourceBundleError):
    """Malformed source input rejected before managed storage effects."""

    def __init__(self, code: str, detail: str) -> None:
        SourceBundleError.__init__(self, code, detail)
        self.typed_reason = InvalidRequestReason.MALFORMED
        self.typed_field = "source_bundle"


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
        raise SourceBundleInvalid(SourceBundleCode.EMPTY, "source bundle has no files")
    stream = io.BytesIO()
    normalized: dict[str, bytes] = {}
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as bundle:
        for raw_path in sorted(files, key=lambda value: value.encode("utf-8")):
            path = _safe_path(raw_path)
            content = files[raw_path]
            if not isinstance(content, bytes):
                raise SourceBundleError(
                    SourceBundleCode.FILE_INVALID, "source bundle file is not binary"
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


def parse_source_bundle(
    archive: bytes, limits: BundleLimits | None = None
) -> GeneratedSourceBundle:
    """Read a canonical source bundle archive into its verified files."""

    active = limits or BundleLimits()
    try:
        manifest = _inspect_archive(archive, active)
        return _generated_bundle(archive, manifest, active)
    except SourceBundleError as error:
        # This boundary owns incoming bytes, before any managed storage effect.
        # Stored readers retain the decoder's unknown-outcome category instead.
        raise SourceBundleInvalid(error.code, error.detail) from error


def inspect_source_bundle(
    payload: IO[bytes], limits: BundleLimits | None = None
) -> SourceBundleManifest:
    active = limits or BundleLimits()
    return _inspect_archive(_read_archive(payload, active), active)


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    value = path.stat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _publish_verification(path: Path, manifest: SourceBundleManifest) -> None:
    receipt = SourceBundleVerification(manifest=manifest, identity=_file_identity(path))
    descriptor, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_message(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.with_suffix(".verified.json"))
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class SourceBundleStore:
    def __init__(self, root: Path, *, limits: BundleLimits | None = None) -> None:
        self._root = root.resolve()
        self._limits = limits or BundleLimits()

    @staticmethod
    @contextmanager
    def _storage_access() -> Iterator[None]:
        """Preserve the storage boundary's explicit denial and unavailable causes."""
        try:
            yield
        except PermissionError as error:
            raise SourceBundleRefused(
                "source bundle storage access was denied"
            ) from error
        except FileNotFoundError as error:
            raise _verification_pending() from error
        except OSError as error:
            raise SourceBundleUnknown(
                SourceBundleCode.STORAGE_UNAVAILABLE,
                "source bundle storage is temporarily unavailable",
            ) from error

    def put(self, expected_sha256: str, payload: IO[bytes]) -> StoredBundle:
        with self._storage_access():
            return self._put(expected_sha256, payload)

    def _put(self, expected_sha256: str, payload: IO[bytes]) -> StoredBundle:
        if (
            len(expected_sha256) != 64
            or expected_sha256.lower() != expected_sha256
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise SourceBundleError(
                SourceBundleCode.DIGEST_INVALID, "expected digest is invalid"
            )
        archive = _read_archive(payload, self._limits)
        manifest = _inspect_archive(archive, self._limits)
        if manifest.sha256 != expected_sha256:
            raise SourceBundleIntegrityRefused("source bundle digest does not match")
        directory = self._root / expected_sha256[:2]
        destination = directory / f"{expected_sha256}.tar"
        directory.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            try:
                existing = destination.read_bytes()
                present = existing == archive
            except FileNotFoundError:
                present = False
            if present:
                _publish_verification(destination, manifest)
                return StoredBundle(destination, manifest, len(existing))
            # A stored copy that no longer verifies is damaged data, and the
            # archive just verified at this ingress is its evidence: it is
            # replaced atomically below, never trusted and never a collision.

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
        _publish_verification(destination, manifest)
        return StoredBundle(destination, manifest, len(archive))

    def verified_manifest(self, sha256: str) -> SourceBundleManifest:
        _validate_digest(sha256, SourceBundleCode.DIGEST_INVALID)
        path = self._root / sha256[:2] / f"{sha256}.tar"
        with self._storage_access():
            try:
                receipt = SourceBundleVerification.model_validate_json(
                    path.with_suffix(".verified.json").read_bytes()
                )
                if (
                    receipt.manifest.sha256 == sha256
                    and receipt.identity == _file_identity(path)
                ):
                    return receipt.manifest
            except ValueError:
                pass
        raise _verification_pending()

    def get(self, sha256: str) -> GeneratedSourceBundle:
        manifest = self.verified_manifest(sha256)
        path = self._root / sha256[:2] / f"{sha256}.tar"
        with self._storage_access():
            identity = _file_identity(path)
            archive = path.read_bytes()
            # A concurrent replacement invalidates this read too, without hashing.
            if (
                self.verified_manifest(sha256) != manifest
                or _file_identity(path) != identity
            ):
                raise _verification_pending()
            try:
                return _generated_bundle(archive, manifest, self._limits)
            except tarfile.TarError as error:
                raise _verification_pending() from error


@runtime_checkable
class SourceBundleStoreProtocol(Protocol):
    """Structural surface shared by the file and database bundle stores.

    Both :class:`SourceBundleStore` and :class:`DatabaseSourceBundleStore` back
    the same content-addressed contract, and readers use the durable manifest or the verified bytes.  Annotating consumers with this protocol keeps the two
    implementations interchangeable without pretending one is a subclass of the
    other.
    """

    def put(self, expected_sha256: str, payload: IO[bytes]) -> StoredBundle: ...

    def get(self, sha256: str) -> GeneratedSourceBundle: ...

    def verified_manifest(self, sha256: str) -> SourceBundleManifest: ...


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
        _validate_digest(expected_sha256, SourceBundleCode.DIGEST_INVALID)
        archive = _read_archive(payload, self._limits)
        manifest = _inspect_archive(archive, self._limits)
        if manifest.sha256 != expected_sha256:
            raise SourceBundleIntegrityRefused("source bundle digest does not match")
        from .identity_insert import insert_identity
        from .models import RecipeSourceBundle, SourceBundleArchive

        with self._sessions.begin() as session:
            metadata, _ = insert_identity(
                session,
                RecipeSourceBundle(
                    sha256=expected_sha256,
                    media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                    archive_bytes=len(archive),
                    total_bytes=manifest.total_bytes,
                    file_count=len(manifest.files),
                    storage_key=f"postgres:{expected_sha256}",
                    manifest=json.loads(canonical_message(manifest)),
                    verified_at=datetime.now(UTC),
                ),
                key=("sha256",),
            )
            metadata.media_type = "application/vnd.vonk-forge.source-bundle.v1+tar"
            metadata.archive_bytes = len(archive)
            metadata.total_bytes = manifest.total_bytes
            metadata.file_count = len(manifest.files)
            metadata.manifest = json.loads(canonical_message(manifest))
            metadata.verified_at = datetime.now(UTC)
            stored, _ = insert_identity(
                session,
                SourceBundleArchive(sha256=expected_sha256, archive=archive),
                key=("sha256",),
            )
            if stored.archive != archive:
                stored.archive = archive
            session.flush()
            # PostgreSQL changes xmin on every row update, including direct SQL.
            # Bind the existing ingress receipt to those exact stored bytes.
            # A subsequent archive update is a miss without scanning the archive.
            version = session.scalar(
                select(literal_column("xmin::text"))
                .select_from(SourceBundleArchive)
                .where(SourceBundleArchive.sha256 == expected_sha256)
            )
            metadata.storage_key = f"postgres:{expected_sha256}:{version}"
        return StoredBundle(
            Path(f"/postgresql/source-bundles/{expected_sha256}"),
            manifest,
            len(archive),
        )

    def _read(
        self, sha256: str, *, include_archive: bool
    ) -> tuple[SourceBundleManifest, bytes]:
        _validate_digest(sha256, SourceBundleCode.DIGEST_INVALID)
        from .models import RecipeSourceBundle, SourceBundleArchive

        with self._sessions() as session:
            row = session.execute(
                select(
                    RecipeSourceBundle.manifest,
                    RecipeSourceBundle.storage_key,
                    literal_column("source_bundle_archives.xmin::text"),
                    SourceBundleArchive.archive
                    if include_archive
                    else literal_column("NULL"),
                )
                .join(SourceBundleArchive)
                .where(RecipeSourceBundle.sha256 == sha256)
            ).one_or_none()
        if row is None or row[1] != f"postgres:{sha256}:{row[2]}":
            raise _verification_pending()
        try:
            manifest = parse_source_bundle_manifest(row[0])
        except SourceBundleError as error:
            raise _verification_pending() from error
        if manifest.sha256 != sha256:
            raise _verification_pending()
        return manifest, row[3] if include_archive else b""

    def verified_manifest(self, sha256: str) -> SourceBundleManifest:
        manifest, _ = self._read(sha256, include_archive=False)
        return manifest

    def get(self, sha256: str) -> GeneratedSourceBundle:
        manifest, archive = self._read(sha256, include_archive=True)
        return _generated_bundle(archive, manifest, self._limits)


def _verification_pending() -> SourceBundleUnknown:
    return SourceBundleUnknown(
        SourceBundleCode.NOT_FOUND,
        "Source bundle verification is pending in the Controller worker; "
        "the verified ingress receipt is missing or the local copy changed. "
        "The worker restores the exact source from catalog closure on its next pass; retry preview.",
    )


def parse_source_bundle_manifest(value: object) -> SourceBundleManifest:
    try:
        return SourceBundleManifest.model_validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise SourceBundleError(
            SourceBundleCode.MANIFEST_INVALID, "stored source manifest is invalid"
        ) from error


def _validate_digest(value: str, code: str) -> None:
    if (
        len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
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
                raise SourceBundleUnknown(
                    SourceBundleCode.READ_FAILED, "source bundle file cannot be read"
                )
            files[_safe_path(member.name)] = stream.read(limits.max_file_bytes + 1)
    return GeneratedSourceBundle(MappingProxyType(files), archive, manifest)


def _read_archive(payload: IO[bytes], limits: BundleLimits) -> bytes:
    archive = payload.read(limits.max_archive_bytes + 1)
    if not isinstance(archive, bytes):
        raise SourceBundleError(
            SourceBundleCode.READ_FAILED, "source bundle is not binary"
        )
    if not archive:
        raise SourceBundleInvalid(SourceBundleCode.EMPTY, "source bundle is empty")
    if len(archive) > limits.max_archive_bytes:
        raise SourceBundleError(
            SourceBundleCode.ARCHIVE_TOO_LARGE, "source bundle is too large"
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
            SourceBundleCode.INVALID_ARCHIVE, "source bundle is invalid"
        ) from error
    with bundle:
        for member in bundle:
            path = _safe_path(member.name)
            if path in seen:
                raise SourceBundleError(
                    SourceBundleCode.DUPLICATE_PATH,
                    "source bundle contains a duplicate path",
                )
            seen.add(path)
            if member.isdir():
                continue
            if not member.isfile():
                raise SourceBundleError(
                    SourceBundleCode.ENTRY_FORBIDDEN,
                    "source bundle may contain only directories and regular files",
                )
            if len(files) >= limits.max_files:
                raise SourceBundleError(
                    SourceBundleCode.TOO_MANY_FILES,
                    "source bundle contains too many files",
                )
            if member.size < 0 or member.size > limits.max_file_bytes:
                raise SourceBundleError(
                    SourceBundleCode.FILE_TOO_LARGE, "source bundle file is too large"
                )
            total += member.size
            if total > limits.max_total_bytes:
                raise SourceBundleError(
                    SourceBundleCode.EXPANDED_TOO_LARGE,
                    "expanded source bundle is too large",
                )
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise SourceBundleError(
                    SourceBundleCode.READ_FAILED, "source bundle file cannot be read"
                )
            content = extracted.read(limits.max_file_bytes + 1)
            if len(content) != member.size:
                raise SourceBundleError(
                    SourceBundleCode.SIZE_MISMATCH,
                    "source bundle file size is inconsistent",
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
        schema_version=1,
        files=tuple(files),
        total_bytes=total,
    )
    return SourceBundleManifest.model_validate_json(
        canonical_message(
            identity.model_dump(mode="json") | {"sha256": identity.digest()}
        )
    )


def _safe_path(value: str) -> str:
    if not value or "\x00" in value or value.startswith("/"):
        raise SourceBundleError(
            SourceBundleCode.PATH_FORBIDDEN, "source bundle path is forbidden"
        )
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SourceBundleError(
            SourceBundleCode.PATH_FORBIDDEN, "source bundle path is forbidden"
        )
    normalized = path.as_posix()
    if len(normalized.encode("utf-8")) > 512:
        raise SourceBundleError(
            SourceBundleCode.PATH_TOO_LONG, "source bundle path is too long"
        )
    return normalized
