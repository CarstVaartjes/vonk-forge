"""Typed, content-addressed GPU node release installation boundary."""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from .deadlines import DeadlineBindingError, MonotonicDeadline

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_RELEASE_FIELDS = frozenset(
    {
        "schema_version",
        "target_name",
        "oci_manifest_digest",
        "target_digest",
        "provenance_digest",
        "adapter_id",
    }
)
_DESCRIPTOR_FIELDS = frozenset(
    {
        "schema_version",
        "target_name",
        "target_digest",
        "target_length",
        "registry_origin",
        "repository",
        "oci_manifest_digest",
        "provenance_digest",
        "adapter_id",
        "adapter_version",
        "architecture",
        "agent_min_version",
        "agent_max_version",
        "protocol_min_version",
        "protocol_max_version",
        "members",
    }
)
_MEMBER_FIELDS = frozenset({"path", "sha256", "size", "mode", "uid", "gid"})
_REPOSITORY = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z")
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_STAGING_NAME = re.compile(r"\.install-[0-9a-f]{64}-([0-9a-f]{16})\Z")
_RECOVERY_NAME = re.compile(r"\.recovery-([0-9a-f]{16})\.state\Z")
_RECOVERY_TEMP_NAME = re.compile(r"\.recovery-([0-9a-f]{16})\.new\Z")
_TREE_QUARANTINE_NAME = re.compile(
    r"\.quarantine-tree-([0-9a-f]{16})-[0-9a-f]{16}\Z"
)
_RECOVERY_QUARANTINE_NAME = re.compile(
    r"\.quarantine-recovery-([0-9a-f]{16})-[0-9a-f]{16}\Z"
)
_MAX_DEFERRED_STAGING = 16
_INSTALL_RECOVERY_LOCK_NAME = ".install-recovery.lock"
# One live install can simultaneously own its active reservation, durable
# intent, staging directory, and completion temporary. Reserving all four
# slots before creating any artifact keeps the real aggregate at or below the
# cap through every transaction phase.
_ACTIVE_INSTALL_RECOVERY_SLOTS = 4
_RECOVERY_SECONDS = 0.1


class ReleaseValidationError(ValueError):
    """A release request or signed descriptor is invalid."""


class ReleaseInstallError(RuntimeError):
    error_code = "release_install_failed"


class ReleaseDisposition(StrEnum):
    READY = "ready"
    SAFE_TO_RESUME = "safe-to-resume"
    COMPLETED = "completed"
    OPERATOR_INTERVENTION = "operator-intervention"


@dataclass(frozen=True)
class ReleaseEvidence:
    status: str
    release_digest: str
    manifest_digest: str
    adapter_id: str

    def __post_init__(self) -> None:
        if self.status not in {"installed", "already-installed"}:
            raise ReleaseValidationError("release evidence status is invalid")
        _digest(self.release_digest, "release digest")
        if not _OCI_DIGEST.fullmatch(self.manifest_digest):
            raise ReleaseValidationError("release manifest digest is invalid")
        _token(self.adapter_id, "adapter ID")

    def to_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "release_digest": self.release_digest,
            "manifest_digest": self.manifest_digest,
            "adapter_id": self.adapter_id,
        }


@dataclass(frozen=True)
class ReleaseInspection:
    disposition: ReleaseDisposition
    evidence: ReleaseEvidence | None = None


@dataclass(frozen=True)
class ReleaseMember:
    path: str
    sha256: str
    size: int
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class ReleaseDescriptor:
    schema_version: int
    target_name: str
    target_digest: str
    target_length: int
    registry_origin: str
    repository: str
    oci_manifest_digest: str
    provenance_digest: str
    adapter_id: str
    adapter_version: str
    architecture: str
    agent_min_version: str
    agent_max_version: str
    protocol_min_version: int
    protocol_max_version: int
    members: tuple[ReleaseMember, ...]

    @classmethod
    def parse(cls, document: Mapping[str, Any]) -> ReleaseDescriptor:
        if not isinstance(document, Mapping) or set(document) != _DESCRIPTOR_FIELDS:
            raise ReleaseValidationError("release descriptor fields are invalid")
        if document["schema_version"] != 1 or isinstance(document["schema_version"], bool):
            raise ReleaseValidationError("release descriptor version is invalid")
        target_length = _bounded_int(document["target_length"], 1, 1 << 30, "target length")
        protocol_min = _bounded_int(document["protocol_min_version"], 1, 1, "protocol range")
        protocol_max = _bounded_int(document["protocol_max_version"], 1, 1, "protocol range")
        origin = _https_origin(document["registry_origin"])
        repository = document["repository"]
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise ReleaseValidationError("OCI repository is invalid")
        members = _members(document["members"])
        if target_length != sum(member.size for member in members):
            raise ReleaseValidationError("target length does not match members")
        descriptor = cls(
            1,
            _token(document["target_name"], "target name"),
            _digest(document["target_digest"], "target digest"),
            target_length,
            origin,
            repository,
            _oci_digest(document["oci_manifest_digest"]),
            _digest(document["provenance_digest"], "provenance digest"),
            _token(document["adapter_id"], "adapter ID"),
            _version(document["adapter_version"], "adapter version"),
            _token(document["architecture"], "architecture"),
            _version(document["agent_min_version"], "minimum agent version"),
            _version(document["agent_max_version"], "maximum agent version"),
            protocol_min,
            protocol_max,
            members,
        )
        if len(_receipt_bytes(descriptor)) > 64 * 1024:
            raise ReleaseValidationError("release descriptor receipt is too large")
        return descriptor

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target_name": self.target_name,
            "target_digest": self.target_digest,
            "target_length": self.target_length,
            "registry_origin": self.registry_origin,
            "repository": self.repository,
            "oci_manifest_digest": self.oci_manifest_digest,
            "provenance_digest": self.provenance_digest,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "architecture": self.architecture,
            "agent_min_version": self.agent_min_version,
            "agent_max_version": self.agent_max_version,
            "protocol_min_version": self.protocol_min_version,
            "protocol_max_version": self.protocol_max_version,
            "members": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "size": item.size,
                    "mode": item.mode,
                    "uid": item.uid,
                    "gid": item.gid,
                }
                for item in self.members
            ],
        }

    def agrees_with(self, request: ReleaseRequest) -> bool:
        return (
            self.target_name == request.target_name
            and self.oci_manifest_digest == request.oci_manifest_digest
            and self.target_digest == request.target_digest
            and self.provenance_digest == request.provenance_digest
            and self.adapter_id == request.adapter_id
        )


class ReleaseTrustBoundary(Protocol):
    def authorize(
        self, request: ReleaseRequest, deadline: MonotonicDeadline
    ) -> ReleaseDescriptor: ...


class ReleaseTransportBoundary(Protocol):
    def pull(
        self,
        descriptor: ReleaseDescriptor,
        destination: Path,
        deadline: MonotonicDeadline,
    ) -> None: ...


class ReleaseInstaller:
    def __init__(
        self,
        trust: ReleaseTrustBoundary,
        transport: ReleaseTransportBoundary,
        releases_root: Path,
        staging_root: Path,
    ) -> None:
        self._trust = trust
        self._transport = transport
        self._releases_root = Path(releases_root)
        self._staging_root = Path(staging_root)
        self._deferred_staging: dict[str, tuple[int, int]] = {}
        self._active_staging = 0
        self._recovery_lock = threading.Lock()

    def install(
        self, request: ReleaseRequest, deadline: datetime | MonotonicDeadline
    ) -> ReleaseEvidence:
        fixed_deadline = _bind_deadline(deadline)
        releases_fd = -1
        staging_root_fd = -1
        recovery_lock_fd = -1
        lock_fd = -1
        staging_fd = -1
        staging_name: str | None = None
        staging_identity: tuple[int, int] | None = None
        recovery_name: str | None = None
        recovery_identity: tuple[int, int] | None = None
        staging_reserved = False
        published = False
        _deadline(fixed_deadline)
        descriptor = self._trust.authorize(request, fixed_deadline)
        _deadline(fixed_deadline)
        try:
            _secure_root(self._releases_root, fixed_deadline)
            _secure_root(self._staging_root, fixed_deadline)
        except ReleaseInstallError:
            raise
        except OSError as error:
            raise ReleaseInstallError(
                "release installation setup failed"
            ) from error
        _deadline(fixed_deadline)
        try:
            releases_fd = os.open(
                self._releases_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            _deadline(fixed_deadline)
            staging_root_fd = os.open(
                self._staging_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            _deadline(fixed_deadline)
            releases_metadata = os.fstat(releases_fd)
            _deadline(fixed_deadline)
            staging_root_metadata = os.fstat(staging_root_fd)
            _deadline(fixed_deadline)
            if releases_metadata.st_dev != staging_root_metadata.st_dev:
                raise ReleaseInstallError(
                    "release staging is not on the install filesystem"
                )
            recovery_lock_fd = _open_install_recovery_lock_fd(
                staging_root_fd, fixed_deadline
            )
        except ReleaseInstallError:
            if recovery_lock_fd >= 0:
                os.close(recovery_lock_fd)
                recovery_lock_fd = -1
            if staging_root_fd >= 0:
                os.close(staging_root_fd)
                staging_root_fd = -1
            if releases_fd >= 0:
                os.close(releases_fd)
                releases_fd = -1
            raise
        except OSError as error:
            if recovery_lock_fd >= 0:
                os.close(recovery_lock_fd)
                recovery_lock_fd = -1
            if staging_root_fd >= 0:
                os.close(staging_root_fd)
                staging_root_fd = -1
            if releases_fd >= 0:
                os.close(releases_fd)
                releases_fd = -1
            raise ReleaseInstallError(
                "release installation setup failed"
            ) from error
        try:
            try:
                self._reap_deferred_staging(staging_root_fd)
            except ReleaseInstallError:
                raise
            except (OSError, TimeoutError) as error:
                raise ReleaseInstallError(
                    "release staging recovery failed"
                ) from error
            _deadline(fixed_deadline)
            try:
                lock_fd = os.open(
                    f".install-{request.target_digest}.lock",
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=releases_fd,
                )
                _deadline(fixed_deadline)
                _acquire_lock(lock_fd, fixed_deadline)
            except ReleaseInstallError:
                raise
            except OSError as error:
                raise ReleaseInstallError(
                    "release installation setup failed"
                ) from error
            _deadline(fixed_deadline)
            try:
                os.stat(
                    request.target_digest,
                    dir_fd=releases_fd,
                    follow_symlinks=False,
                )
                _deadline(fixed_deadline)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise ReleaseInstallError(
                    "release installation setup failed"
                ) from error
            else:
                _verify_installed_fd(
                    releases_fd, request.target_digest, descriptor,
                    fixed_deadline,
                )
                return ReleaseEvidence(
                    "already-installed",
                    request.target_digest,
                    request.oci_manifest_digest,
                    request.adapter_id,
                )
            _deadline(fixed_deadline)
            try:
                self._reserve_staging(
                    staging_root_fd, lambda: _deadline(fixed_deadline)
                )
            except ReleaseInstallError:
                raise
            except (OSError, TimeoutError) as error:
                raise ReleaseInstallError(
                    "release staging recovery failed"
                ) from error
            staging_reserved = True
            try:
                staging_token = secrets.token_hex(8)
                _deadline(fixed_deadline)
                staging_name = f".install-{request.target_digest}-{staging_token}"
                recovery_name = f".recovery-{staging_token}.state"
                recovery_identity = _write_recovery_intent_fd(
                    staging_root_fd,
                    recovery_name,
                    staging_name,
                    check=lambda: _deadline(fixed_deadline),
                )
                _deadline(fixed_deadline)
                os.mkdir(staging_name, 0o700, dir_fd=staging_root_fd)
                _deadline(fixed_deadline)
                staging_fd = os.open(
                    staging_name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    dir_fd=staging_root_fd,
                )
                _deadline(fixed_deadline)
                staging_metadata = os.fstat(staging_fd)
                _deadline(fixed_deadline)
                staging_identity = (
                    staging_metadata.st_dev,
                    staging_metadata.st_ino,
                )
                recovery_identity = _complete_recovery_record_fd(
                    staging_root_fd,
                    recovery_name,
                    staging_name,
                    staging_identity,
                    intent_identity=recovery_identity,
                    check=lambda: _deadline(fixed_deadline),
                )
                _deadline(fixed_deadline)
                os.fchmod(staging_fd, 0o700)
                _deadline(fixed_deadline)
                staging = self._staging_root / staging_name
                self._transport.pull(descriptor, staging, fixed_deadline)
                _deadline(fixed_deadline)
                _require_entry_identity(
                    staging_root_fd,
                    staging_name,
                    staging_metadata,
                    fixed_deadline,
                )
                _verify_release_tree_fd(
                    staging_fd, descriptor, deadline=fixed_deadline
                )
                _write_receipt_fd(staging_fd, descriptor, fixed_deadline)
                if verify_installed_release_fd(
                    staging_fd, fixed_deadline
                ) != descriptor:
                    raise ReleaseInstallError("staged release receipt does not match")
                _fsync_tree_fd(staging_fd, fixed_deadline)
                _require_entry_identity(
                    staging_root_fd,
                    staging_name,
                    staging_metadata,
                    fixed_deadline,
                )
                try:
                    _deadline(fixed_deadline)
                    _rename_noreplace(
                        staging_root_fd,
                        staging_name,
                        releases_fd,
                        request.target_digest,
                    )
                    published = True
                    os.fsync(releases_fd)
                    _deadline(fixed_deadline)
                    _require_root_identity(
                        self._releases_root,
                        releases_metadata,
                        fixed_deadline,
                    )
                    destination_fd = os.open(
                        request.target_digest,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        dir_fd=releases_fd,
                    )
                    try:
                        _deadline(fixed_deadline)
                        installed_metadata = os.fstat(destination_fd)
                        _deadline(fixed_deadline)
                        if (
                            installed_metadata.st_dev,
                            installed_metadata.st_ino,
                        ) != (staging_metadata.st_dev, staging_metadata.st_ino):
                            raise ReleaseInstallError(
                                "published release identity changed"
                            )
                        try:
                            installed_descriptor = verify_installed_release_fd(
                                destination_fd, fixed_deadline
                            )
                        except Exception:
                            # Publication is already durable. An elapsed
                            # deadline leaves the verified staged inode in
                            # place for idempotent re-verification on retry.
                            _deadline(fixed_deadline)
                            _remove_bound_tree_fd(
                                releases_fd,
                                request.target_digest,
                                (
                                    installed_metadata.st_dev,
                                    installed_metadata.st_ino,
                                ),
                                lambda: _deadline(fixed_deadline),
                            )
                            raise
                        if installed_descriptor != descriptor:
                            raise ReleaseInstallError(
                                "published release does not match"
                            )
                    finally:
                        os.close(destination_fd)
                except FileExistsError:
                    _verify_installed_fd(
                        releases_fd, request.target_digest, descriptor,
                        fixed_deadline,
                    )
            except ReleaseInstallError:
                raise
            except Exception as error:
                raise ReleaseInstallError("release installation failed") from error
            finally:
                if staging_fd >= 0:
                    os.close(staging_fd)
                    staging_fd = -1
                deferred = False
                try:
                    if (
                        not published
                        and staging_name is not None
                        and staging_identity is not None
                    ):
                        _deadline(fixed_deadline)
                        _remove_bound_tree_fd(
                            staging_root_fd,
                            staging_name,
                            staging_identity,
                            lambda: _deadline(fixed_deadline),
                        )
                    if recovery_name is not None:
                        record_removed = _remove_recovery_record_fd(
                            staging_root_fd,
                            recovery_name,
                            recovery_identity,
                            lambda: _deadline(fixed_deadline),
                        )
                        if not record_removed:
                            raise ReleaseInstallError(
                                "release staging recovery record identity changed"
                            )
                except (OSError, ReleaseInstallError, TimeoutError):
                    deferred = True
                if staging_reserved:
                    self._finish_staging(
                        staging_name,
                        staging_identity,
                        deferred=deferred,
                    )
                    staging_reserved = False
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)
            if recovery_lock_fd >= 0:
                os.close(recovery_lock_fd)
            if staging_root_fd >= 0:
                os.close(staging_root_fd)
            if releases_fd >= 0:
                os.close(releases_fd)
        return ReleaseEvidence(
            "installed" if published else "already-installed",
            request.target_digest,
            request.oci_manifest_digest,
            request.adapter_id,
        )

    def _reserve_staging(self, parent_fd: int, check: Any) -> None:
        with self._recovery_lock:
            if self._active_staging:
                raise ReleaseInstallError(
                    "release staging recovery backlog is full"
                )
            _read_recovery_records_fd(
                parent_fd,
                check,
                active_reservations=_ACTIVE_INSTALL_RECOVERY_SLOTS,
            )
            if len(self._deferred_staging) + 1 > _MAX_DEFERRED_STAGING:
                raise ReleaseInstallError(
                    "release staging recovery backlog is full"
                )
            self._active_staging += 1

    def _finish_staging(
        self,
        name: str | None,
        identity: tuple[int, int] | None,
        *,
        deferred: bool,
    ) -> None:
        with self._recovery_lock:
            self._active_staging -= 1
            if deferred and name is not None and identity is not None:
                self._deferred_staging[name] = identity

    def _reap_deferred_staging(self, parent_fd: int) -> bool:
        recovery_deadline = time.monotonic() + _RECOVERY_SECONDS

        def recovery_check() -> None:
            if time.monotonic() >= recovery_deadline:
                raise TimeoutError("release staging recovery budget elapsed")

        with self._recovery_lock:
            if self._active_staging:
                return False
            persisted = _read_recovery_records_fd(
                parent_fd,
                recovery_check,
                active_reservations=self._active_staging,
            )
            recovered = bool(persisted or self._deferred_staging)
            for record_name, (name, identity, record_identity) in tuple(
                persisted.items()
            ):
                if identity is None:
                    _quarantine_unproven_staging_fd(
                        parent_fd, name, recovery_check
                    )
                    if not _remove_recovery_record_fd(
                        parent_fd,
                        record_name,
                        record_identity,
                        recovery_check,
                    ):
                        raise ReleaseInstallError(
                            "release staging recovery record identity changed"
                        )
                    persisted.pop(record_name)
                    continue
                self._deferred_staging.setdefault(name, identity)
            if (
                len(persisted) + self._active_staging
                > _MAX_DEFERRED_STAGING
            ):
                raise ReleaseInstallError(
                    "release staging recovery backlog is full"
                )
            for name, identity in tuple(self._deferred_staging.items()):
                try:
                    _remove_bound_tree_fd(
                        parent_fd, name, identity, recovery_check
                    )
                    record_removed = _remove_recovery_record_fd(
                        parent_fd,
                        _recovery_name_for_staging(name),
                        persisted.get(
                            _recovery_name_for_staging(name),
                            (name, identity, None),
                        )[2],
                        recovery_check,
                    )
                    if not record_removed:
                        raise ReleaseInstallError(
                            "release staging recovery record identity changed"
                        )
                except TimeoutError:
                    break
                except OSError:
                    continue
                else:
                    self._deferred_staging.pop(name, None)
            if (
                len(self._deferred_staging) + self._active_staging
                >= _MAX_DEFERRED_STAGING
            ):
                raise ReleaseInstallError(
                    "release staging recovery backlog is full"
                )
            return recovered

    def inspect(
        self,
        request: ReleaseRequest,
        deadline: datetime | MonotonicDeadline,
    ) -> ReleaseInspection:
        try:
            fixed_deadline = _bind_deadline(deadline)
            descriptor = self._trust.authorize(
                request, fixed_deadline,
            )
            releases_fd = -1
            staging_root_fd = -1
            recovery_lock_fd = -1
            try:
                _deadline(fixed_deadline)
                releases_fd = os.open(
                    self._releases_root,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                )
                _deadline(fixed_deadline)
                staging_root_fd = os.open(
                    self._staging_root,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                )
                _deadline(fixed_deadline)
                recovery_lock_fd = _open_install_recovery_lock_fd(
                    staging_root_fd, fixed_deadline
                )
                _deadline(fixed_deadline)
                try:
                    os.stat(
                        request.target_digest,
                        dir_fd=releases_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    _deadline(fixed_deadline)
                else:
                    _deadline(fixed_deadline)
                    _verify_installed_fd(
                        releases_fd,
                        request.target_digest,
                        descriptor,
                        fixed_deadline,
                    )
                    return ReleaseInspection(
                        ReleaseDisposition.COMPLETED,
                        ReleaseEvidence(
                            "already-installed",
                            request.target_digest,
                            request.oci_manifest_digest,
                            request.adapter_id,
                        ),
                    )
                recovered = self._reap_deferred_staging(staging_root_fd)
                _deadline(fixed_deadline)
                names = os.listdir(staging_root_fd)
                _deadline(fixed_deadline)
                prefix = f".install-{request.target_digest}-"
                candidates = tuple(
                    name
                    for name in names
                    if name.startswith(prefix)
                    and _STAGING_NAME.fullmatch(name) is not None
                )
                if len(candidates) == 1:
                    candidate_fd = os.open(
                        candidates[0],
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        dir_fd=staging_root_fd,
                    )
                    try:
                        _deadline(fixed_deadline)
                        candidate_metadata = os.fstat(candidate_fd)
                        _deadline(fixed_deadline)
                        _verify_release_tree_fd(
                            candidate_fd,
                            descriptor,
                            deadline=fixed_deadline,
                        )
                        _require_entry_identity(
                            staging_root_fd,
                            candidates[0],
                            candidate_metadata,
                            fixed_deadline,
                        )
                    finally:
                        os.close(candidate_fd)
                    return ReleaseInspection(ReleaseDisposition.SAFE_TO_RESUME)
                if not candidates:
                    return ReleaseInspection(
                        ReleaseDisposition.SAFE_TO_RESUME
                        if recovered
                        else ReleaseDisposition.OPERATOR_INTERVENTION
                    )
            finally:
                if recovery_lock_fd >= 0:
                    os.close(recovery_lock_fd)
                if staging_root_fd >= 0:
                    os.close(staging_root_fd)
                if releases_fd >= 0:
                    os.close(releases_fd)
        except Exception:  # noqa: BLE001 - inspection intentionally fails closed
            return ReleaseInspection(ReleaseDisposition.OPERATOR_INTERVENTION)
        return ReleaseInspection(ReleaseDisposition.OPERATOR_INTERVENTION)


def verify_release_tree(
    root: Path,
    descriptor: ReleaseDescriptor,
    *,
    _allow_receipt: bool = False,
    deadline: MonotonicDeadline | None = None,
) -> None:
    _deadline_step(deadline)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _deadline_step(deadline)
        _verify_release_tree_fd(
            root_fd, descriptor, allow_receipt=_allow_receipt, deadline=deadline
        )
    finally:
        os.close(root_fd)


def _verify_release_tree_fd(
    root_fd: int,
    descriptor: ReleaseDescriptor,
    *,
    allow_receipt: bool = False,
    deadline: MonotonicDeadline | None = None,
) -> None:
    expected = {member.path: member for member in descriptor.members}
    expected_directories = {""}
    for member in descriptor.members:
        parent = PurePosixPath(member.path).parent
        while str(parent) != ".":
            expected_directories.add(str(parent))
            parent = parent.parent
    seen: set[str] = set()
    identities: set[str] = set()
    count = 0
    try:
        _deadline_step(deadline)
        root_metadata = os.fstat(root_fd)
        _deadline_step(deadline)
        _verify_directory_metadata(root_metadata)
        def walk(directory_fd: int, prefix: str) -> None:
            nonlocal count
            for name in _deadline_names(directory_fd, deadline):
                count += 1
                if count > 512:
                    raise ReleaseInstallError("release member count is excessive")
                relative = name if not prefix else f"{prefix}/{name}"
                identity = unicodedata.normalize("NFC", relative).casefold()
                if identity in identities:
                    raise ReleaseInstallError("release paths collide")
                identities.add(identity)
                _deadline_step(deadline)
                metadata = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                _deadline_step(deadline)
                if relative == ".install-receipt.json" and allow_receipt:
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or stat.S_IMODE(metadata.st_mode) != 0o400
                    ):
                        raise ReleaseInstallError("release receipt metadata is invalid")
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in expected_directories:
                        raise ReleaseInstallError("release contains an unexpected directory")
                    _verify_directory_metadata(metadata)
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    try:
                        _deadline_step(deadline)
                        walk(child, relative)
                    finally:
                        os.close(child)
                    continue
                member = expected.get(relative)
                if member is None:
                    raise ReleaseInstallError("release contains an unexpected member")
                _verify_member(directory_fd, name, member, deadline)
                seen.add(relative)

        walk(root_fd, "")
    except OSError as error:
        raise ReleaseInstallError("release tree is unsafe") from error
    if seen != set(expected):
        raise ReleaseInstallError("release member set is incomplete")


def _verify_member(
    directory_fd: int,
    name: str,
    member: ReleaseMember,
    deadline: MonotonicDeadline | None = None,
) -> None:
    _deadline_step(deadline)
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        _deadline_step(deadline)
        metadata = os.fstat(descriptor)
        _deadline_step(deadline)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != member.uid
            or metadata.st_gid != member.gid
            or stat.S_IMODE(metadata.st_mode) != member.mode
            or metadata.st_size != member.size
            or (metadata.st_size > 0 and metadata.st_blocks * 512 < metadata.st_size)
        ):
            raise ReleaseInstallError("release member metadata is invalid")
        digest = hashlib.sha256()
        total = 0
        while True:
            _deadline_step(deadline)
            chunk = os.read(
                descriptor, min(64 * 1024, member.size - total + 1)
            )
            _deadline_step(deadline)
            if not chunk:
                break
            total += len(chunk)
            if total > member.size:
                raise ReleaseInstallError("release member size changed")
            digest.update(chunk)
        if total != member.size or digest.hexdigest() != member.sha256:
            raise ReleaseInstallError("release member digest is invalid")
    finally:
        os.close(descriptor)


def _verify_directory_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_gid not in {0, os.getegid()}
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or stat.S_IMODE(metadata.st_mode) & 0o700 != 0o700
    ):
        raise ReleaseInstallError("release directory metadata is invalid")


def _receipt_bytes(descriptor: ReleaseDescriptor) -> bytes:
    document = {"schema_version": 1, "release": descriptor.to_mapping()}
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _unique_receipt_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ReleaseInstallError(
                "installed release receipt contains duplicate fields"
            )
        document[key] = value
    return document


def _write_receipt(root: Path, descriptor: ReleaseDescriptor) -> None:
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _write_receipt_fd(root_fd, descriptor)
    finally:
        os.close(root_fd)


def _write_receipt_fd(
    root_fd: int,
    descriptor: ReleaseDescriptor,
    deadline: MonotonicDeadline | None = None,
) -> None:
    data = _receipt_bytes(descriptor)
    _deadline_step(deadline)
    fd = os.open(
        ".install-receipt.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o400,
        dir_fd=root_fd,
    )
    try:
        _deadline_step(deadline)
        offset = 0
        while offset < len(data):
            _deadline_step(deadline)
            written = os.write(fd, data[offset:])
            _deadline_step(deadline)
            if written <= 0:
                raise ReleaseInstallError("release receipt write was incomplete")
            offset += written
        _deadline_step(deadline)
        os.fsync(fd)
        _deadline_step(deadline)
    finally:
        os.close(fd)


def _verify_installed(
    parent: Path,
    name: str,
    descriptor: ReleaseDescriptor,
    deadline: MonotonicDeadline | None = None,
) -> None:
    parent_fd = -1
    root_fd = -1
    try:
        _deadline_step(deadline)
        parent_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        _deadline_step(deadline)
        root_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        _deadline_step(deadline)
        metadata = os.fstat(root_fd)
        _deadline_step(deadline)
        installed_descriptor = verify_installed_release_fd(root_fd, deadline)
        if installed_descriptor != descriptor:
            raise ReleaseInstallError("installed release receipt does not match")
        _deadline_step(deadline)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _deadline_step(deadline)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise ReleaseInstallError("installed release identity changed")
    except ReleaseInstallError:
        raise
    except Exception as error:
        raise ReleaseInstallError("installed release is invalid") from error
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _verify_installed_fd(
    parent_fd: int,
    name: str,
    descriptor: ReleaseDescriptor,
    deadline: MonotonicDeadline | None = None,
) -> None:
    root_fd = -1
    try:
        _deadline_step(deadline)
        root_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        _deadline_step(deadline)
        metadata = os.fstat(root_fd)
        _deadline_step(deadline)
        installed_descriptor = verify_installed_release_fd(root_fd, deadline)
        if installed_descriptor != descriptor:
            raise ReleaseInstallError("installed release receipt does not match")
        _deadline_step(deadline)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _deadline_step(deadline)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise ReleaseInstallError("installed release identity changed")
    except ReleaseInstallError:
        raise
    except Exception as error:
        raise ReleaseInstallError("installed release is invalid") from error
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def verify_installed_release(root: Path) -> ReleaseDescriptor:
    """Return the signed descriptor only when its receipt and tree still agree."""
    root_fd = -1
    try:
        root_fd = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        return verify_installed_release_fd(root_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def verify_installed_release_fd(
    root_fd: int, deadline: MonotonicDeadline | None = None
) -> ReleaseDescriptor:
    """Verify receipt and members through one already-open release identity."""
    try:
        _deadline_step(deadline)
        receipt_fd = os.open(
            ".install-receipt.json",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            _deadline_step(deadline)
            metadata = os.fstat(receipt_fd)
            _deadline_step(deadline)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
                raise ReleaseInstallError("installed release receipt is unsafe")
            _deadline_step(deadline)
            raw = os.read(receipt_fd, 64 * 1024 + 1)
            _deadline_step(deadline)
        finally:
            os.close(receipt_fd)
        document = json.loads(raw, object_pairs_hook=_unique_receipt_object)
        if not isinstance(document, dict) or set(document) != {"schema_version", "release"}:
            raise ReleaseInstallError("installed release receipt is invalid")
        if document["schema_version"] != 1:
            raise ReleaseInstallError("installed release receipt is invalid")
        descriptor = ReleaseDescriptor.parse(document["release"])
        if raw != _receipt_bytes(descriptor):
            raise ReleaseInstallError("installed release receipt does not match")
        _verify_release_tree_fd(
            root_fd, descriptor, allow_receipt=True, deadline=deadline
        )
        return descriptor
    except ReleaseInstallError:
        raise
    except Exception as error:
        raise ReleaseInstallError("installed release is invalid") from error


def _secure_root(
    path: Path, deadline: MonotonicDeadline | None = None
) -> None:
    if not path.is_absolute():
        raise ReleaseInstallError("release root is invalid")
    _deadline_step(deadline)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _deadline_step(deadline)
    metadata = path.lstat()
    _deadline_step(deadline)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ReleaseInstallError("release root is unsafe")


def _bind_deadline(deadline: datetime | MonotonicDeadline) -> MonotonicDeadline:
    try:
        return MonotonicDeadline.bind(deadline)
    except DeadlineBindingError as error:
        raise ReleaseInstallError("release deadline has elapsed") from error


def _deadline(deadline: MonotonicDeadline) -> None:
    try:
        deadline.check()
    except DeadlineBindingError:
        raise ReleaseInstallError("release deadline has elapsed")


def _deadline_step(deadline: MonotonicDeadline | None) -> None:
    if deadline is not None:
        _deadline(deadline)


def _deadline_names(
    directory_fd: int, deadline: MonotonicDeadline | None
):
    _deadline_step(deadline)
    entries = os.scandir(directory_fd)
    try:
        while True:
            _deadline_step(deadline)
            try:
                entry = next(entries)
            except StopIteration:
                break
            _deadline_step(deadline)
            yield entry.name
    finally:
        entries.close()


def _open_install_recovery_lock_fd(
    parent_fd: int, deadline: MonotonicDeadline
) -> int:
    _deadline(deadline)
    descriptor = os.open(
        _INSTALL_RECOVERY_LOCK_NAME,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        _deadline(deadline)
        metadata = os.fstat(descriptor)
        _deadline(deadline)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ReleaseInstallError(
                "release installation setup lock is unsafe"
            )
        _acquire_lock(descriptor, deadline)
        _deadline(deadline)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_lock(descriptor: int, deadline: MonotonicDeadline) -> None:
    while True:
        _deadline(deadline)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline.remaining()
            if remaining <= 0:
                raise ReleaseInstallError("release deadline has elapsed")
            time.sleep(min(0.01, remaining))


def _fsync_tree(root: Path) -> None:
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _fsync_tree_fd(root_fd)
    finally:
        os.close(root_fd)


def _fsync_tree_fd(
    directory_fd: int, deadline: MonotonicDeadline | None = None
) -> None:
    for name in _deadline_names(directory_fd, deadline):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _deadline_step(deadline)
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | (os.O_DIRECTORY if stat.S_ISDIR(metadata.st_mode) else 0),
            dir_fd=directory_fd,
        )
        try:
            _deadline_step(deadline)
            if stat.S_ISDIR(metadata.st_mode):
                _fsync_tree_fd(descriptor, deadline)
            _deadline_step(deadline)
            os.fsync(descriptor)
            _deadline_step(deadline)
        finally:
            os.close(descriptor)
    _deadline_step(deadline)
    os.fsync(directory_fd)
    _deadline_step(deadline)


def _fsync_directory(
    path: Path,
    deadline: MonotonicDeadline | None = None,
    *,
    commit_started: bool = False,
) -> None:
    if not commit_started:
        _deadline_step(deadline)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not commit_started:
            _deadline_step(deadline)
        os.fsync(descriptor)
        _deadline_step(deadline)
    finally:
        os.close(descriptor)


def _rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        1,
    )
    if result != 0:
        value = ctypes.get_errno()
        if value == errno.EEXIST:
            raise FileExistsError(value, os.strerror(value), destination_name)
        raise OSError(value, os.strerror(value), destination_name)


def _require_path_identity(
    path: Path,
    expected: os.stat_result,
    deadline: MonotonicDeadline | None = None,
) -> None:
    try:
        _deadline_step(deadline)
        current = os.stat(path, follow_symlinks=False)
        _deadline_step(deadline)
    except OSError as error:
        raise ReleaseInstallError("release staging identity changed") from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise ReleaseInstallError("release staging identity changed")


def _require_entry_identity(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    deadline: MonotonicDeadline | None = None,
) -> None:
    try:
        _deadline_step(deadline)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _deadline_step(deadline)
    except OSError as error:
        raise ReleaseInstallError("release staging identity changed") from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise ReleaseInstallError("release staging identity changed")


def _require_root_identity(
    path: Path,
    expected: os.stat_result,
    deadline: MonotonicDeadline | None = None,
) -> None:
    try:
        _deadline_step(deadline)
        current = os.stat(path, follow_symlinks=False)
        _deadline_step(deadline)
    except OSError as error:
        raise ReleaseInstallError("release root identity changed") from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise ReleaseInstallError("release root identity changed")


def _remove_bound_tree(parent: Path, name: str, identity: tuple[int, int]) -> None:
    """Remove only the private staging inode created by this installer."""
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (metadata.st_dev, metadata.st_ino) != identity or not stat.S_ISDIR(metadata.st_mode):
            return
        _remove_directory_contents(parent_fd, name, identity)
    finally:
        os.close(parent_fd)


def _remove_directory_contents(
    parent_fd: int, name: str, identity: tuple[int, int]
) -> None:
    directory_fd = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        if (os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino) != identity:
            return
        for child_name in os.listdir(directory_fd):
            child = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
            child_identity = (child.st_dev, child.st_ino)
            if stat.S_ISDIR(child.st_mode):
                _remove_directory_contents(directory_fd, child_name, child_identity)
            else:
                os.unlink(child_name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        os.rmdir(name, dir_fd=parent_fd)


def _remove_bound_tree_fd(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
    check: Any,
) -> None:
    """Remove one owned tree, checking the caller's budget around every syscall."""
    check()
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        check()
    except FileNotFoundError:
        _resume_quarantined_tree_fd(parent_fd, name, identity, check)
        return
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        return
    _remove_directory_contents_fd(parent_fd, name, identity, check)


def _resume_quarantined_tree_fd(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
    check: Any,
) -> None:
    match = _STAGING_NAME.fullmatch(name) or _TREE_QUARANTINE_NAME.fullmatch(
        name
    )
    if match is None:
        return
    prefix = f".quarantine-tree-{match.group(1)}-"
    check()
    candidates = [
        entry for entry in os.listdir(parent_fd) if entry.startswith(prefix)
    ]
    check()
    if not candidates:
        return
    if len(candidates) != 1:
        raise ReleaseInstallError(
            "release staging cleanup quarantine is ambiguous"
        )
    quarantine_name = candidates[0]
    check()
    metadata = os.stat(
        quarantine_name, dir_fd=parent_fd, follow_symlinks=False
    )
    check()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        raise ReleaseInstallError(
            "release staging cleanup identity changed"
        )
    _remove_directory_contents_fd(
        parent_fd, quarantine_name, identity, check
    )


def _recovery_name_for_staging(name: str) -> str:
    match = _STAGING_NAME.fullmatch(name)
    if match is None:
        raise ReleaseInstallError("release staging recovery record is invalid")
    return f".recovery-{match.group(1)}.state"


def _recovery_record_bytes(name: str, identity: tuple[int, int]) -> bytes:
    if _STAGING_NAME.fullmatch(name) is None:
        raise ReleaseInstallError("release staging recovery record is invalid")
    return f"1\n{name}\n{identity[0]}\n{identity[1]}\n".encode("ascii")


def _recovery_intent_bytes(name: str) -> bytes:
    if _STAGING_NAME.fullmatch(name) is None:
        raise ReleaseInstallError("release staging recovery record is invalid")
    return f"0\n{name}\n".encode("ascii")


def _write_recovery_bytes_fd(
    parent_fd: int,
    record_name: str,
    data: bytes,
    check: Any | None = None,
) -> tuple[int, int]:
    check = check or (lambda: None)
    check()
    descriptor = os.open(
        record_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        check()
        offset = 0
        while offset < len(data):
            check()
            written = os.write(descriptor, data[offset:])
            check()
            if written <= 0:
                raise ReleaseInstallError(
                    "release staging recovery record was incomplete"
                )
            offset += written
        check()
        os.fsync(descriptor)
        check()
        check()
        metadata = os.fstat(descriptor)
        check()
        identity = (metadata.st_dev, metadata.st_ino)
    finally:
        os.close(descriptor)
    check()
    os.fsync(parent_fd)
    check()
    return identity


def _write_recovery_intent_fd(
    parent_fd: int,
    record_name: str,
    staging_name: str,
    check: Any | None = None,
) -> tuple[int, int]:
    if record_name != _recovery_name_for_staging(staging_name):
        raise ReleaseInstallError("release staging recovery record is invalid")
    return _write_recovery_bytes_fd(
        parent_fd,
        record_name,
        _recovery_intent_bytes(staging_name),
        check,
    )


def _complete_recovery_record_fd(
    parent_fd: int,
    record_name: str,
    staging_name: str,
    identity: tuple[int, int],
    *,
    intent_identity: tuple[int, int],
    check: Any | None = None,
) -> tuple[int, int]:
    check = check or (lambda: None)
    match = _RECOVERY_NAME.fullmatch(record_name)
    if match is None or record_name != _recovery_name_for_staging(staging_name):
        raise ReleaseInstallError("release staging recovery record is invalid")
    temporary_name = f".recovery-{match.group(1)}.new"
    complete_identity = _write_recovery_bytes_fd(
        parent_fd,
        temporary_name,
        _recovery_record_bytes(staging_name, identity),
        check,
    )
    if not _remove_recovery_record_fd(
        parent_fd,
        record_name,
        intent_identity,
        check,
        missing_ok=False,
    ):
        raise ReleaseInstallError(
            "release staging recovery record identity changed"
        )
    check()
    _rename_noreplace(parent_fd, temporary_name, parent_fd, record_name)
    check()
    check()
    os.fsync(parent_fd)
    check()
    check()
    metadata = os.stat(record_name, dir_fd=parent_fd, follow_symlinks=False)
    check()
    if (metadata.st_dev, metadata.st_ino) != complete_identity:
        raise ReleaseInstallError("release staging recovery record identity changed")
    return complete_identity


def _write_recovery_record_fd(
    parent_fd: int,
    record_name: str,
    staging_name: str,
    identity: tuple[int, int],
    check: Any | None = None,
) -> tuple[int, int]:
    if record_name != _recovery_name_for_staging(staging_name):
        raise ReleaseInstallError("release staging recovery record is invalid")
    return _write_recovery_bytes_fd(
        parent_fd,
        record_name,
        _recovery_record_bytes(staging_name, identity),
        check,
    )


def _read_recovery_records_fd(
    parent_fd: int,
    check: Any,
    *,
    active_reservations: int = 0,
) -> dict[str, tuple[str, tuple[int, int] | None, tuple[int, int]]]:
    check()
    entries = os.listdir(parent_fd)
    check()
    names = [
        name for name in entries if _RECOVERY_NAME.fullmatch(name) is not None
    ]
    temporary_names = [
        name
        for name in entries
        if _RECOVERY_TEMP_NAME.fullmatch(name) is not None
    ]
    staging_names = [
        name for name in entries if _STAGING_NAME.fullmatch(name) is not None
    ]
    quarantined_names = [
        name
        for name in entries
        if name.startswith(
            (".quarantine-", ".unsafe-recovery-", ".remove-")
        )
        or (
            name.startswith(".recovery-")
            and ".state.remove-" in name
        )
    ]
    artifact_count = (
        len(names)
        + len(temporary_names)
        + len(staging_names)
        + len(quarantined_names)
    )
    if artifact_count + active_reservations > _MAX_DEFERRED_STAGING:
        raise ReleaseInstallError("release staging recovery backlog is full")
    recovery_quarantines = [
        name
        for name in quarantined_names
        if _RECOVERY_QUARANTINE_NAME.fullmatch(name) is not None
    ]
    seen_quarantine_tokens: set[str] = set()
    for quarantine_name in recovery_quarantines:
        match = _RECOVERY_QUARANTINE_NAME.fullmatch(quarantine_name)
        if match is None or match.group(1) in seen_quarantine_tokens:
            raise ReleaseInstallError(
                "release staging recovery quarantine is ambiguous"
            )
        seen_quarantine_tokens.add(match.group(1))
        _, _, quarantine_identity = _read_recovery_record_fd(
            parent_fd, quarantine_name, check, quarantine=True
        )
        if not _remove_recovery_quarantine_fd(
            parent_fd, quarantine_name, quarantine_identity, check
        ):
            raise ReleaseInstallError(
                "release staging recovery quarantine identity changed"
            )
    records: dict[
        str, tuple[str, tuple[int, int] | None, tuple[int, int]]
    ] = {}
    for record_name in names:
        records[record_name] = _read_recovery_record_fd(
            parent_fd, record_name, check, temporary=False
        )
    for temporary_name in temporary_names:
        staging_name, identity, temporary_identity = (
            _read_recovery_record_fd(
                parent_fd, temporary_name, check, temporary=True
            )
        )
        if identity is None:
            raise ReleaseInstallError(
                "release staging recovery record is invalid"
            )
        state_name = _recovery_name_for_staging(staging_name)
        current = records.get(state_name)
        if current is not None and (
            current[0] != staging_name or current[1] is not None
        ):
            raise ReleaseInstallError(
                "release staging recovery record is invalid"
            )
        check()
        try:
            staging_metadata = os.stat(
                staging_name, dir_fd=parent_fd, follow_symlinks=False
            )
        except OSError as error:
            raise ReleaseInstallError(
                "release staging recovery identity changed"
            ) from error
        check()
        if (
            not stat.S_ISDIR(staging_metadata.st_mode)
            or (staging_metadata.st_dev, staging_metadata.st_ino) != identity
        ):
            raise ReleaseInstallError(
                "release staging recovery identity changed"
            )
        if current is not None and not _remove_recovery_record_fd(
            parent_fd,
            state_name,
            current[2],
            check,
            missing_ok=False,
        ):
            raise ReleaseInstallError(
                "release staging recovery record identity changed"
            )
        check()
        _rename_noreplace(
            parent_fd, temporary_name, parent_fd, state_name
        )
        check()
        check()
        promoted = os.stat(
            state_name, dir_fd=parent_fd, follow_symlinks=False
        )
        check()
        if (promoted.st_dev, promoted.st_ino) != temporary_identity:
            try:
                _rename_noreplace(
                    parent_fd, state_name, parent_fd, temporary_name
                )
            except FileExistsError as error:
                raise ReleaseInstallError(
                    "release staging recovery record identity changed"
                ) from error
            raise ReleaseInstallError(
                "release staging recovery record identity changed"
            )
        check()
        os.fsync(parent_fd)
        check()
        records[state_name] = (
            staging_name, identity, temporary_identity
        )
    return records


def _read_recovery_record_fd(
    parent_fd: int,
    record_name: str,
    check: Any,
    *,
    temporary: bool = False,
    quarantine: bool = False,
) -> tuple[str, tuple[int, int] | None, tuple[int, int]]:
    if temporary and quarantine:
        raise ReleaseInstallError(
            "release staging recovery record is invalid"
        )
    pattern = (
        _RECOVERY_QUARANTINE_NAME
        if quarantine
        else (_RECOVERY_TEMP_NAME if temporary else _RECOVERY_NAME)
    )
    if pattern.fullmatch(record_name) is None:
        raise ReleaseInstallError(
            "release staging recovery record is invalid"
        )
    check()
    try:
        descriptor = os.open(
            record_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise ReleaseInstallError(
            "release staging recovery record is unsafe"
        ) from error
    try:
        check()
        metadata = os.fstat(descriptor)
        check()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 512
        ):
            raise ReleaseInstallError(
                "release staging recovery record is unsafe"
            )
        check()
        data = os.read(descriptor, 513)
        check()
    finally:
        os.close(descriptor)
    try:
        fields = data.decode("ascii").split("\n")
        version = fields[0]
        staging_name = fields[1]
        if (
            not temporary
            and version == "0"
            and fields == ["0", staging_name, ""]
        ):
            identity = None
            canonical = _recovery_intent_bytes(staging_name)
        elif version == "1" and len(fields) == 5 and fields[-1] == "":
            identity = (int(fields[2]), int(fields[3]))
            canonical = _recovery_record_bytes(staging_name, identity)
        else:
            raise ValueError("invalid recovery fields")
    except (UnicodeDecodeError, ValueError, IndexError) as error:
        raise ReleaseInstallError(
            "release staging recovery record is invalid"
        ) from error
    state_name = _recovery_name_for_staging(staging_name)
    quarantine_match = _RECOVERY_QUARANTINE_NAME.fullmatch(record_name)
    expected_name = state_name.replace(".state", ".new") if temporary else state_name
    name_matches = (
        quarantine_match is not None
        and _RECOVERY_NAME.fullmatch(state_name).group(1)
        == quarantine_match.group(1)
        if quarantine
        else record_name == expected_name
    )
    if (
        identity is not None and any(value < 0 for value in identity)
        or not name_matches
        or data != canonical
    ):
        raise ReleaseInstallError(
            "release staging recovery record is invalid"
        )
    return staging_name, identity, (metadata.st_dev, metadata.st_ino)


def _remove_recovery_quarantine_fd(
    parent_fd: int,
    quarantine_name: str,
    expected_identity: tuple[int, int],
    check: Any,
) -> bool:
    match = _RECOVERY_QUARANTINE_NAME.fullmatch(quarantine_name)
    if match is None:
        raise ReleaseInstallError(
            "release staging recovery quarantine is invalid"
        )
    next_name = (
        f".quarantine-recovery-{match.group(1)}-{secrets.token_hex(8)}"
    )
    check()
    try:
        _rename_noreplace(parent_fd, quarantine_name, parent_fd, next_name)
    except FileNotFoundError:
        return True
    check()
    check()
    metadata = os.stat(next_name, dir_fd=parent_fd, follow_symlinks=False)
    check()
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        try:
            _rename_noreplace(
                parent_fd, next_name, parent_fd, quarantine_name
            )
        except FileExistsError as error:
            raise ReleaseInstallError(
                "release staging recovery quarantine identity changed"
            ) from error
        return False
    check()
    os.unlink(next_name, dir_fd=parent_fd)
    check()
    check()
    os.fsync(parent_fd)
    check()
    return True


def _quarantine_unproven_staging_fd(
    parent_fd: int,
    staging_name: str,
    check: Any,
) -> None:
    match = _STAGING_NAME.fullmatch(staging_name)
    if match is None:
        raise ReleaseInstallError("release staging recovery record is invalid")
    check()
    try:
        os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        check()
    except FileNotFoundError:
        return
    quarantine_name = (
        f".quarantine-{match.group(1)}-{secrets.token_hex(8)}"
    )
    check()
    _rename_noreplace(
        parent_fd, staging_name, parent_fd, quarantine_name
    )
    check()
    os.fsync(parent_fd)
    check()


def _remove_recovery_record_fd(
    parent_fd: int,
    record_name: str,
    expected_identity: tuple[int, int] | None,
    check: Any,
    *,
    missing_ok: bool = True,
) -> bool:
    if _RECOVERY_NAME.fullmatch(record_name) is None:
        raise ReleaseInstallError("release staging recovery record is invalid")
    check()
    try:
        metadata = os.stat(
            record_name, dir_fd=parent_fd, follow_symlinks=False
        )
        check()
    except FileNotFoundError:
        return missing_ok
    if expected_identity is None:
        return False
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (metadata.st_dev, metadata.st_ino) != expected_identity
    ):
        return False
    match = _RECOVERY_NAME.fullmatch(record_name)
    if match is None:
        raise ReleaseInstallError(
            "release staging recovery record is invalid"
        )
    quarantine_name = (
        f".quarantine-recovery-{match.group(1)}-{secrets.token_hex(8)}"
    )
    check()
    _rename_noreplace(
        parent_fd, record_name, parent_fd, quarantine_name
    )
    check()
    quarantined = os.stat(
        quarantine_name, dir_fd=parent_fd, follow_symlinks=False
    )
    check()
    if (quarantined.st_dev, quarantined.st_ino) != expected_identity:
        try:
            _rename_noreplace(
                parent_fd, quarantine_name, parent_fd, record_name
            )
        except FileExistsError as error:
            raise ReleaseInstallError(
                "release staging recovery record identity changed"
            ) from error
        return False
    os.unlink(quarantine_name, dir_fd=parent_fd)
    check()
    os.fsync(parent_fd)
    check()
    return True


def _remove_directory_contents_fd(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
    check: Any,
) -> None:
    check()
    directory_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        check()
        metadata = os.fstat(directory_fd)
        check()
        if (metadata.st_dev, metadata.st_ino) != identity:
            return
        check()
        children = os.listdir(directory_fd)
        check()
        for child_name in children:
            check()
            child = os.stat(
                child_name, dir_fd=directory_fd, follow_symlinks=False
            )
            check()
            child_identity = (child.st_dev, child.st_ino)
            if stat.S_ISDIR(child.st_mode):
                _remove_directory_contents_fd(
                    directory_fd, child_name, child_identity, check
                )
            else:
                if not _remove_leaf_by_identity(
                    directory_fd,
                    child_name,
                    child_identity,
                    stat.S_IFMT(child.st_mode),
                    check,
                ):
                    raise ReleaseInstallError(
                        "release staging cleanup identity changed"
                    )
    finally:
        os.close(directory_fd)
    _remove_empty_directory_by_identity(parent_fd, name, identity, check)


def _remove_leaf_by_identity(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
    file_type: int,
    check: Any,
) -> bool:
    """Move a leaf to a private name and delete only the captured inode."""
    quarantine_name = f".quarantine-leaf-{secrets.token_hex(8)}"
    check()
    try:
        _rename_noreplace(parent_fd, name, parent_fd, quarantine_name)
    except FileNotFoundError:
        return True
    check()
    check()
    quarantined = os.stat(
        quarantine_name, dir_fd=parent_fd, follow_symlinks=False
    )
    check()
    if (
        (quarantined.st_dev, quarantined.st_ino) != identity
        or stat.S_IFMT(quarantined.st_mode) != file_type
    ):
        check()
        try:
            _rename_noreplace(parent_fd, quarantine_name, parent_fd, name)
        except FileExistsError as error:
            raise ReleaseInstallError(
                "release staging cleanup identity changed"
            ) from error
        check()
        return False
    check()
    os.unlink(quarantine_name, dir_fd=parent_fd)
    check()
    return True


def _remove_empty_directory_by_identity(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
    check: Any,
) -> None:
    match = _STAGING_NAME.fullmatch(name) or _TREE_QUARANTINE_NAME.fullmatch(
        name
    )
    quarantine_name = (
        f".quarantine-tree-{match.group(1)}-{secrets.token_hex(8)}"
        if match is not None
        else f".quarantine-tree-{secrets.token_hex(8)}"
    )
    check()
    try:
        _rename_noreplace(parent_fd, name, parent_fd, quarantine_name)
    except FileNotFoundError:
        return
    check()
    quarantined_fd = os.open(
        quarantine_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        check()
        metadata = os.fstat(quarantined_fd)
        check()
    finally:
        os.close(quarantined_fd)
    if (metadata.st_dev, metadata.st_ino) != identity:
        try:
            _rename_noreplace(
                parent_fd, quarantine_name, parent_fd, name
            )
        except FileExistsError as error:
            raise ReleaseInstallError(
                "release staging cleanup identity changed"
            ) from error
        return
    check()
    os.rmdir(quarantine_name, dir_fd=parent_fd)
    check()


@dataclass(frozen=True)
class ReleaseRequest:
    schema_version: int
    target_name: str
    oci_manifest_digest: str
    target_digest: str
    provenance_digest: str
    adapter_id: str

    @classmethod
    def parse(cls, document: Mapping[str, Any]) -> ReleaseRequest:
        if not isinstance(document, Mapping) or set(document) != _RELEASE_FIELDS:
            raise ReleaseValidationError("release request fields are invalid")
        if document["schema_version"] != 1 or isinstance(
            document["schema_version"], bool
        ):
            raise ReleaseValidationError("release request version is invalid")
        target_name = _token(document["target_name"], "target name")
        adapter_id = _token(document["adapter_id"], "adapter ID")
        manifest_digest = document["oci_manifest_digest"]
        target_digest = document["target_digest"]
        provenance_digest = document["provenance_digest"]
        if not isinstance(manifest_digest, str) or not _OCI_DIGEST.fullmatch(
            manifest_digest
        ):
            raise ReleaseValidationError("OCI manifest digest is invalid")
        for value, name in (
            (target_digest, "target digest"),
            (provenance_digest, "provenance digest"),
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ReleaseValidationError(f"{name} is invalid")
        return cls(
            1,
            target_name,
            manifest_digest,
            target_digest,
            provenance_digest,
            adapter_id,
        )


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ReleaseValidationError(f"{name} is invalid")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ReleaseValidationError(f"{name} is invalid")
    return value


def _oci_digest(value: Any) -> str:
    if not isinstance(value, str) or not _OCI_DIGEST.fullmatch(value):
        raise ReleaseValidationError("OCI manifest digest is invalid")
    return value


def _bounded_int(value: Any, minimum: int, maximum: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ReleaseValidationError(f"{name} is invalid")
    return value


def _version(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ReleaseValidationError(f"{name} is invalid")
    return value


def semantic_version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ReleaseValidationError("version is invalid")
    return tuple(int(part) for part in match.groups())


def _https_origin(value: Any) -> str:
    if not isinstance(value, str):
        raise ReleaseValidationError("registry origin is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
    ):
        raise ReleaseValidationError("registry origin is invalid")
    return value


def _members(value: Any) -> tuple[ReleaseMember, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 256:
        raise ReleaseValidationError("release members are invalid")
    members: list[ReleaseMember] = []
    identities: set[str] = set()
    aggregate = 0
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _MEMBER_FIELDS:
            raise ReleaseValidationError("release member fields are invalid")
        path = item["path"]
        if not isinstance(path, str) or not path or "\\" in path:
            raise ReleaseValidationError("release member path is invalid")
        pure = PurePosixPath(path)
        if pure.is_absolute() or str(pure) != path or any(part in {"", ".", ".."} for part in pure.parts):
            raise ReleaseValidationError("release member path is invalid")
        identity = unicodedata.normalize("NFC", path).casefold()
        if identity in identities or unicodedata.normalize("NFC", path) != path:
            raise ReleaseValidationError("release member paths collide")
        identities.add(identity)
        size = _bounded_int(item["size"], 0, 256 * 1024 * 1024, "member size")
        aggregate += size
        if aggregate > 1 << 30:
            raise ReleaseValidationError("release aggregate size is invalid")
        mode = item["mode"]
        if mode not in {0o400, 0o500} or isinstance(mode, bool):
            raise ReleaseValidationError("release member mode is invalid")
        uid = _bounded_int(item["uid"], 0, 65535, "member owner")
        gid = _bounded_int(item["gid"], 0, 65535, "member group")
        members.append(ReleaseMember(path, _digest(item["sha256"], "member digest"), size, mode, uid, gid))
    if tuple(member.path for member in members) != tuple(sorted(member.path for member in members)):
        raise ReleaseValidationError("release members are not canonical")
    return tuple(members)
