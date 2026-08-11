from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .schema_resources import read_runtime_schema


class RecipeContractError(ValueError):
    def __init__(self, code: str, path: str, detail: str) -> None:
        self.code = code
        self.path = path
        self.detail = detail[:240]
        super().__init__(f"{path}: {self.detail}")


def _reject_float(_: str) -> None:
    raise RecipeContractError(
        "recipe.float_forbidden", "$", "floats are not permitted"
    )


def _reject_constant(_: str) -> None:
    raise RecipeContractError(
        "recipe.float_forbidden", "$", "floats are not permitted"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecipeContractError(
                "recipe.duplicate_key", "$", f"duplicate object key: {key}"
            )
        result[key] = value
    return result


def parse_recipe_json(payload: bytes | str) -> Mapping[str, object]:
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except RecipeContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecipeContractError(
            "recipe.invalid_json", "$", "recipe is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict):
        raise RecipeContractError(
            "recipe.object_required", "$", "recipe must be a JSON object"
        )
    return document


def _assert_canonical_value(value: object, path: str = "$") -> None:
    if isinstance(value, float):
        raise RecipeContractError(
            "recipe.float_forbidden", path, "floats are not permitted"
        )
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise RecipeContractError(
                    "recipe.key_type", path, "object keys must be strings"
                )
            _assert_canonical_value(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_canonical_value(child, f"{path}[{index}]")
        return
    raise RecipeContractError(
        "recipe.value_type", path, "recipe contains an unsupported value type"
    )


def canonical_recipe(document: Mapping[str, object]) -> bytes:
    _assert_canonical_value(document)
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def recipe_content_sha256(document: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_recipe(document)).hexdigest()


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(read_runtime_schema("recipe-v1.schema.json"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_recipe(document: Mapping[str, object]) -> None:
    errors = sorted(
        _validator().iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = _most_specific(errors[0])
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise RecipeContractError(
            f"recipe.schema.{error.validator}", path, _safe_detail(error)
        )
    _validate_recipe_semantics(document)


def deployment_profile(
    document: Mapping[str, object], name: str
) -> Mapping[str, object]:
    profiles = document.get("deployment_profiles")
    if not isinstance(profiles, Sequence) or isinstance(profiles, (str, bytes)):
        raise RecipeContractError(
            "recipe.profiles", "deployment_profiles", "profiles are required"
        )
    matches = [
        profile
        for profile in profiles
        if isinstance(profile, Mapping) and profile.get("name") == name
    ]
    if len(matches) != 1:
        raise RecipeContractError(
            "recipe.profile_unknown",
            "deployment_profiles",
            "profile name is missing or not unique",
        )
    return matches[0]


def _validate_recipe_semantics(document: Mapping[str, object]) -> None:
    parameters = _mapping_sequence(document.get("parameters"), "parameters")
    parameter_names = _unique_field(parameters, "name", "parameters")
    for index, parameter in enumerate(parameters):
        path = f"parameters.{index}"
        default = parameter.get("default")
        kind = parameter.get("type")
        if (
            (kind == "integer" and (not isinstance(default, int) or isinstance(default, bool)))
            or (kind == "boolean" and not isinstance(default, bool))
            or (kind in {"string", "enum"} and not isinstance(default, str))
        ):
            raise RecipeContractError(
                "recipe.parameter_type", f"{path}.default", "default does not match parameter type"
            )
        minimum = parameter.get("minimum")
        maximum = parameter.get("maximum")
        if isinstance(minimum, int) and isinstance(maximum, int) and minimum > maximum:
            raise RecipeContractError(
                "recipe.parameter_bounds", path, "minimum exceeds maximum"
            )
        allowed = parameter.get("allowed_values")
        if kind == "enum" and (
            not isinstance(allowed, Sequence) or default not in allowed
        ):
            raise RecipeContractError(
                "recipe.parameter_enum",
                f"{path}.allowed_values",
                "enum default must be allowed",
            )

    artifacts = _mapping_sequence(document.get("artifacts"), "artifacts")
    artifact_ids = _unique_field(artifacts, "id", "artifacts")
    runtime = document.get("runtime")
    if not isinstance(runtime, Mapping):
        raise RecipeContractError("recipe.runtime", "runtime", "runtime is required")
    arguments = _mapping_sequence(runtime.get("arguments"), "runtime.arguments")
    for index, argument in enumerate(arguments):
        parameter = argument.get("parameter")
        if parameter is not None and parameter not in parameter_names:
            raise RecipeContractError(
                "recipe.parameter_unknown",
                f"runtime.arguments.{index}.parameter",
                "parameter does not exist",
            )

    profiles = _mapping_sequence(
        document.get("deployment_profiles"), "deployment_profiles"
    )
    _unique_field(profiles, "name", "deployment_profiles")
    all_role_names: set[object] = set()
    for profile_index, profile in enumerate(profiles):
        path = f"deployment_profiles.{profile_index}"
        roles = _mapping_sequence(profile.get("roles"), f"{path}.roles")
        role_names = _unique_field(roles, "name", f"{path}.roles")
        all_role_names.update(role_names)
        node_count = profile.get("node_count")
        if sum(int(role["count"]) for role in roles) != node_count:
            raise RecipeContractError(
                "recipe.profile_node_count",
                f"{path}.node_count",
                "role counts must equal node_count",
            )
        owners = [role for role in roles if role.get("endpoint_owner") is True]
        if len(owners) != 1 or owners[0].get("count") != 1:
            raise RecipeContractError(
                "recipe.profile_endpoint",
                f"{path}.roles",
                "exactly one single-node role must own the endpoint",
            )
        parallelism = profile.get("parallelism")
        if not isinstance(parallelism, Mapping):
            raise RecipeContractError(
                "recipe.profile_parallelism", f"{path}.parallelism", "parallelism is required"
            )
        world_size = (
            int(parallelism["tensor"])
            * int(parallelism["pipeline"])
            * int(parallelism["data"])
        )
        if world_size != node_count:
            raise RecipeContractError(
                "recipe.profile_parallelism",
                f"{path}.parallelism",
                "parallelism product must equal node_count",
            )
        fabric = profile.get("fabric")
        if not isinstance(fabric, Mapping):
            raise RecipeContractError(
                "recipe.profile_fabric", f"{path}.fabric", "fabric is required"
            )
        if (node_count == 1) != (fabric.get("connectivity") == "none"):
            raise RecipeContractError(
                "recipe.profile_fabric",
                f"{path}.fabric",
                "only a one-node profile may use no fabric",
            )
        overrides = profile.get("parameter_overrides")
        if not isinstance(overrides, Mapping) or not set(overrides).issubset(
            parameter_names
        ):
            raise RecipeContractError(
                "recipe.parameter_unknown",
                f"{path}.parameter_overrides",
                "override parameter does not exist",
            )
        for role_index, role in enumerate(roles):
            if set(role.get("artifacts", ())) - artifact_ids:
                raise RecipeContractError(
                    "recipe.artifact_unknown",
                    f"{path}.roles.{role_index}.artifacts",
                    "artifact does not exist",
                )

    for index, artifact in enumerate(artifacts):
        if set(artifact.get("roles", ())) - all_role_names:
            raise RecipeContractError(
                "recipe.role_unknown",
                f"artifacts.{index}.roles",
                "role does not exist in a profile",
            )


def _mapping_sequence(value: object, path: str) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecipeContractError("recipe.array", path, "array is required")
    if not all(isinstance(item, Mapping) for item in value):
        raise RecipeContractError("recipe.object", path, "object entries are required")
    return list(value)  # type: ignore[arg-type]


def _unique_field(
    values: Sequence[Mapping[str, object]], field: str, path: str
) -> set[object]:
    names = [value.get(field) for value in values]
    if len(names) != len(set(names)):
        raise RecipeContractError(
            "recipe.unique", path, f"{field} values must be unique"
        )
    return set(names)


def _most_specific(error: ValidationError) -> ValidationError:
    candidates = [error]
    pending = list(error.context)
    while pending:
        candidate = pending.pop()
        candidates.append(candidate)
        pending.extend(candidate.context)
    return max(
        candidates,
        key=lambda candidate: (
            candidate.validator == "required",
            len(candidate.absolute_path),
            -len(candidate.context),
        ),
    )


def _safe_detail(error: ValidationError) -> str:
    field = str(error.absolute_path[-1]) if error.absolute_path else "recipe"
    if error.validator == "required":
        missing = sorted(set(error.validator_value) - set(error.instance))
        return f"required field missing: {missing[0]}"
    if error.validator == "additionalProperties":
        allowed = set(error.schema.get("properties", {}))
        extra = sorted(set(error.instance) - allowed)
        return f"unexpected field: {extra[0] if extra else field}"
    if error.validator == "const":
        return f"{field} must equal {error.validator_value!r}"
    if error.validator == "pattern":
        return f"{field} does not match the required format"
    return f"{field} violates the {error.validator} constraint"
