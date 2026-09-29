"""Shared fail-closed validation for execution-harness projections."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath

from vonk_agent_protocol.host_helper import MAX_ARGV_BYTES

from ..runtime_writable_paths import (
    validate_paths as validate_runtime_paths,
)
from ..runtime_writable_paths import (
    validate_telemetry,
)
from .canonical_metadata import CANONICAL_HARNESSES
from .contracts import HarnessBinding, HarnessMount, HarnessProjection

_SAFE_ARGUMENT = re.compile(r'^[A-Za-z0-9_./:+@%=\[\]{},"<>-]{1,2048}$')
_SAFE_ARTIFACT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_NON_ROOT_UID = re.compile(r"^[1-9][0-9]*(?::[1-9][0-9]*)?$")
_SHELL_EXECUTABLES = frozenset(
    {"sh", "bash", "dash", "ash", "zsh", "ksh", "csh", "tcsh", "fish", "busybox"}
)
_SHELL_LAUNCHERS = frozenset({"env", "busybox", "sudo", "doas"})
_CUSTOM_ADAPTER_BIN = PurePosixPath("/opt/vonk/adapters/bin")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}$")
_SOCKET_NAMES = ("docker.sock", "podman.sock", "containerd.sock", "cri-dockerd.sock")
_BUILTIN_HARNESS_SLUGS = frozenset(metadata.slug for metadata in CANONICAL_HARNESSES)


class HarnessCompileError(ValueError):
    pass


def structured_command(
    value: object, *, canonical_argv: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HarnessCompileError("harness command must be a structured argv")
    command = tuple(value)
    if not command or len(command) > 256:
        raise HarnessCompileError("harness command size is invalid")
    if any(
        type(item) is not str
        or len(item.encode("utf-8")) > (65536 if canonical_argv else 2048)
        or "\x00" in item
        or (not canonical_argv and _SAFE_ARGUMENT.fullmatch(item) is None)
        for item in command
    ):
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    executable = PurePosixPath(command[0]).name.lower()
    if executable in (_SHELL_EXECUTABLES | _SHELL_LAUNCHERS):
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    if (
        canonical_argv
        and sum(len(item.encode("utf-8")) for item in command) > MAX_ARGV_BYTES
    ):
        raise HarnessCompileError("harness command exceeds its total argv bound")
    return command


def validate_projection(
    projection: HarnessProjection,
    *,
    canonical_argv: bool = False,
    canonical_mounts: bool = False,
) -> None:
    if type(projection.slug) is not str or not projection.slug:
        raise HarnessCompileError("harness projection slug is invalid")
    if type(projection.command) is not tuple:
        raise HarnessCompileError("harness command must use the exact tuple contract")
    structured_command(projection.command, canonical_argv=canonical_argv)
    if type(projection.contract_version) is not int or projection.contract_version != 1:
        raise HarnessCompileError("harness contract version is invalid")
    if type(projection.image) is not str or _IMAGE.fullmatch(projection.image) is None:
        raise HarnessCompileError("harness image must be digest-pinned")
    if type(projection.network_mode) is not str or (
        projection.network_mode != "none"
        and not (canonical_argv and projection.network_mode == "bridge")
    ):
        raise HarnessCompileError("harness projection requires an offline network")
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
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", item[0])
            or type(item[1]) is not str
            or len(item[1].encode("utf-8")) > (65536 if canonical_argv else 2048)
            or "\x00" in item[1]
            or (not canonical_argv and _SAFE_ARGUMENT.fullmatch(item[1]) is None)
            for item in projection.environment
        )
        or len({name for name, _value in projection.environment})
        != len(projection.environment)
    ):
        raise HarnessCompileError("harness projection environment is invalid")
    enforce_engine_contract = (
        not canonical_argv
        and projection.slug in _BUILTIN_HARNESS_SLUGS
        and _is_builtin_launch_command(projection.slug, projection.command)
    )
    if enforce_engine_contract:
        if projection.telemetry is None:
            raise HarnessCompileError("built-in harness telemetry is missing")
        try:
            validate_telemetry(
                projection.slug, projection.telemetry, dict(projection.environment)
            )
        except (TypeError, ValueError) as error:
            raise HarnessCompileError(str(error)) from error
        _validate_engine_launch(projection.slug, projection.command)
    if enforce_engine_contract or projection.writable_paths:
        try:
            validate_runtime_paths(
                projection.slug,
                projection.writable_paths,
                dict(projection.environment),
            )
        except (TypeError, ValueError) as error:
            raise HarnessCompileError(str(error)) from error
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
    if not canonical_mounts:
        _disjoint_mount_paths(tuple(mount.source for mount in mounts))
    _disjoint_mount_paths(tuple(mount.target for mount in mounts))
    for source in (mount.source for mount in mounts):
        for target in (mount.target for mount in mounts):
            if _overlaps(source, target):
                raise HarnessCompileError("harness mount source and target overlap")
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


def _overlaps(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _disjoint_mount_paths(paths: tuple[str, ...]) -> None:
    for index, path in enumerate(paths):
        if any(_overlaps(path, other) for other in paths[index + 1 :]):
            raise HarnessCompileError("harness mounts overlap")


_ENGINE_LAUNCH_PREFIXES: dict[str, tuple[str, ...]] = {
    "vllm": ("/opt/vonk/bin/vllm", "serve"),
    "sglang": ("/opt/vonk/bin/sglang-serve",),
    "tensorrt-llm": ("/usr/local/bin/trtllm-serve", "serve", "/models"),
    "llama-cpp": ("/opt/vonk/bin/llama-server",),
    "ds4": ("/opt/vonk/bin/ds4-serve",),
    "tensorfold": ("/opt/vonk/bin/tensorfold-serve",),
    "diffusers": ("/opt/vonk/bin/diffusers-job",),
    "comfyui": ("/opt/vonk/bin/comfyui-job",),
    "pytorch-pipeline": ("/opt/vonk/bin/pytorch-pipeline",),
}


def _validate_engine_launch(slug: str, command: tuple[str, ...]) -> None:
    """Check the stable wrapper boundary while retaining recipe arguments."""
    prefix = _ENGINE_LAUNCH_PREFIXES[slug]
    if command[: len(prefix)] != prefix:
        raise HarnessCompileError("built-in harness launch wrapper is invalid")
    if slug == "vllm" and (
        len(command) < 3
        or not (command[2] == "/models" or command[2].startswith("/models/"))
    ):
        raise HarnessCompileError("vLLM launch must name its model mount")
    if slug in {"vllm", "sglang", "tensorrt-llm", "llama-cpp", "ds4", "tensorfold"}:
        if "--host" not in command or "--port" not in command:
            raise HarnessCompileError("serving harness launch lacks host and port")
    else:
        try:
            output_index = command.index("--output-dir")
        except ValueError as error:
            raise HarnessCompileError(
                "job harness launch lacks output directory"
            ) from error
        if output_index + 1 >= len(command) or command[output_index + 1] != "/outputs":
            raise HarnessCompileError("job harness output directory is invalid")


def _is_builtin_launch_command(slug: str, command: tuple[str, ...]) -> bool:
    # Synthetic test projections use a deliberately relative ``serve`` argv.
    # Every trusted executable projection is absolute, so trusted builtin
    # variants still receive the same central writable/security checks even
    # when their wrapper prefix differs from the stock compiler.
    return (
        slug in _BUILTIN_HARNESS_SLUGS
        and command[0].startswith("/")
        and not command[0].startswith(f"{_CUSTOM_ADAPTER_BIN}/")
    )
