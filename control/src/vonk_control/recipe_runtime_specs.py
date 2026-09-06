"""Compile canonical public RecipeDefinition documents into agent runtimes.

RecipeDefinition and ModelDefinition are the only authoring authorities.  The
compiler consumes their validated projections plus a package/build handle and
adds the platform execution invariants at the final boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256
from vonk_forge_contracts.resolver import (
    ContractResolutionError,
    validate_recipe_package_paths,
)

from .harnesses.canonical import compile_canonical_harness
from .harnesses.common import HarnessCompileError
from .models import CatalogDocumentRevision
from .runtime_writable_paths import document as writable_path_document


class RecipeRuntimeSpecError(ValueError):
    """The canonical recipe cannot produce a secure runtime projection."""


def _recipe(value: object) -> RecipeDefinition:
    if isinstance(value, RecipeDefinition):
        return value
    raw = getattr(value, "document", value)
    if not isinstance(raw, Mapping):
        raise RecipeRuntimeSpecError("recipe projection is invalid")
    try:
        return RecipeDefinition.model_validate(raw)
    except Exception as error:
        raise RecipeRuntimeSpecError("recipe does not satisfy RecipeDefinition v2") from error


def _models(value: object) -> tuple[ModelDefinition, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecipeRuntimeSpecError("canonical model projections are missing")
    result: list[ModelDefinition] = []
    for item in value:
        if isinstance(item, ModelDefinition):
            result.append(item)
            continue
        raw = getattr(item, "document", item)
        if not isinstance(raw, Mapping):
            raise RecipeRuntimeSpecError("canonical model projection is invalid")
        try:
            result.append(ModelDefinition.model_validate(raw))
        except Exception as error:
            raise RecipeRuntimeSpecError("canonical model projection is invalid") from error
    return tuple(result)


def _package(value: object, resolved: Mapping[str, object]) -> object:
    if value is not None:
        return value
    for name in ("package_handle", "build_receipt", "package"):
        if name in resolved:
            return resolved[name]
    return None


def _artifact_inputs(package: object) -> dict[str, str]:
    raw = package.get("artifact_inputs") if isinstance(package, Mapping) else getattr(package, "artifact_inputs", None)
    if raw is None:
        return {}
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise RecipeRuntimeSpecError("package artifact projection is invalid")
    result: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, Mapping) or type(item.get("selection_id")) is not str or type(item.get("artifact_key")) is not str:
            raise RecipeRuntimeSpecError("package artifact projection is invalid")
        if item["selection_id"] in result:
            raise RecipeRuntimeSpecError("package artifact projection repeats a selection")
        result[item["selection_id"]] = item["artifact_key"]
    return result


def _package_paths(package: object) -> Sequence[str] | None:
    raw = package.get("paths") if isinstance(package, Mapping) else getattr(package, "paths", None)
    if raw is None and isinstance(package, Mapping):
        raw = package.get("member_paths")
    if raw is None:
        return None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or any(type(path) is not str for path in raw):
        raise RecipeRuntimeSpecError("package paths are invalid")
    return raw


def _resolved_inputs(
    resolved_entities: Mapping[str, object] | None,
    models: Sequence[ModelDefinition] | None,
    package_handle: object,
    parsed: RecipeDefinition,
) -> tuple[tuple[ModelDefinition, ...], object]:
    resolved = {} if resolved_entities is None else dict(resolved_entities)
    allowed = {"recipe", "models", "model_projections", "package_handle", "build_receipt", "package"}
    unknown = set(resolved) - allowed
    if unknown:
        raise RecipeRuntimeSpecError("resolved inputs contain retired authorities")
    resolved_recipe = resolved.get("recipe")
    if resolved_recipe is not None:
        candidate = _recipe(resolved_recipe)
        if content_sha256(candidate) != content_sha256(parsed):
            raise RecipeRuntimeSpecError("resolved recipe projection does not match the candidate")
    supplied_models = models if models is not None else resolved.get("models", resolved.get("model_projections"))
    return _models(supplied_models), _package(package_handle, resolved)


def compile_runtime_spec(
    recipe: RecipeDefinition | Mapping[str, object] | object,
    resolved_entities: Mapping[str, object] | None = None,
    parameters: Mapping[str, object] | None = None,
    role: str | None = None,
    rank: int | None = None,
    *,
    models: Sequence[ModelDefinition] | None = None,
    package_handle: object = None,
) -> dict[str, object]:
    """Compile one canonical recipe role into a secure runtime transport.

    ``resolved_entities`` is retained as the composition seam name used by
    callers, but only canonical model projections and a package/build handle
    are accepted.  Retired harness/distribution/patch identities are rejected.
    """
    parsed = _recipe(recipe)
    if resolved_entities is not None and not isinstance(resolved_entities, Mapping):
        raise RecipeRuntimeSpecError("resolved canonical inputs are invalid")
    if type(role) is not str or not role:
        raise RecipeRuntimeSpecError("mapped role is invalid")
    if type(rank) is not int or isinstance(rank, bool) or rank < 0:
        raise RecipeRuntimeSpecError("mapped rank is invalid")
    supplied_models, package = _resolved_inputs(
        resolved_entities, models, package_handle, parsed
    )
    try:
        # The package handle is also the exact closure authority when member
        # paths are available.  Source/build inputs are never inferred from a
        # recipe document copy.
        paths = _package_paths(package)
        if paths is not None:
            validate_recipe_package_paths(parsed, paths)
        projection, artifacts, _image_digest = compile_canonical_harness(
            parsed,
            supplied_models,
            package,
            role=role,
            rank=rank,
            settings=parameters,
        )
    except (HarnessCompileError, ContractResolutionError, ValueError, TypeError) as error:
        raise RecipeRuntimeSpecError(str(error)) from error
    binding = projection.binding
    if binding is None:
        raise RecipeRuntimeSpecError("canonical harness binding is missing")
    interface = parsed.interfaces[0]
    environment = writable_path_document(parsed.runtime.engine, projection.environment)
    compiled_arguments = _compiled_arguments(parsed, parameters)
    runtime: dict[str, object] = {
        "interface": "vonk.runtime.v1",
        "adapter": projection.slug,
        "adapter_version": projection.contract_version,
        "image": projection.image,
        "architecture": projection.architecture,
        "entrypoint": list(projection.command),
        "arguments": compiled_arguments,
        "environment": [
            {"name": name, "value": value, "secret": None}
            for name, value in projection.environment
        ],
        "writable_paths": environment,
    }
    if parsed.topology.node_count > 1:
        runtime["placement_environment"] = {
            "local_address": "VONK_LOCAL_ADDR",
            "master_address": "VONK_MASTER_ADDR",
            "master_port": "VONK_MASTER_PORT",
        }
    artifact_inputs = _artifact_inputs(package)
    dependencies = []
    for selection in parsed.models:
        dependencies.append(
            {
                "selection_id": selection.id,
                "publisher": selection.model.publisher,
                "slug": selection.model.slug,
                "content_sha256": selection.model.content_sha256,
                "artifact_key": artifact_inputs.get(selection.id),
            }
        )
    mounts = [
        {
            "source": mount.source,
            "target": mount.target,
            "read_only": mount.read_only,
        }
        for mount in projection.model_mounts
    ]
    mounts.append({"source": "/run/vonk/outputs", "target": "/outputs", "read_only": False})
    if projection.input_mount is not None:
        mounts.append({"source": "/run/vonk/inputs", "target": "/inputs", "read_only": True})
    lifecycle = parsed.runtime.lifecycle
    security = {
        "devices": list(projection.devices) if hasattr(projection, "devices") else [],
        "user": projection.user,
        "capabilities": list(projection.capabilities),
        "privileged": False,
        "host_network": projection.network_mode == "host",
        "network_mode": projection.network_mode,
        "mounts": mounts,
        "read_only_root": projection.read_only_root,
        "no_new_privileges": projection.no_new_privileges,
    }
    lifecycle_spec = {
        "pre_start": copy.deepcopy(parsed.runtime.lifecycle.pre_start),
        "post_stop": copy.deepcopy(parsed.runtime.lifecycle.post_stop),
        "stop_timeout_seconds": lifecycle.stop_timeout_seconds,
    }
    topology_spec = {
        "name": parsed.topology.name,
        "mode": parsed.topology.mode,
        "node_count": parsed.topology.node_count,
        "world_size": parsed.topology.parallelism.world_size,
        "rank": rank,
        "role": role,
        "backend": parsed.topology.parallelism.backend,
    }
    spec: dict[str, object] = {
        "identity": {
            "recipe_revision_sha256": content_sha256(parsed),
            "model_dependencies": dependencies,
            "harness_sha256": binding.harness_content_sha256,
            "execution_sha256": None,
            "build_input_sha256": _build_input_digest(package),
        },
        "model_dependencies": dependencies,
        "runtime": runtime,
        # These are compiler output, assembled from ModelDefinition selectors;
        # they are not a second recipe authoring authority.
        "artifacts": copy.deepcopy(list(artifacts)),
        "security": security,
        "lifecycle": lifecycle_spec,
        "topology": topology_spec,
    }
    if interface.adapter == "openai":
        spec["endpoint"] = {
            "protocol": "openai",
            "port": interface.port,
            "model_aliases": list(interface.model_aliases),
            "health_path": interface.health_path,
        }
    else:
        spec["job"] = {
            "interface": interface.adapter,
            "input": (
                None
                if interface.input is None
                else interface.input.model_dump(mode="json")
            ),
            "output_path": interface.output.path,
            "timeout_seconds": lifecycle.stop_timeout_seconds,
        }
    spec["identity"]["execution_sha256"] = _execution_digest(
        {
            "harness_sha256": binding.harness_content_sha256,
            "model_dependencies": dependencies,
            "artifacts": artifacts,
            "runtime": runtime,
            "security": security,
            "lifecycle": lifecycle_spec,
            "topology": topology_spec,
            "interface": spec.get("endpoint", spec.get("job")),
        }
    )
    return spec


def _execution_digest(projection: Mapping[str, object]) -> str:
    """Hash only normalized compiled launch behavior and platform invariants."""
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _build_input_digest(package: object) -> str | None:
    raw = (
        package.get("build_input_sha256")
        if isinstance(package, Mapping)
        else getattr(package, "build_input_sha256", None)
    )
    if raw is None:
        raw = (
            package.get("build_input_digest")
            if isinstance(package, Mapping)
            else getattr(package, "build_input_digest", None)
        )
    if raw is None:
        return None
    if type(raw) is not str:
        raise RecipeRuntimeSpecError("build input digest is invalid")
    value = raw.removeprefix("sha256:")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise RecipeRuntimeSpecError("build input digest is invalid")
    return value


def _compiled_arguments(
    recipe: RecipeDefinition, supplied: Mapping[str, object] | None
) -> list[dict[str, object]]:
    values: dict[str, object] = {}
    for name in ("context_tokens", "concurrency", "max_batch_tokens"):
        setting = getattr(recipe.settings, name, None)
        if setting is not None:
            values[name] = setting.value
    values.update({name: setting.value for name, setting in recipe.settings.knobs.items()})
    if supplied:
        values.update(supplied)
    result: list[dict[str, object]] = []
    for argument in recipe.runtime.arguments:
        value = argument.value if argument.setting is None else values[argument.setting]
        result.append({"name": argument.name, "value": value})
    return result


def resolve_recipe_entities(
    session: Session, document: Mapping[str, object]
) -> dict[str, object]:
    """Resolve a canonical recipe and its exact active Model revisions."""
    try:
        recipe = RecipeDefinition.model_validate(document)
    except (TypeError, ValueError) as error:
        raise RecipeRuntimeSpecError(
            "recipe does not satisfy the canonical contract"
        ) from error

    models: list[CatalogDocumentRevision] = []
    for selection in recipe.models:
        reference = selection.model
        revision = session.scalar(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.publisher == reference.publisher,
                CatalogDocumentRevision.slug == reference.slug,
                CatalogDocumentRevision.content_digest == reference.content_sha256,
                CatalogDocumentRevision.state == "active",
            )
            .limit(1)
        )
        if revision is None:
            raise RecipeRuntimeSpecError("exact recipe model is not active")
        models.append(revision)
    return {"recipe": recipe, "models": tuple(models)}


__all__ = [
    "RecipeRuntimeSpecError",
    "compile_runtime_spec",
    "resolve_recipe_entities",
]
