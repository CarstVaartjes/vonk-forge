"""Representative real-catalog recipes and the launch payloads they compile to.

The committed fixtures are a compact slice of a signed recipe release: the
smallest recipe for every engine x topology x interface x option shape that the
catalog uses, verbatim (recipe and model documents keep their published
digests). ``compile_recipe_payloads`` is the production compiler path with
synthetic cache receipts; the payloads it produces are committed next to the
agent protocol fixtures so the Rust agent parses exactly what the Controller
emits.

Refresh after an intentional compiler or release change with::

    cd control && uv run python -m tests.catalog_launch_fixtures \\
        refresh path/to/catalog-index.json
    cd control && uv run python -m tests.catalog_launch_fixtures payloads
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_agent_protocol.compiled_execution_plan import CompiledPlacement
from vonk_control.compiled_execution_plan import (
    CompiledRuntimeImage,
    VerifiedModelObject,
    compile_verified_execution_plan,
)
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

REPOSITORY = Path(__file__).resolve().parents[2]
INPUTS = Path(__file__).resolve().parent / "fixtures" / "catalog_launch"
PAYLOADS = REPOSITORY / "agent_protocol" / "tests" / "fixtures" / "catalog-launch"

_SYNTHETIC_IMAGE = CompiledRuntimeImage(
    image_digest="sha256:" + "a" * 64,
    oci_layout_sha256="f" * 64,
    image_bytes=4096,
    build_id="catalog-fixture-build",
    local_image_config_id="sha256:" + "b" * 64,
    runtime_interface_label="v1",
)


def load_inputs() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return the committed recipe documents by slug and model documents by digest."""
    recipes = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((INPUTS / "recipes").glob("*.json"))
    }
    models = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((INPUTS / "models").glob("*.json"))
    }
    return recipes, models


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


def _receipts(models: Mapping[str, ModelDefinition]) -> list[dict[str, object]]:
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


def _choice_shape(recipe: RecipeDefinition, choice: Any) -> tuple[str, ...]:
    base_arguments = {argument.name for argument in recipe.runtime.arguments}
    base_environment = {item.name for item in recipe.runtime.environment}
    shape = {
        "args-replace" if argument.name in base_arguments else "args-append"
        for argument in choice.args
    }
    shape |= {
        "env-replace" if name in base_environment else "env-append"
        for name in choice.env
    }
    return tuple(sorted(shape))


def option_variants(recipe: RecipeDefinition) -> Iterator[tuple[str, dict[str, str]]]:
    """The default launch plus one non-default choice per distinct merge shape.

    Choices that merge into the runtime the same way (replace or append, argument
    or environment) exercise the same compiler and wire paths, so one of each
    shape is enough.
    """
    yield "default", {}
    seen: set[tuple[str, ...]] = set()
    for option in recipe.options:
        for choice in option.choices:
            shape = _choice_shape(recipe, choice)
            if not choice.default and shape and shape not in seen:
                seen.add(shape)
                yield f"{option.name}-{choice.value}", {option.name: choice.value}


def sampled_ranks(recipe: RecipeDefinition) -> list[tuple[str, int]]:
    """The first rank of every role, and the last rank of a multi-rank role."""
    result: list[tuple[str, int]] = []
    first = 0
    for role in recipe.topology.roles:
        result.append((role.name, first))
        if role.count > 1:
            result.append((role.name, first + role.count - 1))
        first += role.count
    return result


def payload_name(slug: str, variant: str, rank: int) -> str:
    return f"{slug}--{variant}--rank{rank}.json"


def launch_cases(
    recipe: RecipeDefinition,
) -> Iterator[tuple[str, dict[str, str], str, int]]:
    """Every (variant, option choices, role, rank) a recipe's fixtures cover.

    Option choices merge identically on every rank, so a non-default variant is
    compiled for one rank only.
    """
    ranks = sampled_ranks(recipe)
    for variant, choices in option_variants(recipe):
        for role, rank in ranks[:1] if choices else ranks:
            yield variant, choices, role, rank


def expected_payload_names(recipe: RecipeDefinition) -> set[str]:
    return {
        payload_name(recipe.identity.slug, variant, rank)
        for variant, _choices, _role, rank in launch_cases(recipe)
    }


def compile_launch_plan(
    recipe: RecipeDefinition,
    recipe_digest: str,
    models: Mapping[str, ModelDefinition],
    *,
    role: str,
    rank: int,
    option_choices: Mapping[str, str] | None = None,
) -> WireCompiledExecutionPlan:
    """One rank's launch plan, compiled through the production pipeline.

    Only the simulated fleet addresses are supplied; memory and port semantics
    come from the production placement constructor.
    """

    receipts = _receipts(models)
    compiled = compile_runtime_spec(
        recipe,
        models=models,
        recipe_digest=recipe_digest,
        package_handle=_package(recipe),
        option_choices=option_choices,
        role=role,
        rank=rank,
    )
    runtime = _bind_runtime_artifacts(compiled, models)
    selected = {
        (artifact.model.content_sha256, artifact.file_id)
        for artifact in runtime.artifacts
    }
    plan = compile_verified_execution_plan(
        runtime,
        model_artifact_set_sha256="c" * 64,
        model_objects=[
            VerifiedModelObject.model_validate(receipt)
            for receipt in receipts
            if (receipt["model_content_sha256"], receipt["file_id"]) in selected
        ],
        runtime_image=_SYNTHETIC_IMAGE,
    )
    world_size = recipe.topology.world_size
    placement = _placement(
        recipe, runtime, ClusterMappingNode(rank=rank, role=role), world_size
    )
    simulated = placement.model_dump(mode="json")
    if placement.port is not None:
        simulated["endpoint_address"] = "100.100.20.30"
    if world_size > 1:
        simulated["local_address"] = f"100.100.20.{rank + 2}"
        simulated["master_address"] = "100.100.20.2"
    return plan.to_compiled_launch_payload(
        runtime, placement=CompiledPlacement.model_validate(simulated)
    )


def compile_recipe_payloads(
    recipe_document: Mapping[str, Any], model_documents: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Compile a recipe document to launch payloads by fixture file name."""
    recipe = read_recipe(recipe_document)
    recipe_digest = document_sha256(recipe_document)
    models: dict[str, ModelDefinition] = {}
    revisions = []
    for selection in recipe.models:
        digest = selection.model.content_sha256
        document = model_documents[digest]
        assert document_sha256(document) == digest
        models[digest] = read_model(document)
        revisions.append(SimpleNamespace(document=document, content_digest=digest))
    slug = recipe.identity.slug
    payloads: dict[str, dict[str, Any]] = {}
    for variant, choices, role, rank in launch_cases(recipe):
        payloads[payload_name(slug, variant, rank)] = compile_launch_plan(
            recipe,
            recipe_digest,
            models,
            role=role,
            rank=rank,
            option_choices=choices,
        ).model_dump(mode="json")
    return payloads


def _dump(document: object) -> str:
    return json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


# --- selection from a release catalog (maintenance only) --------------------


def _features(recipe: RecipeDefinition) -> set[tuple]:
    engine = recipe.runtime.engine
    interface = recipe.interfaces[0]
    features: set[tuple] = {
        (engine, recipe.topology.name, interface.adapter),
        ("adapter", interface.adapter),
    }
    for option in recipe.options:
        for choice in option.choices:
            for kind in _choice_shape(recipe, choice):
                features.add(("option", kind))
    for argument in recipe.runtime.arguments:
        kind = (
            "setting" if argument.setting is not None else type(argument.value).__name__
        )
        features.add(("argument", kind))
    names = [item.name for item in recipe.runtime.environment]
    names += [name for o in recipe.options for c in o.choices for name in c.env]
    if any(name != name.upper() for name in names):
        features.add(("mixed-case-environment",))
    if len(recipe.models) > 1:
        features.add(("multi-model",))
    if getattr(interface, "input", None) is not None:
        features.add(("job-input", interface.adapter))
    return features


def select_representatives(index: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Greedy smallest-first cover of the catalog's engine/topology/option shapes."""
    entities = {
        entity["content_sha256"]: entity["document"]
        for entity in index["catalog_entities"]
        if entity["document"].get("kind") == "model"
    }
    candidates = []
    for entry in index["recipes"]:
        document = entry["document"]
        recipe = read_recipe(document)
        model_documents = {
            selection.model.content_sha256: entities[selection.model.content_sha256]
            for selection in recipe.models
        }
        try:
            compile_recipe_payloads(document, model_documents)
        except Exception as error:  # noqa: BLE001 - reported, then skipped
            print(
                f"skip {recipe.identity.slug}: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            continue
        size = len(json.dumps(document)) + sum(
            len(json.dumps(item)) for item in model_documents.values()
        )
        candidates.append((size, document, _features(recipe)))
    # Smallest recipes first, keeping each one that covers a shape not yet seen.
    candidates.sort(key=lambda item: (item[0], item[1]["identity"]["slug"]))
    covered: set[tuple] = set()
    chosen: list[dict[str, Any]] = []
    for _size, document, features in candidates:
        if features - covered:
            covered |= features
            chosen.append(document)
    return chosen


def refresh_inputs(index_path: Path) -> None:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entities = {
        entity["content_sha256"]: entity["document"]
        for entity in index["catalog_entities"]
    }
    chosen = select_representatives(index)
    for directory in ("recipes", "models"):
        target = INPUTS / directory
        target.mkdir(parents=True, exist_ok=True)
        for stale in target.glob("*.json"):
            stale.unlink()
    for document in chosen:
        recipe = read_recipe(document)
        (INPUTS / "recipes" / f"{recipe.identity.slug}.json").write_text(
            _dump(document), encoding="utf-8"
        )
        for selection in recipe.models:
            digest = selection.model.content_sha256
            (INPUTS / "models" / f"{digest}.json").write_text(
                _dump(entities[digest]), encoding="utf-8"
            )
    print(f"selected {len(chosen)} recipes from {index['contract_version']}")


def refresh_payloads() -> None:
    recipes, models = load_inputs()
    PAYLOADS.mkdir(parents=True, exist_ok=True)
    for stale in PAYLOADS.glob("*.json"):
        stale.unlink()
    total = 0
    for document in recipes.values():
        for name, payload in compile_recipe_payloads(document, models).items():
            (PAYLOADS / name).write_text(
                json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                + "\n",
                encoding="utf-8",
            )
            total += 1
    print(f"wrote {total} launch payloads")


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "refresh":
        refresh_inputs(Path(argv[2]))
        return 0
    if len(argv) == 2 and argv[1] == "payloads":
        refresh_payloads()
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
