"""Shared fail-closed validation and synthetic compiler support."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .contracts import HarnessMount, HarnessProjection

_SAFE_ARGUMENT = re.compile(r"^[A-Za-z0-9_./:+@%=-]{1,2048}$")
_NON_ROOT_UID = re.compile(r"^[1-9][0-9]*(?::[1-9][0-9]*)?$")
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "zsh", "/bin/sh", "/bin/bash"})


class HarnessCompileError(ValueError):
    pass


def structured_command(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HarnessCompileError("harness command must be a structured argv")
    command = tuple(value)
    if not command or len(command) > 64:
        raise HarnessCompileError("harness command size is invalid")
    if any(not isinstance(item, str) or _SAFE_ARGUMENT.fullmatch(item) is None for item in command):
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    if command[0] in _SHELL_EXECUTABLES or "-c" in command:
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    return command


def validate_projection(projection: HarnessProjection) -> None:
    structured_command(projection.command)
    if projection.contract_version != 1:
        raise HarnessCompileError("harness contract version is invalid")
    if projection.architecture != "linux/arm64":
        raise HarnessCompileError("harness projection must target linux/arm64")
    if _NON_ROOT_UID.fullmatch(projection.user) is None:
        raise HarnessCompileError("harness projection user must be numeric and non-root")
    if not projection.offline_runtime:
        raise HarnessCompileError("harness runtime must be offline")
    if projection.docker_socket:
        raise HarnessCompileError("harness projection must not expose the Docker socket")
    if not projection.no_new_privileges:
        raise HarnessCompileError("harness projection must set no-new-privileges")
    if projection.capabilities:
        raise HarnessCompileError("harness projection must drop all capabilities")
    if not projection.model_mounts or any(
        not mount.read_only for mount in projection.model_mounts
    ):
        raise HarnessCompileError("harness model mounts must be read-only")
    if projection.output_mount.read_only or not projection.output_mount.isolated:
        raise HarnessCompileError("harness outputs must be isolated and writable")


class SyntheticHarnessCompiler:
    """Test double used until concrete harness compilers arrive in Task 5."""

    contract_version = 1

    def __init__(self, slug: str) -> None:
        self.slug = slug

    def compile(
        self,
        recipe: Mapping[str, object],
        distribution: Mapping[str, object],
        patch: Mapping[str, object] | None,
        parameters: Mapping[str, object],
        topology: Mapping[str, object],
        role: str,
        rank: int,
    ) -> HarnessProjection:
        runtime = recipe.get("runtime")
        entrypoint = runtime.get("entrypoint") if isinstance(runtime, Mapping) else None
        return HarnessProjection(
            slug=self.slug,
            contract_version=self.contract_version,
            command=structured_command(entrypoint),
            architecture="linux/arm64",
            user="10001:10001",
            offline_runtime=True,
            docker_socket=False,
            no_new_privileges=True,
            capabilities=(),
            model_mounts=(HarnessMount("model", "/models", read_only=True),),
            output_mount=HarnessMount(
                "outputs", "/outputs", read_only=False, isolated=True
            ),
        )
