"""Canonical, content-addressed control-host deployment bundles."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from .platform_release import OciDeploymentBundle

_FORMAT = "vonk-control-deployment-bundle-v1"
_MANIFEST = "deployment-bundle.json"
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_DIGEST_PREFIX = "sha256:"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OCI_REFERENCE = re.compile(
    r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9._/-]*"
    r"@sha256:[0-9a-f]{64}\Z"
)
_OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_OCI_LAYER_MEDIA_TYPE = "application/vnd.vonk-forge.control-deployment.v1.tar"
_MAX_OCI_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_FILES = 256
_MAX_SOURCE_DEPTH = 64
_MAX_SOURCE_ENTRIES = 4096

REQUIRED_DEPLOYMENT_ASSETS = (
    "Caddyfile",
    "bin/harden-hermes-egress",
    "caddy/entrypoint.sh",
    "compose.yaml",
    "grafana/dashboards/fleet.json",
    "grafana/dashboards/jobs.json",
    "grafana/provisioning/dashboards/default.yaml",
    "grafana/provisioning/datasources/prometheus.yaml",
    "hermes-agent/compose.yaml",
    "images.lock.json",
    "litellm/bootstrap-config.json",
    "litellm/config.yaml",
    "litellm/config_supervisor.py",
    "litellm/entrypoint.sh",
    "prometheus/alerts.yaml",
    "prometheus/prometheus.yml",
    "postgres/init-databases.sh",
    "registry/config.yml",
    "step-ca/ca.json",
    "tailscale/compose.yaml",
    "tailscale/configure.sh",
    "trust/litellm-cosign.pub",
)

_EXECUTABLE_ASSETS = frozenset(
    {
        "litellm/config_supervisor.py",
        "litellm/entrypoint.sh",
        "postgres/init-databases.sh",
        "bin/harden-hermes-egress",
    }
)
_SOURCE_ONLY_FILES = frozenset(
    {
        ".env.example",
        "README.md",
        "bin/publish-release",
        "hermes-agent/Dockerfile",
        "hermes-agent/entrypoint.sh",
        "registry/README.md",
        "tailscale/README.md",
        "tailscale/grants.example.hujson",
    }
)


class DeploymentBundleError(ValueError):
    """A control deployment bundle is incomplete, unsafe, or noncanonical."""


@dataclass(frozen=True)
class BundleFile:
    path: str
    mode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class VerifiedDeploymentBundle:
    archive_sha256: str
    manifest_sha256: str
    files: Mapping[str, BundleFile]


class _DuplicateField(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise _DuplicateField(name)
        result[name] = value
    return result


def _canonical(document: object) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _expected_mode(path: str) -> int:
    return 0o755 if path in _EXECUTABLE_ASSETS else 0o644


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_source_root(path: Path) -> int:
    try:
        before = os.stat(path, follow_symlinks=False)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise DeploymentBundleError(
            "deployment source root is unsafe or missing"
        ) from error
    after = os.fstat(descriptor)
    if not stat.S_ISDIR(before.st_mode) or _identity(before) != _identity(after):
        os.close(descriptor)
        raise DeploymentBundleError("deployment source root is unsafe or unstable")
    return descriptor


def _open_source_directory(parent: int, name: str, relative: str) -> int:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
    except OSError as error:
        raise DeploymentBundleError(
            f"deployment source contains an unsafe directory: {relative}"
        ) from error
    after = os.fstat(descriptor)
    if not stat.S_ISDIR(before.st_mode) or _identity(before) != _identity(after):
        os.close(descriptor)
        raise DeploymentBundleError(
            f"deployment source directory changed during traversal: {relative}"
        )
    return descriptor


def _read_source(parent: int, name: str, relative: str) -> bytes:
    try:
        path_identity = os.stat(name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
    except OSError as error:
        raise DeploymentBundleError(
            f"deployment asset is unsafe or missing: {relative}"
        ) from error
    try:
        before = os.fstat(descriptor)
        expected_mode = _expected_mode(relative)
        if (
            not stat.S_ISREG(before.st_mode)
            or _identity(path_identity) != _identity(before)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != expected_mode
            or not 0 < before.st_size <= _MAX_FILE_BYTES
        ):
            raise DeploymentBundleError(f"deployment asset is unsafe: {relative}")
        content = bytearray()
        while len(content) <= _MAX_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, _MAX_FILE_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) != before.st_size or _identity(before) != _identity(after):
            raise DeploymentBundleError(
                f"deployment asset changed while being read: {relative}"
            )
        return bytes(content)
    except OSError as error:
        raise DeploymentBundleError(
            f"deployment asset cannot be read safely: {relative}"
        ) from error
    finally:
        os.close(descriptor)


def _read_relative(root: int, relative: str) -> bytes:
    parts = PurePosixPath(relative).parts
    parent = os.dup(root)
    try:
        for index, part in enumerate(parts[:-1]):
            child = _open_source_directory(
                parent,
                part,
                "/".join(parts[: index + 1]),
            )
            os.close(parent)
            parent = child
        return _read_source(parent, parts[-1], relative)
    finally:
        os.close(parent)


def _source_file_set(source_root: int) -> set[str]:
    files: set[str] = set()
    entry_count = 0
    stack: list[tuple[int, str, int, Any]] = []
    try:
        root = os.dup(source_root)
        try:
            stack.append((root, "", 0, os.scandir(root)))
        except OSError:
            os.close(root)
            raise
        while stack:
            directory, prefix, depth, entries = stack[-1]
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                os.close(directory)
                stack.pop()
                continue
            entry_count += 1
            if entry_count > _MAX_SOURCE_ENTRIES:
                raise DeploymentBundleError(
                    "deployment source contains too many entries"
                )
            name = entry.name
            relative = f"{prefix}/{name}" if prefix else name
            if relative == "tests" or name == "__pycache__" or name.endswith(".pyc"):
                continue
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError as error:
                raise DeploymentBundleError(
                    f"deployment source entry cannot be inspected: {relative}"
                ) from error
            if stat.S_ISREG(info.st_mode):
                files.add(relative)
            elif stat.S_ISDIR(info.st_mode):
                if depth >= _MAX_SOURCE_DEPTH:
                    raise DeploymentBundleError(
                        "deployment source directory depth exceeds its bound"
                    )
                child = _open_source_directory(directory, name, relative)
                try:
                    child_entries = os.scandir(child)
                except OSError:
                    os.close(child)
                    raise
                stack.append((child, relative, depth + 1, child_entries))
            else:
                raise DeploymentBundleError(
                    f"deployment source contains an unsafe entry: {relative}"
                )
    except OSError as error:
        raise DeploymentBundleError("deployment source is unreadable") from error
    finally:
        for directory, _prefix, _depth, entries in reversed(stack):
            entries.close()
            os.close(directory)
    return files


def _manifest(contents: dict[str, bytes]) -> tuple[bytes, tuple[BundleFile, ...]]:
    files = tuple(
        BundleFile(
            path=path,
            mode=_expected_mode(path),
            size=len(contents[path]),
            sha256=_sha256(contents[path]),
        )
        for path in REQUIRED_DEPLOYMENT_ASSETS
    )
    document = {
        "files": [
            {
                "mode": item.mode,
                "path": item.path,
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in files
        ],
        "format": _FORMAT,
        "schema_version": 1,
    }
    return _canonical(document), files


def _tar_info(path: str, content: bytes, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.size = len(content)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.type = tarfile.REGTYPE
    return info


def _archive(contents: dict[str, bytes]) -> bytes:
    manifest, _files = _manifest(contents)
    entries = {**contents, _MANIFEST: manifest}
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output,
        mode="w",
        format=tarfile.USTAR_FORMAT,
    ) as archive:
        for path in sorted(entries):
            content = entries[path]
            mode = 0o644 if path == _MANIFEST else _expected_mode(path)
            archive.addfile(_tar_info(path, content, mode), io.BytesIO(content))
    raw = output.getvalue()
    if len(raw) > _MAX_ARCHIVE_BYTES:
        raise DeploymentBundleError("deployment bundle exceeds its size bound")
    return raw


def build_deployment_bundle(source_root: Path) -> bytes:
    source_root = Path(source_root)
    root = _open_source_root(source_root)
    try:
        actual = _source_file_set(root)
        required = set(REQUIRED_DEPLOYMENT_ASSETS)
        missing = sorted(required - actual)
        if missing:
            raise DeploymentBundleError(
                "deployment source is missing required assets: " + ", ".join(missing)
            )
        unexpected = sorted(actual - required - _SOURCE_ONLY_FILES)
        if unexpected:
            raise DeploymentBundleError(
                "deployment source contains unexpected assets: " + ", ".join(unexpected)
            )
        contents = {
            relative: _read_relative(root, relative)
            for relative in REQUIRED_DEPLOYMENT_ASSETS
        }
    finally:
        os.close(root)
    return _archive(contents)


def _parse_manifest(raw: bytes) -> tuple[BundleFile, ...]:
    try:
        document = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        canonical = _canonical(document)
    except (
        _DuplicateField,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise DeploymentBundleError("deployment bundle manifest is invalid") from error
    if not isinstance(document, dict) or raw != canonical:
        raise DeploymentBundleError("deployment bundle manifest is noncanonical")
    raw_files = document.get("files")
    if (
        set(document) != {"files", "format", "schema_version"}
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or document.get("format") != _FORMAT
        or not isinstance(raw_files, list)
        or not 0 < len(raw_files) <= 256
    ):
        raise DeploymentBundleError("deployment bundle manifest is invalid")
    files_list: list[BundleFile] = []
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {
            "mode",
            "path",
            "sha256",
            "size",
        }:
            raise DeploymentBundleError("deployment bundle manifest is invalid")
        path = item["path"]
        mode = item["mode"]
        size = item["size"]
        sha256 = item["sha256"]
        if (
            not isinstance(path, str)
            or not _safe_member_name(path)
            or type(mode) is not int
            or mode not in {0o644, 0o755}
            or type(size) is not int
            or not 0 < size <= _MAX_FILE_BYTES
            or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None
        ):
            raise DeploymentBundleError("deployment bundle manifest is invalid")
        files_list.append(BundleFile(path, mode, size, sha256))
    files = tuple(files_list)
    if tuple(item.path for item in files) != REQUIRED_DEPLOYMENT_ASSETS:
        raise DeploymentBundleError("deployment bundle file set is invalid")
    if any(item.mode != _expected_mode(item.path) for item in files):
        raise DeploymentBundleError("deployment bundle file mode is invalid")
    return files


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and "\\" not in name
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.as_posix() == name
        and len(name) <= 240
    )


def _read_archive(raw: bytes) -> dict[str, bytes]:
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            contents: dict[str, bytes] = {}
            expanded_bytes = 0
            for member_count, member in enumerate(archive, start=1):
                if member_count > _MAX_MANIFEST_FILES + 1:
                    raise DeploymentBundleError(
                        "deployment bundle contains too many archive members"
                    )
                if member.sparse is not None:
                    raise DeploymentBundleError(
                        "deployment bundle contains a sparse archive member"
                    )
                if (
                    not member.isreg()
                    or not _safe_member_name(member.name)
                    or member.name in contents
                    or member.size <= 0
                    or member.size > _MAX_FILE_BYTES
                ):
                    raise DeploymentBundleError(
                        "deployment bundle contains an unsafe archive member"
                    )
                expanded_bytes += member.size
                if expanded_bytes > _MAX_ARCHIVE_BYTES:
                    raise DeploymentBundleError(
                        "deployment bundle expanded size exceeds its bound"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise DeploymentBundleError(
                        "deployment bundle member cannot be read"
                    )
                content = source.read(_MAX_FILE_BYTES + 1)
                if len(content) != member.size:
                    raise DeploymentBundleError(
                        "deployment bundle member size is invalid"
                    )
                contents[member.name] = content
    except DeploymentBundleError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise DeploymentBundleError("deployment bundle archive is invalid") from error
    return contents


def _validate_oci_descriptor(descriptor: OciDeploymentBundle) -> None:
    if not isinstance(descriptor, OciDeploymentBundle):
        raise DeploymentBundleError("deployment bundle descriptor is invalid")
    if not all(
        isinstance(value, str)
        for value in (
            descriptor.reference,
            descriptor.manifest_digest,
            descriptor.manifest_media_type,
            descriptor.layer_digest,
            descriptor.layer_media_type,
        )
    ):
        raise DeploymentBundleError("deployment bundle descriptor is invalid")
    if (
        _OCI_REFERENCE.fullmatch(descriptor.reference) is None
        or _OCI_DIGEST.fullmatch(descriptor.manifest_digest) is None
        or descriptor.reference.rsplit("@", 1)[-1] != descriptor.manifest_digest
    ):
        raise DeploymentBundleError("deployment bundle manifest digest is invalid")
    if (
        isinstance(descriptor.manifest_size, bool)
        or not isinstance(descriptor.manifest_size, int)
        or not 0 < descriptor.manifest_size <= _MAX_OCI_MANIFEST_BYTES
    ):
        raise DeploymentBundleError("deployment bundle manifest size is invalid")
    if descriptor.manifest_media_type != _OCI_MANIFEST_MEDIA_TYPE:
        raise DeploymentBundleError("deployment bundle manifest media type is invalid")
    if _OCI_DIGEST.fullmatch(descriptor.layer_digest) is None:
        raise DeploymentBundleError("deployment bundle layer digest is invalid")
    if (
        isinstance(descriptor.layer_size, bool)
        or not isinstance(descriptor.layer_size, int)
        or not 0 < descriptor.layer_size <= _MAX_ARCHIVE_BYTES
    ):
        raise DeploymentBundleError("deployment bundle layer size is invalid")
    if descriptor.layer_media_type != _OCI_LAYER_MEDIA_TYPE:
        raise DeploymentBundleError("deployment bundle layer media type is invalid")


def _verify_archive(raw: bytes) -> VerifiedDeploymentBundle:
    if not isinstance(raw, bytes):
        raise DeploymentBundleError("deployment bundle must be immutable bytes")
    if not 0 < len(raw) <= _MAX_ARCHIVE_BYTES:
        raise DeploymentBundleError("deployment bundle archive size is invalid")
    contents = _read_archive(raw)
    if _MANIFEST not in contents:
        raise DeploymentBundleError("deployment bundle manifest is missing")
    files = _parse_manifest(contents[_MANIFEST])
    expected_names = {_MANIFEST, *(item.path for item in files)}
    if set(contents) != expected_names:
        raise DeploymentBundleError("deployment bundle archive file set is invalid")
    for item in files:
        content = contents[item.path]
        if len(content) != item.size or _sha256(content) != item.sha256:
            raise DeploymentBundleError("deployment bundle file binding is invalid")
    asset_contents = {item.path: contents[item.path] for item in files}
    if raw != _archive(asset_contents):
        raise DeploymentBundleError("deployment bundle archive is noncanonical")
    return VerifiedDeploymentBundle(
        archive_sha256=_sha256(raw),
        manifest_sha256=_sha256(contents[_MANIFEST]),
        files=MappingProxyType({item.path: item for item in files}),
    )


def verify_deployment_bundle(
    raw: bytes,
    descriptor: OciDeploymentBundle,
) -> VerifiedDeploymentBundle:
    _validate_oci_descriptor(descriptor)
    if not isinstance(raw, bytes):
        raise DeploymentBundleError("deployment bundle must be immutable bytes")
    if len(raw) != descriptor.layer_size:
        raise DeploymentBundleError("deployment bundle descriptor size is invalid")
    if _DIGEST_PREFIX + _sha256(raw) != descriptor.layer_digest:
        raise DeploymentBundleError("deployment bundle descriptor digest is invalid")
    return _verify_archive(raw)


def _write_file(parent: int, name: str, content: bytes, mode: int) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
        dir_fd=parent,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_relative(root: int, relative: str, content: bytes, mode: int) -> None:
    parts = PurePosixPath(relative).parts
    parent = os.dup(root)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            os.close(parent)
            parent = child
        _write_file(parent, parts[-1], content, mode)
        os.fsync(parent)
    finally:
        os.close(parent)


def _clear_directory(directory: int) -> None:
    for name in os.listdir(directory):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            try:
                _clear_directory(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory)
        else:
            os.unlink(name, dir_fd=directory)


def extract_deployment_bundle(
    raw: bytes,
    destination: Path,
    verified: VerifiedDeploymentBundle,
) -> None:
    if not isinstance(verified, VerifiedDeploymentBundle):
        raise DeploymentBundleError("verified deployment bundle receipt is invalid")
    actual = _verify_archive(raw)
    if actual != verified:
        raise DeploymentBundleError("verified deployment bundle receipt is stale")
    destination = Path(destination)
    if destination.name in {"", ".", ".."}:
        raise DeploymentBundleError("deployment destination must be a new directory")
    parent = -1
    root = -1
    created = False
    try:
        parent = os.open(
            destination.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        os.mkdir(destination.name, 0o700, dir_fd=parent)
        created = True
        root = os.open(
            destination.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
    except OSError as error:
        if root >= 0:
            os.close(root)
        if created and parent >= 0:
            try:
                os.rmdir(destination.name, dir_fd=parent)
                os.fsync(parent)
            except OSError:
                pass
        if parent >= 0:
            os.close(parent)
        raise DeploymentBundleError(
            "deployment destination must be a new directory"
        ) from error
    contents = _read_archive(raw)
    try:
        for path in sorted(contents):
            mode = 0o644 if path == _MANIFEST else _expected_mode(path)
            _write_relative(root, path, contents[path], mode)
        os.fsync(root)
        root_info = os.fstat(root)
        path_info = os.stat(
            destination.name,
            dir_fd=parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(path_info.st_mode)
            or root_info.st_dev != path_info.st_dev
            or root_info.st_ino != path_info.st_ino
        ):
            raise DeploymentBundleError(
                "deployment destination changed during extraction"
            )
        os.fsync(parent)
    except (OSError, DeploymentBundleError) as error:
        if created:
            try:
                _clear_directory(root)
                os.fsync(root)
                root_info = os.fstat(root)
                path_info = os.stat(
                    destination.name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
                if (
                    root_info.st_dev == path_info.st_dev
                    and root_info.st_ino == path_info.st_ino
                ):
                    os.rmdir(destination.name, dir_fd=parent)
                    os.fsync(parent)
            except OSError:
                pass
        raise DeploymentBundleError("deployment bundle extraction failed") from error
    finally:
        if root >= 0:
            os.close(root)
        if parent >= 0:
            os.close(parent)
