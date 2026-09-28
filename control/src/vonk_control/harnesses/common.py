"""Shared fail-closed validation for execution-harness projections."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath

from vonk_agent_protocol.host_helper import MAX_ARGV_BYTES

from ..runtime_writable_paths import (
    effective_environment,
    resolve_requirements,
    split_requirements,
    telemetry_contract,
    validate_telemetry,
)
from ..runtime_writable_paths import (
    validate_paths as validate_runtime_paths,
)
from ..runtime_writable_paths import (
    writable_paths as engine_writable_paths,
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
        type(projection.architecture) is not str
        or projection.architecture != "linux/arm64"
    ):
        raise HarnessCompileError("harness projection must target linux/arm64")
    if (
        type(projection.user) is not str
        or _NON_ROOT_UID.fullmatch(projection.user) is None
    ):
        raise HarnessCompileError(
            "harness projection user must be numeric and non-root"
        )
    if (
        type(projection.no_new_privileges) is not bool
        or not projection.no_new_privileges
    ):
        raise HarnessCompileError("harness projection must set no-new-privileges")
    if (
        type(projection.capabilities) is not tuple
        or any(type(capability) is not str for capability in projection.capabilities)
        or projection.capabilities
    ):
        raise HarnessCompileError("harness projection must drop all capabilities")
    if type(projection.read_only_root) is not bool or not projection.read_only_root:
        raise HarnessCompileError("harness projection requires a read-only root")
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


def job_input_contract(recipe: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the exact per-job input contract, if this recipe declares one."""
    interfaces = recipe.get("interfaces")
    if type(interfaces) is not list or len(interfaces) != 1:
        raise HarnessCompileError("harness interface is invalid")
    interface = interfaces[0]
    if not isinstance(interface, Mapping):
        raise HarnessCompileError("harness interface is invalid")
    value = interface.get("input")
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value)
        not in (
            {"path", "required", "media_types", "max_bytes"},
            {"path", "required", "media_types", "max_bytes", "slots"},
        )
        or value.get("path") != "/inputs"
        or type(value.get("required")) is not bool
        or type(value.get("media_types")) is not list
        or not value["media_types"]
        or any(
            type(media_type) is not str
            or re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media_type)
            is None
            for media_type in value["media_types"]
        )
        or len(set(value["media_types"])) != len(value["media_types"])
        or len(value["media_types"]) > 16
        or type(value.get("max_bytes")) is not int
        or not 1 <= value["max_bytes"] <= 1024**3
    ):
        raise HarnessCompileError("harness input contract is invalid")
    slots = value.get("slots")
    if slots is not None:
        if type(slots) is not list or not 1 <= len(slots) <= 32:
            raise HarnessCompileError("harness input slot contract is invalid")
        identifiers: set[str] = set()
        for slot in slots:
            if (
                not isinstance(slot, Mapping)
                or set(slot)
                != {
                    "id",
                    "label",
                    "description",
                    "media_types",
                    "extensions",
                    "min_files",
                    "max_files",
                    "max_file_bytes",
                    "max_total_bytes",
                }
                or type(slot.get("id")) is not str
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", slot["id"]) is None
                or slot["id"] in identifiers
                or type(slot.get("media_types")) is not list
                or not slot["media_types"]
                or not set(slot["media_types"]) <= set(value["media_types"])
                or type(slot.get("extensions")) is not list
                or any(
                    type(extension) is not str
                    or re.fullmatch(r"\.[a-z0-9][a-z0-9._-]{0,15}", extension) is None
                    for extension in slot["extensions"]
                )
                or type(slot.get("min_files")) is not int
                or type(slot.get("max_files")) is not int
                or not 0 <= slot["min_files"] <= slot["max_files"] <= 32
                or slot["max_files"] < 1
                or type(slot.get("max_file_bytes")) is not int
                or not 1 <= slot["max_file_bytes"] <= 512 * 1024**2
                or type(slot.get("max_total_bytes")) is not int
                or not slot["max_file_bytes"]
                <= slot["max_total_bytes"]
                <= value["max_bytes"]
            ):
                raise HarnessCompileError("harness input slot contract is invalid")
            identifiers.add(slot["id"])
    return value


def validate_topology(
    topology: Mapping[str, object],
    role: str,
    rank: int,
    *,
    modes: frozenset[str],
) -> tuple[int, Mapping[str, object]]:
    if not isinstance(topology, Mapping):
        raise HarnessCompileError("harness topology is invalid")
    node_count = topology.get("node_count")
    mode = topology.get("mode")
    parallelism = topology.get("parallelism")
    fabric = topology.get("fabric")
    roles = topology.get("roles")
    if (
        type(node_count) is not int
        or node_count < 1
        or type(mode) is not str
        or mode not in modes
        or not isinstance(parallelism, Mapping)
        or not isinstance(fabric, Mapping)
        or type(roles) is not list
        or type(role) is not str
        or type(rank) is not int
        or not 0 <= rank < node_count
    ):
        raise HarnessCompileError("harness topology is invalid")
    world_size = parallelism.get("world_size")
    tensor = parallelism.get("tensor")
    pipeline = parallelism.get("pipeline")
    data = parallelism.get("data")
    if (
        type(world_size) is not int
        or world_size != node_count
        or type(tensor) is not int
        or type(pipeline) is not int
        or type(data) is not int
        or tensor < 1
        or pipeline < 1
        or data < 1
        or tensor * pipeline * data != node_count
    ):
        raise HarnessCompileError("harness topology parallelism is inconsistent")
    backend = parallelism.get("backend")
    dimensions = (tensor, pipeline, data)
    mode_is_consistent = (
        mode == "single"
        and node_count == 1
        and dimensions == (1, 1, 1)
        or mode == "tensor_parallel"
        and node_count > 1
        and tensor == node_count
        and pipeline == data == 1
        or mode == "pipeline_parallel"
        and node_count > 1
        and pipeline == node_count
        and tensor == data == 1
        or mode == "data_parallel"
        and node_count > 1
        and data == node_count
        and tensor == pipeline == 1
        or mode == "hybrid"
        and node_count > 1
        and sum(dimension > 1 for dimension in dimensions) > 1
        or mode == "ray"
        and node_count > 1
        and backend == "ray"
        or mode == "mpi"
        and node_count > 1
        and backend == "mpi"
        or mode == "distributed"
        and node_count > 1
        and tensor == node_count
        and pipeline == data == 1
    )
    if not mode_is_consistent:
        raise HarnessCompileError(
            "harness topology mode and parallelism are inconsistent"
        )
    connectivity = fabric.get("connectivity")
    bandwidth = fabric.get("minimum_bandwidth_mbps")
    if (
        type(backend) is not str
        or not backend
        or type(connectivity) is not str
        or type(bandwidth) is not int
        or bandwidth < 0
        or (
            node_count == 1
            and (backend != "local" or connectivity != "none" or bandwidth != 0)
        )
        or (
            node_count > 1
            and (backend == "local" or connectivity == "none" or bandwidth < 1)
        )
    ):
        raise HarnessCompileError("harness topology fabric is inconsistent")
    offset = 0
    matched = False
    for declared_role in roles:
        if not isinstance(declared_role, Mapping):
            raise HarnessCompileError("harness topology role is invalid")
        name = declared_role.get("name")
        count = declared_role.get("count")
        if type(name) is not str or type(count) is not int or count < 1:
            raise HarnessCompileError("harness topology role is invalid")
        if name == role and offset <= rank < offset + count:
            matched = True
        offset += count
    if offset != node_count or not matched:
        raise HarnessCompileError("harness topology role and rank are inconsistent")
    return node_count, parallelism


def projection(
    *,
    slug: str,
    command: tuple[str, ...],
    recipe: Mapping[str, object],
    distribution: Mapping[str, object],
    environment: tuple[tuple[str, str], ...],
    allow_local_media_input: bool = False,
) -> HarnessProjection:
    platform = distribution.get("platform")
    image = distribution.get("image")
    security = distribution.get("security")
    if (
        type(platform) is not str
        or platform != "linux/arm64"
        or type(image) is not str
        or not isinstance(security, Mapping)
        or set(security)
        != {"network_mode", "user", "no_new_privileges", "capabilities"}
        or security.get("network_mode") != "none"
        or type(security.get("user")) is not str
        or security.get("no_new_privileges") is not True
        or security.get("capabilities") != []
    ):
        raise HarnessCompileError("runtime distribution security is invalid")
    distribution_capabilities = distribution.get("capabilities")
    distributed_runtime = None
    if isinstance(distribution_capabilities, Mapping):
        distributed_runtime = next(
            (
                distribution_capabilities.get(name)
                for name in ("distributed_vllm", "distributed_sglang")
                if isinstance(distribution_capabilities.get(name), Mapping)
            ),
            None,
        )
    topology = recipe.get("topology")
    _require_recipe_mounts(
        recipe,
        str(security["user"]),
        allow_host_network=isinstance(distributed_runtime, Mapping)
        and distributed_runtime.get("verified") is True
        and isinstance(topology, Mapping)
        and topology.get("mode") == "distributed",
        allow_input_mount=allow_local_media_input,
    )
    input_mount = (
        HarnessMount("/run/vonk/inputs", "/inputs", read_only=True, isolated=True)
        if job_input_contract(recipe) is not None or allow_local_media_input
        else None
    )
    try:
        declared, remaining = split_requirements(environment)
        requirement_paths, requirement_environment = resolve_requirements(
            slug, declared
        )
        effective = effective_environment(slug, remaining)
    except (TypeError, ValueError) as error:
        raise HarnessCompileError(str(error)) from error
    effective = _merge_requirement_environment(effective, requirement_environment)
    runtime_paths = (*engine_writable_paths(slug), *requirement_paths)
    validate_runtime_paths(slug, runtime_paths, dict(effective))
    value = HarnessProjection(
        slug=slug,
        contract_version=1,
        command=structured_command(command),
        image=image,
        network_mode="none",
        architecture="linux/arm64",
        user=str(security["user"]),
        no_new_privileges=True,
        capabilities=(),
        model_mounts=(HarnessMount("/run/vonk/models", "/models", read_only=True),),
        output_mount=HarnessMount(
            "/run/vonk/outputs", "/outputs", read_only=False, isolated=True
        ),
        input_mount=input_mount,
        environment=effective,
        writable_paths=runtime_paths,
        telemetry=telemetry_contract(slug),
        read_only_root=True,
    )
    return value


def _merge_requirement_environment(
    environment: tuple[tuple[str, str], ...],
    requirement_environment: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    names = {name for name, _value in environment}
    for name, _value in requirement_environment:
        if name in names:
            raise HarnessCompileError(
                f"runtime requirement environment collides with the recipe: {name}"
            )
    return (*environment, *requirement_environment)


_ENGINE_LAUNCH_PREFIXES: dict[str, tuple[str, ...]] = {
    "vllm": ("/opt/vonk/bin/vllm", "serve"),
    "sglang": ("/opt/vonk/bin/sglang-serve",),
    "tensorrt-llm": ("/usr/local/bin/trtllm-serve", "serve", "/models"),
    "llama-cpp": ("/opt/vonk/bin/llama-server",),
    "ds4": ("/opt/vonk/bin/ds4-serve",),
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
    if slug in {"vllm", "sglang", "tensorrt-llm", "llama-cpp", "ds4"}:
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


def model_artifact_mounts(
    recipe: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    """Return canonical artifact ids and their declared model mount targets."""
    artifacts = recipe.get("artifacts")
    if type(artifacts) is not list or not artifacts:
        raise HarnessCompileError("harness recipe artifact mounts are invalid")
    result: list[tuple[str, str]] = []
    ids: set[str] = set()
    targets: set[str] = set()
    for artifact in artifacts:
        artifact_id = artifact.get("id") if isinstance(artifact, Mapping) else None
        mount = artifact.get("mount") if isinstance(artifact, Mapping) else None
        target = mount.get("target") if isinstance(mount, Mapping) else None
        if len(artifacts) == 1 and artifact_id is None and target == "/models":
            # Synthetic harness conformance predates the full recipe schema. Keep its
            # sole canonical root mount valid without weakening named mount checks.
            artifact_id = "model"
        if (
            type(artifact_id) is not str
            or _SAFE_ARTIFACT_ID.fullmatch(artifact_id) is None
            or artifact_id in ids
            or not isinstance(mount, Mapping)
            or set(mount) != {"target", "read_only"}
            or type(target) is not str
            or mount.get("read_only") is not True
            or target in targets
        ):
            raise HarnessCompileError("harness recipe artifact mounts are invalid")
        result.append((artifact_id, target))
        ids.add(artifact_id)
        targets.add(target)
    if len(result) == 1:
        artifact_id, target = result[0]
        if target not in {"/models", f"/models/{artifact_id}"}:
            raise HarnessCompileError("harness recipe artifact mount path is invalid")
    elif "target" not in ids or any(
        target != f"/models/{artifact_id}" for artifact_id, target in result
    ):
        raise HarnessCompileError(
            "multiple model artifacts require unique canonical mount paths and one target artifact"
        )
    return tuple(result)


def integer(minimum: int, maximum: int) -> Callable[[str], bool]:
    def validate(value: str) -> bool:
        try:
            parsed = int(value)
        except ValueError:
            return False
        return str(parsed) == value and minimum <= parsed <= maximum

    return validate


def model_file(*suffixes: str) -> Callable[[str], bool]:
    return lambda value: (
        value.startswith("/models/")
        and not any(part in value for part in ("//", "/./", "/../", "\\"))
        and value.endswith(suffixes)
    )


def sha256(value: str) -> bool:
    return re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _require_recipe_mounts(
    recipe: Mapping[str, object],
    user: str,
    *,
    allow_host_network: bool = False,
    allow_input_mount: bool = False,
) -> None:
    model_artifact_mounts(recipe)
    runtime = recipe.get("runtime")
    security = runtime.get("security") if isinstance(runtime, Mapping) else None
    mounts = security.get("mounts") if isinstance(security, Mapping) else None
    input_contract = job_input_contract(recipe)
    expected_mounts = {
        ("model", "/models", True),
        ("outputs", "/outputs", False),
    }
    if input_contract is not None or allow_input_mount:
        expected_mounts.add(("inputs", "/inputs", True))
    if (
        not isinstance(security, Mapping)
        or security.get("user") != user
        or security.get("privileged") is not False
        or (
            security.get("host_network") is not False
            and not (allow_host_network and security.get("host_network") is True)
        )
        or security.get("capabilities") != []
        or type(mounts) is not list
        or {
            (
                mount.get("source"),
                mount.get("target"),
                mount.get("read_only"),
            )
            for mount in mounts
            if isinstance(mount, Mapping)
        }
        != expected_mounts
    ):
        raise HarnessCompileError("harness recipe mounts or input mount are invalid")
