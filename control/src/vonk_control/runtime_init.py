"""Root-only initialization of private control-container runtime material."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

_MAX_PRIVATE_KEY_BYTES = 16 * 1024


class RuntimeSecretError(RuntimeError):
    """A private runtime secret cannot be projected safely."""


@dataclass(frozen=True)
class SharedRuntimePaths:
    """Shared named-volume roots initialized by the control API pre-exec."""

    routes: Path = Path("/routes")
    supervisor: Path = Path("/supervisor")
    verifier: Path = Path("/verifier")
    agent_publication: Path = Path("/agent-tuf")
    workload_publication: Path = Path("/workload-tuf")


def stage_private_key(
    source: Path,
    destination: Path,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
    mode: int = 0o444,
) -> Path:
    """Copy a file-backed runtime secret into a Docker-managed volume atomically."""
    source = Path(source)
    destination = Path(destination)
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise RuntimeSecretError("runtime secret source is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= _MAX_PRIVATE_KEY_BYTES
        ):
            raise RuntimeSecretError("runtime secret source is unsafe")
        content = bytearray()
        while len(content) <= _MAX_PRIVATE_KEY_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, _MAX_PRIVATE_KEY_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) != before.st_size or _identity(before) != _identity(after):
            raise RuntimeSecretError("runtime secret changed while read")
    except OSError as error:
        raise RuntimeSecretError("runtime secret cannot be read") from error
    finally:
        os.close(descriptor)

    parent = destination.parent
    parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(parent, 0, 10001)
    # Consumers use different UIDs. The directory is traversable, while each
    # staged file remains owner-readable only.
    os.chmod(parent, 0o755)
    temporary = parent / f".{destination.name}.{secrets.token_hex(12)}.new"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
        )
        try:
            os.fchown(descriptor, owner_uid, owner_gid)
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        return destination
    except OSError as error:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise RuntimeSecretError("runtime secret staging failed") from error


def stage_compose_secrets(
    source_root: Path = Path("/run/secrets"),
    destination_root: Path = Path("/normalized"),
) -> None:
    """Normalize all file-backed Compose secrets for their runtime consumers."""
    source_root = Path(source_root)
    destination_root = Path(destination_root)
    for name in (
        "package-helper-grant-private-key",
        "package-helper-receipt-private-key",
        "host-runtime-grant-private-key",
    ):
        stage_private_key(
            source_root / name,
            destination_root / name,
            owner_uid=10001,
            owner_gid=10001,
            mode=0o400,
        )
    for name in (
        "database-url",
        "token-signing-key",
        "metrics-token",
        "agent-client-ca",
        "agent-intermediate-certificate",
        "controller-ca",
        "agent-proxy-auth",
        "worker-api-token",
    ):
        stage_private_key(
            source_root / name,
            destination_root / name,
            owner_uid=10001,
            owner_gid=10001,
            mode=0o400,
        )
    for name in (
        "litellm-master-key",
        "litellm-upstream-key",
        "litellm-database-url",
    ):
        stage_private_key(
            source_root / name,
            destination_root / name,
            owner_uid=10002,
            owner_gid=10001,
            mode=0o400,
        )
    stage_private_key(
        source_root / "metrics-token",
        destination_root / "prometheus-metrics-token",
        owner_uid=65534,
        owner_gid=65534,
        mode=0o400,
    )
    stage_private_key(
        source_root / "grafana-admin-password",
        destination_root / "grafana-admin-password",
        owner_uid=472,
        owner_gid=472,
        mode=0o400,
    )
    for name in (
        "agent-ca-credential",
        "agent-ca-provisioner-public-jwk",
        "step-ca-root-certificate",
        "agent-intermediate-key",
    ):
        source = source_root / name
        if source.exists():
            stage_private_key(
                source,
                destination_root / name,
                owner_uid=10001,
                owner_gid=10001,
                mode=0o400,
            )
    for name in (
        "step-ca-root-certificate",
        "agent-intermediate-certificate",
        "step-ca-intermediate-key",
        "step-ca-password",
    ):
        source = source_root / name
        if source.exists():
            destination_name = {
                "step-ca-root-certificate": "root-certificate",
                "agent-intermediate-certificate": "intermediate-certificate",
                "step-ca-intermediate-key": "intermediate-key",
                "step-ca-password": "password",
            }[name]
            stage_private_key(
                source,
                destination_root / "step-ca" / destination_name,
                owner_uid=1000,
                owner_gid=1000,
                mode=0o400,
            )


def _directory(path: Path, uid: int, gid: int, mode: int) -> Path:
    target = Path(path)
    if not target.is_absolute() or len(target.parts) < 2:
        raise RuntimeSecretError("shared runtime directory is unsafe")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for component in target.parts[1:]:
            if component in {"", ".", ".."}:
                raise RuntimeSecretError("shared runtime directory is unsafe")
            try:
                os.mkdir(component, mode=mode, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        return target
    except RuntimeSecretError:
        raise
    except OSError as error:
        raise RuntimeSecretError("shared runtime directory is unsafe") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def prepare_shared_volumes(paths: SharedRuntimePaths | None = None) -> None:
    """Apply the existing per-consumer ownership contract to shared volumes."""
    paths = SharedRuntimePaths() if paths is None else paths
    routes = _directory(paths.routes, 10001, 10001, 0o750)
    _directory(routes / "generations", 10001, 10001, 0o750)
    _directory(paths.supervisor, 10002, 10001, 0o750)
    _directory(paths.verifier, 10003, 10001, 0o700)

    agent = _directory(paths.agent_publication, 10001, 10001, 0o750)
    for name in ("metadata", "targets"):
        _directory(agent / name, 10001, 10001, 0o750)

    workload = _directory(paths.workload_publication, 10001, 10001, 0o750)
    for name in ("metadata", "targets"):
        _directory(workload / name, 10003, 10001, 0o750)


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
