"""Shared fail-closed validation and synthetic compiler support."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .contracts import HarnessMount, HarnessProjection

_SAFE_ARGUMENT = re.compile(r"^[A-Za-z0-9_./:+@%=-]{1,2048}$")
_NON_ROOT_UID = re.compile(r"^[1-9][0-9]*(?::[1-9][0-9]*)?$")
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "zsh", "/bin/sh", "/bin/bash"})
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}$")
_SOCKET_NAMES = ("docker.sock", "podman.sock", "containerd.sock", "cri-dockerd.sock")


class HarnessCompileError(ValueError):
    pass


def structured_command(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HarnessCompileError("harness command must be a structured argv")
    command = tuple(value)
    if not command or len(command) > 64:
        raise HarnessCompileError("harness command size is invalid")
    if any(
        not isinstance(item, str) or _SAFE_ARGUMENT.fullmatch(item) is None
        for item in command
    ):
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    if command[0] in _SHELL_EXECUTABLES or "-c" in command:
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    return command


def validate_projection(projection: HarnessProjection) -> None:
    structured_command(projection.command)
    if type(projection.contract_version) is not int or projection.contract_version != 1:
        raise HarnessCompileError("harness contract version is invalid")
    if _IMAGE.fullmatch(projection.image) is None:
        raise HarnessCompileError("harness image must be digest-pinned")
    if projection.network_mode != "none":
        raise HarnessCompileError("harness projection requires an offline network")
    if projection.architecture != "linux/arm64":
        raise HarnessCompileError("harness projection must target linux/arm64")
    if _NON_ROOT_UID.fullmatch(projection.user) is None:
        raise HarnessCompileError(
            "harness projection user must be numeric and non-root"
        )
    if (
        type(projection.no_new_privileges) is not bool
        or not projection.no_new_privileges
    ):
        raise HarnessCompileError("harness projection must set no-new-privileges")
    if projection.capabilities:
        raise HarnessCompileError("harness projection must drop all capabilities")
    if not projection.model_mounts or any(
        type(mount.read_only) is not bool or not mount.read_only
        for mount in projection.model_mounts
    ):
        raise HarnessCompileError("harness model mounts must be read-only")
    if (
        type(projection.output_mount.read_only) is not bool
        or projection.output_mount.read_only
        or type(projection.output_mount.isolated) is not bool
        or not projection.output_mount.isolated
    ):
        raise HarnessCompileError("harness outputs must be isolated and writable")
    mounts = (*projection.model_mounts, projection.output_mount)
    for mount in mounts:
        _mount_path(mount.source)
        _mount_path(mount.target)
    _disjoint_mount_paths(tuple(mount.source for mount in mounts))
    _disjoint_mount_paths(tuple(mount.target for mount in mounts))
    for source in (mount.source for mount in mounts):
        for target in (mount.target for mount in mounts):
            if _overlaps(source, target):
                raise HarnessCompileError("harness mount source and target overlap")
    binding = projection.binding
    if (
        binding is None
        or not isinstance(binding.harness_content_sha256, str)
        or not isinstance(binding.distribution_content_sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", binding.harness_content_sha256)
        or not re.fullmatch(r"[a-f0-9]{64}", binding.distribution_content_sha256)
        or type(binding.topology_node_count) is not int
        or binding.topology_node_count < 1
        or not isinstance(binding.role, str)
        or not binding.role
        or type(binding.rank) is not int
        or not 0 <= binding.rank < binding.topology_node_count
    ):
        raise HarnessCompileError("harness projection binding is invalid")


def _mount_path(value: str) -> None:
    if not isinstance(value, str) or not value.startswith("/") or value == "/":
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


class SyntheticHarnessCompiler:
    """Test double used until concrete harness compilers arrive in Task 5."""

    contract_version = 1

    def __init__(
        self,
        slug: str,
        *,
        source_bundle_digest: str | None = None,
        source_bundle_signer: str | None = None,
    ) -> None:
        self.slug = slug
        self.source_bundle_digest = source_bundle_digest
        self.source_bundle_signer = source_bundle_signer

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
        security = distribution.get("security")
        if not isinstance(security, Mapping):
            raise HarnessCompileError("runtime distribution security is invalid")
        capabilities = security.get("capabilities")
        if not isinstance(capabilities, list) or not all(
            isinstance(value, str) for value in capabilities
        ):
            raise HarnessCompileError("runtime distribution capabilities are invalid")
        return HarnessProjection(
            slug=self.slug,
            contract_version=self.contract_version,
            command=structured_command(entrypoint),
            image=str(distribution.get("image")),
            network_mode=str(security.get("network_mode")),
            architecture="linux/arm64",
            user=str(security.get("user")),
            no_new_privileges=security.get("no_new_privileges") is True,
            capabilities=tuple(capabilities),
            model_mounts=(HarnessMount("/run/vonk/models", "/models", read_only=True),),
            output_mount=HarnessMount(
                "/run/vonk/outputs", "/outputs", read_only=False, isolated=True
            ),
        )
