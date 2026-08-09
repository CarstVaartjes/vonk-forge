"""Initialize the image-based development runtime without sharing authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from .host_state import HostOperationPlan, SelectionReceipt

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_WORKER_TOKEN = re.compile(rb"[A-Za-z0-9_-]{43}\Z")
_PUBLIC_REPOSITORY_URL = "https://github.com/CarstVaartjes/vonk-forge.git"
_API_UID = 10001
_API_GID = 10001
_GIT_UID = 65534
_GIT_GID = 65534
_MAX_SECRET_BYTES = 64 * 1024
_TARGET_SHA256 = "0" * 64
_BUILD_DIGEST = "sha256:" + "1" * 64
_VERSION = "0.1.0"
_DATABASE_REVISION = "0020_recipe_catalog_bridge"
_API_IMAGE = "vonk-forge-dev/control-api@sha256:" + "0" * 64
_WORKER_IMAGE = "vonk-forge-dev/control-worker@sha256:" + "1" * 64
_GIT_HOME = "/nonexistent/vonk-control"


class DevInitError(RuntimeError):
    """The development runtime cannot be initialized safely."""


def _git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": _GIT_HOME,
        "PATH": os.defpath,
    }


def _git_identity() -> dict[str, object]:
    if os.geteuid() != 0:
        return {}
    return {"user": _GIT_UID, "group": _GIT_GID, "extra_groups": ()}


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def _local_acceptance_enabled() -> bool:
    return os.environ.get("VONK_DEV_LOCAL_ACCEPTANCE") == "1"


def _origin_is_allowed(repository_url: str) -> bool:
    if repository_url == _PUBLIC_REPOSITORY_URL:
        return False
    if not _local_acceptance_enabled():
        raise DevInitError("development repository origin is invalid")
    parsed = urlsplit(repository_url)
    if (
        parsed.scheme != "file"
        or parsed.hostname not in {None, "", "localhost"}
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DevInitError("development repository origin is invalid")
    return True


def _git(
    root: Path | None,
    arguments: tuple[str, ...],
    *,
    action: str,
    local_origin: bool,
) -> str:
    command = (
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        f"protocol.file.allow={'always' if local_origin else 'never'}",
    )
    if root is not None:
        command += ("-C", str(root))
    command += arguments
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_environment(),
            check=False,
            text=True,
            timeout=60,
            **_git_identity(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DevInitError(f"Git could not {action}") from error
    if result.returncode != 0:
        raise DevInitError(f"Git could not {action}")
    return result.stdout.strip()


def _commit(value: str) -> str:
    if _COMMIT.fullmatch(value) is None:
        raise DevInitError("expected commit must be a full lowercase 40-hex ID")
    return value


def _directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise DevInitError(f"{label} is missing") from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise DevInitError(f"{label} is unsafe")


def _repository_is_clean(root: Path, *, local_origin: bool) -> None:
    status = _git(
        root,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        action="inspect repository status",
        local_origin=local_origin,
    )
    if status:
        raise DevInitError("development repository worktree must be clean")


def _is_ancestor(
    root: Path, ancestor: str, descendant: str, *, local_origin: bool
) -> bool:
    command = (
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        f"protocol.file.allow={'always' if local_origin else 'never'}",
        "-C",
        str(root),
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
    )
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_git_environment(),
            check=False,
            timeout=60,
            **_git_identity(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DevInitError("Git could not compare repository commits") from error
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise DevInitError("Git could not compare repository commits")


def _chown_repository_tree(
    root: Path, uid: int, gid: int, *, root_last: bool
) -> None:
    if os.geteuid() != 0:
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def chown_directory(descriptor: int) -> None:
        if not root_last:
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o700)
        for name in os.listdir(descriptor):
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                os.chown(
                    name,
                    uid,
                    gid,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                continue
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (metadata.st_dev, metadata.st_ino)
                ):
                    raise DevInitError(
                        "development repository ownership traversal changed"
                    )
                chown_directory(child)
            finally:
                os.close(child)
        if root_last:
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o700)

    descriptor = -1
    try:
        descriptor = os.open(root, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise DevInitError("development repository root is unsafe")
        chown_directory(descriptor)
    except DevInitError:
        raise
    except OSError as error:
        raise DevInitError("development repository ownership cannot be initialized") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _initialize_fresh_repository(
    root: Path, repository_url: str, expected_commit: str, *, local_origin: bool
) -> None:
    _prepare_empty_repository_root(root)
    _git(
        None,
        ("clone", "--no-checkout", "--origin", "origin", repository_url, str(root)),
        action="clone development repository",
        local_origin=local_origin,
    )
    _git(
        root,
        ("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"),
        action="fetch development repository main",
        local_origin=local_origin,
    )
    if not _is_ancestor(root, expected_commit, "refs/remotes/origin/main", local_origin=local_origin):
        raise DevInitError("expected development commit is not reachable from origin/main")
    _git(
        root,
        ("checkout", "--force", "-B", "main", expected_commit),
        action="check out development repository main",
        local_origin=local_origin,
    )


def _prepare_empty_repository_root(root: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = -1
    try:
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        descriptor = os.open(root, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise DevInitError("development repository root is unsafe")
        if os.listdir(descriptor):
            raise DevInitError("development repository root is not empty")
        if os.geteuid() == 0:
            os.fchown(descriptor, _GIT_UID, _GIT_GID)
        os.fchmod(descriptor, 0o700)
    except DevInitError:
        raise
    except OSError as error:
        raise DevInitError("development repository root cannot be prepared") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _initialize_existing_repository(
    root: Path, repository_url: str, expected_commit: str, *, local_origin: bool
) -> None:
    _directory(root / ".git", label="development repository metadata")
    _repository_is_clean(root, local_origin=local_origin)
    origin = _git(
        root,
        ("remote", "get-url", "origin"),
        action="read development repository origin",
        local_origin=local_origin,
    )
    if origin != repository_url:
        raise DevInitError("development repository origin changed")
    branch = _git(
        root,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        action="read development repository branch",
        local_origin=local_origin,
    )
    if branch != "main":
        raise DevInitError("development repository must check out main")
    current = _git(
        root,
        ("rev-parse", "--verify", "refs/heads/main^{commit}"),
        action="read development repository main",
        local_origin=local_origin,
    )
    _commit(current)
    _git(
        root,
        ("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"),
        action="fetch development repository main",
        local_origin=local_origin,
    )
    if not _is_ancestor(root, expected_commit, "refs/remotes/origin/main", local_origin=local_origin):
        raise DevInitError("expected development commit is not reachable from origin/main")
    if not _is_ancestor(root, current, expected_commit, local_origin=local_origin):
        raise DevInitError("development repository update is not a fast-forward")
    _git(
        root,
        ("update-ref", "refs/heads/main", expected_commit, current),
        action="compare-and-swap development repository main",
        local_origin=local_origin,
    )
    _git(
        root,
        ("reset", "--hard", expected_commit),
        action="reset development repository worktree",
        local_origin=local_origin,
    )


def initialize_repository(root: Path, repository_url: str, expected_commit: str) -> None:
    """Clone or fast-forward one clean NAS-local checkout of public ``main``."""
    root = Path(root)
    expected_commit = _commit(expected_commit)
    local_origin = _origin_is_allowed(repository_url)
    if root.is_symlink():
        raise DevInitError("development repository root is unsafe")
    managed_ownership = False
    try:
        if root.exists():
            _directory(root, label="development repository root")
            if any(root.iterdir()):
                if os.geteuid() == 0:
                    managed_ownership = True
                    _chown_repository_tree(
                        root, _GIT_UID, _GIT_GID, root_last=False
                    )
                _initialize_existing_repository(
                    root, repository_url, expected_commit, local_origin=local_origin
                )
            else:
                managed_ownership = os.geteuid() == 0
                _initialize_fresh_repository(
                    root, repository_url, expected_commit, local_origin=local_origin
                )
        else:
            managed_ownership = os.geteuid() == 0
            _initialize_fresh_repository(
                root, repository_url, expected_commit, local_origin=local_origin
            )
        _repository_is_clean(root, local_origin=local_origin)
        if _git(
            root,
            ("rev-parse", "--verify", "refs/heads/main^{commit}"),
            action="verify development repository main",
            local_origin=local_origin,
        ) != expected_commit:
            raise DevInitError(
                "development repository did not resolve to the expected commit"
            )
    finally:
        if managed_ownership and root.exists() and not root.is_symlink():
            _chown_repository_tree(root, _API_UID, _API_GID, root_last=True)


def _read_source_secret(root: Path, name: str) -> bytes:
    path = root / name
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError as error:
        raise DevInitError(f"development secret source {name} is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= _MAX_SECRET_BYTES
        ):
            raise DevInitError(f"development secret source {name} is unsafe")
        content = bytearray()
        while len(content) <= _MAX_SECRET_BYTES:
            chunk = os.read(descriptor, min(4096, _MAX_SECRET_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        updated_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(content) != before.st_size or identity != updated_identity:
            raise DevInitError(f"development secret source {name} changed while read")
        return bytes(content)
    except OSError as error:
        raise DevInitError(f"development secret source {name} cannot be read") from error
    finally:
        os.close(descriptor)


def _service_identity() -> tuple[int, int]:
    if os.geteuid() == 0:
        return _API_UID, _API_GID
    return os.geteuid(), os.getegid()


def _projection_path(root: Path, *, label: str) -> tuple[str, ...]:
    if root.anchor != "/" or len(root.parts) < 2 or any(
        part in {"", ".", ".."} for part in root.parts[1:]
    ):
        raise DevInitError(f"{label} projection path must be absolute and normalized")
    return root.parts[1:]


def _open_projection_directory(root: Path, *, label: str) -> int:
    components = _projection_path(root, label=label)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        current = os.open("/", flags)
    except OSError as error:
        raise DevInitError(f"{label} projection root is unsafe") from error
    try:
        for component in components:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except OSError as error:
        os.close(current)
        raise DevInitError(f"{label} projection component is unsafe") from error


def _prepare_projection_directory(parent: int) -> None:
    try:
        uid, gid = _service_identity()
        os.fchown(parent, uid, gid)
        os.fchmod(parent, 0o700)
    except OSError as error:
        raise DevInitError("development secret projection cannot be prepared") from error


def _clear_projection(parent: int) -> None:
    try:
        names = os.listdir(parent)
        for name in names:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                raise DevInitError("development secret projection contains a directory")
            os.unlink(name, dir_fd=parent)
    except OSError as error:
        raise DevInitError("development secret projection cannot be cleared") from error


def _read_generated_credential(parent: int, name: str) -> bytes | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DevInitError(f"generated credential {name} is unsafe") from error
    try:
        before = os.fstat(descriptor)
        uid, gid = _service_identity()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or before.st_gid != gid
            or stat.S_IMODE(before.st_mode) != 0o400
            or not 0 < before.st_size <= _MAX_SECRET_BYTES
        ):
            raise DevInitError(f"generated credential {name} is unsafe")
        content = bytearray()
        while len(content) <= _MAX_SECRET_BYTES:
            chunk = os.read(descriptor, min(4096, _MAX_SECRET_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(content) != before.st_size or before_identity != after_identity:
            raise DevInitError(f"generated credential {name} changed while read")
        return bytes(content)
    except OSError as error:
        raise DevInitError(f"generated credential {name} cannot be read") from error
    finally:
        os.close(descriptor)


def _admin_credential(parent: int) -> bytes:
    name = "admin-grant-private-key"
    content = _read_generated_credential(parent, name)
    if content is None:
        return ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    try:
        private_key = serialization.load_pem_private_key(content, password=None)
    except (TypeError, ValueError) as error:
        raise DevInitError(f"generated credential {name} is malformed") from error
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise DevInitError(f"generated credential {name} is malformed")
    return content


def _worker_credential(parent: int) -> bytes:
    name = "worker-api-token"
    content = _read_generated_credential(parent, name)
    if content is None:
        return secrets.token_urlsafe(32).encode("ascii")
    if _WORKER_TOKEN.fullmatch(content) is None:
        raise DevInitError(f"generated credential {name} is malformed")
    return content


def _seal_projection(parent: int) -> None:
    try:
        os.fchmod(parent, 0o550)
        os.fsync(parent)
    except OSError as error:
        raise DevInitError("development secret projection cannot be sealed") from error


def _write_projection_secret(parent: int, name: str, content: bytes) -> None:
    temporary = f".{name}.{secrets.token_hex(12)}.new"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise DevInitError("development secret write was incomplete")
            offset += written
        uid, gid = _service_identity()
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except DevInitError:
        raise
    except OSError as error:
        raise DevInitError("development secret cannot be staged") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _is_distinct_projection_roots(api_root: Path, worker_root: Path) -> bool:
    _projection_path(api_root, label="API")
    _projection_path(worker_root, label="worker")
    api = api_root
    worker = worker_root
    return not (
        api == worker or api.is_relative_to(worker) or worker.is_relative_to(api)
    )


def stage_runtime_secrets(source: Path, api_root: Path, worker_root: Path) -> None:
    """Stage two disjoint, service-owned runtime-secret projections."""
    source = Path(source)
    api_root = Path(api_root)
    worker_root = Path(worker_root)
    _directory(source, label="development secret source")
    if not _is_distinct_projection_roots(api_root, worker_root):
        raise DevInitError("development secret projections must be distinct")
    database_url = _read_source_secret(source, "database-url")
    signing_key = _read_source_secret(source, "git-signing-key")
    api = _open_projection_directory(api_root, label="API")
    worker = -1
    try:
        worker = _open_projection_directory(worker_root, label="worker")
        api_identity = os.fstat(api)
        worker_identity = os.fstat(worker)
        if (api_identity.st_dev, api_identity.st_ino) == (
            worker_identity.st_dev,
            worker_identity.st_ino,
        ):
            raise DevInitError(
                "development secret projections must be physically distinct"
            )
        _prepare_projection_directory(api)
        _prepare_projection_directory(worker)
        admin_key = _admin_credential(api)
        worker_token = _worker_credential(worker)
        _clear_projection(api)
        _clear_projection(worker)
        for name, content in (
            ("database-url", database_url),
            ("git-signing-key", signing_key),
            ("admin-grant-private-key", admin_key),
        ):
            _write_projection_secret(api, name, content)
        for name, content in (
            ("database-url", database_url),
            ("worker-api-token", worker_token),
        ):
            _write_projection_secret(worker, name, content)
        _seal_projection(api)
        _seal_projection(worker)
    finally:
        os.close(api)
        if worker >= 0:
            os.close(worker)


def _active_projection() -> bytes:
    target_name = f"platform/releases/{_VERSION}/{_TARGET_SHA256}.json"
    plan = HostOperationPlan(
        operation_id="dev-compose",
        plan_digest="sha256:" + "2" * 64,
        generation_id="gen-" + _TARGET_SHA256[:24],
        platform_target_name=target_name,
        platform_target_sha256=_TARGET_SHA256,
        tuf_targets_version=1,
        release_digest="sha256:" + _TARGET_SHA256,
        build_digest=_BUILD_DIGEST,
        platform_version=_VERSION,
        deployment_bundle_digest="sha256:" + "3" * 64,
        api_image=_API_IMAGE,
        worker_image=_WORKER_IMAGE,
        database_revision=_DATABASE_REVISION,
    )
    selection = SelectionReceipt.from_plan(plan, previous_generation=None)
    generation_raw = _canonical(selection.generation.document())
    selection_raw = _canonical(selection.document())
    return _canonical(
        {
            "generation_receipt_sha256": hashlib.sha256(generation_raw).hexdigest(),
            "projection_kind": "active",
            "projection_sequence": 1,
            "schema_version": 1,
            "selection": selection.document(),
            "selection_receipt_sha256": hashlib.sha256(selection_raw).hexdigest(),
        }
    )


def _initialize_directory(path: Path, *, mode: int, uid: int, gid: int) -> None:
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        _directory(path, label="development runtime directory")
        if os.geteuid() == 0:
            os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
    except OSError as error:
        raise DevInitError("development runtime directory cannot be initialized") from error


def _write_active_projection(identity_root: Path) -> None:
    _initialize_directory(identity_root, mode=0o755, uid=0, gid=0)
    destination = identity_root / "active.json"
    temporary = identity_root / f".active.json.{secrets.token_hex(12)}.new"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o644,
        )
        content = _active_projection()
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise DevInitError("development identity write was incomplete")
            offset += written
        if os.geteuid() == 0:
            os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
    except OSError as error:
        raise DevInitError("development identity cannot be initialized") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DevInitError(f"{name} is required")
    return value


def main() -> int:
    """Initialize repository, synthetic state, and disjoint runtime authority."""
    repository_path = Path(_required_environment("VONK_REPOSITORY_PATH"))
    repository_url = _required_environment("VONK_DEV_REPOSITORY_URL")
    expected_commit = _required_environment("VONK_DEV_EXPECTED_COMMIT")
    secret_source = Path(_required_environment("VONK_DEV_SECRET_SOURCE_ROOT"))
    api_secret_root = Path(_required_environment("VONK_DEV_API_SECRET_ROOT"))
    worker_secret_root = Path(_required_environment("VONK_DEV_WORKER_SECRET_ROOT"))
    initialize_repository(
        repository_path,
        repository_url,
        expected_commit,
    )
    stage_runtime_secrets(
        secret_source,
        api_secret_root,
        worker_secret_root,
    )
    identity_root = Path(os.environ.get("VONK_CONTROL_IDENTITY_ROOT", "/control-identity"))
    _write_active_projection(identity_root)
    uid, gid = _service_identity()
    for name, default in (
        ("VONK_STATE_PATH", "/state"),
        ("VONK_ROUTE_ROOT", "/routes"),
        ("VONK_SUPERVISOR_ROOT", "/supervisor"),
    ):
        _initialize_directory(Path(os.environ.get(name, default)), mode=0o750, uid=uid, gid=gid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
