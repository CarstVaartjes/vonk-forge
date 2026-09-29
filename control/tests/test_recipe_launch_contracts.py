"""Every catalog rank compiles to a launch payload the agent protocol accepts.

Uses the real model and recipe definitions of the canonical recipe library and
the production runtime compiler with synthetic cache receipts. This proves
structure, not downloaded bytes or hardware execution.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from vonk_agent_protocol import canonical_message, validate_compiled_execution_plan
from vonk_control.compiled_execution_plan import compile_verified_execution_plan
from vonk_control.execution_plan_service import _bind_runtime_artifacts, _placement
from vonk_control.models import ClusterMappingNode
from vonk_control.recipe_runtime_specs import compile_runtime_spec
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
    read_model,
    read_recipe,
)

from .recipe_library_source import recipe_library_root


def _package(recipe: RecipeDefinition) -> dict[str, object]:
    build = getattr(recipe.execution, "build", None)
    paths = [build.context.path, build.dockerfile] if build is not None else []
    if build is not None:
        paths.extend(patch.path for patch in build.patches)
    for check in recipe.validation.serving.checks:
        request = check.request
        fixture = getattr(request, "fixture", None)
        if fixture:
            paths.append(fixture)
        paths.extend(getattr(request, "input_slots", {}).values())
    digest = "a" * 64
    return {
        "image_digest": digest,
        "image_reference": f"localhost/vonk/recipe-build@sha256:{digest}",
        "paths": paths,
    }


def _receipts(models: dict[str, ModelDefinition]) -> list[dict[str, object]]:
    result: dict[tuple[str, str], dict[str, object]] = {}
    for model_digest, model in models.items():
        for file in model.files:
            result.setdefault(
                (model_digest, file.id),
                {
                    "model_content_sha256": model_digest,
                    "file_id": file.id,
                    "path": file.path,
                    "sha256": file.sha256,
                    "bytes": file.size_bytes,
                    "roles": list(file.roles),
                    "distribution_object": {
                        "name": file.path,
                        "sha256": file.sha256,
                        "bytes": file.size_bytes,
                        "kind": "model",
                    },
                },
            )
    return list(result.values())


def _image() -> dict[str, object]:
    return {
        "image_digest": "sha256:" + "a" * 64,
        "oci_layout_sha256": "f" * 64,
        "image_bytes": 4096,
        "build_id": "audit-build",
        "local_image_config_id": "sha256:" + "b" * 64,
        "runtime_interface_label": "v1",
    }


def check_catalog(root: Path) -> dict[str, Any]:
    models_by_digest: dict[str, ModelDefinition] = {}
    raw_models: dict[str, dict[str, Any]] = {}
    for path in (root / "models").glob("*.json"):
        raw = json.loads(path.read_text())
        digest = document_sha256(raw)
        raw_models[digest] = raw
        models_by_digest[digest] = read_model(raw)
    rows = []
    errors = []
    for path in sorted((root / "recipes").glob("*.json")):
        raw_recipe = json.loads(path.read_text())
        recipe = read_recipe(raw_recipe)
        models = {
            selection.model.content_sha256: models_by_digest[
                selection.model.content_sha256
            ]
            for selection in recipe.models
        }
        model_revisions = [
            SimpleNamespace(document=raw_models[digest], content_digest=digest)
            for digest in models
        ]
        receipts = _receipts(models)
        for role_index, role_entry in enumerate(recipe.topology.roles):
            first_rank = sum(item.count for item in recipe.topology.roles[:role_index])
            for rank in range(first_rank, first_rank + role_entry.count):
                runtime = compile_runtime_spec(
                    recipe,
                    models=models,
                    recipe_digest=document_sha256(raw_recipe),
                    package_handle=_package(recipe),
                    role=role_entry.name,
                    rank=rank,
                )
                runtime: dict[str, Any] = _bind_runtime_artifacts(
                    runtime, model_revisions
                )
                selected = {
                    (
                        artifact["model"]["content_sha256"],
                        artifact["file_id"],
                    )
                    for artifact in runtime["artifacts"]
                }
                selected_receipts = [
                    receipt
                    for receipt in receipts
                    if (receipt["model_content_sha256"], receipt["file_id"]) in selected
                ]
                try:
                    plan = compile_verified_execution_plan(
                        runtime,
                        model_artifact_set_sha256="c" * 64,
                        model_objects=selected_receipts,
                        runtime_image=_image(),
                    )
                except Exception as error:
                    raise RuntimeError(f"{path.stem}: {error}") from error
                world_size = recipe.topology.world_size
                placement = _placement(
                    recipe,
                    runtime,
                    ClusterMappingNode(rank=rank, role=role_entry.name),
                    world_size,
                )
                # Supply only the simulated fleet addresses. Memory and port
                # semantics come from the production placement constructor.
                if placement["port"] is not None:
                    placement["endpoint_address"] = "100.100.20.30"
                if world_size > 1:
                    placement["local_address"] = f"100.100.20.{rank + 2}"
                    placement["master_address"] = "100.100.20.2"
                payload = plan.to_compiled_launch_payload(
                    runtime,
                    placement=placement,
                )
                try:
                    validate_compiled_execution_plan(payload)
                except ValueError as error:
                    detail = error
                    while detail.__cause__ is not None:
                        detail = detail.__cause__
                    errors.append(
                        {"recipe": path.stem, "rank": rank, "error": str(detail)}
                    )
                rows.append(
                    (path.stem, len(receipts), len(canonical_message(payload)), rank)
                )
    if not rows:
        raise ValueError("recipe catalog has no runtime projections")
    values = sorted(row[2] for row in rows)
    return {
        "recipes": len({row[0] for row in rows}),
        "models": len(models_by_digest),
        "projections": len(rows),
        "validated_projections": len(rows) - len(errors),
        "max_model_files": max(row[1] for row in rows),
        "median_payload_bytes": statistics.median(values),
        "max_payload_bytes": max(values),
        "largest": sorted(rows, key=lambda row: row[2], reverse=True)[:10],
        "errors": errors,
    }


def test_every_catalog_rank_compiles_to_a_valid_launch_payload() -> None:
    result = check_catalog(recipe_library_root())

    assert result["errors"] == []
    assert result["validated_projections"] == result["projections"]
    assert result["recipes"] > 0
