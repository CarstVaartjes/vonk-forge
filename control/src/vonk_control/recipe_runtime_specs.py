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
    recipe_references,
    recipe_topology,
    validate_recipe,
)

_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BUILTIN_HARNESS_REGISTRY = HarnessRegistry.with_builtins()


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
            for name, value in projection.environment
        ],
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
        implementation = (
            capabilities.get("distributed_vllm")
            if isinstance(capabilities, Mapping)
            else None
        )
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
    if not isinstance(endpoint, Mapping):
        raise RecipeRuntimeSpecError("recipe endpoint is invalid")
    lifecycle = runtime.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        raise RecipeRuntimeSpecError("recipe lifecycle is invalid")
    return {
        "runtime": compiled_runtime,
        "artifacts": role_artifacts,
        "endpoint": {
            "protocol": endpoint["adapter"],
            "port": endpoint["port"],
            "model_aliases": copy.deepcopy(endpoint["model_aliases"]),
            "health_path": endpoint["health_path"],
        },
        "security": {
            "devices": copy.deepcopy(runtime_security["devices"]),
            "user": projection.user,
            "capabilities": list(projection.capabilities),
            "privileged": False,
            "host_network": runtime_security.get("host_network") is True,
            "mounts": [
                {"source": "model", "target": "/models", "read_only": True},
                {"source": "state", "target": "/state", "read_only": False},
            ],
        },
        "lifecycle": {
            "pre_start": copy.deepcopy(lifecycle["pre_start"]),
            "post_stop": copy.deepcopy(lifecycle["post_stop"]),
            "stop_timeout_seconds": lifecycle["stop_timeout_seconds"],
        },
    }


def resolve_recipe_entities(
    session: Session, document: Mapping[str, object]
) -> dict[str, CatalogEntityRevision | None]:
    """Load and cross-check the recipe's exact immutable entity graph."""

    references = recipe_references(document)
    model_version_ref, harness_ref, distribution_ref = references[:3]
    patch_ref = references[3] if len(references) == 4 else None
    model_version = _lookup_exact(session, model_version_ref)
    model_ref = _entity_reference(model_version.document, "model", CatalogKind.MODEL)
    model = _lookup_exact(session, model_ref)
    group_ref = _entity_reference(
        model.document, "model_group", CatalogKind.MODEL_GROUP
    )
    _lookup_exact(session, group_ref)
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
        "harness": harness,
        "runtime_distribution": distribution,
        "patch_bundle": patch,
    }


def _require_resolved_entity(
    resolved_entities: Mapping[str, object], key: str, reference: CatalogReference
) -> None:
    value = resolved_entities.get(key)
    if getattr(value, "content_sha256", None) != reference.content_sha256:
        raise RecipeRuntimeSpecError(f"resolved {key} identity is invalid")


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
