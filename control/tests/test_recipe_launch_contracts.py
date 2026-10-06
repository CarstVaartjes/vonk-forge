"""Every catalog rank compiles to a launch payload the agent protocol accepts.

Uses the real model and recipe definitions of the canonical recipe library and
the production runtime compiler with synthetic cache receipts. This proves
structure, not downloaded bytes or hardware execution.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from vonk_agent_protocol import canonical_message, validate_compiled_execution_plan
from vonk_forge_contracts import (
    ModelDefinition,
    document_sha256,
    read_model,
    read_recipe,
)

from .catalog_launch_fixtures import _receipts, compile_launch_plan
from .recipe_library_source import recipe_library_root


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
        receipts = _receipts(models)
        for role_index, role_entry in enumerate(recipe.topology.roles):
            first_rank = sum(item.count for item in recipe.topology.roles[:role_index])
            for rank in range(first_rank, first_rank + role_entry.count):
                try:
                    plan = compile_launch_plan(
                        recipe,
                        document_sha256(raw_recipe),
                        models,
                        role=role_entry.name,
                        rank=rank,
                    )
                except Exception as error:
                    raise RuntimeError(f"{path.stem}: {error}") from error
                payload = plan.model_dump(mode="json")
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
