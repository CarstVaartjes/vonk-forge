"""Compile a role-specific agent runtime spec from immutable local authority."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog_contract import (
    CatalogContractError,
    CatalogKind,
    CatalogReference,
    parse_catalog_reference,
)
from .harnesses import HarnessRegistry
from .harnesses.common import HarnessCompileError
from .models import CatalogEntity, CatalogEntityRevision
from .recipe_contract import (
    recipe_content_sha256,
    recipe_model_dependencies,
    recipe_patch_bundle,
    recipe_references,
    recipe_topology,
    validate_recipe,
)
from .runtime_writable_paths import document as runtime_writable_path_document
from .runtime_writable_paths import (
    materialize_environment as compile_runtime_environment,
)

_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BUILTIN_HARNESS_REGISTRY = HarnessRegistry.with_builtins()
_JOB_INTERFACES = frozenset(
    {"image-job", "audio-job", "video-job", "mesh-job", "artifact-job"}
)


class RecipeRuntimeSpecError(ValueError):
    pass


def compile_runtime_spec(
    document: Mapping[str, object],
    resolved_entities: Mapping[str, object],
    parameters: Mapping[str, object],
    role: str,
    rank: int,
    recipe_build_id: str,
    image_digest: str,
) -> dict[str, object]:
    validate_recipe(document)
    if _OCI_DIGEST.fullmatch(image_digest) is None:
        raise RecipeRuntimeSpecError("built image digest is invalid")
    if not isinstance(rank, int) or isinstance(rank, bool):
        raise RecipeRuntimeSpecError("mapped rank is invalid")
    references = recipe_references(document)
    model_version = references[0]
    _require_resolved_entity(resolved_entities, "model_version", model_version)
    model_dependencies = recipe_model_dependencies(document)
    resolved_dependencies = resolved_entities.get("model_dependencies", ())
    if not isinstance(resolved_dependencies, (tuple, list)):
        raise RecipeRuntimeSpecError("resolved model dependencies are invalid")
    if len(resolved_dependencies) != len(model_dependencies):
        raise RecipeRuntimeSpecError("resolved model dependencies are incomplete")
    for index, reference in enumerate(model_dependencies):
        _require_resolved_entity(
            {"model_version": resolved_dependencies[index]},
            "model_version",
            reference,
        )
    topology = recipe_topology(document)
    try:
        projection = _BUILTIN_HARNESS_REGISTRY.compile(
            resolved_entities.get("harness"),
            recipe=document,
            distribution=resolved_entities.get("runtime_distribution"),
            patch=resolved_entities.get("patch_bundle"),
            parameters=parameters,
            topology=topology,
            role=role,
            rank=rank,
        )
    except HarnessCompileError as error:
        raise RecipeRuntimeSpecError(str(error)) from error
    binding = projection.binding
    if binding is None:
        raise RecipeRuntimeSpecError("harness projection binding is missing")
    runtime = document["runtime"]
    interfaces = document["interfaces"]
    artifacts = document["artifacts"]
    if (
        not isinstance(runtime, Mapping)
        or not isinstance(interfaces, list)
        or not isinstance(artifacts, list)
    ):
        raise RecipeRuntimeSpecError("recipe runtime is invalid")
    runtime_security = runtime.get("security")
    if not isinstance(runtime_security, Mapping):
        raise RecipeRuntimeSpecError("recipe runtime security is invalid")
    role_artifacts = [
        copy.deepcopy(item)
        for item in artifacts
        if isinstance(item, Mapping)
        and isinstance(item.get("roles"), list)
        and role in item["roles"]
    ]
    if not role_artifacts:
        raise RecipeRuntimeSpecError("mapped role has no runtime artifacts")
    effective_environment = compile_runtime_environment(
        projection.slug,
        projection.environment,
    )
    compiled_runtime: dict[str, object] = {
        "interface": "vonk.runtime.v1",
        "adapter": projection.slug,
        "adapter_version": projection.contract_version,
        "image": (f"localhost/vonk/recipe-build-{recipe_build_id}@{image_digest}"),
        "architecture": projection.architecture,
        "entrypoint": list(projection.command),
        "arguments": [],
        "environment": [
            {"name": name, "value": value, "secret": None}
            for name, value in effective_environment
        ],
        "writable_paths": runtime_writable_path_document(
            projection.slug,
            effective_environment,
        ),
    }
    if topology.get("mode") == "distributed":
        distribution_document = getattr(
            resolved_entities.get("runtime_distribution"), "document", None
        )
        capabilities = (
            distribution_document.get("capabilities")
            if isinstance(distribution_document, Mapping)
            else None
        )
        implementation = None
        if isinstance(capabilities, Mapping):
            implementations = tuple(
                capabilities.get(name)
                for name in ("distributed_vllm", "distributed_sglang")
                if isinstance(capabilities.get(name), Mapping)
            )
            if len(implementations) == 1:
                implementation = implementations[0]
        launch = (
            implementation.get("launch")
            if isinstance(implementation, Mapping)
            else None
        )
        rendezvous = launch.get("rendezvous") if isinstance(launch, Mapping) else None
        if not isinstance(rendezvous, Mapping):
            raise RecipeRuntimeSpecError("distributed launch contract is missing")
        compiled_runtime["placement_environment"] = {
            "local_address": rendezvous["local_address_environment"],
            "master_address": rendezvous["master_address_environment"],
            "master_port": rendezvous["master_port_environment"],
        }
    endpoint = next(
        (
            item
            for item in interfaces
            if isinstance(item, Mapping) and item.get("adapter") == "openai"
        ),
        None,
    )
    job_interface = next(
        (
            item
            for item in interfaces
            if isinstance(item, Mapping) and item.get("adapter") in _JOB_INTERFACES
        ),
        None,
    )
    if (endpoint is None) == (job_interface is None):
        raise RecipeRuntimeSpecError(
            "recipe must declare exactly one service or job interface"
        )
    lifecycle = runtime.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        raise RecipeRuntimeSpecError("recipe lifecycle is invalid")
    spec = {
        "identity": {
            "recipe_revision_sha256": recipe_content_sha256(document),
            "model_version_sha256": model_version.content_sha256,
            "harness_sha256": _resolved_content_sha256(
                resolved_entities.get("harness")
            ),
            "runtime_distribution_sha256": _resolved_content_sha256(
                resolved_entities.get("runtime_distribution")
            ),
            "patch_bundle_sha256": (
                None
                if resolved_entities.get("patch_bundle") is None
                else _resolved_content_sha256(resolved_entities.get("patch_bundle"))
            ),
        },
        "model_dependencies": [
            {
                "kind": reference.kind.value,
                "publisher": reference.publisher,
                "slug": reference.slug,
                "content_sha256": reference.content_sha256,
            }
            for reference in model_dependencies
        ],
        "runtime": compiled_runtime,
        "artifacts": role_artifacts,
        "security": {
            "devices": copy.deepcopy(runtime_security["devices"]),
            "user": projection.user,
            "capabilities": list(projection.capabilities),
            "privileged": False,
            "host_network": runtime_security.get("host_network") is True,
            "mounts": copy.deepcopy(runtime_security["mounts"]),
        },
        "lifecycle": {
            "pre_start": copy.deepcopy(lifecycle["pre_start"]),
            "post_stop": copy.deepcopy(lifecycle["post_stop"]),
            "stop_timeout_seconds": lifecycle["stop_timeout_seconds"],
        },
        "topology": {
            "name": topology["name"],
            "node_count": topology["node_count"],
            "rank": rank,
            "role": role,
        },
    }
    if endpoint is not None:
        spec["endpoint"] = {
            "protocol": endpoint["adapter"],
            "port": endpoint["port"],
            "model_aliases": copy.deepcopy(endpoint["model_aliases"]),
            "health_path": endpoint["health_path"],
        }
    else:
        assert isinstance(job_interface, Mapping)
        readiness = lifecycle.get("readiness")
        if not isinstance(readiness, Mapping):
            raise RecipeRuntimeSpecError("recipe job timeout is invalid")
        timeout_seconds = readiness.get("timeout_seconds")
        output_path = job_interface.get("path")
        input_contract = job_interface.get("input")
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 3600
            or output_path != "/outputs"
            or (input_contract is not None and not isinstance(input_contract, Mapping))
        ):
            raise RecipeRuntimeSpecError("recipe job interface is invalid")
        spec["job"] = {
            "interface": job_interface["adapter"],
            "input": copy.deepcopy(input_contract),
            "output_path": output_path,
            "timeout_seconds": timeout_seconds,
        }
    return spec


def resolve_recipe_entities(
    session: Session, document: Mapping[str, object]
) -> dict[str, object]:
    """Load and cross-check the recipe's exact immutable entity graph."""

    references = recipe_references(document)
    model_version_ref, harness_ref, distribution_ref = references[:3]
    model_dependencies = recipe_model_dependencies(document)
    patch_ref = recipe_patch_bundle(document)
    model_version = _resolve_model_version(session, model_version_ref)
    auxiliary_model_versions = tuple(
        _resolve_model_version(session, reference) for reference in model_dependencies
    )
    harness = _lookup_exact(session, harness_ref)
    distribution = _lookup_exact(session, distribution_ref)
    implemented_harness = _entity_reference(
        distribution.document, "implements_harness", CatalogKind.EXECUTION_HARNESS
    )
    if implemented_harness != harness_ref:
        raise RecipeRuntimeSpecError("runtime distribution does not implement harness")
    patch = None
    if patch_ref is not None:
        patch = _lookup_exact(session, patch_ref)
        applies_to = _entity_reference(
            patch.document, "applies_to", CatalogKind.RUNTIME_DISTRIBUTION
        )
        if applies_to != distribution_ref:
            raise RecipeRuntimeSpecError("patch bundle does not apply to distribution")
    return {
        "model_version": model_version,
        "model_dependencies": auxiliary_model_versions,
        "harness": harness,
        "runtime_distribution": distribution,
        "patch_bundle": patch,
    }


def _resolve_model_version(
    session: Session, reference: CatalogReference
) -> CatalogEntityRevision:
    model_version = _lookup_exact(session, reference)
    model_ref = _entity_reference(model_version.document, "model", CatalogKind.MODEL)
    model = _lookup_exact(session, model_ref)
    group_ref = _entity_reference(
        model.document, "model_group", CatalogKind.MODEL_GROUP
    )
    _lookup_exact(session, group_ref)
    return model_version


def _require_resolved_entity(
    resolved_entities: Mapping[str, object], key: str, reference: CatalogReference
) -> None:
    value = resolved_entities.get(key)
    if getattr(value, "content_sha256", None) != reference.content_sha256:
        raise RecipeRuntimeSpecError(f"resolved {key} identity is invalid")


def _resolved_content_sha256(value: object) -> str:
    digest = getattr(value, "content_sha256", None)
    if not isinstance(digest, str):
        raise RecipeRuntimeSpecError("resolved catalog identity is missing")
    return digest


def _lookup_exact(
    session: Session, reference: CatalogReference
) -> CatalogEntityRevision:
    revision = session.scalar(
        select(CatalogEntityRevision)
        .join(CatalogEntity)
        .where(
            CatalogEntity.kind == reference.kind.value,
            CatalogEntity.publisher == reference.publisher,
            CatalogEntity.slug == reference.slug,
            CatalogEntityRevision.content_sha256 == reference.content_sha256,
            CatalogEntityRevision.lifecycle == "resolved",
        )
    )
    if revision is None:
        raise RecipeRuntimeSpecError("exact recipe dependency is not resolved")
    return revision


def _entity_reference(
    document: Mapping[str, object], field: str, kind: CatalogKind
) -> CatalogReference:
    try:
        return parse_catalog_reference(document.get(field), expected_kind=kind)
    except CatalogContractError as error:
        raise RecipeRuntimeSpecError("exact recipe dependency is invalid") from error


__all__ = [
    "RecipeRuntimeSpecError",
    "compile_runtime_spec",
    "resolve_recipe_entities",
]
