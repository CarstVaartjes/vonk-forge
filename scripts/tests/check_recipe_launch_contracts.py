"""Compile every catalog role and validate its canonical launch contract.

Uses real model/recipe definitions and the production runtime compiler with
synthetic cache receipts. This proves structure, not downloaded bytes or
hardware execution. Run inside the Controller environment with the matching
recipe checkout as the positional argument.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from types import SimpleNamespace

from vonk_agent_protocol import canonical_message, validate_compiled_execution_plan
from vonk_control.compiled_execution_plan import compile_verified_execution_plan
from vonk_control.execution_plan_service import _bind_runtime_artifacts
from vonk_control.recipe_runtime_specs import compile_runtime_spec
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256


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


def _receipts(models: list[ModelDefinition]) -> list[dict[str, object]]:
    result: dict[tuple[str, str], dict[str, object]] = {}
    for model in models:
        model_digest = content_sha256(model)
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
    digest = "sha256:" + "a" * 64
    layout = "f" * 64
    return {
        "image_digest": digest,
        "oci_layout_sha256": layout,
        "image_bytes": 4096,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "source": "controller-build",
        "build_id": "audit-build",
        "registry_manifest_digest": None,
        "platform_manifest_digest": digest,
        "local_image_config_id": "sha256:" + "b" * 64,
        "runtime_interface_label": "v1",
        "distribution_object": {
            "name": "image.oci.tar",
            "sha256": layout,
            "bytes": 4096,
            "kind": "oci-archive",
        },
    }


def check_catalog(root: Path) -> dict[str, object]:
    models_by_digest: dict[str, ModelDefinition] = {}
    for path in (root / "models").glob("*.json"):
        model = ModelDefinition.model_validate(json.loads(path.read_text()))
        models_by_digest[content_sha256(model)] = model
    rows = []
    errors = []
    for path in sorted((root / "recipes").glob("*.json")):
        recipe = RecipeDefinition.model_validate(json.loads(path.read_text()))
        models = [
            models_by_digest[selection.model.content_sha256]
            for selection in recipe.models
        ]
        model_revisions = [
            SimpleNamespace(
                document=model.model_dump(mode="json"),
                content_digest=content_sha256(model),
            )
            for model in models
        ]
        receipts = _receipts(models)
        for role_index, role_entry in enumerate(recipe.topology.roles):
            first_rank = sum(item.count for item in recipe.topology.roles[:role_index])
            for rank in range(first_rank, first_rank + role_entry.count):
                runtime = compile_runtime_spec(
                    recipe,
                    models=models,
                    package_handle=_package(recipe),
                    role=role_entry.name,
                    rank=rank,
                )
                runtime = _bind_runtime_artifacts(runtime, model_revisions)
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
                topology = runtime["topology"]
                world_size = topology["world_size"]
                payload = plan.to_compiled_launch_payload(
                    runtime,
                    placement={
                        "endpoint_address": "100.100.20.30",
                        "rank": rank,
                        "role": role_entry.name,
                        "world_size": world_size,
                        "local_address": f"100.100.20.{rank + 2}"
                        if world_size > 1
                        else None,
                        "master_address": "100.100.20.2" if world_size > 1 else None,
                        "master_port": 29500 if world_size > 1 else None,
                        "port": 8000,
                        "reserved_memory_bytes": 1024,
                    },
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library_root", type=Path)
    args = parser.parse_args()
    result = check_catalog(args.library_root.resolve())
    print(json.dumps(result, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
