"""Shared fail-closed validation for execution-harness projections."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath

from vonk_agent_protocol.document_limits import MAX_ARGV_BYTES

from .contracts import HarnessBinding, HarnessMount, HarnessProjection
from .errors import HarnessCompileError

_NON_ROOT_UID = re.compile(r"^[1-9][0-9]*(?::[1-9][0-9]*)?$")
_SHELL_EXECUTABLES = frozenset(
    {"sh", "bash", "dash", "ash", "zsh", "ksh", "csh", "tcsh", "fish", "busybox"}
)
_SHELL_LAUNCHERS = frozenset({"env", "busybox", "sudo", "doas"})
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}$")
_SOCKET_NAMES = ("docker.sock", "podman.sock", "containerd.sock", "cri-dockerd.sock")


def structured_command(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HarnessCompileError("harness command must be a structured argv")
    command = tuple(value)
    if not command:
        raise HarnessCompileError("harness command size is invalid")
    if any(
        type(item) is not str or len(item.encode("utf-8")) > 65536 or "\x00" in item
        for item in command
    ):
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    executable = PurePosixPath(command[0]).name.lower()
    if executable in (_SHELL_EXECUTABLES | _SHELL_LAUNCHERS):
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    if sum(len(item.encode("utf-8")) for item in command) > MAX_ARGV_BYTES:
        raise HarnessCompileError("harness command exceeds its total argv bound")
    return command


def validate_projection(projection: HarnessProjection) -> None:
    if type(projection.slug) is not str or not projection.slug:
        raise HarnessCompileError("harness projection slug is invalid")
    if type(projection.command) is not tuple:
        raise HarnessCompileError("harness command must use the exact tuple contract")
    structured_command(projection.command)
    if type(projection.contract_version) is not int or projection.contract_version != 1:
        raise HarnessCompileError("harness contract version is invalid")
    if type(projection.image) is not str or _IMAGE.fullmatch(projection.image) is None:
        raise HarnessCompileError("harness image must be digest-pinned")
    if (
        type(projection.user) is not str
        or _NON_ROOT_UID.fullmatch(projection.user) is None
    ):
        raise HarnessCompileError(
            "harness projection user must be numeric and non-root"
        )
    if (
        type(projection.environment) is not tuple
        or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or not re.fullmatch(r"[A-Z][A-Za-z0-9_]{0,127}", item[0])
            or type(item[1]) is not str
            or len(item[1].encode("utf-8")) > 65536
            or "\x00" in item[1]
            for item in projection.environment
        )
        or len({name for name, _value in projection.environment})
        != len(projection.environment)
    ):
        raise HarnessCompileError("harness projection environment is invalid")
    if (
        type(projection.model_mounts) is not tuple
        or not projection.model_mounts
        or any(type(mount) is not HarnessMount for mount in projection.model_mounts)
        or any(
            type(mount.read_only) is not bool or not mount.read_only
            for mount in projection.model_mounts
        )
    ):
        raise HarnessCompileError("harness model mounts must be read-only")
    if type(projection.output_mount) is not HarnessMount:
        raise HarnessCompileError("harness outputs must use the exact mount contract")
    if (
        type(projection.output_mount.read_only) is not bool
        or projection.output_mount.read_only
        or type(projection.output_mount.isolated) is not bool
        or not projection.output_mount.isolated
    ):
        raise HarnessCompileError("harness outputs must be isolated and writable")
    input_mount = projection.input_mount
    if input_mount is not None and (
        type(input_mount) is not HarnessMount
        or input_mount.read_only is not True
        or input_mount.isolated is not True
        or input_mount.target != "/inputs"
    ):
        raise HarnessCompileError("harness inputs must be isolated and read-only")
    mounts = (
        *projection.model_mounts,
        *((input_mount,) if input_mount else ()),
        projection.output_mount,
    )
    for mount in mounts:
        _mount_path(mount.source)
        _mount_path(mount.target)
    binding = projection.binding
    if (
        type(binding) is not HarnessBinding
        or type(binding.harness_content_sha256) is not str
        or type(binding.execution_content_sha256) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", binding.harness_content_sha256)
        or not re.fullmatch(r"[a-f0-9]{64}", binding.execution_content_sha256)
        or type(binding.topology_node_count) is not int
        or binding.topology_node_count < 1
        or type(binding.role) is not str
        or not binding.role
        or type(binding.rank) is not int
        or not 0 <= binding.rank < binding.topology_node_count
    ):
        raise HarnessCompileError("harness projection binding is invalid")


def _mount_path(value: str) -> None:
    if type(value) is not str or not value.startswith("/") or value == "/":
        raise HarnessCompileError("harness mount paths must be absolute")
    if (
        "//" in value
        or value.endswith(("/", "/.", "/.."))
        or "\\" in value
        or "/./" in value
        or "/../" in value
    ):
        raise HarnessCompileError("harness mount path is escaping")
    if any(name in value.lower() for name in _SOCKET_NAMES):
        raise HarnessCompileError(
            "harness mounts must not expose container runtime sockets"
        )
