"""Shared fail-closed validation for execution-harness compilers."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import PurePosixPath

from .contracts import HarnessBinding, HarnessMount, HarnessProjection

_SAFE_ARGUMENT = re.compile(r'^[A-Za-z0-9_./:+@%=\[\]{},"<>-]{1,2048}$')
_SAFE_ADAPTER_BASENAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SAFE_ARTIFACT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_NON_ROOT_UID = re.compile(r"^[1-9][0-9]*(?::[1-9][0-9]*)?$")
_SHELL_EXECUTABLES = frozenset(
    {"sh", "bash", "dash", "ash", "zsh", "ksh", "csh", "tcsh", "fish", "busybox"}
)
_SHELL_LAUNCHERS = frozenset({"env", "busybox", "sudo", "doas"})
_CUSTOM_ADAPTER_BIN = PurePosixPath("/opt/vonk/adapters/bin")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}$")
_SOCKET_NAMES = ("docker.sock", "podman.sock", "containerd.sock", "cri-dockerd.sock")


class HarnessCompileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    """One recipe argument admitted by a concrete built-in compiler."""

    flag: str
    takes_value: bool = True
    emit: bool = True
    validate: Callable[[str], bool] = lambda _value: True


def structured_command(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HarnessCompileError("harness command must be a structured argv")
    command = tuple(value)
    if not command or len(command) > 64:
        raise HarnessCompileError("harness command size is invalid")
    if any(
        type(item) is not str or _SAFE_ARGUMENT.fullmatch(item) is None
        for item in command
    ):
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    executable = PurePosixPath(command[0]).name.lower()
    if executable in (_SHELL_EXECUTABLES | _SHELL_LAUNCHERS) or "-c" in command:
        raise HarnessCompileError("harness command contains unsafe shell syntax")
    return command


def custom_adapter_command(value: object) -> tuple[str, ...]:
    command = structured_command(value)
    executable = command[0]
    path = PurePosixPath(executable)
    if (
        not executable.startswith("/")
        or "//" in executable
        or "\\" in executable
        or "/./" in executable
        or "/../" in executable
        or executable.endswith(("/", "/.", "/.."))
        or path.as_posix() != executable
        or path.parent != _CUSTOM_ADAPTER_BIN
        or _SAFE_ADAPTER_BASENAME.fullmatch(path.name) is None
    ):
        raise HarnessCompileError(
            "custom adapter executable is outside the dedicated allowlist"
        )
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
    if type(projection.network_mode) is not str or projection.network_mode != "none":
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
    if (
        type(projection.environment) is not tuple
        or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", item[0])
            or type(item[1]) is not str
            or _SAFE_ARGUMENT.fullmatch(item[1]) is None
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
        or type(binding.distribution_content_sha256) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", binding.harness_content_sha256)
        or not re.fullmatch(r"[a-f0-9]{64}", binding.distribution_content_sha256)
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


def require_entrypoint(
    recipe: Mapping[str, object], expected: tuple[str, ...]
) -> Mapping[str, object]:
    runtime = recipe.get("runtime")
    if not isinstance(runtime, Mapping):
        raise HarnessCompileError("harness recipe runtime is invalid")
    entrypoint = runtime.get("entrypoint")
    if type(entrypoint) is not list or tuple(entrypoint) != expected:
        raise HarnessCompileError("harness recipe entrypoint is invalid")
    structured_command(tuple(entrypoint))
    return runtime


def compile_arguments(
    recipe: Mapping[str, object],
    parameters: Mapping[str, object],
    specifications: Mapping[str, ArgumentSpec],
) -> tuple[tuple[str, ...], dict[str, str | bool]]:
    """Render exact typed recipe arguments through one engine allowlist."""
    runtime = recipe.get("runtime")
    arguments = runtime.get("arguments") if isinstance(runtime, Mapping) else None
    if type(arguments) is not list or not isinstance(parameters, Mapping):
        raise HarnessCompileError("harness recipe arguments are invalid")
    declarations = _parameter_declarations(recipe.get("parameters"))
    rendered: list[str] = []
    parsed: dict[str, str | bool] = {}
    referenced: set[str] = set()
    for item in arguments:
        if not isinstance(item, Mapping) or set(item) not in (
            {"name", "value"},
            {"name", "parameter"},
        ):
            raise HarnessCompileError("harness recipe argument is invalid")
        name = item.get("name")
        if type(name) is not str:
            raise HarnessCompileError("harness recipe argument is invalid")
        specification = specifications.get(name)
        if specification is None:
            raise HarnessCompileError(f"harness argument is not allowlisted: {name}")
        if specification.flag in parsed:
            raise HarnessCompileError(
                f"harness argument is repeated: {specification.flag}"
            )
        if "parameter" in item:
            parameter = item.get("parameter")
            if type(parameter) is not str or parameter not in declarations:
                raise HarnessCompileError("harness parameter reference is invalid")
            if parameter not in parameters:
                raise HarnessCompileError("harness parameter value is missing")
            value = parameters[parameter]
            _validate_parameter_value(parameter, value, declarations[parameter])
            referenced.add(parameter)
        else:
            value = item.get("value")
        if specification.takes_value:
            text = _safe_scalar(value, "harness argument value")
            if not specification.validate(text):
                raise HarnessCompileError(f"harness argument value is invalid: {name}")
            parsed[specification.flag] = text
            if specification.emit:
                rendered.extend((specification.flag, text))
        else:
            if type(value) is not bool:
                raise HarnessCompileError(
                    f"harness presence argument value is invalid: {name}"
                )
            parsed[specification.flag] = value
            if specification.emit and value:
                rendered.append(specification.flag)
    if (
        any(type(name) is not str for name in parameters)
        or set(parameters) != referenced
    ):
        raise HarnessCompileError("harness parameters are not exact")
    structured_command(("/opt/vonk/bin/argv-check", *rendered))
    return tuple(rendered), parsed


def require_literal_arguments(
    recipe: Mapping[str, object], names: frozenset[str], *, label: str
) -> None:
    """Require selected identity-bearing arguments to be recipe literals."""
    runtime = recipe.get("runtime")
    arguments = runtime.get("arguments") if isinstance(runtime, Mapping) else None
    if type(arguments) is not list:
        raise HarnessCompileError(f"{label} is invalid")
    selected = [
        item
        for item in arguments
        if isinstance(item, Mapping) and item.get("name") in names
    ]
    if (
        len(selected) != len(names)
        or {item.get("name") for item in selected} != names
        or any(set(item) != {"name", "value"} for item in selected)
    ):
        raise HarnessCompileError(f"{label} must be a literal immutable workflow")


def compile_environment(
    recipe: Mapping[str, object], allowlist: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    runtime = recipe.get("runtime")
    environment = runtime.get("environment") if isinstance(runtime, Mapping) else None
    if type(environment) is not list:
        raise HarnessCompileError("harness environment is invalid")
    result: list[tuple[str, str]] = []
    names: set[str] = set()
    for item in environment:
        if not isinstance(item, Mapping) or set(item) != {"name", "value"}:
            raise HarnessCompileError("harness environment is invalid")
        name = item.get("name")
        if type(name) is not str or name not in allowlist or name in names:
            raise HarnessCompileError("harness environment is not allowlisted")
        result.append((name, _safe_scalar(item.get("value"), "harness environment")))
        names.add(name)
    return tuple(result)


def require_openai_interface(recipe: Mapping[str, object]) -> int:
    interfaces = recipe.get("interfaces")
    if type(interfaces) is not list or len(interfaces) != 1:
        raise HarnessCompileError("harness interface is invalid")
    interface = interfaces[0]
    if not isinstance(interface, Mapping) or interface.get("adapter") != "openai":
        raise HarnessCompileError("harness interface is incompatible")
    port = interface.get("port")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise HarnessCompileError("harness interface port is invalid")
    return port


def require_job_interface(recipe: Mapping[str, object], allowed: frozenset[str]) -> str:
    interfaces = recipe.get("interfaces")
    if type(interfaces) is not list or len(interfaces) != 1:
        raise HarnessCompileError("harness interface is invalid")
    interface = interfaces[0]
    adapter = interface.get("adapter") if isinstance(interface, Mapping) else None
    if type(adapter) is not str or adapter not in allowed:
        raise HarnessCompileError("harness interface is incompatible")
    if interface.get("path") != "/outputs":
        raise HarnessCompileError("harness job interface path is invalid")
    return adapter


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
        or set(value) != {"path", "required", "media_types", "max_bytes"}
        or value.get("path") != "/inputs"
        or type(value.get("required")) is not bool
        or type(value.get("media_types")) is not list
        or not value["media_types"]
        or any(
            type(media_type) is not str
            or re.fullmatch(
                r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media_type
            ) is None
            for media_type in value["media_types"]
        )
        or len(set(value["media_types"])) != len(value["media_types"])
        or len(value["media_types"]) > 16
        or type(value.get("max_bytes")) is not int
        or value["max_bytes"] < 1
    ):
        raise HarnessCompileError("harness input contract is invalid")
    return value


def require_mime_validator(
    recipe: Mapping[str, object], interface: str, output_mime: str
) -> None:
    expected_family = {
        "image-job": "image/",
        "audio-job": "audio/",
        "video-job": "video/",
        "mesh-job": "model/",
    }.get(interface)
    if (
        not output_mime
        or "/" not in output_mime
        or (expected_family is not None and not output_mime.startswith(expected_family))
    ):
        raise HarnessCompileError("harness job interface MIME family is incompatible")
    validation = recipe.get("validation")
    validators = (
        validation.get("validators") if isinstance(validation, Mapping) else None
    )
    check = "artifact.mime." + output_mime.replace("/", "-")
    if type(validators) is not list or len(validators) != 1:
        raise HarnessCompileError("harness requires one declared MIME validator")
    validator = validators[0]
    checks = validator.get("checks") if isinstance(validator, Mapping) else None
    if (
        not isinstance(validator, Mapping)
        or validator.get("interface") != interface
        or type(checks) is not list
        or checks != [check]
    ):
        raise HarnessCompileError("harness requires one declared MIME validator")


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
    dimensions = tuple(parallelism.get(name) for name in ("tensor", "pipeline", "data"))
    if (
        type(world_size) is not int
        or world_size != node_count
        or any(type(value) is not int or value < 1 for value in dimensions)
        or dimensions[0] * dimensions[1] * dimensions[2] != node_count
    ):
        raise HarnessCompileError("harness topology parallelism is inconsistent")
    backend = parallelism.get("backend")
    tensor, pipeline, data = dimensions
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
    )
    input_mount = (
        HarnessMount("/run/vonk/inputs", "/inputs", read_only=True, isolated=True)
        if job_input_contract(recipe) is not None
        else None
    )
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
        environment=environment,
    )
    return value


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


def primary_model_artifact_mount(
    recipe: Mapping[str, object],
) -> tuple[str, str]:
    """Resolve the sole artifact, or the explicit ``target`` artifact."""
    mounts = model_artifact_mounts(recipe)
    if len(mounts) == 1:
        return mounts[0]
    return next(item for item in mounts if item[0] == "target")


def integer(minimum: int, maximum: int) -> Callable[[str], bool]:
    def validate(value: str) -> bool:
        try:
            parsed = int(value)
        except ValueError:
            return False
        return str(parsed) == value and minimum <= parsed <= maximum

    return validate


def decimal(minimum: float, maximum: float) -> Callable[[str], bool]:
    def validate(value: str) -> bool:
        try:
            parsed = float(value)
        except ValueError:
            return False
        return isfinite(parsed) and minimum <= parsed <= maximum

    return validate


def one_of(*accepted: str) -> Callable[[str], bool]:
    values = frozenset(accepted)
    return lambda value: value in values


def model_file(*suffixes: str) -> Callable[[str], bool]:
    return lambda value: (
        value.startswith("/models/")
        and not any(part in value for part in ("//", "/./", "/../", "\\"))
        and value.endswith(suffixes)
    )


def source_bundle_file(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        value.startswith("/opt/vonk/source/")
        and "//" not in value
        and "\\" not in value
        and "/./" not in value
        and "/../" not in value
        and path.as_posix() == value
        and path.suffix == ".py"
    )


def require_source_bundle_identity(recipe: Mapping[str, object]) -> None:
    build = recipe.get("build")
    context = build.get("context") if isinstance(build, Mapping) else None
    required = {"sha256", "expected_bytes", "media_type"}
    allowed = required | {"path"}
    path = context.get("path") if isinstance(context, Mapping) else None
    path_is_valid = (
        path is None
        or (
            type(path) is str
            and bool(path)
            and not path.startswith("/")
            and "\\" not in path
            and all(part not in {"", ".", ".."} for part in path.split("/"))
            and PurePosixPath(path).as_posix() == path
        )
    )
    if (
        not isinstance(context, Mapping)
        or not required <= set(context)
        or not set(context) <= allowed
        or type(context.get("sha256")) is not str
        or not sha256(str(context["sha256"]))
        or type(context.get("expected_bytes")) is not int
        or context["expected_bytes"] < 1
        or context.get("media_type")
        != "application/vnd.vonk-forge.source-bundle.v1+tar"
        or not path_is_valid
    ):
        raise HarnessCompileError("PyTorch pipeline source bundle identity is invalid")


def workflow_file(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        value.startswith("/opt/vonk/source/workflows/")
        and "//" not in value
        and "\\" not in value
        and "/./" not in value
        and "/../" not in value
        and path.as_posix() == value
        and path.suffix == ".json"
    )


def sha256(value: str) -> bool:
    return re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _safe_scalar(value: object, label: str) -> str:
    if type(value) is bool:
        rendered = str(value).lower()
    elif type(value) in (str, int):
        rendered = str(value)
    else:
        raise HarnessCompileError(f"{label} value is invalid")
    if _SAFE_ARGUMENT.fullmatch(rendered) is None:
        raise HarnessCompileError(f"{label} value contains unsafe shell syntax")
    return rendered


def _parameter_declarations(value: object) -> dict[str, Mapping[str, object]]:
    if type(value) is not list:
        raise HarnessCompileError("harness parameter declarations are invalid")
    declarations: dict[str, Mapping[str, object]] = {}
    for item in value:
        name = item.get("name") if isinstance(item, Mapping) else None
        if type(name) is not str or name in declarations:
            raise HarnessCompileError("harness parameter declarations are invalid")
        declarations[name] = item
    return declarations


def _validate_parameter_value(
    name: str, value: object, declaration: Mapping[str, object]
) -> None:
    kind = declaration.get("type")
    valid = (
        kind == "integer"
        and type(value) is int
        and (
            type(declaration.get("minimum")) is not int
            or value >= declaration["minimum"]
        )
        and (
            type(declaration.get("maximum")) is not int
            or value <= declaration["maximum"]
        )
    ) or (kind == "boolean" and type(value) is bool)
    if kind in {"string", "enum"}:
        valid = type(value) is str and (
            kind != "enum" or value in declaration.get("allowed_values", ())
        )
    if not valid:
        raise HarnessCompileError(f"harness parameter value is invalid: {name}")


def _require_recipe_mounts(
    recipe: Mapping[str, object], user: str, *, allow_host_network: bool = False
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
    if input_contract is not None:
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


class SyntheticHarnessCompiler:
    """Test-only compiler double; production composition never registers it."""

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
        if type(capabilities) is not list or not all(
            type(value) is str for value in capabilities
        ):
            raise HarnessCompileError("runtime distribution capabilities are invalid")
        image = distribution.get("image")
        network_mode = security.get("network_mode")
        user = security.get("user")
        if (
            type(image) is not str
            or type(network_mode) is not str
            or type(user) is not str
        ):
            raise HarnessCompileError(
                "runtime distribution security strings are invalid"
            )
        return HarnessProjection(
            slug=self.slug,
            contract_version=self.contract_version,
            command=structured_command(entrypoint),
            image=image,
            network_mode=network_mode,
            architecture="linux/arm64",
            user=user,
            no_new_privileges=security.get("no_new_privileges") is True,
            capabilities=tuple(capabilities),
            model_mounts=(HarnessMount("/run/vonk/models", "/models", read_only=True),),
            output_mount=HarnessMount(
                "/run/vonk/outputs", "/outputs", read_only=False, isolated=True
            ),
        )
