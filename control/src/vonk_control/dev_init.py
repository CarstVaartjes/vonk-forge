"""Initialize the image-based development runtime without sharing authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from .dev_cohort import (
    DevelopmentCohortError,
    SelectedDevelopmentCohort,
    require_selected_cohort,
)
from .dev_runtime_assets import stage_development_assets
from .host_state import HostOperationPlan, SelectionReceipt

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}\Z")
_WORKER_TOKEN = re.compile(rb"[A-Za-z0-9_-]{43}\Z")
_PUBLIC_REPOSITORY_URL = "https://github.com/CarstVaartjes/vonk-forge.git"
_DEPLOYMENT_BASE_REF = "refs/vonk/deploy-base"
_ZERO_COMMIT = "0" * 40
_API_UID = 10001
_API_GID = 10001
_CADDY_UID = 10000
_CADDY_GID = 10000
_LITELLM_UID = 10002
_LITELLM_GID = 10001
_TAILSCALE_UID = 0
_TAILSCALE_GID = 0
_GIT_UID = 65534
_GIT_GID = 65534
_MAX_SECRET_BYTES = 64 * 1024
_PROJECTION_FILES = {
    "API": frozenset(
        {
            "database-url",
            "git-signing-key",
            "host-runtime-grant-private-key",
            "admin-grant-private-key",
            "worker-api-token",
            "agent-ca-certificate",
            "agent-ca-key",
            "agent-proxy-auth",
            "management-cidrs",
            "token-signing-key",
        }
    ),
    "migration": frozenset({"database-url"}),
    "worker": frozenset({"database-url", "management-cidrs", "worker-api-token"}),
    "Caddy": frozenset(
        {
            "controller-server-certificate",
            "controller-server-key",
            "agent-ca-certificate",
            "agent-proxy-auth",
            "management-cidrs",
        }
    ),
    "LiteLLM": frozenset({"litellm-master-key", "litellm-upstream-key"}),
    "auth": frozenset({"database-url", "admin-password-verifier"}),
    "Tailscale": frozenset(
        {"tailscale-oauth-client-id", "tailscale-oauth-client-secret"}
    ),
}
_TARGET_SHA256 = "0" * 64
_BUILD_DIGEST = "sha256:" + "1" * 64
_VERSION = "0.1.0"
_DATABASE_REVISION = "0020_recipe_catalog_bridge"
_GIT_HOME = "/nonexistent/vonk-control"


class DevInitError(RuntimeError):
    """The development runtime cannot be initialized safely."""


@dataclass(frozen=True)
class DevelopmentGenerationIdentity:
    """One verified development generation used for repository and state setup."""

    expected_commit: str
    generation_id: str
    release_digest: str
    build_digest: str
    platform_version: str
    api_image: str
    worker_image: str
    database_revision: str


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
    standard_input: str | None = None,
    safe_directory: Path | None = None,
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
    if safe_directory is not None:
        if not local_origin or not safe_directory.is_absolute():
            raise DevInitError("Git safe directory is invalid")
        command += ("-c", f"safe.directory={safe_directory}")
    if root is not None:
        command += ("-C", str(root))
    command += arguments
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL if standard_input is None else None,
            input=standard_input,
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


def _compare_and_swap_refs(
    root: Path,
    updates: tuple[tuple[str, str, str], ...],
    *,
    preserved: tuple[tuple[str, str], ...] = (),
    action: str,
    local_origin: bool,
) -> None:
    commands = ["start"]
    commands.extend(
        f"update {reference} {new_commit} {old_commit}"
        for reference, new_commit, old_commit in updates
    )
    commands.extend(
        f"verify {reference} {expected_commit}"
        for reference, expected_commit in preserved
    )
    commands.extend(("prepare", "commit", ""))
    _git(
        root,
        ("update-ref", "--stdin"),
        action=action,
        local_origin=local_origin,
        standard_input="\n".join(commands),
    )


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


def _merge_base(root: Path, accepted: str, deployed: str, *, local_origin: bool) -> str:
    return _commit(
        _git(
            root,
            ("merge-base", accepted, deployed),
            action="find repository deployment merge-base",
            local_origin=local_origin,
        )
    )


def _chown_repository_tree(root: Path, uid: int, gid: int, *, root_last: bool) -> None:
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
                if not stat.S_ISDIR(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (metadata.st_dev, metadata.st_ino):
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
        raise DevInitError(
            "development repository ownership cannot be initialized"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _initialize_fresh_repository(
    root: Path, repository_url: str, expected_commit: str, *, local_origin: bool
) -> None:
    _prepare_empty_repository_root(root)
    safe_origin = Path(urlsplit(repository_url).path) if local_origin else None
    clone_arguments = ("clone", "--no-checkout", "--origin", "origin")
    upload_pack = None
    if safe_origin is not None:
        upload_pack = (
            "git -c safe.directory=" + shlex.quote(str(safe_origin)) + " upload-pack"
        )
        clone_arguments += (f"--upload-pack={upload_pack}",)
    clone_arguments += (repository_url, str(root))
    _git(
        None,
        clone_arguments,
        action="clone development repository",
        local_origin=local_origin,
        safe_directory=safe_origin,
    )
    _git(
        root,
        (
            "fetch",
            *((f"--upload-pack={upload_pack}",) if upload_pack else ()),
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ),
        action="fetch development repository main",
        local_origin=local_origin,
    )
    if not _is_ancestor(
        root, expected_commit, "refs/remotes/origin/main", local_origin=local_origin
    ):
        raise DevInitError(
            "expected development commit is not reachable from origin/main"
        )
    accepted = _git(
        root,
        ("rev-parse", "--verify", "refs/heads/main^{commit}"),
        action="read development repository main",
        local_origin=local_origin,
    )
    _commit(accepted)
    _compare_and_swap_refs(
        root,
        (
            ("refs/heads/main", expected_commit, accepted),
            ("refs/heads/deploy", expected_commit, _ZERO_COMMIT),
            (_DEPLOYMENT_BASE_REF, expected_commit, _ZERO_COMMIT),
        ),
        action="initialize development repository refs",
        local_origin=local_origin,
    )
    _git(
        root,
        ("checkout", "--force", "deploy"),
        action="check out development repository deploy",
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
    if branch != "deploy":
        raise DevInitError("development repository must check out deploy")
    accepted = _git(
        root,
        ("rev-parse", "--verify", "refs/heads/main^{commit}"),
        action="read development repository main",
        local_origin=local_origin,
    )
    _commit(accepted)
    deployed = _git(
        root,
        ("rev-parse", "--verify", "refs/heads/deploy^{commit}"),
        action="read development repository deploy",
        local_origin=local_origin,
    )
    _commit(deployed)
    deployment_base = _git(
        root,
        ("rev-parse", "--verify", f"{_DEPLOYMENT_BASE_REF}^{{commit}}"),
        action="read development repository deployment base",
        local_origin=local_origin,
    )
    _commit(deployment_base)
    safe_origin = Path(urlsplit(repository_url).path) if local_origin else None
    upload_pack = (
        "git -c safe.directory=" + shlex.quote(str(safe_origin)) + " upload-pack"
        if safe_origin is not None
        else None
    )
    _git(
        root,
        (
            "fetch",
            *((f"--upload-pack={upload_pack}",) if upload_pack else ()),
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ),
        action="fetch development repository main",
        local_origin=local_origin,
    )
    if not _is_ancestor(
        root, expected_commit, "refs/remotes/origin/main", local_origin=local_origin
    ):
        raise DevInitError(
            "expected development commit is not reachable from origin/main"
        )
    if not _is_ancestor(root, accepted, expected_commit, local_origin=local_origin):
        raise DevInitError("development repository accepted baseline is divergent")
    if not _is_ancestor(root, deployment_base, accepted, local_origin=local_origin):
        raise DevInitError(
            "development repository deployment base does not precede "
            "the accepted baseline"
        )
    if not _is_ancestor(root, deployment_base, deployed, local_origin=local_origin):
        raise DevInitError(
            "development repository deployment branch does not descend "
            "from the deployment base"
        )
    if (
        _merge_base(root, accepted, deployed, local_origin=local_origin)
        != deployment_base
    ):
        raise DevInitError(
            "development repository deployment base does not equal "
            "the accepted/deploy merge-base"
        )
    if deployed == deployment_base:
        _compare_and_swap_refs(
            root,
            (
                ("refs/heads/main", expected_commit, accepted),
                ("refs/heads/deploy", expected_commit, deployed),
                (_DEPLOYMENT_BASE_REF, expected_commit, deployment_base),
            ),
            action="advance development repository refs",
            local_origin=local_origin,
        )
        _git(
            root,
            ("reset", "--hard", expected_commit),
            action="reset development repository worktree",
            local_origin=local_origin,
        )
    else:
        _compare_and_swap_refs(
            root,
            (("refs/heads/main", expected_commit, accepted),),
            preserved=(
                ("refs/heads/deploy", deployed),
                (_DEPLOYMENT_BASE_REF, deployment_base),
            ),
            action="advance development repository main",
            local_origin=local_origin,
        )


def initialize_repository(
    root: Path, repository_url: str, expected_commit: str
) -> None:
    """Advance accepted ``main`` while preserving one clean local ``deploy``."""
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
                    _chown_repository_tree(root, _GIT_UID, _GIT_GID, root_last=False)
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
        if (
            _git(
                root,
                ("symbolic-ref", "--quiet", "--short", "HEAD"),
                action="verify development repository branch",
                local_origin=local_origin,
            )
            != "deploy"
        ):
            raise DevInitError("development repository must check out deploy")
        if (
            _git(
                root,
                ("rev-parse", "--verify", "refs/heads/main^{commit}"),
                action="verify development repository main",
                local_origin=local_origin,
            )
            != expected_commit
        ):
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
        raise DevInitError(
            f"development secret source {name} cannot be read"
        ) from error
    finally:
        os.close(descriptor)


def _service_identity() -> tuple[int, int]:
    if os.geteuid() == 0:
        return _API_UID, _API_GID
    return os.geteuid(), os.getegid()


def _projection_identity(uid: int, gid: int) -> tuple[int, int]:
    if os.geteuid() == 0:
        return uid, gid
    return os.geteuid(), os.getegid()


def _projection_path(root: Path, *, label: str) -> tuple[str, ...]:
    if (
        root.anchor != "/"
        or len(root.parts) < 2
        or any(part in {"", ".", ".."} for part in root.parts[1:])
    ):
        raise DevInitError(f"{label} projection path must be absolute and normalized")
    return root.parts[1:]


def _open_projection_directory(
    root: Path,
    *,
    label: str,
    create: bool,
) -> tuple[int, bool] | None:
    components = _projection_path(root, label=label)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        current = os.open("/", flags)
    except OSError as error:
        raise DevInitError(f"{label} projection root is unsafe") from error
    try:
        final_created = False
        for index, component in enumerate(components):
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    os.close(current)
                    return None
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                    created = True
                except FileExistsError:
                    created = False
                child = os.open(component, flags, dir_fd=current)
                if index == len(components) - 1:
                    final_created = created
            os.close(current)
            current = child
        return current, final_created
    except OSError as error:
        os.close(current)
        raise DevInitError(f"{label} projection component is unsafe") from error


def _validate_projection_directory(
    parent: int,
    *,
    label: str,
    uid: int,
    gid: int,
    created: bool,
) -> bool:
    try:
        metadata = os.fstat(parent)
        expected_uid, expected_gid = _projection_identity(uid, gid)
        if not stat.S_ISDIR(metadata.st_mode):
            raise DevInitError(f"{label} projection root is unsafe")
        names = os.listdir(parent)
        if created:
            if names:
                raise DevInitError(f"{label} projection root is unsafe")
            return True
        managed = (
            metadata.st_uid == expected_uid
            and metadata.st_gid == expected_gid
            and stat.S_IMODE(metadata.st_mode) == 0o550
        )
        if names and not managed:
            raise DevInitError(f"{label} projection root is unsafe")
        if not names and not managed:
            if (
                metadata.st_uid == 0
                and metadata.st_gid == 0
                and stat.S_IMODE(metadata.st_mode) == 0o755
            ):
                return True
            raise DevInitError(f"{label} projection root is unsafe")
        allowed = _PROJECTION_FILES[label]
        for name in names:
            if name not in allowed:
                raise DevInitError(f"{label} projection entry is unsafe")
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(entry.st_mode)
                or entry.st_nlink != 1
                or entry.st_uid != expected_uid
                or entry.st_gid != expected_gid
                or stat.S_IMODE(entry.st_mode) != 0o400
                or not 0 < entry.st_size <= _MAX_SECRET_BYTES
            ):
                raise DevInitError(f"{label} projection entry is unsafe")
        return False
    except DevInitError:
        raise
    except OSError as error:
        raise DevInitError(f"{label} projection root is unsafe") from error


def _prepare_projection_directory(
    parent: int, *, uid: int, gid: int, initialize: bool
) -> None:
    try:
        uid, gid = _projection_identity(uid, gid)
        if initialize:
            os.fchown(parent, uid, gid)
        os.fchmod(parent, 0o700)
    except OSError as error:
        raise DevInitError(
            "development secret projection cannot be prepared"
        ) from error


def _read_generated_credential(
    parent: int,
    name: str,
    *,
    uid: int,
    gid: int,
) -> bytes | None:
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
        uid, gid = _projection_identity(uid, gid)
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
    content = _read_generated_credential(
        parent,
        name,
        uid=_API_UID,
        gid=_API_GID,
    )
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
    content = _read_generated_credential(
        parent,
        name,
        uid=_API_UID,
        gid=_API_GID,
    )
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


def _write_projection_secret(
    parent: int,
    name: str,
    content: bytes,
    *,
    uid: int,
    gid: int,
    replace: bool = True,
) -> None:
    temporary = f".{name}.{secrets.token_hex(12)}.new"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise DevInitError("development secret write was incomplete")
            offset += written
        uid, gid = _projection_identity(uid, gid)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if replace:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            os.link(
                temporary,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=parent)
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


def _are_distinct_projection_roots(*roots: tuple[Path, str]) -> bool:
    for root, label in roots:
        _projection_path(root, label=label)
    for index, (root, _label) in enumerate(roots):
        for other, _other_label in roots[index + 1 :]:
            if (
                root == other
                or root.is_relative_to(other)
                or other.is_relative_to(root)
            ):
                return False
    return True


def stage_runtime_secrets(
    source: Path,
    api_root: Path,
    migrate_root: Path,
    worker_root: Path,
    caddy_root: Path,
    litellm_root: Path,
    auth_root: Path,
    tailscale_root: Path,
) -> None:
    """Stage seven disjoint, service-owned runtime-secret projections."""
    source = Path(source)
    roots = (
        (Path(api_root), "API", _API_UID, _API_GID),
        (Path(migrate_root), "migration", _API_UID, _API_GID),
        (Path(worker_root), "worker", _API_UID, _API_GID),
        (Path(caddy_root), "Caddy", _CADDY_UID, _CADDY_GID),
        (Path(litellm_root), "LiteLLM", _LITELLM_UID, _LITELLM_GID),
        (Path(auth_root), "auth", _API_UID, _API_GID),
        (
            Path(tailscale_root),
            "Tailscale",
            _TAILSCALE_UID,
            _TAILSCALE_GID,
        ),
    )
    _directory(source, label="development secret source")
    if not _are_distinct_projection_roots(
        *((root, label) for root, label, _uid, _gid in roots)
    ):
        raise DevInitError("development secret projections must be distinct")

    database_url = _read_source_secret(source, "database-url")
    signing_key = _read_source_secret(source, "git-signing-key")
    host_runtime_grant_key = _read_source_secret(
        source, "host-runtime-grant-private-key"
    )
    agent_ca_certificate = _read_source_secret(source, "agent-ca-certificate")
    agent_ca_key = _read_source_secret(source, "agent-ca-key")
    agent_proxy_auth = _read_source_secret(source, "agent-proxy-auth")
    _read_source_secret(source, "controller-ca")
    controller_server_certificate = _read_source_secret(
        source, "controller-server-certificate"
    )
    controller_server_key = _read_source_secret(source, "controller-server-key")
    litellm_master_key = _read_source_secret(source, "litellm-master-key")
    litellm_upstream_key = _read_source_secret(source, "litellm-upstream-key")
    management_cidrs = _read_source_secret(source, "management-cidrs")
    token_signing_key = _read_source_secret(source, "token-signing-key")
    admin_password_verifier = _read_source_secret(source, "admin-password-verifier")
    tailscale_oauth_client_id = _read_source_secret(source, "tailscale-oauth-client-id")
    tailscale_oauth_client_secret = _read_source_secret(
        source, "tailscale-oauth-client-secret"
    )

    opened: list[tuple[int, bool] | None] = []
    try:
        for root, label, _uid, _gid in roots:
            opened.append(_open_projection_directory(root, label=label, create=False))
        existing = [item for item in opened if item is not None]
        existing_identities = {
            (metadata.st_dev, metadata.st_ino)
            for metadata in (os.fstat(descriptor) for descriptor, _created in existing)
        }
        if len(existing_identities) != len(existing):
            raise DevInitError(
                "development secret projections must be physically distinct"
            )
        for item, (_root, label, uid, gid) in zip(opened, roots, strict=True):
            if item is None:
                continue
            descriptor, created = item
            _validate_projection_directory(
                descriptor,
                label=label,
                uid=uid,
                gid=gid,
                created=created,
            )

        for index, (item, (root, label, _uid, _gid)) in enumerate(
            zip(opened, roots, strict=True)
        ):
            if item is None:
                created_item = _open_projection_directory(
                    root,
                    label=label,
                    create=True,
                )
                if created_item is None:
                    raise DevInitError(f"{label} projection root is unsafe")
                opened[index] = created_item
        complete = [item for item in opened if item is not None]
        if len(complete) != len(roots):
            raise DevInitError("development secret projection root is unsafe")
        descriptors = [descriptor for descriptor, _created in complete]
        identities = {
            (metadata.st_dev, metadata.st_ino)
            for metadata in (os.fstat(descriptor) for descriptor in descriptors)
        }
        if len(identities) != len(descriptors):
            raise DevInitError(
                "development secret projections must be physically distinct"
            )
        initialize_roots: list[bool] = []
        for (descriptor, created), (_root, label, uid, gid) in zip(
            complete, roots, strict=True
        ):
            initialize_roots.append(
                _validate_projection_directory(
                    descriptor,
                    label=label,
                    uid=uid,
                    gid=gid,
                    created=created,
                )
            )

        api, migrate, worker, caddy, litellm, auth, tailscale = descriptors
        admin_present = "admin-grant-private-key" in os.listdir(api)
        worker_present = "worker-api-token" in os.listdir(worker)
        admin_key = _admin_credential(api)
        worker_token = _worker_credential(worker)
        api_worker_token = _read_generated_credential(
            api,
            "worker-api-token",
            uid=_API_UID,
            gid=_API_GID,
        )
        if api_worker_token is not None and api_worker_token != worker_token:
            raise DevInitError("development worker credential projections diverge")

        prepared: list[int] = []
        try:
            for (descriptor, _created), initialize, (
                _root,
                _label,
                uid,
                gid,
            ) in zip(
                complete,
                initialize_roots,
                roots,
                strict=True,
            ):
                prepared.append(descriptor)
                _prepare_projection_directory(
                    descriptor,
                    uid=uid,
                    gid=gid,
                    initialize=initialize,
                )

            if not admin_present:
                _write_projection_secret(
                    api,
                    "admin-grant-private-key",
                    admin_key,
                    uid=_API_UID,
                    gid=_API_GID,
                    replace=False,
                )
            if not worker_present:
                _write_projection_secret(
                    worker,
                    "worker-api-token",
                    worker_token,
                    uid=_API_UID,
                    gid=_API_GID,
                    replace=False,
                )

            for name, content in (
                ("database-url", database_url),
                ("git-signing-key", signing_key),
                ("host-runtime-grant-private-key", host_runtime_grant_key),
                ("worker-api-token", worker_token),
                ("agent-ca-certificate", agent_ca_certificate),
                ("agent-ca-key", agent_ca_key),
                ("agent-proxy-auth", agent_proxy_auth),
                ("management-cidrs", management_cidrs),
                ("token-signing-key", token_signing_key),
            ):
                _write_projection_secret(
                    api,
                    name,
                    content,
                    uid=_API_UID,
                    gid=_API_GID,
                )
            _write_projection_secret(
                migrate,
                "database-url",
                database_url,
                uid=_API_UID,
                gid=_API_GID,
            )
            _write_projection_secret(
                worker,
                "database-url",
                database_url,
                uid=_API_UID,
                gid=_API_GID,
            )
            _write_projection_secret(
                worker,
                "management-cidrs",
                management_cidrs,
                uid=_API_UID,
                gid=_API_GID,
            )
            for name, content in (
                ("controller-server-certificate", controller_server_certificate),
                ("controller-server-key", controller_server_key),
                ("agent-ca-certificate", agent_ca_certificate),
                ("agent-proxy-auth", agent_proxy_auth),
                ("management-cidrs", management_cidrs),
            ):
                _write_projection_secret(
                    caddy,
                    name,
                    content,
                    uid=_CADDY_UID,
                    gid=_CADDY_GID,
                )
            for name, content in (
                ("litellm-master-key", litellm_master_key),
                ("litellm-upstream-key", litellm_upstream_key),
            ):
                _write_projection_secret(
                    litellm,
                    name,
                    content,
                    uid=_LITELLM_UID,
                    gid=_LITELLM_GID,
                )
            for name, content in (
                ("database-url", database_url),
                ("admin-password-verifier", admin_password_verifier),
            ):
                _write_projection_secret(
                    auth,
                    name,
                    content,
                    uid=_API_UID,
                    gid=_API_GID,
                )
            for name, content in (
                ("tailscale-oauth-client-id", tailscale_oauth_client_id),
                ("tailscale-oauth-client-secret", tailscale_oauth_client_secret),
            ):
                _write_projection_secret(
                    tailscale,
                    name,
                    content,
                    uid=_TAILSCALE_UID,
                    gid=_TAILSCALE_GID,
                )
        finally:
            sealing_error: DevInitError | None = None
            for descriptor in prepared:
                try:
                    _seal_projection(descriptor)
                except DevInitError as error:
                    if sealing_error is None:
                        sealing_error = error
            if sealing_error is not None:
                raise sealing_error
    finally:
        for item in opened:
            if item is not None:
                descriptor, _created = item
                os.close(descriptor)


def _pinned_generation_identity(
    expected_commit: str,
    api_image: str,
    worker_image: str,
) -> DevelopmentGenerationIdentity:
    """Adapt the established local/pinned inputs to one generation identity."""
    return DevelopmentGenerationIdentity(
        expected_commit=_commit(expected_commit),
        generation_id="gen-" + _TARGET_SHA256[:24],
        release_digest="sha256:" + _TARGET_SHA256,
        build_digest=_BUILD_DIGEST,
        platform_version=_VERSION,
        api_image=api_image,
        worker_image=worker_image,
        database_revision=_DATABASE_REVISION,
    )


def _selected_generation_identity(
    selected: SelectedDevelopmentCohort,
) -> DevelopmentGenerationIdentity:
    return DevelopmentGenerationIdentity(
        expected_commit=selected.source_commit,
        generation_id=selected.generation_id,
        release_digest=selected.release_digest,
        build_digest=selected.build_digest,
        platform_version=selected.platform_version,
        api_image=selected.api_image,
        worker_image=selected.worker_image,
        database_revision=selected.database_revision,
    )


def _development_generation_identity() -> DevelopmentGenerationIdentity:
    selected_name = "VONK_DEV_SELECTED_COHORT_FILE"
    pinned_names = (
        "VONK_DEV_EXPECTED_COMMIT",
        "VONK_DEV_API_IMAGE",
        "VONK_DEV_WORKER_IMAGE",
    )
    selected_present = selected_name in os.environ
    pinned_present = tuple(name in os.environ for name in pinned_names)
    if selected_present:
        if any(pinned_present):
            raise DevInitError("development identity inputs cannot be combined")
        try:
            selected = require_selected_cohort(
                Path(_required_environment(selected_name)),
                "api",
            )
        except DevelopmentCohortError as error:
            raise DevInitError("development selected cohort is invalid") from error
        return _selected_generation_identity(selected)
    if not all(pinned_present):
        raise DevInitError("development identity input is missing or incomplete")
    return _pinned_generation_identity(
        _required_environment(pinned_names[0]),
        _required_image_environment(pinned_names[1]),
        _required_image_environment(pinned_names[2]),
    )


def _active_projection(identity: DevelopmentGenerationIdentity) -> bytes:
    target_sha256 = identity.release_digest.removeprefix("sha256:")
    target_name = f"platform/releases/{identity.platform_version}/{target_sha256}.json"
    plan = HostOperationPlan(
        operation_id="dev-compose",
        plan_digest="sha256:" + "2" * 64,
        generation_id=identity.generation_id,
        platform_target_name=target_name,
        platform_target_sha256=target_sha256,
        tuf_targets_version=1,
        release_digest=identity.release_digest,
        build_digest=identity.build_digest,
        platform_version=identity.platform_version,
        deployment_bundle_digest="sha256:" + "3" * 64,
        api_image=identity.api_image,
        worker_image=identity.worker_image,
        database_revision=identity.database_revision,
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
        raise DevInitError(
            "development runtime directory cannot be initialized"
        ) from error


def _write_active_projection(
    identity_root: Path,
    identity: DevelopmentGenerationIdentity,
) -> None:
    _initialize_directory(identity_root, mode=0o755, uid=0, gid=0)
    destination = identity_root / "active.json"
    temporary = identity_root / f".active.json.{secrets.token_hex(12)}.new"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        content = _active_projection(identity)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise DevInitError("development identity write was incomplete")
            offset += written
        if os.geteuid() == 0:
            os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o444)
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


def _required_image_environment(name: str) -> str:
    value = _required_environment(name)
    if _IMAGE.fullmatch(value) is None:
        raise DevInitError(f"{name} is invalid")
    return value


def main() -> int:
    """Initialize repository, synthetic state, and disjoint runtime authority."""
    phase = os.environ.get("VONK_DEV_INIT_PHASE", "all")
    if phase not in {"all", "repository", "runtime"}:
        raise DevInitError("VONK_DEV_INIT_PHASE is invalid")
    repository_inputs: tuple[Path, str] | None = None
    if phase in {"all", "repository"}:
        repository_inputs = (
            Path(_required_environment("VONK_REPOSITORY_PATH")),
            _required_environment("VONK_DEV_REPOSITORY_URL"),
        )
    runtime_paths: (
        tuple[Path, Path, Path, Path, Path, Path, Path, Path, Path] | None
    ) = None
    if phase in {"all", "runtime"}:
        runtime_paths = (
            Path(_required_environment("VONK_DEV_SECRET_SOURCE_ROOT")),
            Path(_required_environment("VONK_DEV_API_SECRET_ROOT")),
            Path(_required_environment("VONK_DEV_MIGRATE_SECRET_ROOT")),
            Path(_required_environment("VONK_DEV_WORKER_SECRET_ROOT")),
            Path(_required_environment("VONK_DEV_CADDY_SECRET_ROOT")),
            Path(_required_environment("VONK_DEV_LITELLM_SECRET_ROOT")),
            Path(_required_environment("VONK_DEV_AUTH_SECRET_ROOT")),
            Path(_required_environment("VONK_DEV_TAILSCALE_SECRET_ROOT")),
            Path(_required_environment("VONK_DEV_RUNTIME_CONFIG_ROOT")),
        )
    generation = _development_generation_identity()
    if phase in {"all", "repository"}:
        assert repository_inputs is not None
        repository_path, repository_url = repository_inputs
        initialize_repository(
            repository_path,
            repository_url,
            generation.expected_commit,
        )
        if phase == "repository":
            return 0

    assert runtime_paths is not None
    (
        secret_source,
        api_secret_root,
        migrate_secret_root,
        worker_secret_root,
        caddy_secret_root,
        litellm_secret_root,
        auth_secret_root,
        tailscale_secret_root,
        runtime_config_root,
    ) = runtime_paths
    stage_runtime_secrets(
        secret_source,
        api_secret_root,
        migrate_secret_root,
        worker_secret_root,
        caddy_secret_root,
        litellm_secret_root,
        auth_secret_root,
        tailscale_secret_root,
    )
    stage_development_assets("vonk_control.resources.dev", runtime_config_root)
    identity_root = Path(
        os.environ.get("VONK_CONTROL_IDENTITY_ROOT", "/control-identity")
    )
    _write_active_projection(identity_root, generation)
    uid, gid = _service_identity()
    for name, default in (
        ("VONK_STATE_PATH", "/state"),
        ("VONK_ROUTE_ROOT", "/routes"),
        ("VONK_SUPERVISOR_ROOT", "/supervisor"),
    ):
        _initialize_directory(
            Path(os.environ.get(name, default)), mode=0o750, uid=uid, gid=gid
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
