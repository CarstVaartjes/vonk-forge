"""Root-only initialization of private control-container runtime material."""

from __future__ import annotations

import array
import errno
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from vonk_agent_protocol import WaitReason

from .categorized_errors import UnsettledOutcome
from .runtime_asset_contract import RuntimeAssetInventory

_LOGGER = logging.getLogger(__name__)
_MAX_PRIVATE_KEY_BYTES = 16 * 1024
_MAX_RUNTIME_FILE_BYTES = 64 * 1024


class RuntimeSecretError(RuntimeError):
    """A private runtime secret cannot be projected safely."""


GATEWAY_MASTER_KEY_NAME = "gateway-litellm-master-key"


@dataclass(frozen=True)
class SharedRuntimePaths:
    """Shared named-volume roots initialized by the control API pre-exec."""

    agent_artifacts: Path = Path("/state/agent-artifacts")
    model_cache: Path = Path("/state/model-cache")
    routes: Path = Path("/routes")
    supervisor: Path = Path("/supervisor")
    state: Path = Path("/state")
    gateway: Path = Path("/gateway-secrets")


def read_runtime_secret(
    source: Path, *, maximum_bytes: int = _MAX_PRIVATE_KEY_BYTES
) -> bytes:
    """Read one bounded regular Compose secret without following a symlink."""
    if not 0 < maximum_bytes <= _MAX_PRIVATE_KEY_BYTES:
        raise RuntimeSecretError("runtime secret size bound is invalid")
    last_error = UnsettledOutcome(
        "runtime secret observation exhausted",
        reason=WaitReason.OBSERVATION_UNAVAILABLE,
    )
    for _attempt in range(3):
        try:
            return _read_runtime_file(source, maximum_bytes=maximum_bytes)
        except UnsettledOutcome as error:
            last_error = error
    raise last_error


def _read_runtime_file(source: Path, *, maximum_bytes: int) -> bytes:
    if not 0 < maximum_bytes <= _MAX_RUNTIME_FILE_BYTES:
        raise RuntimeSecretError("runtime file size bound is invalid")
    source = Path(source)
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RuntimeSecretError("runtime secret source is unsafe") from error
        raise UnsettledOutcome(
            "runtime source is unavailable", reason=WaitReason.OBSERVATION_UNAVAILABLE
        ) from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum_bytes
        ):
            raise RuntimeSecretError("runtime secret source is unsafe")
        content = bytearray()
        while len(content) <= maximum_bytes:
            chunk = os.read(
                descriptor,
                min(4096, maximum_bytes + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) != before.st_size or _identity(before) != _identity(after):
            raise UnsettledOutcome(
                "runtime source changed during observation",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
    except OSError as error:
        raise UnsettledOutcome(
            "runtime source is unreadable", reason=WaitReason.OBSERVATION_UNAVAILABLE
        ) from error
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    return bytes(content)


def _stage_runtime_file_once(
    source: Path,
    destination: Path,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
    mode: int = 0o444,
    maximum_bytes: int = _MAX_RUNTIME_FILE_BYTES,
) -> Path:
    """Copy file-backed runtime material into a Docker-managed volume atomically."""
    content = _read_runtime_file(source, maximum_bytes=maximum_bytes)
    destination = Path(destination)

    parent = destination.parent
    temporary = parent / f".{destination.name}.{secrets.token_hex(12)}.new"
    try:
        parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(parent, 0, 10001)
        os.chmod(parent, 0o755)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
        )
        try:
            os.fchown(descriptor, owner_uid, owner_gid)
            os.fchmod(descriptor, mode)
            offset = 0
            # Each successful write advances at least one byte. The source
            # byte bound therefore owns the maximum number of attempts.
            for _write in range(len(content)):
                if offset == len(content):
                    break
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise OSError("runtime staging write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        return destination
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise UnsettledOutcome(
            "runtime file staging is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error


def stage_runtime_file(
    source: Path,
    destination: Path,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
    mode: int = 0o444,
    maximum_bytes: int = _MAX_RUNTIME_FILE_BYTES,
) -> Path:
    """Each staging request owns three fresh observations; prior bytes survive."""
    last_error: BaseException | None = None
    for _attempt in range(3):
        try:
            return _stage_runtime_file_once(
                source,
                destination,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                mode=mode,
                maximum_bytes=maximum_bytes,
            )
        except (OSError, UnsettledOutcome) as error:
            last_error = error
    raise UnsettledOutcome(
        "runtime staging observation exhausted",
        reason=WaitReason.OBSERVATION_UNAVAILABLE,
    ) from last_error


def write_runtime_asset_inventory(source_root: Path) -> bool:
    """Image assembly observes complete kit membership within three attempts."""
    last_error: BaseException | None = None
    for _attempt in range(3):
        try:
            _write_runtime_asset_inventory_once(source_root)
            return True
        except (OSError, UnsettledOutcome, ValueError) as error:
            last_error = error
    _LOGGER.warning("runtime kit assembly observation ended: %s", last_error)
    return False


def _write_runtime_asset_inventory_once(source_root: Path) -> None:
    """Image assembly owns membership; runtime traversal cannot retire files."""
    members: list[str] = []

    def unavailable(error: OSError) -> None:
        raise error

    for root, _, names in os.walk(source_root, onerror=unavailable):
        for name in names:
            path = Path(root) / name
            if name in (".inventory.json", ".inventory.json.new"):
                continue
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsettledOutcome(
                    "runtime kit member observation is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            members.append(path.relative_to(source_root).as_posix())
    inventory = RuntimeAssetInventory(schema_version=2, files=tuple(sorted(members)))
    temporary = source_root / ".inventory.json.new"
    try:
        temporary.write_text(inventory.model_dump_json())
        os.replace(temporary, source_root / ".inventory.json")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def stage_private_key(
    source: Path,
    destination: Path,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
    mode: int = 0o444,
) -> Path:
    """Copy one bounded private file into a Docker-managed volume atomically."""
    return stage_runtime_file(
        source,
        destination,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        mode=mode,
        maximum_bytes=_MAX_PRIVATE_KEY_BYTES,
    )


def _stage_optional_private_key(
    source: Path,
    destination: Path,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
    mode: int = 0o400,
) -> None:
    """Stage an optional secret, removing a stale projection when disabled."""
    source = Path(source)
    destination = Path(destination)
    # Compose uses /dev/null as the bounded default for an unset optional
    # secret. A bind mount appears at /run/secrets/hf-token, so identify it by
    # its zero-length character-device type and exact Linux null-device ID.
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        metadata = None
    source_is_absent = metadata is None or _is_null_device_metadata(metadata)
    if (
        metadata is not None
        and not source_is_absent
        and not stat.S_ISREG(metadata.st_mode)
    ):
        raise RuntimeSecretError("optional runtime secret source is unsafe")
    if source_is_absent or (metadata is not None and metadata.st_size == 0):
        try:
            projected = destination.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISDIR(projected.st_mode):
            raise RuntimeSecretError("optional runtime secret destination is unsafe")
        destination.unlink()
        return
    stage_private_key(
        source,
        destination,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        mode=mode,
    )


def _is_null_device(source: Path) -> bool:
    try:
        metadata = source.stat()
    except OSError:
        return False
    return _is_null_device_metadata(metadata)


def _is_null_device_metadata(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISCHR(metadata.st_mode)
        and metadata.st_size == 0
        and os.major(metadata.st_rdev) == 1
        and os.minor(metadata.st_rdev) == 3
    )


def stage_runtime_assets(
    source_root: Path = Path("/usr/local/share/vonk-forge/runtime-assets"),
    destination_root: Path = Path("/runtime-assets"),
) -> None:
    """Re-observe public kit files within a bounded local staging attempt.

    Atomic per-file replacement retains the previous file on an I/O failure.
    No unavailable inventory is interpreted as permission to remove old files.
    """
    last_error: BaseException | None = None
    for _attempt in range(3):
        try:
            _stage_runtime_assets_once(source_root, destination_root)
            return
        except (OSError, RuntimeSecretError, UnsettledOutcome) as error:
            last_error = error
    raise UnsettledOutcome(
        "runtime assets staging is unavailable",
        reason=WaitReason.OBSERVATION_UNAVAILABLE,
    ) from last_error


def _stage_runtime_assets_once(
    source_root: Path = Path("/usr/local/share/vonk-forge/runtime-assets"),
    destination_root: Path = Path("/runtime-assets"),
) -> None:
    """Publish this release's public runtime configs to the shared volume.

    Every consumer (Caddy, PostgreSQL, LiteLLM, Prometheus, Grafana, the
    registry, the Tailscale configurator and the Hermes key reconciler) reads
    its configuration from this volume, so a pulled Controller image is the
    whole configuration rollout. Files a release no longer ships are removed.
    """
    source_root = Path(source_root)
    destination_root = Path(destination_root)
    try:
        inventory = RuntimeAssetInventory.model_validate_json(
            _read_runtime_file(
                source_root / ".inventory.json", maximum_bytes=_MAX_RUNTIME_FILE_BYTES
            )
        )
        shipped = {Path(member) for member in inventory.files}
        # Observe every authoritative member before any retirement. Incomplete
        # traversal or a vanished sibling can never be evidence of retirement.
        for relative in shipped:
            _read_runtime_file(
                source_root / relative, maximum_bytes=_MAX_RUNTIME_FILE_BYTES
            )
    except (OSError, ValueError) as error:
        raise UnsettledOutcome(
            "runtime kit inventory is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    for relative in sorted(shipped):
        _stage_runtime_file_once(
            source_root / relative,
            destination_root / relative,
            mode=0o444,
            maximum_bytes=_MAX_RUNTIME_FILE_BYTES,
        )
    for path in sorted(destination_root.rglob("*"), reverse=True):
        relative = path.relative_to(destination_root)
        if path.is_dir() and not path.is_symlink():
            if not any(path.iterdir()):
                path.rmdir()
        elif relative not in shipped:
            path.unlink()


def stage_compose_secrets(
    source_root: Path = Path("/run/secrets"),
    destination_root: Path = Path("/normalized"),
) -> None:
    """Startup owns bounded retries, including optional projection cleanup."""
    last_error: BaseException | None = None
    for _attempt in range(3):
        try:
            _stage_compose_secrets_once(source_root, destination_root)
            return
        except (OSError, UnsettledOutcome) as error:
            last_error = error
    raise UnsettledOutcome(
        "runtime secret staging observation exhausted",
        reason=WaitReason.OBSERVATION_UNAVAILABLE,
    ) from last_error


def _stage_compose_secrets_once(
    source_root: Path = Path("/run/secrets"),
    destination_root: Path = Path("/normalized"),
) -> None:
    """Normalize all file-backed Compose secrets for their runtime consumers."""
    source_root = Path(source_root)
    destination_root = Path(destination_root)
    stage_private_key(
        source_root / "host-runtime-grant-private-key",
        destination_root / "host-runtime-grant-private-key",
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
    ):
        stage_private_key(
            source_root / name,
            destination_root / name,
            owner_uid=10001,
            owner_gid=10001,
            mode=0o400,
        )
    # The Controller manages gateway client keys with the LiteLLM master key;
    # it cannot read the litellm-owned copy below, so it gets its own.
    stage_private_key(
        source_root / "litellm-master-key",
        destination_root / GATEWAY_MASTER_KEY_NAME,
        owner_uid=10001,
        owner_gid=10001,
        mode=0o400,
    )
    for name in (
        "litellm-master-key",
        "litellm-database-url",
    ):
        stage_private_key(
            source_root / name,
            destination_root / name,
            owner_uid=10002,
            owner_gid=10001,
            mode=0o400,
        )
    _stage_optional_private_key(
        source_root / "litellm-upstream-key",
        destination_root / "litellm-upstream-key",
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
    _stage_optional_private_key(
        source_root / "hf-token",
        destination_root / "hf-token",
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
    # Public configuration: step-ca owns it, and the Controller group may read
    # the agent provisioner's certificate lifetime from it.
    stage_private_key(
        source_root / "step-ca-config",
        destination_root / "step-ca" / "ca.json",
        owner_uid=1000,
        owner_gid=10001,
        mode=0o440,
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


# ioctl request numbers and flag from <linux/fs.h> (64-bit Linux).
_FS_IOC_GETFLAGS = 0x80086601
_FS_IOC_SETFLAGS = 0x40086602
_FS_NOCOW_FL = 0x00800000


def _disable_copy_on_write(directory: Path) -> None:
    """Best effort ``chattr +C``: files created later in it are not copy-on-write.

    Only new files inherit the attribute; existing files keep theirs. A
    filesystem without the flag (anything but Btrfs) is left as it is.
    """
    try:
        descriptor = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError:
        return
    try:
        import fcntl

        flags = array.array("L", [0])
        fcntl.ioctl(descriptor, _FS_IOC_GETFLAGS, flags, True)
        if not flags[0] & _FS_NOCOW_FL:
            flags[0] |= _FS_NOCOW_FL
            fcntl.ioctl(descriptor, _FS_IOC_SETFLAGS, flags)
    except (OSError, ImportError):
        # ENOTTY/EOPNOTSUPP on other filesystems, EPERM without the capability.
        _LOGGER.info("copy-on-write stays enabled for %s", directory)
    finally:
        os.close(descriptor)


def _disable_copy_on_write_below(root: Path) -> None:
    """Mark the directories that already exist under an object store.

    A directory only passes the attribute to files created after it was set, so
    a store that predates it keeps writing copy-on-write below its existing
    ``objects``, ``partials`` and per-download directories. Marking them lets
    every file created from now on skip it; existing files are not rewritten.
    """
    try:
        children = [
            entry.path
            for entry in os.scandir(root)
            if entry.is_dir(follow_symlinks=False)
        ]
    except OSError:
        return
    for child in children:
        _disable_copy_on_write(Path(child))
        if os.path.basename(child) == "partials":
            try:
                downloads = [
                    entry.path
                    for entry in os.scandir(child)
                    if entry.is_dir(follow_symlinks=False)
                ]
            except OSError:
                continue
            for download in downloads:
                _disable_copy_on_write(Path(download))


def prepare_shared_volumes(paths: SharedRuntimePaths | None = None) -> None:
    """Apply the existing per-consumer ownership contract to shared volumes."""
    paths = SharedRuntimePaths() if paths is None else paths
    _directory(paths.state, 10001, 10001, 0o750)
    # Model and image objects are large, already-compressed, written once and
    # read whole: on Btrfs, copy-on-write and compression only cost CPU and
    # fragmentation, so new files created below these roots skip them.
    for objects in (paths.agent_artifacts, paths.model_cache):
        _disable_copy_on_write(_directory(objects, 10001, 10001, 0o750))
        _disable_copy_on_write_below(objects)
    routes = _directory(paths.routes, 10001, 10001, 0o750)
    _directory(routes / "generations", 10001, 10001, 0o750)
    _directory(paths.supervisor, 10002, 10001, 0o750)
    # Host bind mount holding the default gateway client key. The installer
    # creates it for the bundle owner, who keeps ownership so the host can
    # manage and back it up (owner -1 is left unchanged); the Controller
    # reaches it through the group.
    _directory(paths.gateway, -1, 10001, 0o770)


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
