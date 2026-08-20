"""Fixed, generation-bound backup boundary for control-host upgrades."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Self

from .generation_launch import (
    GenerationReleaseIdentity,
    SelectionRuntime,
    selected_compose_environment,
)
from .host_commands import (
    ArtifactPolicy,
    BoundedCommandRunner,
    CommandPolicy,
    HostCommandError,
)
from .host_state import SelectedGeneration

_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_RECIPIENTS_BYTES = 64 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
}


class BackupError(RuntimeError):
    """Backup inputs, artifacts, or fixed commands failed validation."""


@dataclass(frozen=True)
class BackupSource:
    """One root-owned source tree included under a fixed archive prefix."""

    archive_prefix: str
    path: Path

    def __post_init__(self) -> None:
        prefix = PurePosixPath(self.archive_prefix)
        path = Path(self.path)
        if (
            not self.archive_prefix
            or prefix.is_absolute()
            or any(part in {"", ".", ".."} for part in prefix.parts)
            or not path.is_absolute()
        ):
            raise ValueError("backup source is invalid")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True)
class BackupReceipt:
    schema_version: int
    operation_id: str
    generation_id: str
    generation_receipt_sha256: str
    relative_path: str
    byte_count: int
    sha256: str
    archive_manifest_sha256: str
    recipients_sha256: str

    def __post_init__(self) -> None:
        expected_path = f"backups/{self.operation_id}.age"
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or _IDENTIFIER.fullmatch(self.operation_id) is None
            or _IDENTIFIER.fullmatch(self.generation_id) is None
            or self.relative_path != expected_path
            or isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 1
            or any(
                not isinstance(value, str) or _SHA256.fullmatch(value) is None
                for value in (
                    self.generation_receipt_sha256,
                    self.sha256,
                    self.archive_manifest_sha256,
                    self.recipients_sha256,
                )
            )
        ):
            raise ValueError("backup receipt is invalid")

    def document(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RestoreReceipt:
    schema_version: int
    operation_id: str
    backup_operation_id: str
    backup_sha256: str
    backup_byte_count: int
    generation_id: str
    generation_receipt_sha256: str
    database_revision: str
    archive_manifest_sha256: str
    identity_sha256: str
    site_state_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or _IDENTIFIER.fullmatch(self.operation_id) is None
            or _IDENTIFIER.fullmatch(self.backup_operation_id) is None
            or _IDENTIFIER.fullmatch(self.generation_id) is None
            or _IDENTIFIER.fullmatch(self.database_revision) is None
            or isinstance(self.backup_byte_count, bool)
            or not isinstance(self.backup_byte_count, int)
            or self.backup_byte_count < 1
            or any(
                not isinstance(value, str) or _SHA256.fullmatch(value) is None
                for value in (
                    self.backup_sha256,
                    self.generation_receipt_sha256,
                    self.archive_manifest_sha256,
                    self.identity_sha256,
                    self.site_state_sha256,
                )
            )
        ):
            raise ValueError("restore receipt is invalid")

    def document(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class VerifiedBackup:
    """A verified backup held open so restore cannot race its pathname."""

    path: Path
    byte_count: int
    sha256: str
    receipt: BackupReceipt
    _descriptor: int

    def fileno(self) -> int:
        if self._descriptor < 0:
            raise ValueError("verified backup is closed")
        return self._descriptor

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def __enter__(self) -> Self:
        self.fileno()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _ArchiveFile:
    name: str
    path: Path
    identity: _FileIdentity
    sha256: str


def _identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _trusted_owner(metadata: os.stat_result) -> bool:
    return metadata.st_uid in {0, os.geteuid()}


def _validate_private_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise BackupError(f"{label} is missing or unsafe") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or not _trusted_owner(metadata)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BackupError(f"{label} is missing or unsafe")


def _ensure_private_directory(path: Path, label: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise BackupError(f"{label} is unsafe") from error
    _validate_private_directory(path, label)


def _open_regular(
    path: Path,
    *,
    label: str,
    exact_mode: int | None = None,
    maximum: int | None = None,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise BackupError(f"{label} is missing, a symlink, or unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not _trusted_owner(metadata)
            or (exact_mode is not None and mode != exact_mode)
            or (exact_mode is None and mode & 0o022 != 0)
            or (maximum is not None and not 1 <= metadata.st_size <= maximum)
        ):
            raise BackupError(f"{label} is unsafe")
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    count = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        count += len(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return count, digest.hexdigest()


def _canonical(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_receipt(path: Path, receipt: BackupReceipt) -> None:
    content = _canonical(receipt.document())
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o400,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("backup receipt write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_restore_receipt(path: Path, receipt: RestoreReceipt) -> None:
    content = _canonical(receipt.document())
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o400,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("restore receipt write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_receipt(raw: bytes) -> BackupReceipt:
    class DuplicateField(ValueError):
        pass

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for name, value in pairs:
            if name in document:
                raise DuplicateField(name)
            document[name] = value
        return document

    try:
        document = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        if not isinstance(document, dict) or raw != _canonical(document):
            raise ValueError("noncanonical")
        expected = {
            "schema_version",
            "operation_id",
            "generation_id",
            "generation_receipt_sha256",
            "relative_path",
            "byte_count",
            "sha256",
            "archive_manifest_sha256",
            "recipients_sha256",
        }
        if set(document) != expected:
            raise ValueError("fields")
        return BackupReceipt(**document)  # type: ignore[arg-type]
    except (DuplicateField, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BackupError("backup receipt sidecar is not canonical") from error


def _parse_restore_receipt(raw: bytes) -> RestoreReceipt:
    try:
        document = json.loads(raw)
        if (
            not isinstance(document, dict)
            or raw != _canonical(document)
            or set(document)
            != {
                "schema_version",
                "operation_id",
                "backup_operation_id",
                "backup_sha256",
                "backup_byte_count",
                "generation_id",
                "generation_receipt_sha256",
                "database_revision",
                "archive_manifest_sha256",
                "identity_sha256",
                "site_state_sha256",
            }
        ):
            raise ValueError("noncanonical")
        return RestoreReceipt(**document)  # type: ignore[arg-type]
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise BackupError("restore receipt sidecar is not canonical") from error


def _validate_archive_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(name.encode("utf-8")) > 240
    ):
        raise BackupError("backup archive name is unsafe")


def _iter_source_files(source: BackupSource) -> Iterator[tuple[str, Path]]:
    try:
        metadata = source.path.lstat()
    except OSError as error:
        raise BackupError("backup source is missing or unsafe") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise BackupError("backup source is a symlink")
    if stat.S_ISREG(metadata.st_mode):
        yield source.archive_prefix, source.path
        return
    if not stat.S_ISDIR(metadata.st_mode) or not _trusted_owner(metadata):
        raise BackupError("backup source is unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BackupError("backup source directory is writable by an untrusted user")
    try:
        children = sorted(source.path.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise BackupError("backup source is unreadable") from error
    for child in children:
        try:
            child_metadata = child.lstat()
        except OSError as error:
            raise BackupError("backup source changed during traversal") from error
        if stat.S_ISLNK(child_metadata.st_mode):
            raise BackupError("backup source contains a symlink")
        relative = child.relative_to(source.path).as_posix()
        nested = BackupSource(f"{source.archive_prefix}/{relative}", child)
        if stat.S_ISDIR(child_metadata.st_mode):
            yield from _iter_source_files(nested)
        elif stat.S_ISREG(child_metadata.st_mode):
            yield nested.archive_prefix, child
        else:
            raise BackupError("backup source contains a non-regular entry")


class _CheckedReader:
    def __init__(self, file: BinaryIO) -> None:
        self._file = file
        self._digest = hashlib.sha256()
        self._count = 0

    def read(self, size: int = -1) -> bytes:
        content = self._file.read(size)
        self._digest.update(content)
        self._count += len(content)
        return content

    @property
    def result(self) -> tuple[int, str]:
        return self._count, self._digest.hexdigest()


class _BoundedWriter:
    def __init__(self, file: BinaryIO, limit: int) -> None:
        self._file = file
        self._limit = limit
        self._count = 0

    def write(self, content: bytes) -> int:
        if self._count + len(content) > self._limit:
            raise BackupError("backup archive exceeded its byte limit")
        written = self._file.write(content)
        self._count += written
        return written

    def tell(self) -> int:
        return self._file.tell()

    def flush(self) -> None:
        self._file.flush()


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


class HostBackupBoundary:
    """Create and verify backups without caller-supplied commands or output paths."""

    def __init__(
        self,
        *,
        state_root: Path,
        recipients_file: Path,
        identity_file: Path | None = None,
        site_sources: tuple[BackupSource, ...] = (),
        compose_environment: Mapping[str, str] | None = None,
        control_identity_root: Path | None = None,
        runner: BoundedCommandRunner | None = None,
        command_policy: CommandPolicy | None = None,
        artifact_policy: ArtifactPolicy | None = None,
    ) -> None:
        self._state_root = Path(state_root)
        self._recipients_file = Path(recipients_file)
        self._identity_file = None if identity_file is None else Path(identity_file)
        self._site_sources = tuple(site_sources)
        self._compose_environment = dict(compose_environment or {})
        self._control_identity_root = (
            None if control_identity_root is None else Path(control_identity_root)
        )
        self._runner = runner or BoundedCommandRunner()
        self._command = command_policy or CommandPolicy(600, 0, 1024 * 1024)
        self._probe_command = CommandPolicy(30, 256, 4096)
        self._artifact = artifact_policy or ArtifactPolicy(64 * 1024**3, 1024**3)
        if (
            not self._state_root.is_absolute()
            or not self._recipients_file.is_absolute()
            or (
                self._identity_file is not None
                and not self._identity_file.is_absolute()
            )
        ):
            raise ValueError("backup roots must be absolute")
        if any(not isinstance(source, BackupSource) for source in self._site_sources):
            raise TypeError("site backup sources are invalid")
        if any(
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,126}", name)
            or not isinstance(value, str)
            or "\x00" in value
            for name, value in self._compose_environment.items()
        ):
            raise ValueError("Compose environment is invalid")
        prefixes = [source.archive_prefix for source in self._site_sources]
        if len(prefixes) != len(set(prefixes)) or any(
            prefix == reserved or prefix.startswith(reserved + "/")
            for prefix in prefixes
            for reserved in ("database.dump", "generation", "manifest.json")
        ):
            raise ValueError("site backup source prefixes must be unique")
        for index, prefix in enumerate(prefixes):
            if any(
                other.startswith(prefix + "/") or prefix.startswith(other + "/")
                for other in prefixes[index + 1 :]
            ):
                raise ValueError("site backup source prefixes must not overlap")

    def _compose_arguments(self, compose: Path) -> tuple[str, ...]:
        return ("--file", str(compose))

    def _compose_env(self, generation: SelectedGeneration) -> dict[str, str]:
        try:
            identity = GenerationReleaseIdentity(
                generation_id=generation.generation_id,
                database_revision=generation.database_revision,
                platform_version=generation.platform_version,
                release_digest=generation.release_digest,
                build_digest=generation.build_digest,
                api_image=generation.api_image,
                worker_image=generation.worker_image,
            )
            environment = selected_compose_environment(
                identity,
                SelectionRuntime.selected("0" * 64),
            )
        except ValueError as error:
            raise BackupError("selected generation identity is invalid") from error
        environment.update(self._compose_environment)
        if self._control_identity_root is not None:
            environment["CONTROL_IDENTITY_PATH"] = str(self._control_identity_root)
        return {**_ENVIRONMENT, **environment}

    def create_upgrade_backup(
        self,
        generation: SelectedGeneration,
        operation_id: str,
    ) -> BackupReceipt:
        if not isinstance(generation, SelectedGeneration):
            raise BackupError("selected generation is invalid")
        if _IDENTIFIER.fullmatch(operation_id) is None:
            raise BackupError("backup operation ID is invalid")
        _validate_private_directory(self._state_root, "control-host state root")
        generation_root, compose_file = self._validate_selected_generation(generation)

        recipients_fd, recipients_stat = _open_regular(
            self._recipients_file,
            label="backup recipients file",
            exact_mode=0o400,
            maximum=_MAX_RECIPIENTS_BYTES,
        )
        try:
            recipients_size, recipients_sha256 = _hash_descriptor(recipients_fd)
            if recipients_size != recipients_stat.st_size or _identity(
                os.fstat(recipients_fd)
            ) != _identity(recipients_stat):
                raise BackupError("backup recipients file changed while being read")
        finally:
            os.close(recipients_fd)

        backups = self._state_root / "backups"
        _ensure_private_directory(backups, "backup directory")
        final = backups / f"{operation_id}.age"
        final_sidecar = backups / f"{operation_id}.receipt.json"
        self._prepare_backup_retry(operation_id)
        recovered = self._recover_pending_backup_publication(
            operation_id,
            generation,
            recipients_sha256,
        )
        if recovered is not None:
            return recovered
        existing = self.probe(operation_id)
        if existing is not None:
            if (
                existing.generation_id != generation.generation_id
                or existing.generation_receipt_sha256
                != generation.generation_receipt_sha256
                or existing.recipients_sha256 != recipients_sha256
            ):
                raise BackupError("existing backup does not match exact inputs")
            stale_staging = backups / f".{operation_id}.staging"
            if stale_staging.exists() or stale_staging.is_symlink():
                _validate_private_directory(stale_staging, "stale backup staging")
                self._clear_pending_backup_staging(stale_staging)
            return existing
        staging = backups / f".{operation_id}.staging"
        try:
            staging.mkdir(mode=0o700)
        except OSError as error:
            raise BackupError(
                "backup operation staging already exists or is unsafe"
            ) from error

        database = staging / "database.dump"
        archive = staging / "archive.tar"
        encrypted = staging / "encrypted.partial"
        recipients_snapshot = staging / "recipients.txt"
        receipt_staging = staging / "receipt.json"
        try:
            self._copy_recipients(recipients_snapshot, recipients_sha256)
            database_fd = os.open(
                database,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                self._runner.stream(
                    (
                        "/usr/bin/docker",
                        "compose",
                        *self._compose_arguments(compose_file),
                        "exec",
                        "--no-TTY",
                        "postgres",
                        "pg_dump",
                        "--format=custom",
                        "--username=control",
                        "--dbname=control",
                    ),
                    cwd=generation_root,
                    env=self._compose_env(generation),
                    source_fd=None,
                    sink_fd=database_fd,
                    command=self._command,
                    artifact=self._artifact,
                )
            finally:
                os.close(database_fd)

            archive_files = self._inventory(
                generation_root=generation_root,
                database=database,
            )
            manifest = {
                "files": {item.name: item.sha256 for item in archive_files},
                "format": "vonk-control-backup-v2",
                "generation_id": generation.generation_id,
                "operation_id": operation_id,
            }
            manifest_raw = _canonical(manifest)
            manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
            self._write_archive(archive, archive_files, manifest_raw)

            archive_fd = os.open(archive, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            encrypted_fd = os.open(
                encrypted,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                artifact_receipt = self._runner.stream(
                    (
                        "/usr/bin/age",
                        "--encrypt",
                        "--recipients-file",
                        str(recipients_snapshot),
                    ),
                    cwd=generation_root,
                    env=_ENVIRONMENT,
                    source_fd=archive_fd,
                    sink_fd=encrypted_fd,
                    command=self._command,
                    artifact=self._artifact,
                )
                actual_size, actual_sha256 = _hash_descriptor(encrypted_fd)
                if (
                    actual_size != artifact_receipt.byte_count
                    or actual_sha256 != artifact_receipt.sha256
                ):
                    raise BackupError("encrypted backup receipt is inconsistent")
            finally:
                os.close(encrypted_fd)
                os.close(archive_fd)
            result = BackupReceipt(
                schema_version=1,
                operation_id=operation_id,
                generation_id=generation.generation_id,
                generation_receipt_sha256=generation.generation_receipt_sha256,
                relative_path=f"backups/{operation_id}.age",
                byte_count=artifact_receipt.byte_count,
                sha256=artifact_receipt.sha256,
                archive_manifest_sha256=manifest_sha256,
                recipients_sha256=recipients_sha256,
            )
            _write_new_receipt(receipt_staging, result)
            _fsync_directory(staging)
            os.rename(receipt_staging, final_sidecar)
            _fsync_directory(backups)
            os.rename(encrypted, final)
            _fsync_directory(backups)
        except (BackupError, HostCommandError, OSError, tarfile.TarError) as error:
            if final.exists() and final.is_file() and not final.is_symlink():
                final.unlink()
            if (
                final_sidecar.exists()
                and final_sidecar.is_file()
                and not final_sidecar.is_symlink()
            ):
                final_sidecar.unlink()
            if isinstance(error, BackupError):
                raise
            if isinstance(error, HostCommandError):
                raise BackupError("backup command failed") from error
            raise BackupError("backup creation failed") from error
        finally:
            preserve_receipt_first = (
                (final_sidecar.exists() or final_sidecar.is_symlink())
                and not final.exists()
                and not final.is_symlink()
            )
            if not preserve_receipt_first:
                for path in (
                    receipt_staging,
                    encrypted,
                    archive,
                    database,
                    recipients_snapshot,
                ):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                try:
                    staging.rmdir()
                except FileNotFoundError:
                    pass
        return result

    def probe(self, operation_id: str) -> BackupReceipt | None:
        if _IDENTIFIER.fullmatch(operation_id) is None:
            raise BackupError("backup operation ID is invalid")
        _validate_private_directory(self._state_root, "control-host state root")
        backups = self._state_root / "backups"
        if not backups.exists() and not backups.is_symlink():
            return None
        _validate_private_directory(backups, "backup directory")
        artifact = backups / f"{operation_id}.age"
        artifact_exists = artifact.exists() or artifact.is_symlink()
        receipt = self._load_backup_sidecar(operation_id)
        sidecar_exists = receipt is not None
        if not artifact_exists and not sidecar_exists:
            return None
        if not artifact_exists or not sidecar_exists:
            raise BackupError("backup publication is not complete")
        assert receipt is not None
        if receipt.operation_id != operation_id:
            raise BackupError("backup receipt operation binding is invalid")
        with self._verify_artifact(receipt):
            pass
        return receipt

    def _load_backup_sidecar(self, operation_id: str) -> BackupReceipt | None:
        sidecar = self._state_root / "backups" / f"{operation_id}.receipt.json"
        if not sidecar.exists() and not sidecar.is_symlink():
            return None
        descriptor, before = _open_regular(
            sidecar,
            label="backup receipt sidecar",
            exact_mode=0o400,
            maximum=_MAX_RECEIPT_BYTES,
        )
        try:
            raw = bytearray()
            while True:
                chunk = os.read(descriptor, _MAX_RECEIPT_BYTES + 1 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > _MAX_RECEIPT_BYTES:
                    raise BackupError("backup receipt sidecar is too large")
            if _identity(os.fstat(descriptor)) != _identity(before):
                raise BackupError("backup receipt sidecar changed while being read")
        finally:
            os.close(descriptor)
        return _parse_receipt(bytes(raw))

    def _recover_pending_backup_publication(
        self,
        operation_id: str,
        generation: SelectedGeneration,
        recipients_sha256: str,
    ) -> BackupReceipt | None:
        backups = self._state_root / "backups"
        artifact = backups / f"{operation_id}.age"
        receipt = self._load_backup_sidecar(operation_id)
        if receipt is None or artifact.exists() or artifact.is_symlink():
            return None
        if (
            receipt.operation_id != operation_id
            or receipt.generation_id != generation.generation_id
            or receipt.generation_receipt_sha256 != generation.generation_receipt_sha256
            or receipt.recipients_sha256 != recipients_sha256
        ):
            raise BackupError("pending backup does not match exact inputs")
        staging = backups / f".{operation_id}.staging"
        if not staging.exists() and not staging.is_symlink():
            sidecar = backups / f"{operation_id}.receipt.json"
            sidecar.unlink()
            _fsync_directory(backups)
            return None
        _validate_private_directory(staging, "pending backup staging")
        encrypted = staging / "encrypted.partial"
        descriptor, before = _open_regular(
            encrypted,
            label="pending encrypted backup",
            exact_mode=0o600,
        )
        try:
            byte_count, sha256 = _hash_descriptor(descriptor)
            if (
                _identity(os.fstat(descriptor)) != _identity(before)
                or byte_count != receipt.byte_count
                or sha256 != receipt.sha256
            ):
                raise BackupError("pending encrypted backup receipt is invalid")
        finally:
            os.close(descriptor)
        os.rename(encrypted, artifact)
        _fsync_directory(backups)
        self._clear_pending_backup_staging(staging)
        return receipt

    def _prepare_backup_retry(self, operation_id: str) -> None:
        backups = self._state_root / "backups"
        artifact = backups / f"{operation_id}.age"
        receipt = self._load_backup_sidecar(operation_id)
        artifact_exists = artifact.exists() or artifact.is_symlink()
        sidecar_exists = receipt is not None
        staging = backups / f".{operation_id}.staging"
        staging_exists = staging.exists() or staging.is_symlink()
        if artifact_exists and not sidecar_exists:
            descriptor, _ = _open_regular(
                artifact,
                label="orphan encrypted backup",
                exact_mode=0o600,
            )
            os.close(descriptor)
            artifact.unlink()
            _fsync_directory(backups)
            artifact_exists = False
        if not artifact_exists and not sidecar_exists and staging_exists:
            _validate_private_directory(staging, "interrupted backup staging")
            self._clear_pending_backup_staging(staging)

    def _clear_pending_backup_staging(self, staging: Path) -> None:
        allowed = {
            "archive.tar",
            "database.dump",
            "encrypted.partial",
            "receipt.json",
            "recipients.txt",
        }
        for child in staging.iterdir():
            if child.name not in allowed or child.is_dir() or child.is_symlink():
                raise BackupError("pending backup staging contains an unsafe entry")
            child.unlink()
        staging.rmdir()

    def load_exact(
        self,
        generation: SelectedGeneration,
        operation_id: str,
    ) -> BackupReceipt:
        if not isinstance(generation, SelectedGeneration):
            raise BackupError("selected generation is invalid")
        self._validate_selected_generation(generation)
        receipt = self.probe(operation_id)
        if receipt is None:
            raise BackupError("backup publication is not complete")
        recipients_fd, recipients_stat = _open_regular(
            self._recipients_file,
            label="backup recipients file",
            exact_mode=0o400,
            maximum=_MAX_RECIPIENTS_BYTES,
        )
        try:
            recipients_size, recipients_sha256 = _hash_descriptor(recipients_fd)
            if recipients_size != recipients_stat.st_size or _identity(
                os.fstat(recipients_fd)
            ) != _identity(recipients_stat):
                raise BackupError("backup recipients file changed while being read")
        finally:
            os.close(recipients_fd)
        if (
            generation.projection_kind != "active"
            or receipt.generation_id != generation.generation_id
            or receipt.generation_receipt_sha256 != generation.generation_receipt_sha256
            or receipt.recipients_sha256 != recipients_sha256
        ):
            raise BackupError("existing backup does not match exact inputs")
        return receipt

    def verify_for_restore(self, receipt: BackupReceipt) -> VerifiedBackup:
        if not isinstance(receipt, BackupReceipt):
            raise BackupError("backup receipt is invalid")
        expected = f"backups/{receipt.operation_id}.age"
        if receipt.relative_path != expected:
            raise BackupError("backup receipt path binding is invalid")
        _validate_private_directory(self._state_root, "control-host state root")
        backups = self._state_root / "backups"
        _validate_private_directory(backups, "backup directory")
        sidecar = self.probe(receipt.operation_id)
        if sidecar != receipt:
            raise BackupError("backup receipt sidecar binding is invalid")
        return self._verify_artifact(receipt)

    def _verify_artifact(self, receipt: BackupReceipt) -> VerifiedBackup:
        path = self._state_root / receipt.relative_path
        descriptor, before = _open_regular(
            path,
            label="backup receipt artifact",
            exact_mode=0o600,
        )
        try:
            byte_count, sha256 = _hash_descriptor(descriptor)
            after = os.fstat(descriptor)
            if (
                _identity(before) != _identity(after)
                or byte_count != receipt.byte_count
                or sha256 != receipt.sha256
                or receipt.generation_receipt_sha256 == ""
            ):
                raise BackupError("backup receipt verification failed")
            return VerifiedBackup(path, byte_count, sha256, receipt, descriptor)
        except Exception:
            os.close(descriptor)
            raise

    def restore_for_compensation(
        self,
        verified: VerifiedBackup,
        generation: SelectedGeneration,
        operation_id: str,
    ) -> RestoreReceipt:
        if not isinstance(verified, VerifiedBackup) or not isinstance(
            generation, SelectedGeneration
        ):
            raise BackupError("restore inputs are invalid")
        if _IDENTIFIER.fullmatch(operation_id) is None:
            raise BackupError("restore operation ID is invalid")
        self._validate_selected_generation(generation)
        descriptor = verified.fileno()
        before = os.fstat(descriptor)
        byte_count, backup_sha256 = _hash_descriptor(descriptor)
        if (
            _identity(before) != _identity(os.fstat(descriptor))
            or byte_count != verified.byte_count
            or backup_sha256 != verified.sha256
            or verified.receipt.sha256 != verified.sha256
            or self._load_backup_sidecar(verified.receipt.operation_id)
            != verified.receipt
        ):
            raise BackupError("verified backup changed before restore")
        if (
            generation.projection_kind != "active"
            or generation.generation_id != verified.receipt.generation_id
            or generation.generation_receipt_sha256
            != verified.receipt.generation_receipt_sha256
        ):
            raise BackupError("restore generation does not match backup")
        identity_sha256 = self._identity_digest()
        existing = self.probe_restore(operation_id)
        if existing is not None:
            exact = self._require_restore_exact(
                existing,
                verified.receipt,
                generation,
                operation_id,
                identity_sha256,
            )
            if self._restore_state_is_exact(exact, generation):
                stale = (
                    self._state_root / "backups" / f".restore-{operation_id}.staging"
                )
                if stale.exists() or stale.is_symlink():
                    _validate_private_directory(stale, "stale restore staging")
                    self._clear_restore_staging(stale)
                return exact
            receipt_path = self._state_root / "backups" / f"{operation_id}.restore.json"
            receipt_path.unlink()
            _fsync_directory(receipt_path.parent)

        backups = self._state_root / "backups"
        _validate_private_directory(backups, "backup directory")
        staging = backups / f".restore-{operation_id}.staging"
        if staging.exists() or staging.is_symlink():
            _validate_private_directory(staging, "interrupted restore staging")
            self._clear_restore_staging(staging)
        try:
            staging.mkdir(mode=0o700)
        except OSError as error:
            raise BackupError("restore staging already exists or is unsafe") from error
        identity_snapshot = staging / "identity.txt"
        decrypted = staging / "archive.tar"
        restore_output = staging / "restore-output"
        receipt_staging = staging / "receipt.json"
        final_receipt = backups / f"{operation_id}.restore.json"
        extracted = staging / "extracted"
        try:
            self._copy_identity(identity_snapshot, identity_sha256)
            decrypted_fd = os.open(
                decrypted,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                self._runner.stream(
                    (
                        "/usr/bin/age",
                        "--decrypt",
                        "--identity",
                        str(identity_snapshot),
                    ),
                    cwd=self._state_root,
                    env=_ENVIRONMENT,
                    source_fd=descriptor,
                    sink_fd=decrypted_fd,
                    command=self._command,
                    artifact=self._artifact,
                )
            finally:
                os.close(decrypted_fd)
            files, site_state_sha256 = self._extract_restore_archive(
                decrypted,
                extracted,
                verified.receipt,
            )
            database = files.get("database.dump")
            if database is None:
                raise BackupError("backup database dump is missing")
            generation_root = (
                self._state_root / "generations" / generation.generation_id
            )
            _validate_private_directory(generation_root, "restore generation")
            compose = generation_root / "compose.yaml"
            compose_fd, _ = _open_regular(
                compose,
                label="restore generation Compose file",
                maximum=4 * 1024 * 1024,
            )
            os.close(compose_fd)
            database_fd, _ = _open_regular(
                database,
                label="verified database dump",
                maximum=self._artifact.byte_limit,
            )
            output_fd = os.open(
                restore_output,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                self._runner.stream(
                    (
                        "/usr/bin/docker",
                        "compose",
                        *self._compose_arguments(compose),
                        "exec",
                        "--no-TTY",
                        "postgres",
                        "pg_restore",
                        "--clean",
                        "--if-exists",
                        "--exit-on-error",
                        "--single-transaction",
                        "--username=control",
                        "--dbname=control",
                    ),
                    cwd=generation_root,
                    env=self._compose_env(generation),
                    source_fd=database_fd,
                    sink_fd=output_fd,
                    command=self._command,
                    artifact=self._artifact,
                )
            finally:
                os.close(output_fd)
                os.close(database_fd)
            self._restore_site_files(files, operation_id)
            if (
                self._current_site_state_sha256() != site_state_sha256
                or self._current_database_revision(generation)
                != generation.database_revision
            ):
                raise BackupError("restored site or database state is not exact")
            result = RestoreReceipt(
                schema_version=1,
                operation_id=operation_id,
                backup_operation_id=verified.receipt.operation_id,
                backup_sha256=verified.receipt.sha256,
                backup_byte_count=verified.receipt.byte_count,
                generation_id=generation.generation_id,
                generation_receipt_sha256=generation.generation_receipt_sha256,
                database_revision=generation.database_revision,
                archive_manifest_sha256=verified.receipt.archive_manifest_sha256,
                identity_sha256=identity_sha256,
                site_state_sha256=site_state_sha256,
            )
            _write_new_restore_receipt(receipt_staging, result)
            os.rename(receipt_staging, final_receipt)
            _fsync_directory(backups)
        except (BackupError, HostCommandError, OSError, tarfile.TarError) as error:
            if (
                final_receipt.exists()
                and final_receipt.is_file()
                and not final_receipt.is_symlink()
            ):
                final_receipt.unlink()
            if isinstance(error, BackupError):
                raise
            if isinstance(error, HostCommandError):
                raise BackupError("restore command failed") from error
            raise BackupError("compensation restore failed") from error
        finally:
            self._clear_restore_staging(staging)
        return result

    def probe_restore(self, operation_id: str) -> RestoreReceipt | None:
        if _IDENTIFIER.fullmatch(operation_id) is None:
            raise BackupError("restore operation ID is invalid")
        backups = self._state_root / "backups"
        if not backups.exists() and not backups.is_symlink():
            return None
        _validate_private_directory(backups, "backup directory")
        path = backups / f"{operation_id}.restore.json"
        if not path.exists() and not path.is_symlink():
            return None
        descriptor, before = _open_regular(
            path,
            label="restore receipt sidecar",
            exact_mode=0o400,
            maximum=_MAX_RECEIPT_BYTES,
        )
        try:
            raw = os.read(descriptor, _MAX_RECEIPT_BYTES + 1)
            if len(raw) > _MAX_RECEIPT_BYTES or os.read(descriptor, 1):
                raise BackupError("restore receipt sidecar is too large")
            if _identity(os.fstat(descriptor)) != _identity(before):
                raise BackupError("restore receipt changed while being read")
        finally:
            os.close(descriptor)
        receipt = _parse_restore_receipt(raw)
        if receipt.operation_id != operation_id:
            raise BackupError("restore receipt operation binding is invalid")
        return receipt

    def load_restore_exact(
        self,
        backup: BackupReceipt,
        generation: SelectedGeneration,
        operation_id: str,
    ) -> RestoreReceipt:
        if not isinstance(backup, BackupReceipt) or not isinstance(
            generation, SelectedGeneration
        ):
            raise BackupError("restore inputs are invalid")
        self._validate_selected_generation(generation)
        if self.probe(backup.operation_id) != backup:
            raise BackupError("backup publication does not match restore receipt")
        receipt = self.probe_restore(operation_id)
        if receipt is None:
            raise BackupError("restore receipt is not complete")
        exact = self._require_restore_exact(
            receipt,
            backup,
            generation,
            operation_id,
            self._identity_digest(),
        )
        if not self._restore_state_is_exact(exact, generation):
            raise BackupError("restored site or database state is not exact")
        return exact

    def _require_restore_exact(
        self,
        receipt: RestoreReceipt,
        backup: BackupReceipt,
        generation: SelectedGeneration,
        operation_id: str,
        identity_sha256: str,
    ) -> RestoreReceipt:
        if receipt != RestoreReceipt(
            schema_version=1,
            operation_id=operation_id,
            backup_operation_id=backup.operation_id,
            backup_sha256=backup.sha256,
            backup_byte_count=backup.byte_count,
            generation_id=generation.generation_id,
            generation_receipt_sha256=generation.generation_receipt_sha256,
            database_revision=generation.database_revision,
            archive_manifest_sha256=backup.archive_manifest_sha256,
            identity_sha256=identity_sha256,
            site_state_sha256=receipt.site_state_sha256,
        ):
            raise BackupError("existing restore does not match exact inputs")
        return receipt

    def _restore_state_is_exact(
        self,
        receipt: RestoreReceipt,
        generation: SelectedGeneration,
    ) -> bool:
        return (
            self._current_site_state_sha256() == receipt.site_state_sha256
            and self._current_database_revision(generation)
            == generation.database_revision
        )

    def _current_site_state_sha256(self) -> str:
        digests: dict[str, str] = {}
        count = 0
        for source in self._site_sources:
            for name, path in _iter_source_files(source):
                count += 1
                if count > 4096 or name in digests:
                    raise BackupError("site state inventory is invalid")
                descriptor, before = _open_regular(
                    path,
                    label="site state file",
                    maximum=self._artifact.byte_limit,
                )
                try:
                    byte_count, sha256 = _hash_descriptor(descriptor)
                    if byte_count != before.st_size or _identity(
                        os.fstat(descriptor)
                    ) != _identity(before):
                        raise BackupError("site state changed while being read")
                    digests[name] = sha256
                finally:
                    os.close(descriptor)
        return hashlib.sha256(_canonical(digests)).hexdigest()

    def _current_database_revision(self, generation: SelectedGeneration) -> str:
        generation_root, compose = self._validate_selected_generation(generation)
        try:
            result = self._runner.run(
                (
                    "/usr/bin/docker",
                    "compose",
                    *self._compose_arguments(compose),
                    "exec",
                    "--no-TTY",
                    "postgres",
                    "psql",
                    "--username=control",
                    "--dbname=control",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SELECT version_num FROM alembic_version",
                ),
                cwd=generation_root,
                env=self._compose_env(generation),
                policy=self._probe_command,
            )
        except (AttributeError, HostCommandError) as error:
            raise BackupError("database revision probe failed") from error
        try:
            revision = result.stdout.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise BackupError("database revision probe was invalid") from error
        if _IDENTIFIER.fullmatch(revision) is None:
            raise BackupError("database revision probe was invalid")
        return revision

    def _validate_selected_generation(
        self,
        generation: SelectedGeneration,
    ) -> tuple[Path, Path]:
        if not isinstance(generation, SelectedGeneration):
            raise BackupError("selected generation is invalid")
        if generation.projection_kind != "active":
            raise BackupError("selected generation is not active")
        generation_root = self._state_root / "generations" / generation.generation_id
        _validate_private_directory(generation_root, "selected generation")
        receipt_path = generation_root / "generation.json"
        descriptor, before = _open_regular(
            receipt_path,
            label="generation receipt",
            exact_mode=0o400,
            maximum=64 * 1024,
        )
        try:
            byte_count, sha256 = _hash_descriptor(descriptor)
            if (
                byte_count != before.st_size
                or sha256 != generation.generation_receipt_sha256
                or _identity(os.fstat(descriptor)) != _identity(before)
            ):
                raise BackupError("generation receipt binding is invalid")
        finally:
            os.close(descriptor)
        compose = generation_root / "compose.yaml"
        compose_fd, _ = _open_regular(
            compose,
            label="selected generation Compose file",
            maximum=4 * 1024 * 1024,
        )
        os.close(compose_fd)
        return generation_root, compose

    def _copy_recipients(self, destination: Path, expected_sha256: str) -> None:
        source, before = _open_regular(
            self._recipients_file,
            label="backup recipients file",
            exact_mode=0o400,
            maximum=_MAX_RECIPIENTS_BYTES,
        )
        output = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(source, 64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(output, view) :]
            os.fsync(output)
            if _identity(os.fstat(source)) != _identity(before):
                raise BackupError("backup recipients file changed while being copied")
            if digest.hexdigest() != expected_sha256:
                raise BackupError("backup recipients file changed while being copied")
        finally:
            os.close(output)
            os.close(source)
        destination.chmod(0o400)

    def _identity_digest(self) -> str:
        if self._identity_file is None:
            raise BackupError("a fixed root-owned backup identity file is required")
        descriptor, before = _open_regular(
            self._identity_file,
            label="backup identity file",
            exact_mode=0o400,
            maximum=_MAX_RECIPIENTS_BYTES,
        )
        try:
            byte_count, digest = _hash_descriptor(descriptor)
            if byte_count != before.st_size or _identity(
                os.fstat(descriptor)
            ) != _identity(before):
                raise BackupError("backup identity file changed while being read")
            return digest
        finally:
            os.close(descriptor)

    def _copy_identity(self, destination: Path, expected_sha256: str) -> None:
        assert self._identity_file is not None
        source, before = _open_regular(
            self._identity_file,
            label="backup identity file",
            exact_mode=0o400,
            maximum=_MAX_RECIPIENTS_BYTES,
        )
        output = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
        )
        digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(source, 64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(output, view) :]
            os.fsync(output)
            if (
                _identity(os.fstat(source)) != _identity(before)
                or digest.hexdigest() != expected_sha256
            ):
                raise BackupError("backup identity file changed while being copied")
        finally:
            os.close(output)
            os.close(source)

    def _extract_restore_archive(
        self,
        archive_path: Path,
        destination: Path,
        receipt: BackupReceipt,
    ) -> tuple[dict[str, Path], str]:
        destination.mkdir(mode=0o700)
        files: dict[str, Path] = {}
        digests: dict[str, str] = {}
        manifest_raw: bytes | None = None
        total = 0
        try:
            with tarfile.open(archive_path, mode="r:") as archive:
                names: set[str] = set()
                previous = ""
                manifest_seen = False
                member_count = 0
                for member in archive:
                    member_count += 1
                    if member_count > 4096:
                        raise BackupError("backup archive member count is invalid")
                    _validate_archive_name(member.name)
                    if member.name in names:
                        raise BackupError("backup archive contains duplicate members")
                    names.add(member.name)
                    if manifest_seen:
                        raise BackupError("backup archive order is not canonical")
                    if member.name == "manifest.json":
                        manifest_seen = True
                    elif member.name <= previous:
                        raise BackupError("backup archive order is not canonical")
                    else:
                        previous = member.name
                    if (
                        not member.isreg()
                        or member.mode != 0o600
                        or member.mtime != 0
                        or member.uid != 0
                        or member.gid != 0
                        or member.uname
                        or member.gname
                        or member.size < 0
                    ):
                        raise BackupError("backup archive member is unsafe")
                    source = archive.extractfile(member)
                    if source is None:
                        raise BackupError("backup archive member is unreadable")
                    if member.name == "manifest.json":
                        if member.size > _MAX_RECEIPT_BYTES:
                            raise BackupError("backup archive manifest is too large")
                        manifest_raw = source.read(_MAX_RECEIPT_BYTES + 1)
                        if len(manifest_raw) != member.size:
                            raise BackupError("backup archive manifest is truncated")
                        continue
                    total += member.size
                    if total > self._artifact.byte_limit:
                        raise BackupError("backup archive content exceeds its limit")
                    target = destination.joinpath(*PurePosixPath(member.name).parts)
                    self._ensure_restore_parent(destination, target.parent)
                    descriptor = os.open(
                        target,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        0o600,
                    )
                    digest = hashlib.sha256()
                    count = 0
                    try:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                            count += len(chunk)
                            view = memoryview(chunk)
                            while view:
                                view = view[os.write(descriptor, view) :]
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    if count != member.size:
                        raise BackupError("backup archive member is truncated")
                    files[member.name] = target
                    digests[member.name] = digest.hexdigest()
                if member_count < 2:
                    raise BackupError("backup archive member count is invalid")
        except BackupError:
            raise
        except (OSError, tarfile.TarError) as error:
            raise BackupError("backup archive is unreadable") from error
        if manifest_raw is None:
            raise BackupError("backup archive manifest is missing")
        try:
            manifest = json.loads(manifest_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BackupError("backup archive manifest is invalid") from error
        if (
            not isinstance(manifest, dict)
            or manifest_raw != _canonical(manifest)
            or set(manifest) != {"files", "format", "generation_id", "operation_id"}
            or manifest.get("format") != "vonk-control-backup-v2"
            or manifest.get("generation_id") != receipt.generation_id
            or manifest.get("operation_id") != receipt.operation_id
            or manifest.get("files") != digests
            or hashlib.sha256(manifest_raw).hexdigest()
            != receipt.archive_manifest_sha256
        ):
            raise BackupError("backup archive manifest binding is invalid")
        site_digests = {
            name: digest
            for name, digest in digests.items()
            if any(
                name == source.archive_prefix
                or name.startswith(source.archive_prefix + "/")
                for source in self._site_sources
            )
        }
        return files, hashlib.sha256(_canonical(site_digests)).hexdigest()

    def _ensure_restore_parent(self, root: Path, parent: Path) -> None:
        relative = parent.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            _validate_private_directory(current, "restore extraction directory")

    def _restore_site_files(
        self,
        files: Mapping[str, Path],
        operation_id: str,
    ) -> None:
        for source in self._site_sources:
            matching = sorted(
                name
                for name in files
                if name == source.archive_prefix
                or name.startswith(source.archive_prefix + "/")
            )
            for name in matching:
                suffix = name.removeprefix(source.archive_prefix).lstrip("/")
                destination = source.path if not suffix else source.path / suffix
                parent = destination.parent
                _validate_private_directory(parent, "site restore parent")
                if destination.exists() or destination.is_symlink():
                    existing, _ = _open_regular(
                        destination,
                        label="site restore destination",
                    )
                    os.close(existing)
                staged = parent / f".{destination.name}.restore-{operation_id}"
                if staged.exists() or staged.is_symlink():
                    existing_staged, _ = _open_regular(
                        staged,
                        label="interrupted site restore staging",
                        exact_mode=0o600,
                    )
                    os.close(existing_staged)
                    staged.unlink()
                input_fd, _ = _open_regular(
                    files[name],
                    label="verified site restore source",
                    exact_mode=0o600,
                    maximum=self._artifact.byte_limit,
                )
                output_fd = os.open(
                    staged,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    while True:
                        chunk = os.read(input_fd, 1024 * 1024)
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(output_fd, view) :]
                    os.fsync(output_fd)
                finally:
                    os.close(output_fd)
                    os.close(input_fd)
                try:
                    os.replace(staged, destination)
                    _fsync_directory(parent)
                except BaseException:
                    try:
                        staged.unlink()
                    except FileNotFoundError:
                        pass
                    raise
            desired = set(matching)
            for current_name, current_path in list(_iter_source_files(source)):
                if current_name in desired:
                    continue
                descriptor, _ = _open_regular(
                    current_path,
                    label="extra site restore file",
                )
                os.close(descriptor)
                current_path.unlink()
                _fsync_directory(current_path.parent)

    def _clear_restore_staging(self, staging: Path) -> None:
        if not staging.exists() and not staging.is_symlink():
            return
        try:
            paths = sorted(
                staging.rglob("*"),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for path in paths:
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                else:
                    path.unlink()
            staging.rmdir()
        except OSError as error:
            raise BackupError("restore staging cleanup failed") from error

    def _inventory(
        self,
        *,
        generation_root: Path,
        database: Path,
    ) -> tuple[_ArchiveFile, ...]:
        sources = (
            BackupSource("database.dump", database),
            BackupSource("generation", generation_root),
            *self._site_sources,
        )
        paths: list[tuple[str, Path]] = []
        for source in sources:
            paths.extend(_iter_source_files(source))
            if len(paths) > 4096:
                raise BackupError("backup source inventory exceeds its limit")
        paths.sort(key=lambda item: item[0])
        names = [name for name, _ in paths]
        if len(names) != len(set(names)) or "manifest.json" in names:
            raise BackupError("backup source archive names overlap")
        result: list[_ArchiveFile] = []
        for name, path in paths:
            _validate_archive_name(name)
            descriptor, before = _open_regular(
                path,
                label=f"backup source {name}",
                maximum=self._artifact.byte_limit,
            )
            try:
                byte_count, digest = _hash_descriptor(descriptor)
                after = os.fstat(descriptor)
                if byte_count != before.st_size or _identity(before) != _identity(
                    after
                ):
                    raise BackupError("backup source changed while being hashed")
                result.append(_ArchiveFile(name, path, _identity(before), digest))
            finally:
                os.close(descriptor)
        return tuple(result)

    def _write_archive(
        self,
        destination: Path,
        files: tuple[_ArchiveFile, ...],
        manifest_raw: bytes,
    ) -> None:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            filesystem = os.fstatvfs(descriptor)
            available = filesystem.f_bavail * filesystem.f_frsize
            if (
                available
                < self._artifact.byte_limit + self._artifact.required_free_bytes
            ):
                raise BackupError("backup archive disk reservation unavailable")
            with os.fdopen(descriptor, "wb", closefd=False) as raw:
                writer = _BoundedWriter(raw, self._artifact.byte_limit)
                with tarfile.open(
                    fileobj=writer,
                    mode="w",
                    format=tarfile.USTAR_FORMAT,
                ) as archive:
                    for item in files:
                        source, before = _open_regular(
                            item.path,
                            label=f"backup source {item.name}",
                            maximum=self._artifact.byte_limit,
                        )
                        try:
                            if _identity(before) != item.identity:
                                raise BackupError(
                                    "backup source changed before archiving"
                                )
                            with os.fdopen(source, "rb", closefd=False) as input_file:
                                checked = _CheckedReader(input_file)
                                archive.addfile(
                                    _tar_info(item.name, before.st_size),
                                    checked,
                                )
                                if checked.result != (before.st_size, item.sha256):
                                    raise BackupError(
                                        "backup source changed while archiving"
                                    )
                            if _identity(os.fstat(source)) != item.identity:
                                raise BackupError(
                                    "backup source changed while archiving"
                                )
                        finally:
                            os.close(source)
                    archive.addfile(
                        _tar_info("manifest.json", len(manifest_raw)),
                        io.BytesIO(manifest_raw),
                    )
                raw.flush()
                os.fsync(raw.fileno())
        finally:
            os.close(descriptor)
