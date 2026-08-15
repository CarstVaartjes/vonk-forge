"""Compile a role-specific agent runtime spec from immutable local authority."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping

from .recipe_contract import validate_recipe

_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class RecipeRuntimeSpecError(ValueError):
    pass


def compile_runtime_spec(
    document: Mapping[str, object],
    *,
    parameters: Mapping[str, object],
    role: str,
    recipe_build_id: str,
    image_digest: str,
) -> dict[str, object]:
    validate_recipe(document)
    if _OCI_DIGEST.fullmatch(image_digest) is None:
        raise RecipeRuntimeSpecError("built image digest is invalid")
    runtime = document["runtime"]
    execution = document["execution"]
    interfaces = document["interfaces"]
    artifacts = document["artifacts"]
    if (
        not isinstance(runtime, Mapping)
        or not isinstance(execution, Mapping)
        or not isinstance(interfaces, list)
        or not isinstance(artifacts, list)
    ):
        raise RecipeRuntimeSpecError("recipe runtime is invalid")
    arguments: list[dict[str, object]] = []
    for raw in runtime["arguments"]:
        if not isinstance(raw, Mapping):
            raise RecipeRuntimeSpecError("runtime argument is invalid")
        if "parameter" in raw:
            parameter = raw["parameter"]
            if not isinstance(parameter, str) or parameter not in parameters:
                raise RecipeRuntimeSpecError("mapped runtime parameter is unavailable")
            value = copy.deepcopy(parameters[parameter])
        else:
            value = copy.deepcopy(raw["value"])
        arguments.append({"name": str(raw["name"]), "value": value})
    role_artifacts = [
        copy.deepcopy(item)
        for item in artifacts
        if isinstance(item, Mapping)
        and isinstance(item.get("roles"), list)
        and role in item["roles"]
    ]
    if not role_artifacts:
        raise RecipeRuntimeSpecError("mapped role has no runtime artifacts")
    compiled_runtime = {
        "execution_harness": copy.deepcopy(execution["harness"]),
        "distribution": copy.deepcopy(runtime["distribution"]),
        "image": (f"localhost/vonk/recipe-build-{recipe_build_id}@{image_digest}"),
        "architecture": "linux/arm64",
        "entrypoint": copy.deepcopy(runtime["entrypoint"]),
        "arguments": arguments,
        "environment": copy.deepcopy(runtime["environment"]),
    }
    return {
        "runtime": compiled_runtime,
        "artifacts": role_artifacts,
        "interfaces": copy.deepcopy(interfaces),
        "security": copy.deepcopy(runtime["security"]),
        "lifecycle": copy.deepcopy(runtime["lifecycle"]),
    }


__all__ = ["RecipeRuntimeSpecError", "compile_runtime_spec"]
